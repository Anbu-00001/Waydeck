"""Translate client input messages into injector calls.

Two strategies:

* touch (default) — forward raw touches as real touchscreen events. The
  compositor then provides native semantics itself: kinetic scrolling,
  long-press context menus, multi-finger gestures.
* pointer — emulate a mouse for compositors/apps that mishandle touch:
  tap = left click, long-press without movement = right click, drag = press-
  move-release, two-finger vertical drag = wheel scroll.

This module is deliberately free of gi imports: the injector and the timer
scheduler are passed in, so the gesture logic is unit-testable.

Client messages (normalized coordinates 0..1):
  {"t": "touch", "ph": "d"|"m"|"u", "slot": 0, "x": 0.42, "y": 0.77}
  {"t": "key", "sym": 65293, "down": true}
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

BTN_LEFT = 0x110
BTN_RIGHT = 0x111

LONG_PRESS_SECONDS = 0.55
TAP_SLOP = 0.012  # normalized distance a "tap" may wander (~12px on 1080p)
SCROLL_GAIN = 900.0  # normalized dy -> wheel delta


@dataclass
class _Finger:
    x: float
    y: float
    start_x: float
    start_y: float
    moved: bool = False


class InputRouter:
    """`fire` schedules an injector call on the GLib thread (fire-and-forget).
    `call_later(delay, fn) -> cancel_handle` and `cancel(handle)` abstract the
    event loop so tests can drive time manually."""

    def __init__(
        self,
        injector,
        fire: Callable[..., None],
        mode: str,
        size: tuple[int, int],
        call_later: Callable[[float, Callable[[], None]], object],
        cancel: Callable[[object], None],
    ) -> None:
        self._inj = injector
        self._fire = fire
        self._mode = mode
        self._w, self._h = size
        self._call_later = call_later
        self._cancel = cancel
        self._fingers: dict[int, _Finger] = {}
        self._long_press_timer: object | None = None
        self._pointer_pressed = False
        self._long_press_fired = False
        self._scrolling = False

    def set_size(self, size: tuple[int, int]) -> None:
        self._w, self._h = size

    # -- entry point -------------------------------------------------------

    def handle(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "touch":
            self._handle_touch(msg)
        elif kind == "key":
            sym, down = int(msg["sym"]), bool(msg["down"])
            self._fire(self._inj.keysym, sym, down)
        else:
            log.debug("unknown input message type: %r", kind)

    def release_all(self) -> None:
        """Client vanished: release everything we might be holding down."""
        for slot in list(self._fingers):
            if self._mode == "touch":
                self._fire(self._inj.touch_up, slot)
        self._fingers.clear()
        self._cancel_long_press()
        if self._pointer_pressed:
            self._fire(self._inj.pointer_button, BTN_LEFT, False)
            self._pointer_pressed = False
        self._scrolling = False

    # -- shared ------------------------------------------------------------

    def _px(self, msg: dict) -> tuple[float, float]:
        x = min(max(float(msg["x"]), 0.0), 1.0)
        y = min(max(float(msg["y"]), 0.0), 1.0)
        return x * self._w, y * self._h

    def _handle_touch(self, msg: dict) -> None:
        slot = int(msg.get("slot", 0))
        phase = msg.get("ph")
        if self._mode == "touch":
            self._touch_native(slot, phase, msg)
        else:
            self._touch_pointer(slot, phase, msg)

    # -- native touch strategy ----------------------------------------------

    def _touch_native(self, slot: int, phase: str, msg: dict) -> None:
        if phase == "d":
            px, py = self._px(msg)
            self._fingers[slot] = _Finger(px, py, px, py)
            self._fire(self._inj.touch_down, slot, px, py)
        elif phase == "m" and slot in self._fingers:
            px, py = self._px(msg)
            f = self._fingers[slot]
            f.x, f.y = px, py
            self._fire(self._inj.touch_motion, slot, px, py)
        elif phase == "u" and slot in self._fingers:
            del self._fingers[slot]
            self._fire(self._inj.touch_up, slot)

    # -- pointer emulation strategy ------------------------------------------

    def _touch_pointer(self, slot: int, phase: str, msg: dict) -> None:
        if phase == "d":
            px, py = self._px(msg)
            self._fingers[slot] = _Finger(px, py, px, py)
            if len(self._fingers) == 1:
                self._long_press_fired = False
                self._fire(self._inj.pointer_motion, px, py)
                self._long_press_timer = self._call_later(
                    LONG_PRESS_SECONDS, self._long_press
                )
            elif len(self._fingers) == 2:
                # Second finger: this is a scroll, not a click.
                self._cancel_long_press()
                if self._pointer_pressed:
                    self._fire(self._inj.pointer_button, BTN_LEFT, False)
                    self._pointer_pressed = False
                self._scrolling = True
            return

        if phase == "m" and slot in self._fingers:
            px, py = self._px(msg)
            f = self._fingers[slot]
            prev_y = f.y
            f.x, f.y = px, py
            if self._norm_dist(f) > TAP_SLOP:
                f.moved = True
            if self._scrolling:
                dy = (py - prev_y) / self._h * SCROLL_GAIN
                if dy:
                    self._fire(self._inj.pointer_axis, 0.0, -dy, False)
                return
            if f.moved and not self._long_press_fired:
                self._cancel_long_press()
                if not self._pointer_pressed:
                    # Drag: press at the original point, then follow.
                    self._fire(self._inj.pointer_motion, f.start_x, f.start_y)
                    self._fire(self._inj.pointer_button, BTN_LEFT, True)
                    self._pointer_pressed = True
                self._fire(self._inj.pointer_motion, px, py)
            return

        if phase == "u" and slot in self._fingers:
            f = self._fingers.pop(slot)
            if self._scrolling:
                if not self._fingers:
                    self._fire(self._inj.pointer_axis, 0.0, 0.0, True)
                    self._scrolling = False
                return
            self._cancel_long_press()
            if self._long_press_fired:
                return
            if self._pointer_pressed:
                self._fire(self._inj.pointer_motion, f.x, f.y)
                self._fire(self._inj.pointer_button, BTN_LEFT, False)
                self._pointer_pressed = False
            elif not f.moved:
                # Quick tap: click where the finger landed.
                self._fire(self._inj.pointer_motion, f.x, f.y)
                self._fire(self._inj.pointer_button, BTN_LEFT, True)
                self._fire(self._inj.pointer_button, BTN_LEFT, False)

    def _long_press(self) -> None:
        self._long_press_timer = None
        finger = next(iter(self._fingers.values()), None)
        if finger is None or finger.moved or len(self._fingers) != 1:
            return
        self._long_press_fired = True
        self._fire(self._inj.pointer_motion, finger.x, finger.y)
        self._fire(self._inj.pointer_button, BTN_RIGHT, True)
        self._fire(self._inj.pointer_button, BTN_RIGHT, False)

    def _cancel_long_press(self) -> None:
        if self._long_press_timer is not None:
            self._cancel(self._long_press_timer)
            self._long_press_timer = None

    def _norm_dist(self, f: _Finger) -> float:
        """Distance travelled since touch-down, as a fraction of the larger
        monitor dimension (comparable against TAP_SLOP)."""
        dx, dy = f.x - f.start_x, f.y - f.start_y
        return (dx * dx + dy * dy) ** 0.5 / max(self._w, self._h)
