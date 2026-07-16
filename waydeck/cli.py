"""waydeck CLI: one command that creates the monitor, starts the server,
prints the QR, and tears everything down cleanly on Ctrl-C."""

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
    from .adapters.base import AdapterError
    from .adapters.gnome import GnomeAdapter, screencast_api_version
    from .glibloop import GLibRunner
    from .server.app import WaydeckServer
    from .stream.capture import KeepalivePipeline, PipelineError, gst_init, resolve_target
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

    adapter = GnomeAdapter()
    keepalive = None
    server = None
    usb = None
    try:
        version = await runner.acall(screencast_api_version)
        print(f"\n  {bold('waydeck')} v{__version__} — GNOME adapter "
              f"{dim(f'(Mutter ScreenCast API v{version})') if version else ''}")

        # 1. Virtual monitor session
        node_fut: asyncio.Future[int] = loop.create_future()

        def on_node(node_id: int) -> None:
            loop.call_soon_threadsafe(
                lambda: node_fut.set_result(node_id) if not node_fut.done() else None
            )

        def on_closed(reason: str) -> None:
            loop.call_soon_threadsafe(request_shutdown, reason)

        try:
            await runner.acall(adapter.start, on_node, on_closed)
        except AdapterError as e:
            return _fail(str(e))
        try:
            node_id = await asyncio.wait_for(node_fut, timeout=15)
        except asyncio.TimeoutError:
            return _fail(
                "Mutter never announced the PipeWire stream (15s timeout).",
                "Try --verbose; check `journalctl --user -u gnome-shell` for errors.",
            )

        # 2. Keepalive pipeline — makes the monitor materialize and pins its size
        size_fut: asyncio.Future[tuple[int, int]] = loop.create_future()

        def on_size(w: int, h: int) -> None:
            loop.call_soon_threadsafe(
                lambda: size_fut.set_result((w, h)) if not size_fut.done() else None
            )

        def on_pipe_error(msg: str) -> None:
            loop.call_soon_threadsafe(request_shutdown, f"capture pipeline error: {msg}")

        await runner.acall(gst_init)
        target = await runner.acall(resolve_target, node_id)
        try:
            keepalive = await runner.acall(
                KeepalivePipeline, target, cfg.width, cfg.height, on_size, on_pipe_error
            )
        except PipelineError as e:
            return _fail(str(e), "Is gstreamer1.0-pipewire installed?")
        try:
            width, height = await asyncio.wait_for(size_fut, timeout=10)
        except asyncio.TimeoutError:
            return _fail(
                "PipeWire format negotiation timed out.",
                f"The compositor may have rejected {cfg.width}x{cfg.height}; "
                "try another --size.",
            )
        if (width, height) != (cfg.width, cfg.height):
            print(f"  {yellow('!')} compositor negotiated {width}x{height} "
                  f"instead of {cfg.width}x{cfg.height}")
        print(f"  {green('✓')} virtual monitor {bold(f'{width}x{height}')} created — "
              f"arrange it in {bold('Settings → Displays')}")

        # 3. Server
        server = WaydeckServer(cfg, runner, target, adapter.input)
        server.size = (width, height)
        await runner.acall(server.detect_h264)
        enc_desc = server.h264.kind if server.h264 else "none (JPEG only)"
        print(f"  {green('✓')} H.264 encoder: {enc_desc}   input: {cfg.input_mode}")

        # 4. Addresses: LAN + localhost (the latter for USB mode)
        lan = cfg.bind or lan_address()
        binds = [addr for addr in dict.fromkeys([lan, "127.0.0.1"]) if addr]
        for addr in binds:
            if not port_free(addr, cfg.port):
                return _fail(f"port {cfg.port} is busy on {addr}", "pick another with --port")
        await server.start(binds)

        # 5. USB dock mode
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
            note = "browser opened on phone" if opened else "open this on the phone"
            print(f"  {green('✓')} USB dock: {usb.serial} (adb reverse tcp:{cfg.port}) — {note}")
            print(f"    {cyan(usb_url)}  {dim('(secure context: H.264 + wake lock)')}")
        if lan_url:
            fallback = " (JPEG fallback on plain HTTP)" if server.h264 else ""
            print(f"  {green('→')} LAN: {cyan(lan_url)}{dim(fallback)}")
            print(f"    {yellow('unencrypted on your LAN — anyone with this URL can view')}")

        qr_url = lan_url if (lan_url and not usb_active) else usb_url
        code = terminal_qr(qr_url)
        if code:
            print("\n" + code)
        else:
            print(f"\n  {yellow('!')} python3-qrcode not installed — no QR; "
                  "type the URL on the phone instead")

        def on_client(desc: str) -> None:
            stamp = green("● client connected") if desc else dim("○ client disconnected")
            print(f"  {stamp} {desc}")

        server.on_client_change = on_client

        print(f"\n  {dim('Ctrl-C stops and removes the monitor cleanly.')}\n")

        # 6. Run until stopped
        await shutdown.wait()
        if shutdown_reason:
            print(f"\n  {yellow('!')} shutting down: {shutdown_reason[0]}")
        return 0
    finally:
        # Teardown in reverse: clients, sockets, USB, pipeline, session.
        # The session Stop is what removes the monitor — never skip it.
        if server:
            await server.stop()
        if usb:
            usb.stop()
        if keepalive:
            await runner.acall(keepalive.stop)
        await runner.acall(adapter.stop)
        runner.stop()
        print(f"  {green('✓')} torn down — virtual monitor removed\n")
