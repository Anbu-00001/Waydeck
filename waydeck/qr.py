"""Terminal QR rendering. Degrades gracefully if the qrcode module is absent —
the URL still gets printed, pairing just needs typing instead of scanning."""

from __future__ import annotations

import io


def terminal_qr(url: str) -> str | None:
    """Return the QR code as terminal text, or None if qrcode is unavailable."""
    try:
        import qrcode
    except ImportError:
        return None
    code = qrcode.QRCode(border=2)
    code.add_data(url)
    buf = io.StringIO()
    # invert=True renders dark-on-light modules, which scans reliably on
    # dark terminal themes.
    code.print_ascii(out=buf, invert=True)
    return buf.getvalue()
