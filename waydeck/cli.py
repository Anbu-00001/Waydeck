"""waydeck CLI: one command that starts the server, prints the QR, creates a
screen for each phone that connects, and tears everything down on Ctrl-C.

The CLI is one of two frontends over waydeck/serve.py (the other is the GTK
window, waydeck/gui). Non-verbose output follows the plain-language rule:
device names, never compositor/session jargon — technical detail lives
behind --verbose."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from . import __version__
from .config import Config, config_from_args
from .qr import terminal_qr
from .serve import ServeError, ServeInfo, serve

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

    if cfg.gui:
        from .gui.app import run_gui

        return run_gui(cfg)

    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        return 130


def _print_ready(cfg: Config, info: ServeInfo) -> None:
    print(f"\n  {bold('waydeck')} v{__version__} "
          f"{dim(f'(GNOME, ScreenCast API v{info.screencast_version})')}")
    if cfg.verbose:
        log.debug("H.264 encoder: %s; input mode: %s",
                  info.h264_kind or "none (JPEG only)", cfg.input_mode)

    if info.usb_active:
        print(f"  {green('✓')} phone connected by USB — opening on the phone…")
        print(f"    {cyan(info.usb_url)}")
    if info.lan_url:
        print(f"  {green('→')} on the same WiFi, scan the QR or open: {cyan(info.lan_url)}")
        lan_warning = "this link is unencrypted on your network — anyone who has it can view"
        print(f"    {yellow(lan_warning)}")

    qr_url = info.lan_url if (info.lan_url and not info.usb_active) else info.usb_url
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


async def _run(cfg: Config) -> int:
    from .glibloop import GLibRunner

    runner = GLibRunner()
    runner.start()
    try:
        reason = await serve(
            cfg,
            runner,
            on_event=lambda msg: print(f"  {green('●')} {msg}"),
            on_ready=lambda info: _print_ready(cfg, info),
        )
        if reason:
            print(f"\n  {yellow('!')} stopping: {reason}")
        return 0
    except ServeError as e:
        return _fail(str(e), e.hint)
    finally:
        runner.stop()
        print(f"  {green('✓')} all screens removed — goodbye\n")
