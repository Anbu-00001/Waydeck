"""Device lifecycle: one connected phone = one Device = one virtual monitor.

Phase 2 (M2.1) replaces the single-monitor-at-startup model: monitors are
provisioned when a phone connects and torn down after a linger window
(so a page reload or brief WiFi drop doesn't destroy and recreate the
monitor — GNOME would forget its arrangement). Mutter supports multiple
concurrent RemoteDesktop+ScreenCast sessions (verified live on GNOME 46),
so each Device owns a full independent session + keepalive pipeline.

The provisioning flow (D-Bus dance + PipeWire negotiation) is injectable so
the linger/reattach state machine is unit-testable without gi.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

NODE_ANNOUNCE_TIMEOUT_S = 15
SIZE_NEGOTIATION_TIMEOUT_S = 10

_ANDROID_MODEL_RE = re.compile(r"Android [^;)]+; ([^;)]+)")


class DeviceError(RuntimeError):
    """Provisioning failed or the device limit was reached."""


def device_name_from_ua(ua: str) -> str:
    """Human name for status lines: 'Redmi 4', 'iPhone', … never raw UA."""
    m = _ANDROID_MODEL_RE.search(ua)
    if m:
        name = m.group(1).strip()
        # Some builds append "Build/XXX" to the model token.
        return name.split(" Build")[0] or "Android device"
    for token in ("iPhone", "iPad"):
        if token in ua:
            return token
    return "Device"


@dataclass
class Device:
    id: str
    name: str
    adapter: Any  # CompositorAdapter
    keepalive: Any  # KeepalivePipeline
    target: str  # pipewiresrc target-object
    size: tuple[int, int]
    ws: Any = None  # active WebSocketResponse or None (lingering)
    linger_handle: Any = field(default=None, repr=False)

    @property
    def injector(self):
        return self.adapter.input


class DeviceManager:
    """asyncio-side owner of all Devices. GLib-thread work goes through
    `runner`; `provision` may be overridden in tests."""

    def __init__(
        self,
        cfg: Config,
        runner,
        on_event: Callable[[str], None] = lambda msg: None,
        provision: Callable[..., Awaitable[tuple[Any, Any, str, tuple[int, int]]]]
        | None = None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.on_event = on_event
        self._provision = provision or self._provision_gnome
        self._devices: dict[str, Device] = {}
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> dict[str, Device]:
        return self._devices

    # -- acquisition --------------------------------------------------------

    async def acquire(self, device_id: str | None, ua: str) -> Device:
        """Reattach to a lingering Device, or provision a new one.

        If the id matches a device with an ACTIVE connection, the caller is
        expected to displace it (latest-wins, same policy as Phase 1) —
        we just return the device; the server closes the old socket.
        """
        async with self._lock:
            if device_id and device_id in self._devices:
                device = self._devices[device_id]
                self._cancel_linger(device)
                log.debug("reattach to device %s (%s)", device.id, device.name)
                self.on_event(f"{device.name} reconnected to its screen")
                return device

            if len(self._devices) >= self.cfg.max_devices:
                raise DeviceError(
                    f"device limit reached ({self.cfg.max_devices}); "
                    "disconnect another device or raise --max-devices"
                )

            name = device_name_from_ua(ua)
            adapter, keepalive, target, size = await self._provision(name)
            device = Device(
                id=secrets.token_hex(4),
                name=name,
                adapter=adapter,
                keepalive=keepalive,
                target=target,
                size=size,
            )
            self._devices[device.id] = device
            self.on_event(f"{device.name} connected — new screen created ({size[0]}x{size[1]})")
            return device

    def release(self, device: Device) -> None:
        """Connection gone: keep the monitor for the linger window so a
        reload/brief drop reattaches to the same screen."""
        device.ws = None
        if device.id not in self._devices:
            return
        if self.cfg.linger <= 0:
            asyncio.ensure_future(self.remove(device, "disconnected"))
            return
        self.on_event(
            f"{device.name} disconnected — keeping its screen for {self.cfg.linger:.0f}s"
        )
        loop = asyncio.get_running_loop()
        self._cancel_linger(device)
        device.linger_handle = loop.call_later(
            self.cfg.linger,
            lambda: asyncio.ensure_future(self.remove(device, "not reconnected")),
        )

    def _cancel_linger(self, device: Device) -> None:
        if device.linger_handle is not None:
            device.linger_handle.cancel()
            device.linger_handle = None

    # -- teardown -----------------------------------------------------------

    async def remove(self, device: Device, reason: str) -> None:
        if self._devices.pop(device.id, None) is None:
            return
        self._cancel_linger(device)
        if device.ws is not None and not device.ws.closed:
            await device.ws.close(code=1001, message=reason.encode())
        if device.keepalive is not None:
            await self.runner.acall(device.keepalive.stop)
        await self.runner.acall(device.adapter.stop)
        self.on_event(f"{device.name} removed ({reason})")

    async def shutdown(self) -> None:
        for device in list(self._devices.values()):
            await self.remove(device, "shutting down")

    # -- real provisioning (GNOME) ------------------------------------------

    async def _provision_gnome(self, name: str):
        from .adapters.gnome import GnomeAdapter
        from .stream.capture import KeepalivePipeline, resolve_target

        log.debug("provisioning virtual monitor for %s", name)
        loop = asyncio.get_running_loop()
        adapter = GnomeAdapter()

        node_fut: asyncio.Future[int] = loop.create_future()

        def on_node(node_id: int) -> None:
            loop.call_soon_threadsafe(
                lambda: node_fut.set_result(node_id) if not node_fut.done() else None
            )

        def on_closed(reason: str) -> None:
            # The compositor ended this device's session (e.g. screen lock).
            loop.call_soon_threadsafe(self._session_closed, adapter, reason)

        await self.runner.acall(adapter.start, on_node, on_closed)
        try:
            node_id = await asyncio.wait_for(node_fut, timeout=NODE_ANNOUNCE_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.runner.acall(adapter.stop)
            raise DeviceError("compositor never announced the video stream") from None

        target = await self.runner.acall(resolve_target, node_id)

        size_fut: asyncio.Future[tuple[int, int]] = loop.create_future()

        def on_size(w: int, h: int) -> None:
            loop.call_soon_threadsafe(
                lambda: size_fut.set_result((w, h)) if not size_fut.done() else None
            )

        def on_pipe_error(msg: str) -> None:
            log.error("keepalive pipeline error: %s", msg)

        keepalive = await self.runner.acall(
            KeepalivePipeline, target, self.cfg.width, self.cfg.height, on_size, on_pipe_error
        )
        try:
            size = await asyncio.wait_for(size_fut, timeout=SIZE_NEGOTIATION_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.runner.acall(keepalive.stop)
            await self.runner.acall(adapter.stop)
            raise DeviceError(
                f"screen size negotiation timed out (requested "
                f"{self.cfg.width}x{self.cfg.height}; try another --size)"
            ) from None
        return adapter, keepalive, target, size

    def _session_closed(self, adapter, reason: str) -> None:
        for device in list(self._devices.values()):
            if device.adapter is adapter:
                asyncio.ensure_future(self.remove(device, reason))
                return
