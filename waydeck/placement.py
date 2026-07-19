"""Client side of the Waydeck Placement GNOME Shell extension.

The extension (shipped in waydeck/data/gnome-extension/) keeps windows
launched from a phone screen on that screen and exposes Move/List/
VirtualMonitors over D-Bus. This module detects it, installs it into the
user's extensions directory, and wraps its D-Bus API.

waydeck degrades gracefully without it: windows just open on the primary
display as GNOME normally does — never an error.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

EXT_UUID = "waydeck-placement@waydeck.dev"
BUS_NAME = "org.gnome.Shell"
OBJECT_PATH = "/org/gnome/Shell/Extensions/WaydeckPlacement"
IFACE = "org.gnome.Shell.Extensions.WaydeckPlacement"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


def bundled_extension_dir() -> Path:
    return Path(__file__).parent / "data" / "gnome-extension" / EXT_UUID


def user_extension_dir(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".local" / "share" / "gnome-shell" / "extensions" / EXT_UUID


def _call(method: str, params=None, reply_fmt=None):
    """Sync D-Bus call to the extension; returns unpacked reply or None on
    any failure (extension absent/disabled)."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        reply = bus.call_sync(
            BUS_NAME, OBJECT_PATH, IFACE if method != "Get" else PROPS_IFACE,
            method, params, reply_fmt, Gio.DBusCallFlags.NONE, 3000, None,
        )
        return reply.unpack() if reply else None
    except GLib.Error as e:
        log.debug("placement extension call %s failed: %s", method, e.message)
        return None


def detect() -> int | None:
    """Extension's Version if installed AND enabled, else None."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib

    result = _call("Get", GLib.Variant("(ss)", (IFACE, "Version")))
    return int(result[0]) if result else None


def move_window(win_id: int, monitor: int) -> bool:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib

    result = _call("Move", GLib.Variant("(ui)", (win_id, monitor)))
    return bool(result and result[0])


def list_windows() -> list[dict] | None:
    result = _call("List")
    return json.loads(result[0]) if result else None


def virtual_monitors() -> list[int] | None:
    result = _call("VirtualMonitors")
    return list(result[0]) if result else None


def _add_to_enabled_gsetting() -> None:
    """Append the uuid to org.gnome.shell enabled-extensions directly.
    `gnome-extensions enable` refuses for extensions the running Shell
    hasn't discovered yet (GNOME only scans at login), but the setting
    itself can be written any time — the extension then activates at the
    next login with no further action."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    settings = Gio.Settings(schema_id="org.gnome.shell")
    enabled = list(settings.get_strv("enabled-extensions"))
    if EXT_UUID not in enabled:
        enabled.append(EXT_UUID)
        settings.set_strv("enabled-extensions", enabled)
        settings.sync()


def install() -> tuple[bool, str]:
    """Copy the bundled extension into the user's extensions dir and enable
    it. Returns (active_now, human_message). GNOME only discovers manually
    installed extensions at login, so first-time installs report that a
    log-out/log-in finishes the job — that's GNOME behavior, not an error."""
    src = bundled_extension_dir()
    if not src.is_dir():
        return False, f"bundled extension not found at {src}"
    dest = user_extension_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    # Try live enable first: works when the Shell already knows the uuid
    # (reinstall/upgrade case).
    subprocess.run(["gnome-extensions", "enable", EXT_UUID], capture_output=True, text=True)
    _add_to_enabled_gsetting()

    if detect() is not None:
        return True, "placement helper installed and active"
    return False, (
        "placement helper installed and enabled — log out and back in once "
        "to finish (GNOME loads newly installed extensions at login)"
    )
