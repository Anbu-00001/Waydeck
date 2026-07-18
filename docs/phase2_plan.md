# waydeck Phase 2 — Plan & Research

Written by Sonnet for Fable to implement. Phase 0 (GNOME MVP) and Phase 1
(H.264/WebCodecs, latency-verified) are done — see
[phase2_backlog memory / prior session] and the README's "Honest
Limitations" section for exact current state. This document plans the next
increment: KDE + wlroots adapters, a friendlier non-technical interface,
offline hotspot mode, and the window-placement fix. Every claim below that
came from live web research is cited; treat anything uncited as this
session's own design reasoning, not verified fact — re-check before
building on it if it matters.

**Ground rule carried over from Phase 0/1 and restated here because Phase 2
touches more surface area where it's tempting to cut corners**: no hardcoded
assumptions about versions, extension availability, chipset capability, or
compositor command syntax. Every new capability must version-gate or
capability-probe at runtime and fail with a clear, specific message when
unsupported — exactly like the existing `screencast_api_version` gate in
`adapters/gnome.py`. See §7 for a concrete checklist.

---

## 0. Direct answers to the three questions asked

**Q1 — Can a device connect through a pure hotspot with no internet at all?**
Yes, cleanly, using NetworkManager's own AP-mode support (`ipv4.method
shared` + its bundled dnsmasq) — this is a first-party, documented feature,
not a hack; GNOME's own Settings hotspot toggle uses the identical code
path. No upstream/WAN connection is needed or used. Two real caveats:
Intel's common `iwlwifi` chipsets (AX200/AX201/AX210 — very common on Ubuntu
laptops) only support AP mode on 2.4 GHz, and if the laptop's WiFi adapter
is already connected to a network as a client, starting a hotspot will
likely disconnect it unless the specific driver supports concurrent AP+STA
(not guaranteed). Full design in §5.

**Q2 — Can the "add a device / arrange it" interface be made genuinely
friendly?** Yes — and there's a strong, directly-reusable precedent already
inside GNOME itself: **gnome-network-displays** solves an architecturally
identical problem (cast a display into a Quick Settings picker, GNOME-native,
zero jargon) and GNOME's own Displays panel already has the exact
drag-rectangle arrangement widget this needs. Full design in §6.

**Q3 — Is there open source we should reuse?** One genuinely load-bearing
find: the GNOME Shell extensions **Window Calls** and **Window Commander**
expose a D-Bus API (`MoveResize`, and Window Commander's `MoveToMonitor`)
that does exactly what the "new windows don't follow the phone monitor"
backlog item needs — but neither should be a *runtime dependency* (see §4
for why waydeck should ship its own small extension instead, informed by
their API shape rather than depending on them directly). Everything else
found was either not a fit (workspace- vs monitor-indexed) or confirmed the
existing plan was already correct (no unified cross-compositor virtual
output protocol exists, validating the per-compositor `CompositorAdapter`
design).

---

## 1. Scope of Phase 2

From the README's "Honest Limitations" plus the backlog captured after
real-phone testing:

1. KDE Plasma compositor adapter
2. wlroots (Sway/Hyprland) compositor adapter
3. Fix: new windows opened from the phone's monitor don't stay there
4. Fix: screen lock kills the session (GNOME auto-stops RemoteDesktop
   sessions on lock — deferred from Phase 1 testing)
5. Offline hotspot connection mode (no WiFi router required)
6. A genuinely non-technical "add a device / arrange it" interface
7. (Enables, doesn't require) multi-device support — see §3

Not in scope for Phase 2 (still Phase 3+ per the original blueprint):
packaging (.deb/AUR), public launch playbook, stylus pressure/tilt, audio.

---

## 2. Architecture decision: app window, not a background daemon — and why

The UX research surfaced two families of prior art:

- **Run-when-you-want-it apps** (Duet Display, Spacedesk, Luna Display):
  you open an app, it does its thing, you close it. This matches waydeck's
  current model almost exactly — the gap is only that the "app" is
  currently a raw terminal.
- **Always-on background indicators** (Blueman, gnome-network-displays,
  GNOME's own Bluetooth Quick Settings): a tray/Quick-Settings icon is
  always present; a picker or settings window opens on demand.

The second pattern is unambiguously friendlier, but it requires solving
autostart-on-login, a systemd user service, and "is the D-Bus session
listener always running and is that an acceptable idle resource/security
cost" — real questions this document hasn't researched and Fable
shouldn't answer by default. **Recommendation: build the friendlier
run-when-you-want-it version first** — replace the raw terminal with a
small native GTK4/libadwaita window (waydeck already depends on PyGObject,
so `gi.repository.Gtk`/`Adw` add no new heavy dependency, and GTK4+
libadwaita ship standard on GNOME desktops). This alone removes essentially
all of the jargon problem the UX research flagged (see quoted `cli.py`
strings in the research), without committing to background-daemon
architecture. **Defer the persistent Quick-Settings-indicator version to
Phase 3+**, explicitly, as a follow-on once the run-on-demand version is
proven. If the user wants to accelerate straight to the daemon model,
that's a one-message redirect — don't guess silently either way, the
window this document was written in already had this exact conversation
once (window placement) and asking cheaply beat guessing.

This also resolves the "add devices" (plural) question without needing a
background service: **within a single run**, the server should support more
than one concurrently-connected phone, each getting its own virtual
monitor. See §3.

---

## 3. Multi-device support (enables the "add devices" UI)

Today `WaydeckServer` holds one `_current_ws` and a new connection replaces
it (`server/app.py`). For an "add a device" interface to mean anything, a
second phone joining should get its *own* virtual monitor, not steal the
first one's.

Concrete shape: introduce a `Device` object bundling what's currently
global per-process state — `GnomeAdapter` instance, `KeepalivePipeline`,
negotiated size, token, and the one `WebSocketResponse` — and a
`DeviceManager` that creates one per pairing instead of the CLI creating a
single adapter/pipeline pair directly. Each device needs its own:

- D-Bus RemoteDesktop+ScreenCast session (Mutter supports multiple
  concurrent sessions — nothing in the Phase 0 research suggested a
  single-session limit, but this needs a quick live check before relying
  on it, since it was never exercised with two sessions at once)
- Port *or* a shared port with per-device WebSocket sub-paths
  (`/ws/<device-id>`) — prefer the latter, one port is simpler for hotspot
  and firewall reasoning
- Own random token, own QR code

This is the single largest code change in Phase 2. It touches `cli.py`,
`server/app.py`, and introduces the new GTK window as the thing that
creates/destroys `Device` instances instead of the CLI doing it once at
startup. Do this **before** the KDE/wlroots adapters — those slot into the
existing `CompositorAdapter` interface unchanged, but the GUI/multi-device
work changes the shape everything else plugs into.

---

## 4. Window-placement fix (new windows should follow the phone's monitor)

**Confirmed root cause** (from Phase 1 real-device testing, and reconfirmed
architecturally by this session's research): Wayland gives no external tool
window-placement control; waydeck only injects input. GNOME's own official
"Auto Move Windows" extension is workspace-indexed, not monitor-indexed, so
it can't solve this. `smart-auto-move` is monitor-aware but only replays a
placement a user already demonstrated once — no help for a window's first
launch, which is the exact case reported.

**What actually fits**: the **Window Calls** / **Window Commander** GNOME
Shell extensions expose a D-Bus surface (`org.gnome.Shell.Extensions.*`,
methods like `List` — returns each window's `monitor` index — and
`MoveResize`/`Place`/`MoveToMonitor`) that is precisely the missing
primitive. Neither emits a "window created" *signal* though — both need
polling.

**Recommendation: don't take a runtime dependency on either.** They're
small, single-maintainer extensions (Window Commander: 12 stars) — fine
as design references, risky as an unreviewed, auto-installed dependency
users would need to trust and keep compatible with their exact GNOME Shell
version. Instead, **waydeck ships its own minimal GNOME Shell extension**,
MIT-licensed alongside the rest of the repo, whose entire job is:

```js
// sketch — Fable should write this properly, this is the shape not the code
global.display.connect('window-created', (display, win) => {
  if (winIsOnMonitor(win, TARGET_MONITOR_INDEX)) {
    // already there, nothing to do
  } else if (parentAppWasOnTargetMonitor(win)) {
    moveWindowToMonitor(win, TARGET_MONITOR_INDEX);
  }
});
// + a small D-Bus interface so the Python daemon can set TARGET_MONITOR_INDEX
// per active device without the user editing GSettings by hand.
```

This is a ~50-80 line GJS extension, not the "bigger scope" custom helper
originally worried about in Phase 1 — the research narrowed it from "build
a window manager" to "emit one signal, call one move, expose one D-Bus
setter." Package it in the repo (e.g. `extensions/waydeck-placement/`), and
have the GUI/CLI detect at startup whether it's installed+enabled (query its
D-Bus name) and offer a one-click "install this extension" path (GNOME
Shell extensions can self-install from a local `.zip` via
`gnome-extensions install`) rather than silently failing or requiring a
manual extensions.gnome.org trip. **Must degrade gracefully** when the
extension isn't installed: windows just behave as they do today (open on
Primary Display), not an error.

Also test whether setting the virtual monitor as **Primary Display**
(the quick test proposed at the end of Phase 1, never actually confirmed)
changes the default placement enough to reduce how often the extension
needs to intervene — cheap to check, do it first.

---

## 5. Offline hotspot mode

**Feasibility: confirmed, first-party NetworkManager behavior**, not a
workaround (see §0/Q1 and the full citation list at the end of this
document). Design, mirroring the existing USB mode's shape in
`waydeck/usb/adb.py`:

- New module `waydeck/hotspot/nm.py`. **Use `gi.repository.NM` (libnm via
  PyGObject)**, not hand-rolled `nmcli` subprocess calls — libnm is
  NetworkManager's own recommended Python path, PyGObject is already a
  waydeck dependency, and it avoids parsing `nmcli` text output. (Two
  pure-Python NetworkManager wrapper libraries were checked and rejected:
  `python-networkmanager` is archived, `sdbus-networkmanager`'s last PyPI
  release predates its GitHub activity — don't depend on either.)
- Random SSID and password per run, generated the same way `Config.token`
  already is — **do not hardcode a fixed SSID/password**.
- **Do not hardcode 2.4 GHz.** Probe the adapter's supported AP bands/modes
  at runtime (equivalent of `iw list`'s "Supported interface modes" /
  "valid interface combinations") and only fall back to forcing 2.4 GHz if
  5 GHz AP genuinely isn't supported — expose the result in `--verbose`
  output so a user with a 5 GHz-capable card isn't silently downgraded.
- Before starting the hotspot, detect whether the adapter already has an
  active client connection. If so, **do not silently disconnect the
  user's WiFi** — warn explicitly in the terminal/GUI ("This will
  disconnect you from Wi-Fi 'X' — continue?") since concurrent AP+STA is
  driver-dependent and not something to assume works.
- Teardown symmetric with `UsbDock`: bring the connection down and
  **delete** the temporary NM connection profile on exit (don't leave a
  stale hotspot profile behind), and if a prior WiFi connection was
  disconnected to make room, reconnect it.
- No client-side mitigation is available for Android's "no internet, stay
  connected?" prompt — that fix (`WifiNetworkSpecifier`) requires an
  installed native Android app, which contradicts waydeck's zero-install
  browser-only design. **State this honestly in the README** rather than
  promising a smooth connect; it's one harmless tap, not a blocker.
- CLI/GUI surface: `--hotspot on|off|auto` mirroring the existing `--usb`
  flag shape, auto-preferring USB > hotspot > LAN in that order when
  multiple are available (USB is still lowest-latency and needs no
  wireless negotiation at all).

---

## 6. Non-technical interface design

Full survey in the research appendix; the actionable shape:

**Pairing ("add a device")**: one screen, one QR code, nothing else visible
until a device connects — mirrors gnome-network-displays' Quick Settings
picker and Chromecast's setup screen, both zero-jargon precedents. On
connect, the QR is replaced by the device's name (from its browser's
`navigator.userAgent`, cleaned up — e.g. "Redmi 4" not the raw UA string)
and a live thumbnail, echoing Duet Display's "Connect → Launching" swap.

**Arrangement**: reuse GNOME's own Displays-panel drag-rectangle widget
almost verbatim (desktop rectangle + one rectangle per connected phone,
drag to position, snaps into place) — this is a well-trodden, already
GNOME-native pattern, not new design. For "always open my notes app
there," the research found no prior art to copy (Spacedesk only has
auto-launch-the-viewer options, not per-app placement) — this is
legitimately original UI for waydeck to design: a simple per-running-app
toggle ("Show \[app] on \[phone name]") is the proposed shape, backed by
§4's placement extension.

**Words to delete from every user-facing surface** (verified by directly
reading today's `cli.py` output — these exact phrases are what's printed
right now):

| Today prints | Replace with |
|---|---|
| `"compositor negotiated WxH"` | don't surface |
| `"virtual monitor WxH created"` | `"[device name] connected"` |
| `"torn down — virtual monitor removed"` | `"[device name] disconnected"` |
| `"session"`, `"D-Bus"`, `"PipeWire"` | never mentioned |
| `"pairing token"` | don't surface — implicit in the QR |
| raw IP address | device name only, unless troubleshooting |

The terminal/`--verbose` output should keep the precise technical language
— it's genuinely useful for the `claude-code-guide`-style debugging this
project has leaned on all along — but it should no longer be the *primary*
UI once the GTK window exists.

---

## 7. "No hardcoded dumb bits" checklist for Fable

Concrete, non-negotiable, extending the pattern already established in
`config.py` and `adapters/gnome.py`:

- [ ] KDE adapter version-gates on the actual KWin/`zkde_screencast_unstable_v1`
      protocol version being present, exactly like `screencast_api_version`
      does for Mutter — never assume a Plasma version number implies the
      protocol exists.
- [ ] wlroots adapter probes for `swaymsg`/`hyprctl` presence AND does a
      dry-run capability check before ever calling `create_output` — the
      Hyprland `hyprctl output create headless` crash regression found in
      research (open as of this writing) means a hardcoded call path could
      crash a user's entire compositor. Catch and report, don't assume
      success.
- [ ] Never hardcode a Wayland/D-Bus protocol version number as a literal
      in more than one place — centralize it like `MIN_SCREENCAST_VERSION`.
- [ ] Hotspot band/channel: probe, don't assume 2.4 GHz (see §5).
- [ ] Hotspot SSID/password: random per run, never a fixed string.
- [ ] Window-placement extension: detect presence via D-Bus at runtime;
      never assume it's installed.
- [ ] Any new GStreamer element choice (KDE/wlroots capture pipelines)
      goes through the same `_element_exists` capability check pattern
      already used in `stream/encoder.py` — don't assume an element is
      present because it was on the dev machine.
- [ ] Multi-device: don't hardcode a device limit as a magic number buried
      in code — if one's needed (resource ceiling), make it a named
      constant with a comment explaining *why* that number.

---

## 8. Milestones

| # | Goal | Acceptance test |
|---|---|---|
| M2.1 | Multi-device `DeviceManager` refactor (§3) | Two phones can connect in the same run, each gets its own virtual monitor, disconnecting one doesn't affect the other |
| M2.2 | GTK4/libadwaita companion window replaces raw terminal as primary UI | A person who has never used a terminal can pair a phone and arrange it using only the window |
| M2.3 | Window-placement extension (§4) | Opening a file from a Files window on the phone's monitor keeps the new window on that monitor |
| M2.4 | Screensaver/lock handling (Phase 1 backlog item) | Locking the screen pauses rather than kills the session; unlocking resumes without a full reconnect |
| M2.5 | Offline hotspot mode (§5) | A phone with WiFi but no router/internet can connect end-to-end via a waydeck-created hotspot |
| M2.6 | wlroots adapter (Sway first, Hyprland gated behind the regression check) | Virtual monitor + pointer-mode input works on a Sway session; Hyprland either works or fails with a clear, specific message |
| M2.7 | KDE adapter (KWin D-Bus/EIS path, not krfb-virtualmonitor) | Virtual monitor + touch input works on a current Plasma 6 Wayland session |

Suggested order: M2.1 and M2.2 first (they're prerequisites for everything
else being *usable* by the target non-technical audience), M2.3/M2.4 next
(both are already-known real bugs from Phase 1 testing), M2.5 (self-
contained, no dependency on the others), M2.6/M2.7 last (highest research
uncertainty — KDE's EIS-session-correlation question and the live Hyprland
regression are both unresolved-from-desk-research and need hands-on
verification before committing to the exact call sequence).

---

## 9. Open questions genuinely unresolved by desk research

Flagging rather than guessing, per the "think thrice" instruction:

1. **Does Mutter support multiple concurrent RemoteDesktop+ScreenCast
   session pairs from the same client process?** Never tested. M2.1
   depends on this; verify with a quick two-session spike before building
   the full `DeviceManager` refactor around the assumption.
2. **KDE**: does KWin correlate a `zkde_screencast_unstable_v1` virtual
   output session with an `org.kde.KWin.EIS.RemoteDesktop` input session
   automatically (like Mutter's explicit session-binding), or are they
   independent and need manual correlation?** Unconfirmed by any public
   source found.
3. **wlroots multi-touch**: no virtual-touch Wayland protocol was found
   anywhere in the wlr-protocols ecosystem. Confirm this gap is real (not
   just under-documented) before committing wlroots to pointer-mode-only
   input — if genuinely absent, this is worth an upstream wlr-protocols
   issue/proposal, not just a permanent waydeck limitation.
4. **Polkit permissions for hotspot creation** from a non-root session
   process are expected to work without a password prompt on upstream
   NetworkManager defaults, but this is **not confirmed on Ubuntu 24.04's
   actual packaged policy** — test locally (`nmcli general permissions`)
   before assuming M2.5 needs no privilege-escalation UX at all.

---

## Sources

KDE/wlroots research: krfb source (github.com/KDE/krfb), KDE bugs 458636 /
470996 / 479558, discuss.kde.org/t/more-info-on-krfb-virtualmonitor/33036,
plasma-wayland-protocols (github.com/KDE/plasma-wayland-protocols),
xdg-desktop-portal-kde remotedesktop.cpp, kwin!5496, github.com/isac322/kwin-mcp,
libkscreen doctor source, github.com/swaywm/sway issue #5553, Hyprland wiki
hyprctl docs, github.com/hyprwm/Hyprland discussion #14933 / issue #14899,
github.com/any1/wayvnc + manpage, wayland.app wlr-output-management-unstable-v1,
github.com/wheaney/breezy-desktop discussion #1.

Hotspot research: help.gnome.org/gnome-help/net-wireless-adhoc.html,
discourse.gnome.org/t/wifi-hotspot-without-internet-connection-sharing/25855,
networkmanager.dev nmcli-examples, wiki.archlinux.org Software_access_point
+ NetworkManager, Red Hat RHEL8 networking docs, community.intel.com AX200/
AX201 5GHz-hotspot threads, discourse.ubuntu.com/t/simultaneous-sta-ap-mode/19964,
github.com/lwfinger/rtw89 issue #402, developer.android.com WifiNetworkSpecifier
+ captive-portal docs, github.com NetworkManager .policy source,
networkmanager.dev/docs/libnm usage docs, github.com/NetworkManager/NetworkManager
examples/python/dbus/wifi-hotspot.py.

UX research: support.apple.com Sidecar/Universal Control, duetdisplay.com
getting-started, manual.spacedesk.net, astropad.com Luna Display,
github.com/debauchee/barrier, userbase.kde.org KDE Connect pairing,
moonlight-stream setup guide, support.microsoft.com multiple-monitors,
support.google.com Chromecast setup, gitlab.gnome.org/GNOME/gnome-network-displays,
help.gnome.org display-dual-monitors + bluetooth-connect-device,
github.com/blueman-project/blueman, developer.gnome.org/hig.

Window-placement + landscape scan: extensions.gnome.org (Auto Move Windows,
Window Calls, Window Calls Extended, Window Commander), gitlab.gnome.org/GNOME/
gnome-shell-extensions, github.com/khimaros/smart-auto-move,
github.com/ickyicky/window-calls, github.com/gnikolaos/window-commander,
phoronix.com GNOME-50-Remote-Desktop-HiDPI, gitlab.gnome.org/GNOME/
gnome-remote-desktop merge request 69, github.com/seveas/python-networkmanager
(archived), github.com/python-sdbus/python-sdbus-networkmanager.
