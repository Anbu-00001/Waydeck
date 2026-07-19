"""waydeck's GTK4/libadwaita companion window — the non-technical frontend.

Architecture (verified for this stack: PyGObject 3.48 lacks gi.events, so
asyncio can't share GTK's loop natively): GTK owns the main thread and
iterates the default GLib main context; the aiohttp/asyncio server runs on
a worker thread via waydeck/serve.py with install_signals=False. Traffic
between the two:

  worker -> GTK : GLib.idle_add (thread-safe, lands on the main context)
  GTK -> worker : ServeInfo.request_shutdown (call_soon_threadsafe inside)
                  and asyncio.run_coroutine_threadsafe for manager calls

The GLibRunner is created with external=True: GTK's loop already iterates
the context the runner marshals D-Bus/GStreamer work onto.

User-facing language rule: device names and plain words only — no
"virtual monitor", "session", "compositor" anywhere in this window.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..config import Config  # noqa: E402
from ..serve import ServeError, ServeInfo, serve  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "dev.waydeck.Waydeck"
QR_MODULE_PX = 8  # rendered pixel size of one QR module before GTK scaling


def qr_texture(url: str) -> Gdk.Texture | None:
    """Render the pairing QR to a Gdk.Texture via cairo (no PIL needed).
    Returns None if the qrcode module is unavailable — callers fall back
    to showing the URL as text."""
    try:
        import qrcode
    except ImportError:
        return None
    import cairo

    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    n = len(matrix)
    size = n * QR_MODULE_PX
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.set_source_rgb(0, 0, 0)
    for y, row in enumerate(matrix):
        for x, module in enumerate(row):
            if module:
                ctx.rectangle(x * QR_MODULE_PX, y * QR_MODULE_PX, QR_MODULE_PX, QR_MODULE_PX)
    ctx.fill()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))


class WaydeckWindow(Adw.ApplicationWindow):
    def __init__(self, app: WaydeckApp) -> None:
        super().__init__(application=app, title="waydeck")
        self.app = app
        self.set_default_size(420, 640)

        self._toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_margin_top(12)
        self._content.set_margin_bottom(24)
        self._content.set_margin_start(24)
        self._content.set_margin_end(24)

        # -- pairing area (filled in show_ready) --
        self._qr_picture = Gtk.Picture()
        self._qr_picture.set_size_request(280, 280)
        self._qr_picture.set_halign(Gtk.Align.CENTER)

        self._headline = Gtk.Label(label="Starting…")
        self._headline.add_css_class("title-2")

        self._subline = Gtk.Label(label="")
        self._subline.add_css_class("dim-label")
        self._subline.set_wrap(True)
        self._subline.set_justify(Gtk.Justification.CENTER)

        self._url_label = Gtk.Label(label="")
        self._url_label.set_selectable(True)
        self._url_label.add_css_class("caption")
        self._url_label.add_css_class("dim-label")
        self._url_label.set_wrap(True)

        self._devices_group = Adw.PreferencesGroup(title="Connected phones")
        self._device_rows: list[Gtk.Widget] = []

        for widget in (
            self._qr_picture,
            self._headline,
            self._subline,
            self._url_label,
            self._devices_group,
        ):
            self._content.append(widget)

        clamp = Adw.Clamp(maximum_size=480)
        clamp.set_child(self._content)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

        self.connect("close-request", self._on_close_request)

    # -- called on the GTK thread via idle_add ------------------------------

    def show_ready(self, info: ServeInfo) -> None:
        qr_url = info.lan_url if (info.lan_url and not info.usb_active) else info.usb_url
        texture = qr_texture(qr_url)
        if texture:
            self._qr_picture.set_paintable(texture)
        if info.usb_active:
            self._headline.set_label("Phone connected by USB")
            self._subline.set_label(
                "The screen should open on your phone by itself. "
                "If it doesn't, open the link below in the phone's browser."
            )
        else:
            self._headline.set_label("Scan with your phone's camera")
            self._subline.set_label(
                "Your phone becomes an extra screen for this computer — "
                "no app needed. You can also plug it in with a USB cable."
            )
        self._url_label.set_label(qr_url)
        self.refresh_devices()

    def show_error(self, msg: str, hint: str) -> None:
        self._headline.set_label("Something went wrong")
        self._subline.set_label(f"{msg}\n{hint}".strip())
        self._qr_picture.set_paintable(None)

    def toast(self, msg: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=msg, timeout=3))

    def refresh_devices(self) -> None:
        info = self.app.info
        for row in self._device_rows:
            self._devices_group.remove(row)
        self._device_rows.clear()
        devices = list(info.manager.devices.values()) if info else []
        if not devices:
            placeholder = Adw.ActionRow(
                title="No phones connected yet",
                subtitle="They appear here as soon as one joins",
            )
            placeholder.set_sensitive(False)
            self._devices_group.add(placeholder)
            self._device_rows.append(placeholder)
            return
        for device in devices:
            connected = device.ws is not None
            row = Adw.ActionRow(
                title=device.name,
                subtitle="Connected" if connected else "Waiting to reconnect…",
            )
            icon = Gtk.Image.new_from_icon_name(
                "phone-symbolic" if connected else "phone-disabled-symbolic"
            )
            row.add_prefix(icon)
            remove = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
            remove.add_css_class("destructive-action")
            remove.connect("clicked", self._on_remove_clicked, device)
            row.add_suffix(remove)
            self._devices_group.add(row)
            self._device_rows.append(row)

    # -- user actions -------------------------------------------------------

    def _on_remove_clicked(self, _button, device) -> None:
        info = self.app.info
        if info:
            asyncio.run_coroutine_threadsafe(
                info.manager.remove(device, "removed from the waydeck window"),
                info.loop,
            )

    def _on_close_request(self, _window) -> bool:
        self.app.begin_shutdown("window closed")
        # Keep the window until the server thread confirms teardown — the
        # app quits from _server_thread_finished.
        self.set_sensitive(False)
        return True


class WaydeckApp(Adw.Application):
    def __init__(self, cfg: Config) -> None:
        super().__init__(application_id=APP_ID)
        self.cfg = cfg
        self.info: ServeInfo | None = None
        self.win: WaydeckWindow | None = None
        self._thread: threading.Thread | None = None
        self._shutting_down = False
        self.exit_code = 0

    def do_activate(self) -> None:
        if self.win is not None:
            self.win.present()
            return
        self.win = WaydeckWindow(self)
        self.win.present()

        # Ctrl-C in the launching terminal should behave like closing the
        # window, not kill GTK mid-frame.
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGINT,
            lambda: (self.begin_shutdown("interrupted"), GLib.SOURCE_REMOVE)[1],
        )

        # Automated-testing hook: quit cleanly after N seconds so CI/agent
        # runs can exercise the full startup/teardown without a human.
        autoquit = os.environ.get("WAYDECK_GUI_AUTOQUIT")
        if autoquit:
            GLib.timeout_add_seconds(
                int(autoquit),
                lambda: (self.begin_shutdown("autoquit"), GLib.SOURCE_REMOVE)[1],
            )

        self._thread = threading.Thread(
            target=self._asyncio_main, name="waydeck-server", daemon=True
        )
        self._thread.start()

    # -- server thread ------------------------------------------------------

    def _asyncio_main(self) -> None:
        from ..glibloop import GLibRunner

        runner = GLibRunner(external=True)
        try:
            asyncio.run(
                serve(
                    self.cfg,
                    runner,
                    on_event=self._on_event_threadsafe,
                    on_ready=self._on_ready_threadsafe,
                    install_signals=False,
                )
            )
        except ServeError as e:
            self.exit_code = 1
            GLib.idle_add(self._show_error, str(e), e.hint)
            return
        except Exception:
            log.exception("server thread crashed")
            self.exit_code = 1
        GLib.idle_add(self._server_thread_finished)

    def _on_event_threadsafe(self, msg: str) -> None:
        GLib.idle_add(self._apply_event, msg)

    def _on_ready_threadsafe(self, info: ServeInfo) -> None:
        self.info = info
        GLib.idle_add(self._apply_ready)

    # -- GTK thread ---------------------------------------------------------

    def _apply_ready(self) -> bool:
        if self.win and self.info:
            self.win.show_ready(self.info)
        return GLib.SOURCE_REMOVE

    def _apply_event(self, msg: str) -> bool:
        if self.win:
            self.win.toast(msg)
            self.win.refresh_devices()
        return GLib.SOURCE_REMOVE

    def _show_error(self, msg: str, hint: str) -> bool:
        if self.win:
            self.win.show_error(msg, hint)
        return GLib.SOURCE_REMOVE

    def begin_shutdown(self, reason: str) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.info is not None:
            self.info.request_shutdown(reason)
        else:
            # Server never became ready; nothing user-visible to tear down.
            self.quit()

    def _server_thread_finished(self) -> bool:
        self.quit()
        return GLib.SOURCE_REMOVE


def run_gui(cfg: Config) -> int:
    app = WaydeckApp(cfg)
    # Empty argv: waydeck's own flags were already parsed; GTK must not
    # try to interpret them.
    app.run([])
    return app.exit_code
