"""GNOME adapter: drives Mutter's private RemoteDesktop + ScreenCast D-Bus
APIs — the same path gnome-remote-desktop uses.

Call sequence (validated against mutter's gnome-46 interface XML):
  1. org.gnome.Mutter.RemoteDesktop.CreateSession() -> rd session
  2. read rd session's SessionId property
  3. org.gnome.Mutter.ScreenCast.CreateSession({'remote-desktop-session-id': id})
     -> sc session bound to the rd session (input + video share one session)
  4. sc session.RecordVirtual({'cursor-mode': 1, 'is-platform': True}) -> stream
     - cursor-mode 1 (embedded): metadata mode has a known flicker bug on
       virtual monitors (mutter#3105)
     - is-platform True (ScreenCast API >= 3): monitor is treated as real
       hardware, not a screen-share
  5. subscribe to the stream's PipeWireStreamAdded(u node_id) BEFORE starting —
     the monitor materializes asynchronously after PipeWire negotiation
  6. rd session.Start() (starts the bundled screen-cast session too)

Teardown: rd session.Stop(). The monitor lives exactly as long as the session.

This is a private Mutter API (the xdg portal has no virtual-monitor support),
so we version-gate at startup and keep the blast radius inside this adapter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .base import AdapterError, CompositorAdapter, InputInjector  # noqa: E402

log = logging.getLogger(__name__)

RD_BUS = "org.gnome.Mutter.RemoteDesktop"
RD_PATH = "/org/gnome/Mutter/RemoteDesktop"
RD_IFACE = RD_BUS
RD_SESSION_IFACE = RD_BUS + ".Session"

SC_BUS = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
SC_IFACE = SC_BUS
SC_SESSION_IFACE = SC_BUS + ".Session"
SC_STREAM_IFACE = SC_BUS + ".Stream"

PROPS_IFACE = "org.freedesktop.DBus.Properties"

# is-platform appeared in ScreenCast API version 3 (this machine: 4).
MIN_SCREENCAST_VERSION = 3

CURSOR_MODE_HIDDEN = 0
CURSOR_MODE_EMBEDDED = 1
CURSOR_MODE_METADATA = 2


def _call(
    bus: Gio.DBusConnection,
    bus_name: str,
    path: str,
    iface: str,
    method: str,
    params: GLib.Variant | None = None,
) -> GLib.Variant:
    try:
        return bus.call_sync(
            bus_name, path, iface, method, params, None, Gio.DBusCallFlags.NONE, 8000, None
        )
    except GLib.Error as e:
        raise AdapterError(f"D-Bus call {iface}.{method} failed: {e.message}") from e


def _get_prop(bus: Gio.DBusConnection, bus_name: str, path: str, iface: str, prop: str):
    reply = _call(
        bus, bus_name, path, PROPS_IFACE, "Get", GLib.Variant("(ss)", (iface, prop))
    )
    return reply.unpack()[0]


def screencast_api_version(bus: Gio.DBusConnection | None = None) -> int | None:
    """Return Mutter's ScreenCast API version, or None if GNOME isn't running."""
    bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        return int(_get_prop(bus, SC_BUS, SC_PATH, SC_IFACE, "Version"))
    except AdapterError:
        return None


class MutterInput(InputInjector):
    """Fire-and-forget async D-Bus calls; errors are logged, never raised —
    a dropped input event must not take down the stream."""

    def __init__(self, bus: Gio.DBusConnection, rd_session_path: str, stream_path: str):
        self._bus = bus
        self._path = rd_session_path
        self._stream = stream_path

    def _notify(self, method: str, params: GLib.Variant) -> None:
        self._bus.call(
            RD_BUS, self._path, RD_SESSION_IFACE, method, params,
            None, Gio.DBusCallFlags.NONE, 2000, None, self._finish, method,
        )

    @staticmethod
    def _finish(bus: Gio.DBusConnection, res: Gio.AsyncResult, method: str) -> None:
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            log.warning("input %s failed: %s", method, e.message)

    def touch_down(self, slot: int, x: float, y: float) -> None:
        self._notify("NotifyTouchDown", GLib.Variant("(sudd)", (self._stream, slot, x, y)))

    def touch_motion(self, slot: int, x: float, y: float) -> None:
        self._notify("NotifyTouchMotion", GLib.Variant("(sudd)", (self._stream, slot, x, y)))

    def touch_up(self, slot: int) -> None:
        self._notify("NotifyTouchUp", GLib.Variant("(u)", (slot,)))

    def pointer_motion(self, x: float, y: float) -> None:
        self._notify(
            "NotifyPointerMotionAbsolute", GLib.Variant("(sdd)", (self._stream, x, y))
        )

    def pointer_button(self, button: int, pressed: bool) -> None:
        self._notify("NotifyPointerButton", GLib.Variant("(ib)", (button, pressed)))

    def pointer_axis(self, dx: float, dy: float, finish: bool = False) -> None:
        flags = 1 if finish else 0
        self._notify("NotifyPointerAxis", GLib.Variant("(ddu)", (dx, dy, flags)))

    def keysym(self, keysym: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeysym", GLib.Variant("(ub)", (keysym, pressed)))


class GnomeAdapter(CompositorAdapter):
    name = "gnome"

    def __init__(self, cursor_mode: int = CURSOR_MODE_EMBEDDED):
        self._cursor_mode = cursor_mode
        self._bus: Gio.DBusConnection | None = None
        self._rd_path: str | None = None
        self._sc_path: str | None = None
        self._stream_path: str | None = None
        self._input: MutterInput | None = None
        self._subs: list[int] = []
        self._stopped = False
        self._on_closed: Callable[[str], None] | None = None

    def start(
        self,
        on_node_id: Callable[[int], None],
        on_closed: Callable[[str], None],
    ) -> None:
        self._on_closed = on_closed
        bus = self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        version = screencast_api_version(bus)
        if version is None:
            raise AdapterError(
                "Mutter's ScreenCast D-Bus service is not reachable — is this a "
                "GNOME Wayland session?"
            )
        if version < MIN_SCREENCAST_VERSION:
            raise AdapterError(
                f"Mutter ScreenCast API version {version} is too old (need >= "
                f"{MIN_SCREENCAST_VERSION}, i.e. GNOME 44+). This is a private Mutter "
                "API; please open an issue with your GNOME version."
            )
        log.debug("Mutter ScreenCast API version %d", version)

        reply = _call(bus, RD_BUS, RD_PATH, RD_IFACE, "CreateSession")
        self._rd_path = reply.unpack()[0]
        session_id = _get_prop(bus, RD_BUS, self._rd_path, RD_SESSION_IFACE, "SessionId")
        log.debug("remote-desktop session %s (id %s)", self._rd_path, session_id)

        reply = _call(
            bus, SC_BUS, SC_PATH, SC_IFACE, "CreateSession",
            GLib.Variant(
                "(a{sv})", ({"remote-desktop-session-id": GLib.Variant("s", session_id)},)
            ),
        )
        self._sc_path = reply.unpack()[0]

        reply = _call(
            bus, SC_BUS, self._sc_path, SC_SESSION_IFACE, "RecordVirtual",
            GLib.Variant(
                "(a{sv})",
                (
                    {
                        "cursor-mode": GLib.Variant("u", self._cursor_mode),
                        "is-platform": GLib.Variant("b", True),
                    },
                ),
            ),
        )
        self._stream_path = reply.unpack()[0]
        log.debug("virtual stream %s", self._stream_path)

        # Subscribe BEFORE Start: the node id arrives asynchronously.
        def on_stream_added(_bus, _sender, _path, _iface, _signal, params):
            node_id = params.unpack()[0]
            log.debug("PipeWireStreamAdded: node %d", node_id)
            on_node_id(node_id)

        def on_session_closed(_bus, _sender, path, _iface, _signal, _params):
            if not self._stopped and self._on_closed:
                self._on_closed(f"compositor closed the session ({path})")

        self._subs.append(
            bus.signal_subscribe(
                SC_BUS, SC_STREAM_IFACE, "PipeWireStreamAdded", self._stream_path,
                None, Gio.DBusSignalFlags.NONE, on_stream_added,
            )
        )
        for iface, path in ((SC_SESSION_IFACE, self._sc_path), (RD_SESSION_IFACE, self._rd_path)):
            self._subs.append(
                bus.signal_subscribe(
                    None, iface, "Closed", path, None,
                    Gio.DBusSignalFlags.NONE, on_session_closed,
                )
            )

        self._input = MutterInput(bus, self._rd_path, self._stream_path)
        _call(bus, RD_BUS, self._rd_path, RD_SESSION_IFACE, "Start")
        log.debug("session started; waiting for PipeWire negotiation")

    def stop(self) -> None:
        if self._stopped or not self._bus:
            return
        self._stopped = True
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs.clear()
        if self._rd_path:
            try:
                _call(self._bus, RD_BUS, self._rd_path, RD_SESSION_IFACE, "Stop")
                log.debug("session stopped cleanly")
            except AdapterError as e:
                # Session may already be gone (e.g. compositor closed it).
                log.debug("session stop: %s", e)

    @property
    def input(self) -> MutterInput:
        if not self._input:
            raise AdapterError("adapter not started")
        return self._input
