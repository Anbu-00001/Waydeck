# waydeck

Turn any old phone or tablet into a wireless or wired touch monitor for your Linux desktop. One command, zero client app installation, no hardware dongle.

Waydeck creates a true virtual display on your Linux desktop and streams it directly to any modern web browser over WiFi or USB. Windows can be dragged onto the phone screen, and finger taps, drags, and scrolling are injected back into the desktop as native touchscreen events.

## Current Implementation Status

Waydeck is currently functional on GNOME Wayland desktop environments. The codebase includes a full asynchronous daemon, GStreamer capture engine, D-Bus input injector, and a web client with built-in telemetry.

### Implemented Features

- True Virtual Display: Registers a genuine virtual output with GNOME Shell (Mutter) using private `RemoteDesktop` and `ScreenCast` D-Bus APIs. The output appears in system Display Settings alongside physical monitors and can be arranged, scaled, or rotated independently.
- Zero-Install Web Client: Works in standard mobile web browsers (Chrome, Firefox, Safari) without installing third-party apps on the client device.
- Dual Video Transports:
  - H.264 Streaming: Decodes hardware-accelerated H.264 streams in the browser via WebCodecs. Works over USB connections or secure contexts. Supports VA-API hardware encoding (`vah264enc`, `vaapih264enc`) with fallback to software `x264enc`.
  - JPEG Fallback: Automatic fallback to low-latency JPEG streaming over plain HTTP LAN connections where browser security policies restrict WebCodecs.
- Native Touch and Mouse Emulation:
  - Touch Mode (default): Direct injection of multi-touch events (`NotifyTouchDown`, `NotifyTouchMotion`, `NotifyTouchUp`) into Mutter. Desktop handles kinetic scrolling, long-press context menus, and multi-finger gestures natively.
  - Pointer Mode: Mouse emulation with client-side gesture parsing (tap to click, long-press for right-click, single-finger drag, two-finger vertical scroll).
- USB Dock Mode: Automated USB tethering using `adb reverse`. Automatically detects attached Android devices, forwards network ports, launches the default browser, charges the phone, and provides lowest-latency video without WiFi congestion.
- Real-Time Telemetry HUD: On-screen telemetry overlay in the browser client displaying real-time FPS, bitrate, RTT, and sub-millisecond latency breakdowns (capture+encode, network, decode+render). Stats are also exposed via `window.__wdStats` for automated testing.
- Session Security: Random per-session authentication tokens embedded in terminal QR codes and checked via constant-time token comparison.
- Clean Teardown: Intercepts SIGINT/SIGTERM to cleanly unbind D-Bus sessions, stop GStreamer pipelines, and remove virtual monitors without leaving phantom displays in GNOME.

## Feature Comparison

| Feature | waydeck | Weylus | Deskreen | VirtScreen | krfb-virtualmonitor |
|---|---|---|---|---|---|
| True Virtual Monitor | Yes | No (requires HDMI dummy) | No (requires dummy plug) | Yes (X11 only) | Yes |
| Touch Forwarding | Yes (Native Touch) | Yes | No (View only) | Yes (via VNC) | Yes |
| Wayland Support | Yes (GNOME) | Partial / Experimental | Partial | No | Yes (KDE) |
| Client Requirement | Web Browser | Web Browser | Web Browser | VNC App | VNC App |
| Zero Dongle Needed | Yes | No | No | Yes | Yes |

## Requirements

### Host Desktop
- Operating System: Linux running GNOME 44+ on Wayland (GNOME 46 tested baseline).
- Python: Version 3.10 or higher.
- System Dependencies (Debian / Ubuntu):

```bash
sudo apt install python3-gi gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

### Client Device
- Any modern web browser (Google Chrome, Mozilla Firefox, Apple Safari).
- For USB mode: Android device with USB debugging enabled, and `adb` installed on the host desktop.

## Installation

Install via `pipx` or `uvx`:

```bash
pipx install waydeck
```

Or run directly using `uvx`:

```bash
uvx waydeck
```

## Usage

Start `waydeck` from your terminal:

```bash
waydeck
```

Scanning the terminal QR code with your phone connects the browser and activates the display.

### Command Line Options

```bash
# Set custom resolution (e.g. portrait panel matching phone screen)
waydeck --size 1080x2400

# Force USB dock mode via adb
waydeck --usb on

# Force JPEG fallback transport
waydeck --transport jpeg

# Use mouse pointer emulation instead of native touch
waydeck --input pointer

# Specify custom port
waydeck --port 8420

# Display full usage help
waydeck --help
```

### Options Reference

- `--port PORT`: TCP port for web server (default: 8420).
- `--bind ADDR`: Custom bind IP address (default: auto-detected LAN address).
- `--size WxH`: Virtual display resolution (default: 1920x1080).
- `--quality 10-100`: Quality setting for JPEG fallback stream (default: 80).
- `--bitrate KBPS`: Target bitrate for H.264 stream in kbit/s (default: 8000).
- `--keyframe-interval FRAMES`: Maximum frames between H.264 IDR keyframes (default: 60).
- `--encoder {auto,vah264,vaapi,x264}`: Select H.264 encoder backend (default: auto).
- `--transport {auto,jpeg,h264}`: Video stream transport mode (default: auto).
- `--input {touch,pointer}`: Input injection strategy (default: touch).
- `--usb {auto,on,off}`: USB dock mode configuration (default: auto).
- `--no-open`: Disable auto-opening browser on phone in USB mode.
- `--token TOKEN`: Set custom session authentication token (default: random per run).
- `-v, --verbose`: Enable verbose debug logging.

## System Architecture

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

1. Session Negotiation: `waydeck` creates a `RemoteDesktop` session and binds a `ScreenCast` session via Mutter D-Bus APIs. `RecordVirtual` spawns a software virtual output.
2. Capture & Encode: PipeWire captures frames from the virtual output. GStreamer encodes video into H.264 (via VA-API hardware or x264 software) or JPEG.
3. Network Transport: `aiohttp` streams encoded frames over WebSocket and handles client handshake and ping/pong latency measurement.
4. Input Forwarding: Touch gestures or pointer interactions sent over WebSocket are converted into D-Bus calls on `org.gnome.Mutter.RemoteDesktop.Session`.

## Honest Limitations

- Compositor Support: Currently supports GNOME on Wayland only. KDE Plasma (`krfb-virtualmonitor`) and wlroots (Sway/Hyprland) backends are planned.
- Display Resizing: Resolution is fixed per session. Dynamic mid-session display resizes are disabled to prevent Mutter instability; changing resolution requires restarting with `--size`.
- WebCodecs Context: H.264 WebCodecs decoding requires a secure browser context (HTTPS or localhost). USB mode provides localhost automatically; plain-HTTP LAN connections fall back to JPEG.
- Single Client: Supports one active client connection at a time. Connecting a second device disconnects the existing session.
- Features Not Yet Implemented: Audio passthrough and active stylus (pressure/tilt) input are currently omitted.

## Roadmap

Phase 2 (KDE and wlroots adapters, a non-technical companion interface,
multi-device support, offline hotspot pairing, and the window-placement
fix) is planned in detail, with cited research on the risky parts, in
[docs/phase2_plan.md](docs/phase2_plan.md).

## Development and Testing

Clone the repository and run locally:

```bash
git clone https://github.com/anbuchelvan/waydeck
cd waydeck

# Run daemon directly from checkout
python3 -m waydeck --verbose

# Execute pure-Python unit test suite
PYTHONPATH=. pytest
```

## License

This project is licensed under the [MIT License](LICENSE).
