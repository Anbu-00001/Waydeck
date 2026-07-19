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

SHELL_PATH = "/org/gnome/Shell"
SHELL_IFACE = "org.gnome.Shell"
# The extensions manager is a *separate* bus name from org.gnome.Shell.
EXTENSIONS_BUS = "org.gnome.Shell.Extensions"
EXTENSIONS_PATH = "/org/gnome/Shell/Extensions"
EXTENSIONS_IFACE = "org.gnome.Shell.Extensions"

# Ubuntu's default, ships-enabled tiling extension. On GNOME 46 (Ubuntu
# 24.04 LTS) it crashes gnome-shell — SIGABRT, "meta_window_get_workspaces:
# code should not be reached" — whenever a window moves across monitors,
# which is exactly what waydeck does when it sends apps to the phone screen.
# Known upstream as Ubuntu bug #2068539 / Tiling-Assistant #329; fixed from
# GNOME 47 / Ubuntu 24.10, so above this major version we stay silent.
TILING_ASSISTANT_UUID = "tiling-assistant@ubuntu.com"
TILING_CRASH_MAX_SHELL_MAJOR = 46
# gnome-shell ExtensionState.ENABLED
_EXT_STATE_ENABLED = 1


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


# -- tiling-assistant conflict (see TILING_ASSISTANT_UUID above) ------------


def shell_version() -> tuple[int, ...] | None:
    """(major, minor, …) of the running GNOME Shell, or None if unreachable."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        reply = bus.call_sync(
            BUS_NAME, SHELL_PATH, PROPS_IFACE, "Get",
            GLib.Variant("(ss)", (SHELL_IFACE, "ShellVersion")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 3000, None,
        )
    except GLib.Error as e:
        log.debug("ShellVersion query failed: %s", e.message)
        return None
    if not reply:
        return None
    parts = str(reply.unpack()[0]).split(".")
    nums = tuple(int(p) for p in parts if p.isdigit())
    return nums or None


def _extension_enabled(uuid: str) -> bool:
    """True if the Shell reports `uuid` as currently ENABLED."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        reply = bus.call_sync(
            EXTENSIONS_BUS, EXTENSIONS_PATH, EXTENSIONS_IFACE, "GetExtensionInfo",
            GLib.Variant("(s)", (uuid,)), GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE, 3000, None,
        )
        info = reply.unpack()[0] if reply else {}
    except GLib.Error as e:
        log.debug("GetExtensionInfo(%s) failed: %s", uuid, e.message)
        return False
    state = info.get("state")
    return state is not None and int(state) == _EXT_STATE_ENABLED


def _tiling_warning(enabled: bool, shell_ver: tuple[int, ...] | None) -> str | None:
    """Policy split out from the D-Bus probing so it's unit-testable. Warn
    when tiling-assistant is enabled on an affected (or unknown) GNOME
    version; stay silent once it's known-fixed."""
    if not enabled:
        return None
    if shell_ver and shell_ver[0] > TILING_CRASH_MAX_SHELL_MAJOR:
        return None
    return (
        "Ubuntu's tiling-assistant extension is on; on this GNOME version it "
        "can crash your desktop when a window moves to the phone screen "
        "(known bug #2068539). Re-run with --tame-tiling to have waydeck turn "
        "it off for this session and restore it on exit, or turn it off "
        f"yourself: gnome-extensions disable {TILING_ASSISTANT_UUID}"
    )


def tiling_conflict() -> str | None:
    """Human-readable warning if the tiling-assistant crash applies here,
    else None. waydeck moves windows across monitors, the exact trigger."""
    return _tiling_warning(_extension_enabled(TILING_ASSISTANT_UUID), shell_version())


def set_extension_enabled(uuid: str, enabled: bool) -> bool:
    """Enable/disable an already-installed extension; takes effect live
    (unlike installing new ones). Returns whether the command succeeded."""
    action = "enable" if enabled else "disable"
    r = subprocess.run(["gnome-extensions", action, uuid], capture_output=True, text=True)
    if r.returncode != 0:
        log.debug("gnome-extensions %s %s failed: %s", action, uuid, r.stderr.strip())
    return r.returncode == 0


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
