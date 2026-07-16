"""Small network helpers."""

from __future__ import annotations

import socket


def lan_address() -> str | None:
    """Best-effort LAN IP: UDP connect sends no packets but selects the
    default route's source address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return s.getsockname()[0]
    except OSError:
        return None


def port_free(addr: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((addr, port))
            return True
        except OSError:
            return False
