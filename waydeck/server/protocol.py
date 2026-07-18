"""Wire protocol shared with the browser client (see static/client.js).

One WebSocket carries everything:
  * binary, server -> client: video frames with a 16-byte header
  * JSON text, both ways: hello/config handshake, input events, ping/pong

Binary frame header (little-endian):
  u8   type              (1 = video)
  u8   flags              (bit 0 = keyframe)
  u16  reserved
  f64  server send time, ms since epoch (for the client's latency HUD)
  f32  capture+encode ms, NaN if unmeasured (see stream/capture.py)
"""

from __future__ import annotations

import hmac
import math
import struct
from dataclasses import dataclass

FRAME_TYPE_VIDEO = 1
FLAG_KEYFRAME = 0b0000_0001

_HEADER = struct.Struct("<BBHdf")
HEADER_SIZE = _HEADER.size

TRANSPORT_JPEG = "jpeg"
TRANSPORT_H264 = "h264"

# WebSocket close codes (4000-4999 are application-defined).
CLOSE_REPLACED = 4000
CLOSE_BAD_TOKEN = 4003
CLOSE_UNSUPPORTED = 4005


def pack_video_frame(
    payload: bytes, keyframe: bool, send_time_ms: float, capture_encode_ms: float | None
) -> bytes:
    flags = FLAG_KEYFRAME if keyframe else 0
    encode_field = math.nan if capture_encode_ms is None else capture_encode_ms
    return _HEADER.pack(FRAME_TYPE_VIDEO, flags, 0, send_time_ms, encode_field) + payload


def unpack_header(data: bytes) -> tuple[int, bool, float, float | None]:
    """Returns (type, keyframe, send_time_ms, capture_encode_ms). Used by
    tests; the JS client mirrors this parsing."""
    ftype, flags, _, ts, encode_ms = _HEADER.unpack_from(data)
    return ftype, bool(flags & FLAG_KEYFRAME), ts, (None if math.isnan(encode_ms) else encode_ms)


def token_ok(expected: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(expected, presented)


@dataclass(frozen=True)
class ClientHello:
    webcodecs: bool = False
    secure: bool = False
    user_agent: str = ""
    device_id: str | None = None  # previous device id for reconnect-reattach

    @classmethod
    def from_msg(cls, msg: dict) -> ClientHello:
        raw_device = msg.get("device")
        return cls(
            webcodecs=bool(msg.get("webcodecs")),
            secure=bool(msg.get("secure")),
            user_agent=str(msg.get("ua", ""))[:200],
            device_id=str(raw_device)[:32] if raw_device else None,
        )


def decide_transport(
    configured: str, hello: ClientHello, h264_available: bool
) -> tuple[str, str | None]:
    """Pick the video transport for a client.

    Returns (transport, error). H.264 decode in the browser uses WebCodecs,
    which only exists in secure contexts (https:// or localhost — the latter
    is what USB mode provides), so an insecure LAN client falls back to JPEG.
    """
    h264_possible = h264_available and hello.webcodecs and hello.secure
    if configured == TRANSPORT_H264:
        if not h264_available:
            return "", "H.264 requested but no encoder is available on the host"
        if not h264_possible:
            return "", (
                "H.264 requested but this client cannot use WebCodecs "
                "(needs a secure context: use USB mode or TLS)"
            )
        return TRANSPORT_H264, None
    if configured == TRANSPORT_JPEG:
        return TRANSPORT_JPEG, None
    return (TRANSPORT_H264 if h264_possible else TRANSPORT_JPEG), None
