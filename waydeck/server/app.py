"""HTTP + WebSocket server: serves the browser client, negotiates transport,
relays video frames out and input events in.

M2.1: multi-device. Each connecting phone acquires a Device (its own virtual
monitor + compositor session) from the DeviceManager; reconnects presenting
a known device id reattach to the lingering monitor instead of creating a
new one. A connection presenting the id of an actively-connected device
displaces that connection (latest wins, same policy as Phase 1)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from .. import __version__
from ..config import Config
from ..device import Device, DeviceError, DeviceManager
from ..input.router import InputRouter
from ..stream import encoder as enc
from ..stream.capture import ClientPipeline
from . import protocol as proto

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HELLO_TIMEOUT = 10
# If the client can't drain this many H.264 frames, resync from a keyframe
# instead of letting latency build up.
H264_BACKLOG_LIMIT = 30


class FrameRelay:
    """Buffers frames between the GStreamer callback (foreign thread) and the
    WS sender task. JPEG: latest frame wins, stale ones are dropped. H.264:
    ordered queue — deltas depend on their predecessors — with a
    clear-and-resync when the client falls behind."""

    def __init__(self, h264: bool, request_keyframe) -> None:
        self._h264 = h264
        self._request_keyframe = request_keyframe
        self._frames: deque[tuple[bytes, bool, float | None]] = deque()
        self._waiting_key = False
        self._event = asyncio.Event()

    def feed(self, data: bytes, keyframe: bool, encode_ms: float | None) -> None:
        """Runs on the asyncio loop (via call_soon_threadsafe)."""
        if self._h264:
            if self._waiting_key:
                if not keyframe:
                    return
                self._waiting_key = False
            self._frames.append((data, keyframe, encode_ms))
            if len(self._frames) > H264_BACKLOG_LIMIT:
                self._frames.clear()
                self._waiting_key = True
                self._request_keyframe()
                return
        else:
            self._frames.clear()
            self._frames.append((data, keyframe, encode_ms))
        self._event.set()

    async def drain(self) -> list[tuple[bytes, bool, float | None]]:
        await self._event.wait()
        self._event.clear()
        out = list(self._frames)
        self._frames.clear()
        return out


class WaydeckServer:
    def __init__(self, cfg: Config, runner, manager: DeviceManager) -> None:
        self.cfg = cfg
        self.runner = runner
        self.manager = manager
        self.h264: enc.H264Encoder | None = None
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/client.js", self._static("client.js", "text/javascript"))
        self._app.router.add_get("/style.css", self._static("style.css", "text/css"))
        self._app.router.add_get("/ws", self._ws)
        self._web_runner: web.AppRunner | None = None

    def detect_h264(self) -> None:
        """Run on the GLib thread once GStreamer is initialized."""
        if self.cfg.transport != proto.TRANSPORT_JPEG:
            self.h264 = enc.pick_h264(
                self.cfg.encoder, self.cfg.h264_bitrate, self.cfg.keyframe_interval
            )

    async def start(self, binds: list[str]) -> None:
        self._web_runner = web.AppRunner(self._app, access_log=None)
        await self._web_runner.setup()
        for addr in binds:
            site = web.TCPSite(self._web_runner, addr, self.cfg.port)
            await site.start()

    async def stop(self) -> None:
        await self.manager.shutdown()
        if self._web_runner:
            await self._web_runner.cleanup()

    # -- HTTP ---------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        return proto.token_ok(self.cfg.token, request.query.get("t"))

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(
                status=403,
                content_type="text/html",
                text="<h1>403</h1><p>Missing or invalid session token — "
                "scan the QR code shown in the waydeck terminal.</p>",
            )
        return web.FileResponse(STATIC_DIR / "index.html")

    def _static(self, name: str, ctype: str):
        path = STATIC_DIR / name

        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.FileResponse(path, headers={"Content-Type": ctype})

        return handler

    # -- WebSocket session ----------------------------------------------------

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        if not self._authorized(request):
            await ws.close(code=proto.CLOSE_BAD_TOKEN, message=b"bad token")
            return ws

        device: Device | None = None
        pipeline: ClientPipeline | None = None
        router: InputRouter | None = None
        sender: asyncio.Task | None = None
        peer = request.remote
        try:
            hello = await self._wait_hello(ws)
            if hello is None:
                return ws
            transport, err = proto.decide_transport(
                self.cfg.transport, hello, self.h264 is not None
            )
            if err:
                await ws.send_json({"t": "error", "msg": err})
                await ws.close(code=proto.CLOSE_UNSUPPORTED, message=b"unsupported")
                return ws

            try:
                device = await self.manager.acquire(hello.device_id, hello.user_agent)
            except DeviceError as e:
                await ws.send_json({"t": "error", "msg": str(e)})
                await ws.close(code=proto.CLOSE_UNSUPPORTED, message=b"device limit")
                device = None
                return ws

            if device.ws is not None and not device.ws.closed:
                log.info("displacing previous connection of device %s", device.id)
                await device.ws.close(code=proto.CLOSE_REPLACED, message=b"replaced")
            device.ws = ws

            w, h = device.size
            await ws.send_json(
                {
                    "t": "config",
                    "device": device.id,
                    "name": device.name,
                    "transport": transport,
                    "codec": self.h264.codec if transport == proto.TRANSPORT_H264 else None,
                    "width": w,
                    "height": h,
                    "inputMode": self.cfg.input_mode,
                    "version": __version__,
                }
            )
            log.info(
                "client %s -> device %s (%s, %s)", peer, device.id, transport, device.name
            )

            loop = asyncio.get_running_loop()
            relay = FrameRelay(
                transport == proto.TRANSPORT_H264,
                request_keyframe=lambda: (
                    self.runner.fire(pipeline.force_keyframe) if pipeline else None
                ),
            )

            def on_frame(data: bytes, key: bool, encode_ms: float | None) -> None:
                # GStreamer thread
                loop.call_soon_threadsafe(relay.feed, data, key, encode_ms)

            fragment = (
                self.h264.fragment
                if transport == proto.TRANSPORT_H264
                else enc.jpeg_fragment(self.cfg.jpeg_quality)
            )
            pipeline = await self.runner.acall(
                ClientPipeline,
                device.target,
                w,
                h,
                fragment,
                on_frame,
                lambda msg: loop.call_soon_threadsafe(self._pipeline_failed, ws, msg),
            )

            router = InputRouter(
                device.injector,
                self.runner.fire,
                self.cfg.input_mode,
                device.size,
                call_later=lambda d, fn: loop.call_later(d, fn),
                cancel=lambda handle: handle.cancel(),
            )
            sender = asyncio.create_task(self._send_frames(ws, relay))
            await self._receive(ws, router)
        finally:
            if sender:
                sender.cancel()
            if router:
                router.release_all()
            if pipeline:
                self.runner.fire(pipeline.stop)
            if device is not None and device.ws is ws:
                self.manager.release(device)
            log.info("client %s disconnected", peer)
        return ws

    async def _wait_hello(self, ws: web.WebSocketResponse) -> proto.ClientHello | None:
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(code=proto.CLOSE_UNSUPPORTED, message=b"hello timeout")
            return None
        if msg.type != WSMsgType.TEXT:
            return None
        data = json.loads(msg.data)
        if data.get("t") != "hello":
            return None
        return proto.ClientHello.from_msg(data)

    async def _send_frames(self, ws: web.WebSocketResponse, relay: FrameRelay) -> None:
        try:
            while not ws.closed:
                for data, key, encode_ms in await relay.drain():
                    await ws.send_bytes(
                        proto.pack_video_frame(data, key, time.time() * 1000.0, encode_ms)
                    )
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    async def _receive(self, ws: web.WebSocketResponse, router: InputRouter) -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = data.get("t")
            if kind == "ping":
                await ws.send_json(
                    {"t": "pong", "t0": data.get("t0"), "t1": time.time() * 1000.0}
                )
            else:
                router.handle(data)

    def _pipeline_failed(self, ws: web.WebSocketResponse, msg: str) -> None:
        log.error("client pipeline failed: %s", msg)
        if not ws.closed:
            asyncio.ensure_future(ws.close(code=1011, message=b"pipeline error"))
