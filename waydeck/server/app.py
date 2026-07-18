"""HTTP + WebSocket server: serves the browser client, negotiates transport,
relays video frames out and input events in. One phone at a time (v1);
a newer connection replaces the current one."""

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
    def __init__(self, cfg: Config, runner, target: str, injector) -> None:
        self.cfg = cfg
        self.runner = runner
        self.target = target  # pipewiresrc target-object (object.serial)
        self.injector = injector
        # Actual size can differ from requested if the compositor overrides.
        self.size: tuple[int, int] = (cfg.width, cfg.height)
        self.h264: enc.H264Encoder | None = None
        self.on_client_change = lambda desc: None  # CLI status line hook
        self._current_ws: web.WebSocketResponse | None = None
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/client.js", self._static("client.js", "text/javascript"))
        self._app.router.add_get("/style.css", self._static("style.css", "text/css"))
        self._app.router.add_get("/ws", self._ws)
        self._runner_site: web.TCPSite | None = None
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
        if self._current_ws is not None:
            await self._current_ws.close(code=1001, message=b"server shutting down")
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

        if self._current_ws is not None:
            log.info("new client connected; replacing the previous one")
            await self._current_ws.close(code=proto.CLOSE_REPLACED, message=b"replaced")
        self._current_ws = ws

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

            w, h = self.size
            await ws.send_json(
                {
                    "t": "config",
                    "transport": transport,
                    "codec": self.h264.codec if transport == proto.TRANSPORT_H264 else None,
                    "width": w,
                    "height": h,
                    "inputMode": self.cfg.input_mode,
                    "version": __version__,
                }
            )
            self.on_client_change(f"{peer} · {transport}")
            log.info("client %s connected (%s, %s)", peer, transport, hello.user_agent[:60])

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
                self.target,
                w,
                h,
                fragment,
                on_frame,
                lambda msg: loop.call_soon_threadsafe(self._pipeline_failed, ws, msg),
            )

            router = InputRouter(
                self.injector,
                self.runner.fire,
                self.cfg.input_mode,
                self.size,
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
            if self._current_ws is ws:
                self._current_ws = None
                self.on_client_change("")
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
