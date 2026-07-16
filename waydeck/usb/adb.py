"""USB dock mode: `adb reverse` forwards the phone's localhost port to the
laptop, so the browser connects to http://localhost:PORT — no WiFi, lowest
latency, and a secure context for free (Wake Lock + WebCodecs both work
without TLS). Command builders are pure functions for testability."""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)


def find_adb() -> str | None:
    return shutil.which("adb")


def build_devices_cmd(adb: str) -> list[str]:
    return [adb, "devices"]


def build_reverse_cmd(adb: str, serial: str, port: int) -> list[str]:
    return [adb, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"]


def build_reverse_remove_cmd(adb: str, serial: str, port: int) -> list[str]:
    return [adb, "-s", serial, "reverse", "--remove", f"tcp:{port}"]


def build_open_url_cmd(adb: str, serial: str, url: str) -> list[str]:
    return [adb, "-s", serial, "shell", "am", "start",
            "-a", "android.intent.action.VIEW", "-d", url]


def parse_devices(output: str) -> list[str]:
    """Serials of devices in the ready state (skips 'unauthorized',
    'offline', and the header/footer lines)."""
    serials = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


class UsbDock:
    def __init__(self, port: int) -> None:
        self.port = port
        self.adb = find_adb()
        self.serial: str | None = None

    def detect(self) -> str | None:
        """Return the serial of an attached, authorized device (or None)."""
        if not self.adb:
            return None
        ok, out = _run(build_devices_cmd(self.adb))
        if not ok:
            log.debug("adb devices failed: %s", out)
            return None
        devices = parse_devices(out)
        self.serial = devices[0] if devices else None
        if len(devices) > 1:
            log.info("multiple adb devices; using %s", self.serial)
        return self.serial

    def start(self) -> bool:
        if not (self.adb and self.serial):
            return False
        ok, out = _run(build_reverse_cmd(self.adb, self.serial, self.port))
        if not ok:
            log.warning("adb reverse failed: %s", out)
        return ok

    def open_url(self, url: str) -> bool:
        if not (self.adb and self.serial):
            return False
        ok, out = _run(build_open_url_cmd(self.adb, self.serial, url))
        if not ok:
            log.debug("adb open url failed: %s", out)
        return ok

    def stop(self) -> None:
        if self.adb and self.serial:
            _run(build_reverse_remove_cmd(self.adb, self.serial, self.port))
