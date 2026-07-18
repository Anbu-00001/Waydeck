"""waydeck CLI: one command that starts the server, prints the QR, creates a
screen for each phone that connects, and tears everything down on Ctrl-C.

M2.1: monitors are provisioned per-connecting-device (see waydeck/device.py)
instead of one fixed monitor at startup. Non-verbose output follows the
plain-language rule: device names, never compositor/session jargon — the
technical detail lives behind --verbose."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .config import Config, config_from_args
from .netutil import lan_address, port_free
from .qr import terminal_qr

log = logging.getLogger(__name__)

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def _fail(msg: str, hint: str = "") -> int:
    print(f"{red('✗')} {msg}", file=sys.stderr)
    if hint:
        print(f"  {dim(hint)}", file=sys.stderr)
    return 1


def _preflight() -> str | None:
    if os.environ.get("XDG_SESSION_TYPE", "") == "x11":
        return (
            "this is an X11 session — waydeck targets Wayland. "
            "(On X11, xrandr-based tools like VirtScreen still work.)"
        )
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if desktop and "gnome" not in desktop.lower():
        return (
            f"desktop {desktop!r} is not GNOME — the GNOME adapter is the only "
            "one implemented so far (KDE and wlroots adapters are on the roadmap)."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    cfg = config_from_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    warning = _preflight()
    if warning:
        return _fail(warning)

    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        return 130


async def _run(cfg: Config) -> int:
    # Imports that need gi live here so `waydeck --help` works anywhere.
    from .adapters.gnome import screencast_api_version
    from .device import DeviceManager
    from .glibloop import GLibRunner
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

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_shutdown)

    runner = GLibRunner()
    runner.start()

    server = None
    usb = None
    try:
        # Version-gate the compositor API up front so failure is immediate
        # and specific, not deferred to the first phone connecting.
        version = await runner.acall(screencast_api_version)
        if version is None:
            return _fail(
                "couldn't reach the desktop's screen-sharing service — "
                "is this a GNOME session on Wayland?"
            )
        print(f"\n  {bold('waydeck')} v{__version__} "
              f"{dim(f'(GNOME, ScreenCast API v{version})')}")

        def on_device_event(msg: str) -> None:
            print(f"  {green('●')} {msg}")

        manager = DeviceManager(cfg, runner, on_event=on_device_event)
        server = WaydeckServer(cfg, runner, manager)
        await runner.acall(gst_init)
        await runner.acall(server.detect_h264)
        if cfg.verbose:
            enc_desc = server.h264.kind if server.h264 else "none (JPEG only)"
            log.debug("H.264 encoder: %s; input mode: %s", enc_desc, cfg.input_mode)

        # Addresses: LAN + localhost (the latter for USB mode)
        lan = cfg.bind or lan_address()
        binds = [addr for addr in dict.fromkeys([lan, "127.0.0.1"]) if addr]
        for addr in binds:
            if not port_free(addr, cfg.port):
                return _fail(f"port {cfg.port} is busy on {addr}", "pick another with --port")
        await server.start(binds)

        # USB dock mode
        usb_active = False
        if cfg.usb != "off":
            usb = UsbDock(cfg.port)
            serial = usb.detect()
            if serial:
                usb_active = usb.start()
            elif cfg.usb == "on":
                return _fail(
                    "--usb on, but no authorized adb device found.",
                    "Enable USB debugging on the phone and accept the prompt.",
                )

        usb_url = f"http://localhost:{cfg.port}/?t={cfg.token}"
        lan_url = f"http://{lan}:{cfg.port}/?t={cfg.token}" if lan else None

        if usb_active:
            opened = cfg.open_browser and usb.open_url(usb_url)
            note = "opening on the phone…" if opened else "open this on the phone:"
            print(f"  {green('✓')} phone connected by USB — {note}")
            print(f"    {cyan(usb_url)}")
        if lan_url:
            print(f"  {green('→')} on the same WiFi, scan the QR or open: {cyan(lan_url)}")
            lan_warning = "this link is unencrypted on your network — anyone who has it can view"
            print(f"    {yellow(lan_warning)}")

        qr_url = lan_url if (lan_url and not usb_active) else usb_url
        code = terminal_qr(qr_url)
        if code:
            print("\n" + code)
        else:
            print(f"\n  {yellow('!')} QR unavailable (install python3-qrcode) — "
                  "type the link on the phone instead")

        hint = (
            f"Each phone that connects gets its own screen (up to "
            f"{cfg.max_devices}). Ctrl-C stops everything."
        )
        print(f"  {dim(hint)}\n")

        await shutdown.wait()
        if shutdown_reason:
            print(f"\n  {yellow('!')} stopping: {shutdown_reason[0]}")
        return 0
    finally:
        # server.stop() shuts down the DeviceManager first: every device's
        # session Stop is what removes its monitor — never skip it.
        if server:
            await server.stop()
        if usb:
            usb.stop()
        runner.stop()
        print(f"  {green('✓')} all screens removed — goodbye\n")
