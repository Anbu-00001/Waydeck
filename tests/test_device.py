"""DeviceManager state machine tests: provisioning, reattach, linger expiry,
device limit — all with an injected fake provision, no gi required.

Async tests run through plain asyncio.run so no pytest-asyncio plugin is
needed (neither the apt-packaged pytest nor CI installs it)."""

import asyncio

import pytest

from waydeck.config import Config
from waydeck.device import DeviceError, DeviceManager, device_name_from_ua


class FakeAdapter:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeKeepalive:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeRunner:
    def fire(self, fn, *args):
        fn(*args)

    async def acall(self, fn, *args, timeout=30):
        return fn(*args)


def make_manager(**cfg_kwargs):
    cfg = Config(**cfg_kwargs)
    events = []
    provisioned = []

    async def fake_provision(name):
        adapter, keepalive = FakeAdapter(), FakeKeepalive()
        provisioned.append(adapter)
        return adapter, keepalive, f"target-{len(provisioned)}", (1280, 720)

    manager = DeviceManager(
        cfg, FakeRunner(), on_event=events.append, provision=fake_provision
    )
    return manager, events, provisioned


UA = "Mozilla/5.0 (Linux; Android 7.1.2; Redmi 4) AppleWebKit/537.36"


def test_new_connection_provisions_device():
    async def scenario():
        manager, events, provisioned = make_manager()
        device = await manager.acquire(None, UA)
        assert device.name == "Redmi 4"
        assert device.size == (1280, 720)
        assert len(provisioned) == 1
        assert device.id in manager.devices
        assert any("connected" in e for e in events)

    asyncio.run(scenario())


def test_two_devices_coexist():
    async def scenario():
        manager, _, provisioned = make_manager()
        a = await manager.acquire(None, UA)
        b = await manager.acquire(None, "Mozilla/5.0 (iPhone; CPU iPhone OS)")
        assert a.id != b.id
        assert len(manager.devices) == 2
        assert len(provisioned) == 2
        assert b.name == "iPhone"

    asyncio.run(scenario())


def test_reattach_reuses_lingering_device():
    async def scenario():
        manager, events, provisioned = make_manager(linger=60)
        device = await manager.acquire(None, UA)
        manager.release(device)
        assert device.linger_handle is not None
        again = await manager.acquire(device.id, UA)
        assert again is device
        assert again.linger_handle is None  # linger cancelled
        assert len(provisioned) == 1  # no second monitor
        assert any("reconnected" in e for e in events)

    asyncio.run(scenario())


def test_unknown_device_id_provisions_fresh():
    async def scenario():
        manager, _, provisioned = make_manager()
        device = await manager.acquire("feedbeef", UA)
        assert device.id != "feedbeef"
        assert len(provisioned) == 1

    asyncio.run(scenario())


def test_linger_expiry_removes_device():
    async def scenario():
        manager, events, provisioned = make_manager(linger=0.05)
        device = await manager.acquire(None, UA)
        manager.release(device)
        await asyncio.sleep(0.15)
        assert device.id not in manager.devices
        assert provisioned[0].stopped
        assert any("removed" in e for e in events)

    asyncio.run(scenario())


def test_zero_linger_removes_immediately():
    async def scenario():
        manager, _, provisioned = make_manager(linger=0)
        device = await manager.acquire(None, UA)
        manager.release(device)
        await asyncio.sleep(0.05)  # let the removal task run
        assert device.id not in manager.devices
        assert provisioned[0].stopped

    asyncio.run(scenario())


def test_device_limit_enforced():
    async def scenario():
        manager, _, _ = make_manager(max_devices=1)
        await manager.acquire(None, UA)
        with pytest.raises(DeviceError):
            await manager.acquire(None, UA)

    asyncio.run(scenario())


def test_shutdown_removes_everything():
    async def scenario():
        manager, _, provisioned = make_manager()
        await manager.acquire(None, UA)
        await manager.acquire(None, UA)
        await manager.shutdown()
        assert not manager.devices
        assert all(a.stopped for a in provisioned)

    asyncio.run(scenario())


def test_device_names_from_ua():
    assert device_name_from_ua(UA) == "Redmi 4"
    assert device_name_from_ua("(Linux; Android 14; Pixel 8 Build/AD1A)") == "Pixel 8"
    assert device_name_from_ua("Mozilla/5.0 (iPad; CPU OS 16_0)") == "iPad"
    assert device_name_from_ua("Mozilla/5.0 (X11; Linux x86_64)") == "Device"
