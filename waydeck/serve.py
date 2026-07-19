"""Frontend-independent server orchestration.

Both frontends — the terminal CLI and the GTK window — drive this one
coroutine. The frontend supplies callbacks (`on_event` for human-readable
device events, `on_ready` once the server is listening) and receives a
`ServeInfo` with everything it needs to render URLs/QR and to request
shutdown from another thread.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .device import DeviceManager
from .netutil import lan_address, port_free

log = logging.getLogger(__name__)


class ServeError(RuntimeError):
    def __init__(self, msg: str, hint: str = "") -> None:
        super().__init__(msg)
        self.hint = hint


@dataclass
class ServeInfo:
    screencast_version: int
    lan_url: str | None
    usb_url: str
    usb_active: bool
    usb_serial: str | None
    h264_kind: str | None
    placement_version: int | None  # placement extension, None = not active
    manager: DeviceManager
    loop: asyncio.AbstractEventLoop
    request_shutdown: Callable[[str], None]  # safe to call from any thread


async def serve(
    cfg: Config,
    runner,
    on_event: Callable[[str], None],
    on_ready: Callable[[ServeInfo], None],
    install_signals: bool = True,
) -> str | None:
    """Run the server until shutdown is requested. Returns the shutdown
    reason (or None for a plain exit). Raises ServeError for preflight
    failures. `install_signals=False` for frontends that own signal
    handling themselves (the GTK window) or run this off the main thread."""
    from .adapters.gnome import screencast_api_version
    from .server.app import WaydeckServer
    from .stream.capture import gst_init
    from .usb.adb import UsbDock

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    shutdown_reason: list[str] = []

    def request_shutdown(reason: str = "") -> None:
        if reason:
            shutdown_reason.append(reason)
        shutdown.set()

    def request_shutdown_threadsafe(reason: str = "") -> None:
        loop.call_soon_threadsafe(request_shutdown, reason)

    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_shutdown)

    # Version-gate the compositor API up front so failure is immediate and
    # specific, not deferred to the first phone connecting.
    version = await runner.acall(screencast_api_version)
    if version is None:
        raise ServeError(
            "couldn't reach the desktop's screen-sharing service — "
            "is this a GNOME session on Wayland?"
        )

    manager = DeviceManager(cfg, runner, on_event=on_event)
    server = WaydeckServer(cfg, runner, manager)
    usb = None
    try:
        await runner.acall(gst_init)
        await runner.acall(server.detect_h264)

        lan = cfg.bind or lan_address()
        binds = [addr for addr in dict.fromkeys([lan, "127.0.0.1"]) if addr]
        for addr in binds:
            if not port_free(addr, cfg.port):
                raise ServeError(
                    f"port {cfg.port} is busy on {addr}", "pick another with --port"
                )
        await server.start(binds)

        usb_active = False
        usb_serial = None
        if cfg.usb != "off":
            usb = UsbDock(cfg.port)
            usb_serial = usb.detect()
            if usb_serial:
                usb_active = usb.start()
            elif cfg.usb == "on":
                raise ServeError(
                    "--usb on, but no authorized adb device found.",
                    "Enable USB debugging on the phone and accept the prompt.",
                )

        usb_url = f"http://localhost:{cfg.port}/?t={cfg.token}"
        if usb_active and cfg.open_browser:
            usb.open_url(usb_url)

        from . import placement

        placement_version = await runner.acall(placement.detect)

        on_ready(
            ServeInfo(
                screencast_version=version,
                lan_url=f"http://{lan}:{cfg.port}/?t={cfg.token}" if lan else None,
                usb_url=usb_url,
                usb_active=usb_active,
                usb_serial=usb_serial,
                h264_kind=server.h264.kind if server.h264 else None,
                placement_version=placement_version,
                manager=manager,
                loop=loop,
                request_shutdown=request_shutdown_threadsafe,
            )
        )

        await shutdown.wait()
        return shutdown_reason[0] if shutdown_reason else None
    finally:
        # server.stop() shuts down the DeviceManager first: every device's
        # session Stop is what removes its monitor — never skip it.
        await server.stop()
        if usb:
            usb.stop()
