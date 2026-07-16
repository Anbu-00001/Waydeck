"""Compositor abstraction: one interface over the different ways Wayland
desktops create virtual monitors (Mutter RecordVirtual today; KDE
krfb-virtualmonitor and wlroots headless outputs are Phase 2 adapters)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class AdapterError(RuntimeError):
    """The compositor refused or the API surface is unusable."""


class UnsupportedCompositorError(AdapterError):
    """No adapter matches the running compositor."""


class InputInjector(ABC):
    """Injects input into the compositor. Coordinates are in virtual-monitor
    pixels (stream-relative — no global desktop math needed)."""

    @abstractmethod
    def touch_down(self, slot: int, x: float, y: float) -> None: ...

    @abstractmethod
    def touch_motion(self, slot: int, x: float, y: float) -> None: ...

    @abstractmethod
    def touch_up(self, slot: int) -> None: ...

    @abstractmethod
    def pointer_motion(self, x: float, y: float) -> None: ...

    @abstractmethod
    def pointer_button(self, button: int, pressed: bool) -> None: ...

    @abstractmethod
    def pointer_axis(self, dx: float, dy: float, finish: bool = False) -> None: ...

    @abstractmethod
    def keysym(self, keysym: int, pressed: bool) -> None: ...


class CompositorAdapter(ABC):
    """Owns the virtual monitor lifecycle. All methods run on the GLib thread."""

    name: str = "base"

    @abstractmethod
    def start(
        self,
        on_node_id: Callable[[int], None],
        on_closed: Callable[[str], None],
    ) -> None:
        """Create the virtual monitor session. `on_node_id` fires with the
        PipeWire node id once the compositor announces the stream (async by
        design — the monitor does not exist until negotiation completes).
        `on_closed` fires if the compositor ends the session externally."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the session. The virtual monitor lives exactly as long as
        the session, so this must always run on exit — no ghost monitors."""

    @property
    @abstractmethod
    def input(self) -> InputInjector: ...
