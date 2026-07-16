"""Gesture logic tests: a fake injector records calls, a fake scheduler lets
tests fire the long-press timer deterministically."""

from waydeck.input.router import BTN_LEFT, BTN_RIGHT, InputRouter


class FakeInjector:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))

        return record


class FakeScheduler:
    def __init__(self):
        self.pending = []

    def call_later(self, delay, fn):
        handle = [fn]
        self.pending.append(handle)
        return handle

    def cancel(self, handle):
        handle[0] = None

    def fire_all(self):
        for handle in self.pending:
            if handle[0]:
                fn, handle[0] = handle[0], None
                fn()


def make_router(mode="touch", size=(1000, 500)):
    inj = FakeInjector()
    sched = FakeScheduler()
    router = InputRouter(
        inj,
        fire=lambda fn, *a: fn(*a),
        mode=mode,
        size=size,
        call_later=sched.call_later,
        cancel=sched.cancel,
    )
    return router, inj, sched


def touch(router, phase, slot=0, x=0.5, y=0.5):
    router.handle({"t": "touch", "ph": phase, "slot": slot, "x": x, "y": y})


def test_touch_mode_forwards_scaled_coordinates():
    router, inj, _ = make_router("touch")
    touch(router, "d", 0, 0.5, 0.5)
    touch(router, "m", 0, 0.6, 0.5)
    touch(router, "u", 0)
    assert inj.calls == [
        ("touch_down", 0, 500.0, 250.0),
        ("touch_motion", 0, 600.0, 250.0),
        ("touch_up", 0),
    ]


def test_touch_mode_multitouch_slots():
    router, inj, _ = make_router("touch")
    touch(router, "d", 0, 0.2, 0.2)
    touch(router, "d", 1, 0.8, 0.8)
    touch(router, "u", 0)
    touch(router, "u", 1)
    assert ("touch_down", 1, 800.0, 400.0) in inj.calls
    assert inj.calls[-2:] == [("touch_up", 0), ("touch_up", 1)]


def test_touch_mode_coordinates_clamped():
    router, inj, _ = make_router("touch")
    touch(router, "d", 0, -0.5, 1.7)
    assert inj.calls == [("touch_down", 0, 0.0, 500.0)]


def test_keys_forwarded_in_any_mode():
    router, inj, _ = make_router("touch")
    router.handle({"t": "key", "sym": 0xFF0D, "down": True})
    router.handle({"t": "key", "sym": 0xFF0D, "down": False})
    assert inj.calls == [("keysym", 0xFF0D, True), ("keysym", 0xFF0D, False)]


def test_pointer_mode_tap_is_left_click():
    router, inj, _ = make_router("pointer")
    touch(router, "d", 0, 0.5, 0.5)
    touch(router, "u", 0, 0.5, 0.5)
    assert ("pointer_button", BTN_LEFT, True) in inj.calls
    assert ("pointer_button", BTN_LEFT, False) in inj.calls
    assert not any(c[0].startswith("touch") for c in inj.calls)


def test_pointer_mode_long_press_is_right_click():
    router, inj, sched = make_router("pointer")
    touch(router, "d", 0, 0.5, 0.5)
    sched.fire_all()  # long-press timer elapses without movement
    touch(router, "u", 0, 0.5, 0.5)
    assert ("pointer_button", BTN_RIGHT, True) in inj.calls
    assert ("pointer_button", BTN_RIGHT, False) in inj.calls
    # ... and the release must NOT also produce a left click
    assert ("pointer_button", BTN_LEFT, True) not in inj.calls


def test_pointer_mode_drag_presses_at_origin():
    router, inj, _ = make_router("pointer")
    touch(router, "d", 0, 0.2, 0.2)
    touch(router, "m", 0, 0.6, 0.6)  # beyond slop -> drag starts
    touch(router, "m", 0, 0.7, 0.7)
    touch(router, "u", 0, 0.7, 0.7)
    press_idx = inj.calls.index(("pointer_button", BTN_LEFT, True))
    origin_move = inj.calls[press_idx - 1]
    assert origin_move == ("pointer_motion", 200.0, 100.0)
    assert inj.calls[-1] == ("pointer_button", BTN_LEFT, False)


def test_pointer_mode_two_fingers_scroll():
    router, inj, _ = make_router("pointer")
    touch(router, "d", 0, 0.5, 0.5)
    touch(router, "d", 1, 0.5, 0.6)
    touch(router, "m", 0, 0.5, 0.4)  # finger 0 moves up
    touch(router, "u", 0)
    touch(router, "u", 1)
    axis_calls = [c for c in inj.calls if c[0] == "pointer_axis"]
    assert axis_calls, "expected scroll events"
    assert axis_calls[-1][3] is True  # finish flag on release
    assert ("pointer_button", BTN_LEFT, True) not in inj.calls


def test_release_all_lifts_held_state():
    router, inj, _ = make_router("touch")
    touch(router, "d", 0)
    touch(router, "d", 1)
    router.release_all()
    assert ("touch_up", 0) in inj.calls
    assert ("touch_up", 1) in inj.calls
