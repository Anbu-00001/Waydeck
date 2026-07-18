"""Encoder selection and GStreamer launch-fragment construction.

H.264 encoder preference: VA-API hardware (vah264enc, then legacy
vaapih264enc) before software x264. Each choice pairs a launch fragment with
the WebCodecs codec string the browser needs. Streams are Annex-B
(byte-stream) with SPS/PPS repeated at every IDR (h264parse
config-interval=-1), so the client's VideoDecoder needs no out-of-band
description — that is exactly the WebCodecs Annex-B mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# WebCodecs codec strings: avc1.PPCCLL (profile, constraints, level).
# Browsers select the decoder from this; with in-band SPS/PPS they are
# tolerant of level mismatches. Level 4.0 (0x28) covers 1080p30.
_CODEC_CONSTRAINED_BASELINE = "avc1.42E028"
_CODEC_MAIN = "avc1.4D0028"


@dataclass(frozen=True)
class H264Encoder:
    kind: str  # vah264 | vaapi | x264
    fragment: str  # gst-launch fragment, encoder through parser
    codec: str  # WebCodecs codec string


def _element_exists(name: str) -> bool:
    from gi.repository import Gst

    return Gst.ElementFactory.find(name) is not None


def jpeg_fragment(quality: int) -> str:
    return f"jpegenc quality={quality}"


def pick_h264(preference: str, bitrate_kbps: int, keyframe_interval: int) -> H264Encoder | None:
    """Return the best available H.264 encoder chain, or None if H.264 is
    impossible on this host (missing gstreamer1.0-plugins-bad/-ugly)."""
    if not _element_exists("h264parse"):
        log.warning(
            "h264parse not found (install gstreamer1.0-plugins-bad); "
            "H.264 transport disabled, JPEG fallback only"
        )
        return None

    parse = "h264parse config-interval=-1"
    candidates: list[H264Encoder] = [
        # The profile capsfilter is load-bearing: the codec string we send the
        # browser MUST describe what the encoder actually emits, so force the
        # profile at negotiation time — a mismatch fails the pipeline loudly
        # instead of streaming bytes the codec string lies about.
        H264Encoder(
            "vah264",
            f"vah264enc bitrate={bitrate_kbps} key-int-max={keyframe_interval} "
            f"! video/x-h264,profile=main ! {parse}",
            _CODEC_MAIN,
        ),
        H264Encoder(
            "vaapi",
            f"vaapih264enc rate-control=cbr bitrate={bitrate_kbps} "
            f"keyframe-period={keyframe_interval} ! video/x-h264,profile=main ! {parse}",
            _CODEC_MAIN,
        ),
        H264Encoder(
            "x264",
            f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
            f"key-int-max={keyframe_interval} bframes=0 byte-stream=true "
            f"! video/x-h264,profile=constrained-baseline ! {parse}",
            _CODEC_CONSTRAINED_BASELINE,
        ),
    ]
    if preference != "auto":
        candidates = [c for c in candidates if c.kind == preference]

    for cand in candidates:
        element = cand.fragment.split(None, 1)[0]
        if _element_exists(element):
            log.debug("H.264 encoder: %s", cand.kind)
            return cand
    log.warning("no H.264 encoder available (preference=%s); JPEG fallback only", preference)
    return None
