"""PipeWire capture pipelines.

Two-pipeline design:

* KeepalivePipeline — connects to the virtual monitor's PipeWire node for the
  whole program lifetime. Mutter only *creates* the monitor once a consumer
  negotiates a format, and tears it down when the last consumer disconnects —
  so this pipeline is what makes the monitor exist and stay up between
  clients. It also pins the negotiated size, so per-client pipelines never
  trigger a mid-session renegotiation (historically a mutter crash: see
  gnome-remote-desktop!69).

* ClientPipeline — one per connected phone, created with the negotiated
  transport's encoder and destroyed on disconnect. PipeWire allows multiple
  consumers per node, which is what makes this split possible without
  dynamic tee surgery.

All control runs on the GLib thread; appsink callbacks arrive on GStreamer
streaming threads and hand frames to the caller as plain bytes.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

log = logging.getLogger(__name__)

_gst_initialized = False


def gst_init() -> None:
    global _gst_initialized
    if not _gst_initialized:
        Gst.init(None)
        _gst_initialized = True


class PipelineError(RuntimeError):
    pass


def _size_caps(width: int, height: int) -> str:
    # Width/height only: mutter's virtual stream negotiates the refresh rate
    # itself, and extra caps fields would over-constrain the intersection.
    return f"video/x-raw,width={width},height={height}"


# gst-pipewire (<= 1.0.x) registers pipewiresrc's node as
# "Stream/Input/Unknown" because caps aren't fixed at connect time. Ubuntu's
# WirePlumber 0.4 then derives media.type = nil and its canLink() check
# rejects the match against the mutter stream ("target not found"), while the
# no-target path crashes policy-node.lua outright. Declaring the class
# ourselves makes target resolution work.
_STREAM_PROPS = (
    "stream-properties=props,"
    "media.class=(string)Stream/Input/Video,"
    "media.type=(string)Video"
)


def _src(target: str) -> str:
    return f"pipewiresrc target-object={target} do-timestamp=true {_STREAM_PROPS}"


def resolve_target(node_id: int) -> str:
    """Map mutter's PipeWire node id to what pipewiresrc's target-object
    property actually matches: the object.serial (PipeWire >= 0.3.64). Falls
    back to the raw node id, which older stacks matched directly."""
    try:
        out = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        ).stdout
        for obj in json.loads(out):
            if obj.get("id") == node_id:
                props = (obj.get("info") or {}).get("props") or {}
                serial = props.get("object.serial")
                if serial is not None:
                    log.debug("node %d -> object.serial %s", node_id, serial)
                    return str(serial)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log.debug("pw-dump lookup failed (%s); using node id directly", e)
    return str(node_id)


class _BasePipeline:
    def __init__(self) -> None:
        self._pipeline: Gst.Pipeline | None = None
        self._on_error: Callable[[str], None] | None = None

    def _build(self, description: str) -> None:
        gst_init()
        log.debug("gst-launch %s", description)
        try:
            self._pipeline = Gst.parse_launch(description)
        except GLib.Error as e:
            raise PipelineError(f"pipeline construction failed: {e.message}") from e
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._bus_error)

    def _play(self) -> None:
        """Callbacks must be attached between _build and _play: caps can
        negotiate synchronously inside set_state(PLAYING)."""
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            desc = "pipeline refused to start"
            self.stop()
            raise PipelineError(desc)

    def _bus_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, debug = msg.parse_error()
        log.error("gstreamer: %s (%s)", err.message, debug)
        if self._on_error:
            self._on_error(err.message)

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.get_bus().remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None


class KeepalivePipeline(_BasePipeline):
    """pipewiresrc -> capsfilter(WxH) -> fakesink. Holds the monitor alive and
    fixes its resolution. Reports the actually-negotiated size (the compositor
    has the final word) via `on_size`."""

    def __init__(
        self,
        target: str,
        width: int,
        height: int,
        on_size: Callable[[int, int], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._on_error = on_error
        self._on_size = on_size
        self._size_reported = False
        desc = (
            f"{_src(target)} "
            f"! capsfilter name=wd_caps caps={_size_caps(width, height)} "
            f"! fakesink sync=false async=false"
        )
        self._build(desc)
        pad = self._pipeline.get_by_name("wd_caps").get_static_pad("src")
        pad.connect("notify::caps", self._caps_changed)
        self._play()
        self._caps_changed(pad, None)  # in case negotiation beat the connect

    def _caps_changed(self, pad: Gst.Pad, _pspec) -> None:
        caps = pad.get_current_caps()
        if not caps or caps.get_size() == 0 or self._size_reported:
            return
        s = caps.get_structure(0)
        ok_w, width = s.get_int("width")
        ok_h, height = s.get_int("height")
        if ok_w and ok_h:
            self._size_reported = True
            log.debug("negotiated virtual monitor size: %dx%d", width, height)
            self._on_size(width, height)


class ClientPipeline(_BasePipeline):
    """pipewiresrc -> videoconvert -> encoder -> appsink, one per client.

    `on_frame(data, is_keyframe)` is invoked on a GStreamer streaming thread —
    the caller must trampoline into its own event loop.
    """

    def __init__(
        self,
        target: str,
        width: int,
        height: int,
        encoder_fragment: str,
        on_frame: Callable[[bytes, bool], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._on_error = on_error
        self._on_frame = on_frame
        desc = (
            f"{_src(target)} "
            f"! {_size_caps(width, height)} "
            f"! queue leaky=downstream max-size-buffers=3 "
            f"! videoconvert "
            f"! {encoder_fragment} "
            f"! appsink name=wd_sink emit-signals=true sync=false "
            f"max-buffers=4 drop=false"
        )
        self._build(desc)
        self._appsink = self._pipeline.get_by_name("wd_sink")
        self._appsink.connect("new-sample", self._new_sample)
        self._play()

    def _new_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        is_key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        self._on_frame(data, is_key)
        return Gst.FlowReturn.OK

    def force_keyframe(self) -> None:
        """Ask the encoder for an immediate IDR (used after frame drops so the
        decoder can resync)."""
        if not self._pipeline:
            return
        event = GstVideo.video_event_new_upstream_force_key_unit(
            Gst.CLOCK_TIME_NONE, True, 0
        )
        self._appsink.send_event(event)
