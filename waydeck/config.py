"""Runtime configuration. Every knob is a CLI flag; nothing is hardcoded elsewhere."""

from __future__ import annotations

import argparse
import re
import secrets
from dataclasses import dataclass, field

DEFAULT_PORT = 8420
DEFAULT_SIZE = "1920x1080"
DEFAULT_JPEG_QUALITY = 80
DEFAULT_H264_BITRATE_KBPS = 8000
DEFAULT_KEYFRAME_INTERVAL = 60  # frames between forced IDR frames
# Each device costs a compositor session + a capture pipeline; 4 covers a
# desk full of phones while bounding resource use. Raise with --max-devices.
DEFAULT_MAX_DEVICES = 4
# Seconds a disconnected device's monitor survives awaiting reconnect, so a
# page reload or brief WiFi drop doesn't destroy the screen arrangement.
DEFAULT_LINGER_S = 45.0

_SIZE_RE = re.compile(r"^(\d{3,5})x(\d{3,5})$")


@dataclass
class Config:
    port: int = DEFAULT_PORT
    bind: str = ""  # empty = auto-detect LAN address
    width: int = 1920
    height: int = 1080
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    h264_bitrate: int = DEFAULT_H264_BITRATE_KBPS
    keyframe_interval: int = DEFAULT_KEYFRAME_INTERVAL
    encoder: str = "auto"  # auto | vah264 | vaapi | x264
    transport: str = "auto"  # auto | jpeg | h264
    input_mode: str = "touch"  # touch | pointer
    usb: str = "auto"  # auto | on | off
    max_devices: int = DEFAULT_MAX_DEVICES
    linger: float = DEFAULT_LINGER_S
    open_browser: bool = True  # in USB mode, auto-open the phone browser via adb
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    verbose: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


def parse_size(value: str) -> tuple[int, int]:
    m = _SIZE_RE.match(value.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid size {value!r}: expected WIDTHxHEIGHT, e.g. 1920x1080 or 1080x2400"
        )
    w, h = int(m.group(1)), int(m.group(2))
    if not (160 <= w <= 8192 and 160 <= h <= 8192):
        raise argparse.ArgumentTypeError(f"size {value!r} out of range (160..8192 per side)")
    return w, h


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="waydeck",
        description=(
            "Turn any old phone into a wireless touch monitor for your Linux desktop. "
            "One command. No app. No dongle."
        ),
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})"
    )
    p.add_argument(
        "--bind",
        default="",
        metavar="ADDR",
        help="address to bind (default: auto-detected LAN address; USB mode adds localhost)",
    )
    p.add_argument(
        "--size",
        type=parse_size,
        default=None,
        metavar="WxH",
        help=f"virtual monitor resolution (default {DEFAULT_SIZE}; portrait e.g. 1080x2400)",
    )
    p.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        choices=range(10, 101),
        metavar="10-100",
        help=f"JPEG quality for the fallback transport (default {DEFAULT_JPEG_QUALITY})",
    )
    p.add_argument(
        "--bitrate",
        type=int,
        default=DEFAULT_H264_BITRATE_KBPS,
        metavar="KBPS",
        help=f"H.264 target bitrate in kbit/s (default {DEFAULT_H264_BITRATE_KBPS})",
    )
    p.add_argument(
        "--keyframe-interval",
        type=int,
        default=DEFAULT_KEYFRAME_INTERVAL,
        metavar="FRAMES",
        help=f"max frames between H.264 keyframes (default {DEFAULT_KEYFRAME_INTERVAL})",
    )
    p.add_argument(
        "--encoder",
        choices=["auto", "vah264", "vaapi", "x264"],
        default="auto",
        help="H.264 encoder (default auto: hardware if available, else x264)",
    )
    p.add_argument(
        "--transport",
        choices=["auto", "jpeg", "h264"],
        default="auto",
        help="video transport (default auto: negotiated per client; H.264/WebCodecs "
        "needs a secure context — free over USB, JPEG fallback on plain-HTTP LAN)",
    )
    p.add_argument(
        "--input",
        dest="input_mode",
        choices=["touch", "pointer"],
        default="touch",
        help="inject real touchscreen events (default) or emulate a pointer with "
        "client-side gestures (tap=click, long-press=right-click, two-finger=scroll)",
    )
    p.add_argument(
        "--usb",
        choices=["auto", "on", "off"],
        default="auto",
        help="USB dock mode via adb reverse (default auto: use it when a device is attached)",
    )
    p.add_argument(
        "--max-devices",
        type=int,
        default=DEFAULT_MAX_DEVICES,
        metavar="N",
        help=f"maximum simultaneously connected devices (default {DEFAULT_MAX_DEVICES})",
    )
    p.add_argument(
        "--linger",
        type=float,
        default=DEFAULT_LINGER_S,
        metavar="SECONDS",
        help="keep a disconnected device's screen this long awaiting reconnect "
        f"(default {DEFAULT_LINGER_S:.0f}; 0 removes it immediately)",
    )
    p.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="in USB mode, do not auto-open the browser on the phone",
    )
    p.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="session token (default: random per run; embedded in the QR URL)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def config_from_args(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    w, h = args.size if args.size else parse_size(DEFAULT_SIZE)
    cfg = Config(
        port=args.port,
        bind=args.bind,
        width=w,
        height=h,
        jpeg_quality=args.quality,
        h264_bitrate=args.bitrate,
        keyframe_interval=args.keyframe_interval,
        encoder=args.encoder,
        transport=args.transport,
        input_mode=args.input_mode,
        usb=args.usb,
        max_devices=args.max_devices,
        linger=args.linger,
        open_browser=args.open_browser,
        verbose=args.verbose,
    )
    if args.token:
        cfg.token = args.token
    return cfg
