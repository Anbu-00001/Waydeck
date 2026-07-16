# waydeck

<!-- demo.gif goes here the moment M0.6 is real: terminal → QR scan → window dragged onto the phone → finger taps a button -->

**Turn any old phone into a wireless touch monitor for your Linux desktop.
One command. No app. No dongle.**

Every drawer has a retired phone. `waydeck` gives it a second life as a *real*
extra display: run one command, scan a QR code, and a true virtual monitor
appears in your display settings — drag windows onto it, tap to click, scroll
with a flick. It works in any modern mobile browser, streams over WiFi or a
USB cable, and installs nothing on the phone.

```
pipx install waydeck    # or: uvx waydeck
waydeck
```

That's it. A QR code appears in your terminal, a monitor appears in
**Settings → Displays**, and your phone becomes a touchscreen for your desktop.

## Why waydeck

No maintained tool on Wayland Linux offers all three of: a **true virtual
monitor** (not mirroring), a **zero-install browser client**, and **touch
forwarded back as real input** — behind one command. Every ingredient exists
and is proven separately; the glue did not exist.

| | True virtual monitor | Touch input back | Wayland | Client |
|---|---|---|---|---|
| **waydeck** | ✅ | ✅ real touchscreen events | ✅ GNOME (KDE/wlroots planned) | any browser |
| Weylus | ❌ needs HDMI dummy plug | ✅ | ⚠️ experimental, partly broken | browser |
| Deskreen | ❌ needs dummy plug | ❌ view only | partial | browser |
| VirtScreen | ✅ X11 only | via VNC | ❌ | VNC app |
| krfb-virtualmonitor | ✅ | ✅ | KDE only | VNC app |
| Apollo + Moonlight | ✅ Windows only (Linux planned) | ✅ | n/a | Moonlight app |

*(Kept honest on a best-effort basis — corrections welcome.)*

## Features

- **One command** — `waydeck` detects your compositor and does the right thing.
- **True virtual monitor** — real desktop space, arranged in Settings like any
  monitor. Created via Mutter's own remote-desktop D-Bus API: no root, no
  `/dev/uinput`, no udev rules, no kernel modules.
- **Real touch** — taps, drags, kinetic scrolling and long-press context menus
  are injected as genuine touchscreen events (`--input pointer` for mouse
  emulation instead).
- **Adaptive video** — H.264 decoded in hardware via WebCodecs where the
  browser allows it (USB/localhost or TLS), automatic JPEG fallback on plain
  LAN. See the live transport in the stats HUD (▤ button).
- **USB dock mode** — plug the phone in and `waydeck` routes everything over
  `adb reverse`: no WiFi, lowest latency, the phone charges while docked, and
  the browser opens on the phone automatically.
- **On-screen keyboard passthrough** — the phone keyboard types into the desktop.
- **Secure by default** — random per-session token in the QR URL; USB mode
  never touches the network.

## Requirements

- **Host:** Linux with GNOME 44+ on Wayland (GNOME 46 is the tested baseline).
  KDE and wlroots (Sway/Hyprland) adapters are on the roadmap.
- **Phone:** any modern browser (Chrome/Firefox/Safari). For USB mode:
  USB debugging enabled and `adb` installed on the host.

Debian/Ubuntu system packages:

```bash
sudo apt install python3-gi gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly   # H.264 encoders
```

## Usage

```bash
waydeck                          # LAN mode, QR pairing
waydeck --size 1080x2400         # portrait panel matching your phone
waydeck --usb on                 # force USB dock mode
waydeck --transport jpeg         # skip H.264 entirely
waydeck --input pointer          # mouse emulation instead of touch
waydeck --help                   # every knob
```

Ctrl-C tears everything down cleanly — the monitor disappears, no ghosts.

## How it works

```
+------------------+      WebSocket (token-auth)      +---------------------------+
|  Phone browser   | <--- video (H.264/JPEG) -------- |  waydeck daemon           |
|  canvas + touch  | ---- input events (JSON) ------> |   aiohttp server          |
+------------------+                                  |   GStreamer encoder       |
        ^  QR pairing / adb reverse (USB)             |   compositor adapter      |
        |                                             |   input injector          |
        +-------------------------------------------- +------------+--------------+
                                                                   | D-Bus + PipeWire
                                                      +------------v--------------+
                                                      |  Mutter (GNOME Shell)     |
                                                      +---------------------------+
```

The GNOME adapter drives the same private Mutter D-Bus APIs that
gnome-remote-desktop uses: a `RemoteDesktop` session (which accepts
unprivileged input injection) bound to a `ScreenCast` session whose
`RecordVirtual` call creates a monitor "not backed by real hardware". The
monitor's content arrives as a PipeWire stream, GStreamer encodes it, and a
WebSocket carries frames down and touches back up.

Because this is a private API (the xdg portal has no virtual-monitor support
yet), waydeck version-gates at startup and documents the tested GNOME range.

## Honest limitations (v1)

- GNOME/Wayland only, for now. The compositor-abstraction layer exists so KDE
  (`krfb-virtualmonitor`) and wlroots (headless outputs) adapters can land
  next — issues and PRs very welcome.
- Fixed monitor resolution per run (mid-session virtual-monitor resizes have
  historically crashed mutter; a resolution change means restart).
- H.264 needs a secure context on the client (a browser platform rule for
  WebCodecs). USB mode gets it for free; plain-HTTP LAN falls back to JPEG.
  TLS-on-LAN with trust-on-first-scan is on the roadmap.
- No stylus pressure/tilt, no audio, no multi-client (see roadmap).

## Roadmap

- [ ] KDE Plasma adapter, wlroots (Sway/Hyprland) adapter + uinput input backend
- [ ] TLS on LAN (certificate fingerprint in the QR, trust-on-first-scan)
- [ ] Panel mode: phone-shaped monitor + persistent layout via stable monitor identity
- [ ] `.deb` / AUR packaging
- [ ] WebRTC transport, if latency numbers justify it

## Development

```bash
git clone https://github.com/anbuchelvan/waydeck && cd waydeck
python3 -m waydeck --verbose     # run from checkout (uses system python3-gi)
pytest                           # pure-Python core tests
ruff check waydeck tests
```

## License

[MIT](LICENSE)
