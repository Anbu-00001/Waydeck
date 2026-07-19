/* Waydeck Placement — GNOME Shell extension (ESM, Shell 45+).
 *
 * Two jobs:
 *
 * 1. Auto-placement: when a new normal window appears while the focused
 *    window sits on a *virtual* monitor (waydeck phone screens use Meta-N
 *    connectors), move the new window to that monitor. This fixes the
 *    Wayland gap where apps launched from the phone screen open on the
 *    primary display instead. Physical multi-monitor setups are untouched —
 *    the policy only ever targets virtual monitors.
 *
 * 2. A small D-Bus API (org.gnome.Shell.Extensions.WaydeckPlacement on the
 *    org.gnome.Shell bus name) so the waydeck daemon/GUI can move windows
 *    programmatically ("send this app to the phone") and enumerate them.
 *
 * Policy detail: focus is sampled at window-created time — before the new
 * window itself takes focus — so "the window you launched it from" is what
 * decides placement. The move runs in an idle callback so it lands after
 * mutter's own placement pass.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Mutter names virtual monitor connectors Meta-0, Meta-1, … — probe a
// bounded range rather than assuming a count.
const MAX_VIRTUAL_CONNECTORS = 16;

const DBUS_IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.WaydeckPlacement">
    <method name="Move">
      <arg type="u" direction="in" name="winId"/>
      <arg type="i" direction="in" name="monitor"/>
      <arg type="b" direction="out" name="moved"/>
    </method>
    <method name="List">
      <arg type="s" direction="out" name="windowsJson"/>
    </method>
    <method name="VirtualMonitors">
      <arg type="ai" direction="out" name="monitorIndices"/>
    </method>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

export default class WaydeckPlacementExtension extends Extension {
    enable() {
        this._windowCreatedId = global.display.connect(
            'window-created', (_display, win) => this._onWindowCreated(win));
        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, this);
        this._dbus.export(Gio.DBus.session,
            '/org/gnome/Shell/Extensions/WaydeckPlacement');
    }

    disable() {
        if (this._windowCreatedId) {
            global.display.disconnect(this._windowCreatedId);
            this._windowCreatedId = null;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    get Version() {
        return 1;
    }

    _virtualMonitorIndices() {
        const indices = [];
        const manager = global.backend.get_monitor_manager();
        if (typeof manager.get_monitor_for_connector !== 'function')
            return indices;
        for (let i = 0; i < MAX_VIRTUAL_CONNECTORS; i++) {
            const index = manager.get_monitor_for_connector(`Meta-${i}`);
            if (index >= 0)
                indices.push(index);
        }
        return indices;
    }

    _onWindowCreated(win) {
        try {
            if (win.get_window_type() !== Meta.WindowType.NORMAL)
                return;
            if (win.get_transient_for() !== null || win.skip_taskbar)
                return;
            const focus = global.display.get_focus_window();
            if (!focus || focus === win)
                return;
            const target = focus.get_monitor();
            if (target < 0 || !this._virtualMonitorIndices().includes(target))
                return;
            // Idle: run after mutter's own placement decision for the window.
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                try {
                    if (win.get_monitor() !== target)
                        win.move_to_monitor(target);
                } catch (_e) {
                    // window vanished before the idle ran — nothing to do
                }
                return GLib.SOURCE_REMOVE;
            });
        } catch (e) {
            console.warn(`waydeck-placement: ${e}`);
        }
    }

    // -- D-Bus methods ------------------------------------------------------

    Move(winId, monitor) {
        const actor = global.get_window_actors()
            .find(a => a.meta_window.get_id() === winId);
        if (!actor)
            return false;
        actor.meta_window.move_to_monitor(monitor);
        return actor.meta_window.get_monitor() === monitor;
    }

    List() {
        const windows = global.get_window_actors().map(a => {
            const w = a.meta_window;
            return {
                id: w.get_id(),
                title: w.get_title(),
                wmClass: w.get_wm_class(),
                monitor: w.get_monitor(),
                focus: w.has_focus(),
            };
        });
        return JSON.stringify(windows);
    }

    VirtualMonitors() {
        return this._virtualMonitorIndices();
    }
}
