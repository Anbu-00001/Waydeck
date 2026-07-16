"""Bridge between the GLib main loop (D-Bus signals, GStreamer) and asyncio.

All Mutter D-Bus traffic and GStreamer pipeline control runs on one dedicated
GLib thread; asyncio code talks to it through `call` (awaitable) and
`fire` (fire-and-forget). GLib-side code reaches back into asyncio with
`loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402


class GLibRunner:
    def __init__(self) -> None:
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._run, name="glib-loop", daemon=True)
        self._started = threading.Event()

    def _run(self) -> None:
        # Make this thread the home for Gio async callbacks and GStreamer
        # bus watches created from it.
        self._loop.get_context().push_thread_default()
        self._started.set()
        self._loop.run()

    def start(self) -> None:
        self._thread.start()
        self._started.wait(timeout=5)

    def stop(self) -> None:
        GLib.idle_add(self._loop.quit)
        self._thread.join(timeout=5)

    def fire(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule fn(*args) on the GLib thread; ignore the result."""

        def _once() -> bool:
            fn(*args)
            return False  # GLib.SOURCE_REMOVE

        GLib.idle_add(_once)

    def call(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Schedule fn(*args) on the GLib thread; returns a concurrent Future."""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _once() -> bool:
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # propagate to caller
                fut.set_exception(e)
            return False

        GLib.idle_add(_once)
        return fut

    async def acall(self, fn: Callable[..., Any], *args: Any, timeout: float = 30) -> Any:
        """Await fn(*args) executed on the GLib thread."""
        fut = self.call(fn, *args)
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
