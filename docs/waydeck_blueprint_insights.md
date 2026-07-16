# waydeck — Insights Briefing (for narration)

## The idea, in one breath

waydeck turns an old phone sitting in a drawer into a real second monitor for a Linux desktop. You type one command, scan a QR code, and the phone becomes a touchscreen display you can drag windows onto — taps act as clicks. No app to install, no HDMI dongle, no per-desktop wizardry. That's the whole pitch, and it's deliberately simple because the problem underneath it is not.

## Why this is actually hard, and why nobody's solved it

Linux is mid-migration from X11 to Wayland, and Wayland doesn't have one API for creating a virtual monitor — it has three, one per desktop environment. GNOME uses Mutter's private RecordVirtual D-Bus interface. KDE has its own tool, krfb-virtualmonitor. wlroots-based desktops like Sway and Hyprland do it a third way, through headless outputs. Every existing tool picked one lane and stopped. Weylus, the most popular option, still treats Wayland as experimental — its own maintainers point users toward hand-edited Xorg configs or ask them to go implement the virtual-monitor path themselves. VirtScreen is flatly X11-only and effectively dead for anyone who's upgraded. Even Apollo, the well-funded gaming-streaming fork with a slick built-in virtual display, only ships that feature on Windows — Linux support is still "planned."

So the gap is precise: nobody offers a true virtual monitor, a zero-install browser client, and real touch input, working across GNOME, KDE, and wlroots, behind a single command. Every ingredient exists somewhere. Nobody has glued them together. That's waydeck's whole reason to exist.

## Why now, specifically

Three forces are converging. Distros are actively retiring X11, which is breaking the old tools in real time and sending users into forum threads asking for replacements. GNOME itself is investing in the exact APIs waydeck needs — GNOME 50 is adding virtual-monitor mode lists and HiDPI scaling support, so the ground is getting more solid, not less. And the one serious competitor with money behind it, Apollo, has explicitly left the Linux productivity niche unclaimed while it focuses on Windows gaming. The window is open right now.

## How it actually works, technically

The clever part is that GNOME's compositor, Mutter, already has a private D-Bus API that its own remote-desktop feature uses internally — and waydeck drives that same API directly. You create a remote-desktop session, bind a screen-cast session to it, and call RecordVirtual, which carves out "a region of the stage that isn't backed by real monitor hardware." Here's the subtlety: that monitor doesn't exist synchronously. PipeWire has to negotiate resolution and refresh rate first, and only then does the monitor materialize — so the code has to wait on a signal rather than assuming the call just works. Input is the pleasant surprise: because this is the same session gnome-remote-desktop uses, sending a tap to the desktop needs zero special privileges — no /dev/uinput, no udev rules, no root. You just call NotifyPointerMotionAbsolute with coordinates relative to the stream.

Video starts as simple as possible on purpose: JPEG frames over a WebSocket to a canvas element, good enough to prove the whole loop end-to-end at ten to twenty frames a second on a LAN. That's a deliberate choice — prove the loop first, then optimize. Phase 1 upgrades that to H.264 over Media Source Extensions, the same recipe Weylus uses, with a real latency budget: capture and encode under 25 milliseconds, USB transport under 5, decode and render under 40 — fast enough to feel instant.

## The build plan

Phase 0 is deliberately selfish: make it work end-to-end on the author's own Ubuntu GNOME machine and a Redmi phone, nothing more. That's six milestones — spike the virtual monitor with a shell script, capture frames, serve them over WebSocket with QR pairing, wire up touch input, add a USB dock mode using adb reverse for zero-latency wired operation, and polish it into one clean command. Each milestone has a concrete acceptance test, and the whole phase is scoped at roughly a week and a half of focused work. Only after that does the roadmap widen: Phase 2 adds KDE and wlroots support plus a panel mode, Phase 3 is packaging and a public launch playbook aimed at the communities already searching for exactly this tool.

## The honest caveats

The document doesn't oversell. GNOME's own built-in RDP extend mode already exists as a baseline, but reports on whether touch even works through it conflict, and FreeRDP's maintainers have said extending desktops isn't really what they intend to support — so waydeck treats it as a shaky bar to beat, not a foundation to build on. There's a known Mutter bug that makes cursors flicker in one rendering mode, worked around by using embedded cursor mode. There's a historical crash tied to resizing a virtual monitor mid-session, worked around with fixed-size streams. And panel mode — pinning one app permanently to the phone — is framed honestly: Wayland doesn't let external tools reposition arbitrary windows, so v1's version is "drag it there once, and a stable monitor identity makes the desktop remember," not true auto-placement. That kind of restraint is itself a signal of engineering maturity.

## Why it's worth building beyond the tool itself

Past the product, this project is a deliberately chosen skills showcase: Wayland protocols and D-Bus, a PipeWire-to-GStreamer video pipeline, kernel-level input synthesis, realtime networking, and the open-source mechanics of packaging and community launch. It's scoped so that Phase 0 alone — a personal cyberdeck setup, a repo skeleton, a demo GIF — is already a complete, demonstrable story, with everything after that as upside.
