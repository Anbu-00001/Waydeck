/* waydeck browser client.
 *
 * One WebSocket carries everything: binary video frames (16-byte header:
 * u8 type, u8 flags, u16 reserved, f64 server-send-time-ms, f32
 * capture+encode-ms [NaN if unmeasured]) and JSON text for handshake, input
 * and ping/pong. Video path is negotiated: H.264 via WebCodecs when this
 * page runs in a secure context (USB/localhost or TLS), JPEG +
 * createImageBitmap otherwise.
 *
 * Latency HUD (▤): each frame carries the server's capture+encode time.
 * Network latency = arrival time vs. send time (via the ping/pong clock
 * sync). Decode+render latency = time from arrival to the pixels actually
 * landing on the canvas. Together these three numbers are the blueprint's
 * budget breakdown (capture+encode / network / decode+render), not just an
 * aggregate round-trip guess.
 */
'use strict';

const HEADER_SIZE = 16;
const BUDGET_CAPTURE_ENCODE_MS = 25;
const BUDGET_NETWORK_MS = 5; // meaningful over USB; LAN/WiFi will exceed it
const BUDGET_DECODE_RENDER_MS = 40;

const TOKEN = new URLSearchParams(location.search).get('t') || '';

const canvas = document.getElementById('screen');
const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
const overlay = document.getElementById('overlay');
const overlayMsg = document.getElementById('overlay-msg');
const hud = document.getElementById('hud');
const btns = document.getElementById('btns');
const kbd = document.getElementById('kbd');

let ws = null;
let cfg = null;
let decoder = null;
let waitingKey = true;
let reconnectDelay = 1;
let stopped = false;

// stats — sums/counts reset every HUD tick (see setInterval below), so the
// displayed numbers are a true per-second average, not a single sample.
let framesRendered = 0;
let bytesReceived = 0;
let rttMs = 0;
let clockOffsetMs = 0; // performance.now() + offset ≈ server epoch ms
let clockSynced = false;
let fps = 0;
let mbps = 0;
function freshLegs() {
  return {
    captureEncode: { sum: 0, n: 0 },
    network: { sum: 0, n: 0 },
    decodeRender: { sum: 0, n: 0 },
  };
}
// Per-second bucket for the HUD (reset every tick) plus a per-connection
// lifetime bucket (reset only on reconnect) — a single 1s snapshot can land
// in a quiet gap between bursts of activity, but the lifetime average can't.
const latencyAcc = freshLegs();
const latencyLifetime = freshLegs();
const latencyAvg = { captureEncode: null, network: null, decodeRender: null };

function accumulate(bucket, value) {
  if (value === null || Number.isNaN(value)) return;
  bucket.sum += value;
  bucket.n += 1;
}

/* Called once a frame's pixels have actually landed on the canvas — the
 * single point where all three latency legs are known for that frame. */
function recordFrameLatency(sendTimeMs, captureEncodeMs, tRecv) {
  const networkMs = clockSynced ? (tRecv + clockOffsetMs) - sendTimeMs : null;
  const decodeRenderMs = performance.now() - tRecv;
  for (const [bucket, val] of [
    [latencyAcc.captureEncode, captureEncodeMs], [latencyLifetime.captureEncode, captureEncodeMs],
    [latencyAcc.network, networkMs], [latencyLifetime.network, networkMs],
    [latencyAcc.decodeRender, decodeRenderMs], [latencyLifetime.decodeRender, decodeRenderMs],
  ]) accumulate(bucket, val);
}

function avgOf(bucket) {
  return bucket.n ? bucket.sum / bucket.n : null;
}

function setOverlay(msg) {
  if (msg === null) {
    overlay.classList.add('hidden');
  } else {
    overlayMsg.textContent = msg;
    overlay.classList.remove('hidden');
  }
}

/* ---------------- connection ---------------- */

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    setOverlay('negotiating…');
    ws.send(JSON.stringify({
      t: 'hello',
      webcodecs: 'VideoDecoder' in window,
      secure: window.isSecureContext,
      ua: navigator.userAgent,
    }));
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      onText(JSON.parse(ev.data));
    } else {
      onBinary(ev.data);
    }
  };

  ws.onclose = (ev) => {
    teardownDecoder();
    if (stopped) return;
    if (ev.code === 4000) { setOverlay('replaced by another device'); return; }
    if (ev.code === 4003) { setOverlay('invalid session token — rescan the QR code'); return; }
    if (ev.code === 4005) { setOverlay('server rejected this client: ' + (ev.reason || 'unsupported')); return; }
    setOverlay(`disconnected — retrying in ${reconnectDelay}s`);
    setTimeout(connect, reconnectDelay * 1000);
    reconnectDelay = Math.min(reconnectDelay * 2, 10);
  };
}

function onText(msg) {
  switch (msg.t) {
    case 'config':
      cfg = msg;
      canvas.width = cfg.width;
      canvas.height = cfg.height;
      layout();
      if (cfg.transport === 'h264') setupDecoder(cfg.codec);
      reconnectDelay = 1;
      setOverlay(null);
      for (const leg of Object.values(latencyLifetime)) { leg.sum = 0; leg.n = 0; }
      totalFrames = 0;
      break;
    case 'pong': {
      const now = performance.now();
      rttMs = now - msg.t0;
      clockOffsetMs = msg.t1 + rttMs / 2 - now;
      clockSynced = true;
      break;
    }
    case 'error':
      setOverlay(msg.msg);
      break;
  }
}

/* ---------------- video: h264 / webcodecs ---------------- */

// VideoDecoder's output callback only hands back the VideoFrame, so per-frame
// metadata (needed for the latency HUD) rides alongside in this FIFO queue.
// Safe because both encoder (bframes=0 / zerolatency tune) and decoder
// (optimizeForLatency) are configured not to reorder frames.
const h264Meta = [];

function setupDecoder(codec) {
  waitingKey = true;
  h264Meta.length = 0;
  decoder = new VideoDecoder({
    output: (frame) => {
      const meta = h264Meta.shift();
      paint(frame);
      if (meta) recordFrameLatency(meta.sendTimeMs, meta.encodeMs, meta.tRecv);
      frame.close();
    },
    error: (e) => {
      console.error('decoder', e);
      setOverlay('decoder error — reconnecting');
      ws.close();
    },
  });
  // No `description`: Annex-B stream, SPS/PPS arrive in-band with each IDR.
  decoder.configure({ codec, optimizeForLatency: true });
}

function teardownDecoder() {
  if (decoder && decoder.state !== 'closed') decoder.close();
  decoder = null;
  h264Meta.length = 0;
}

/* ---------------- video: jpeg fallback ---------------- */

let pendingJpeg = null; // { payload, sendTimeMs, encodeMs, tRecv }
let jpegBusy = false;

function decodeJpeg(payload, sendTimeMs, encodeMs, tRecv) {
  pendingJpeg = { payload, sendTimeMs, encodeMs, tRecv };
  if (jpegBusy) return; // latest-wins: this one is picked up when current finishes
  jpegBusy = true;
  const pump = () => {
    const { payload: data, sendTimeMs: st, encodeMs: em, tRecv: tr } = pendingJpeg;
    pendingJpeg = null;
    createImageBitmap(new Blob([data], { type: 'image/jpeg' }))
      .then((bmp) => {
        paint(bmp);
        bmp.close();
        recordFrameLatency(st, em, tr);
        if (pendingJpeg) pump(); else jpegBusy = false;
      })
      .catch(() => { jpegBusy = false; });
  };
  pump();
}

/* ---------------- shared frame path ---------------- */

function onBinary(buf) {
  const tRecv = performance.now();
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 1) return; // not a video frame
  const key = (dv.getUint8(1) & 1) !== 0;
  const sendTimeMs = dv.getFloat64(4, true);
  const encodeMsRaw = dv.getFloat32(12, true);
  const encodeMs = Number.isNaN(encodeMsRaw) ? null : encodeMsRaw;
  bytesReceived += buf.byteLength;
  const payload = new Uint8Array(buf, HEADER_SIZE);

  if (cfg && cfg.transport === 'h264' && decoder) {
    if (waitingKey && !key) return;
    waitingKey = false;
    try {
      h264Meta.push({ sendTimeMs, encodeMs, tRecv });
      decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta',
        timestamp: tRecv * 1000,
        data: payload,
      }));
    } catch (e) {
      h264Meta.pop();
      console.error(e);
      ws.close();
    }
  } else {
    decodeJpeg(payload, sendTimeMs, encodeMs, tRecv);
  }
}

let totalFrames = 0;

function paint(source) {
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  framesRendered += 1;
  totalFrames += 1;
}

/* ---------------- layout ---------------- */

function layout() {
  if (!cfg) return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const scale = Math.min(vw / cfg.width, vh / cfg.height);
  canvas.style.width = `${cfg.width * scale}px`;
  canvas.style.height = `${cfg.height * scale}px`;
}

window.addEventListener('resize', layout);

/* ---------------- touch input ---------------- */

const slots = new Map(); // pointerId -> slot
const pendingMoves = new Map(); // slot -> {x, y}
let moveFlushScheduled = false;

function allocSlot(pointerId) {
  const used = new Set(slots.values());
  for (let s = 0; s < 10; s += 1) {
    if (!used.has(s)) { slots.set(pointerId, s); return s; }
  }
  return null;
}

function norm(ev) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1),
    y: Math.min(Math.max((ev.clientY - r.top) / r.height, 0), 1),
  };
}

function sendInput(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function flushMoves() {
  moveFlushScheduled = false;
  for (const [slot, p] of pendingMoves) {
    sendInput({ t: 'touch', ph: 'm', slot, x: p.x, y: p.y });
  }
  pendingMoves.clear();
}

canvas.addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  canvas.setPointerCapture(ev.pointerId);
  const slot = allocSlot(ev.pointerId);
  if (slot === null) return;
  const p = norm(ev);
  sendInput({ t: 'touch', ph: 'd', slot, x: p.x, y: p.y });
  wakeButtons();
});

canvas.addEventListener('pointermove', (ev) => {
  const slot = slots.get(ev.pointerId);
  if (slot === undefined) return;
  ev.preventDefault();
  pendingMoves.set(slot, norm(ev));
  if (!moveFlushScheduled) {
    moveFlushScheduled = true;
    requestAnimationFrame(flushMoves);
  }
});

function pointerEnd(ev) {
  const slot = slots.get(ev.pointerId);
  if (slot === undefined) return;
  ev.preventDefault();
  slots.delete(ev.pointerId);
  pendingMoves.delete(slot);
  const p = norm(ev);
  sendInput({ t: 'touch', ph: 'm', slot, x: p.x, y: p.y });
  sendInput({ t: 'touch', ph: 'u', slot, x: p.x, y: p.y });
}

canvas.addEventListener('pointerup', pointerEnd);
canvas.addEventListener('pointercancel', pointerEnd);
document.addEventListener('contextmenu', (e) => e.preventDefault());

/* ---------------- keyboard ---------------- */

const KEYSYMS = {
  Enter: 0xff0d, Backspace: 0xff08, Tab: 0xff09, Escape: 0xff1b,
  Delete: 0xffff, Insert: 0xff63,
  ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53, ArrowDown: 0xff54,
  Home: 0xff50, End: 0xff57, PageUp: 0xff55, PageDown: 0xff56,
  Shift: 0xffe1, Control: 0xffe3, Alt: 0xffe9, Meta: 0xffeb, CapsLock: 0xffe5,
};
for (let i = 1; i <= 12; i += 1) KEYSYMS[`F${i}`] = 0xffbe + i - 1;

function charKeysym(ch) {
  const cp = ch.codePointAt(0);
  return cp < 0x100 ? cp : 0x01000000 + cp; // X11 Unicode keysym rule
}

function keyToKeysym(key) {
  if (KEYSYMS[key] !== undefined) return KEYSYMS[key];
  if (key.length === 1) return charKeysym(key);
  return null;
}

function tapKeysym(sym) {
  sendInput({ t: 'key', sym, down: true });
  sendInput({ t: 'key', sym, down: false });
}

kbd.addEventListener('keydown', (ev) => {
  if (ev.key === 'Unidentified' || ev.isComposing || ev.keyCode === 229) return;
  const sym = keyToKeysym(ev.key);
  if (sym === null) return;
  ev.preventDefault();
  sendInput({ t: 'key', sym, down: true });
});

kbd.addEventListener('keyup', (ev) => {
  if (ev.key === 'Unidentified' || ev.isComposing || ev.keyCode === 229) return;
  const sym = keyToKeysym(ev.key);
  if (sym === null) return;
  ev.preventDefault();
  sendInput({ t: 'key', sym, down: false });
});

/* Android soft keyboards often bypass key events (keyCode 229 / IME) and
 * deliver text via input events instead — handle both paths. */
kbd.addEventListener('beforeinput', (ev) => {
  if (ev.inputType === 'insertText' && ev.data) {
    ev.preventDefault();
    for (const ch of ev.data) tapKeysym(charKeysym(ch));
  } else if (ev.inputType === 'deleteContentBackward') {
    ev.preventDefault();
    tapKeysym(KEYSYMS.Backspace);
  } else if (ev.inputType === 'insertLineBreak') {
    ev.preventDefault();
    tapKeysym(KEYSYMS.Enter);
  }
});
kbd.addEventListener('input', () => { kbd.value = ' '; }); // keep backspace deliverable
kbd.value = ' ';

/* ---------------- buttons & hud ---------------- */

document.getElementById('btn-full').addEventListener('click', async () => {
  try {
    await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
    if (cfg && cfg.width > cfg.height && screen.orientation && screen.orientation.lock) {
      await screen.orientation.lock('landscape');
    }
  } catch (e) { /* not fatal */ }
  layout();
});

document.getElementById('btn-kbd').addEventListener('click', () => {
  if (document.activeElement === kbd) kbd.blur(); else kbd.focus();
});

document.getElementById('btn-hud').addEventListener('click', () => {
  hud.classList.toggle('hidden');
});

let fadeTimer = null;
function wakeButtons() {
  btns.classList.remove('faded');
  clearTimeout(fadeTimer);
  fadeTimer = setTimeout(() => btns.classList.add('faded'), 3000);
}
wakeButtons();

function drainAvg(bucket) {
  const avg = bucket.n ? bucket.sum / bucket.n : null;
  bucket.sum = 0;
  bucket.n = 0;
  return avg;
}

function budgetMark(ms, budget) {
  if (ms === null) return '  n/a';
  return `${ms <= budget ? '✓' : '✗'} ${ms.toFixed(1)}`;
}

setInterval(() => {
  fps = framesRendered; framesRendered = 0;
  mbps = (bytesReceived * 8) / 1e6; bytesReceived = 0;
  latencyAvg.captureEncode = drainAvg(latencyAcc.captureEncode);
  latencyAvg.network = drainAvg(latencyAcc.network);
  latencyAvg.decodeRender = drainAvg(latencyAcc.decodeRender);

  // Exposed for automated/CDP-driven verification and debugging; also the
  // cleanest way to read exact numbers off a headless run. `lifetime` is a
  // running average since connect, immune to a query landing in a quiet
  // 1-second gap between bursts of on-screen activity.
  window.__wdStats = {
    transport: cfg && cfg.transport,
    codec: cfg && cfg.codec,
    fps,
    mbps,
    rttMs,
    lastSecond: {
      captureEncodeMs: latencyAvg.captureEncode,
      networkMs: latencyAvg.network,
      decodeRenderMs: latencyAvg.decodeRender,
    },
    lifetime: {
      frames: totalFrames,
      captureEncodeMs: avgOf(latencyLifetime.captureEncode),
      networkMs: avgOf(latencyLifetime.network),
      decodeRenderMs: avgOf(latencyLifetime.decodeRender),
    },
  };

  if (!hud.classList.contains('hidden') && cfg) {
    hud.textContent =
      `${cfg.transport}${cfg.transport === 'h264' ? ` (${cfg.codec})` : ''}  ${cfg.width}×${cfg.height}\n` +
      `fps ${fps}   ${mbps.toFixed(1)} Mb/s   rtt ${rttMs.toFixed(0)} ms\n` +
      `capture+encode ${budgetMark(latencyAvg.captureEncode, BUDGET_CAPTURE_ENCODE_MS)} ms  (budget ${BUDGET_CAPTURE_ENCODE_MS})\n` +
      `network        ${budgetMark(latencyAvg.network, BUDGET_NETWORK_MS)} ms  (budget ${BUDGET_NETWORK_MS}, USB)\n` +
      `decode+render  ${budgetMark(latencyAvg.decodeRender, BUDGET_DECODE_RENDER_MS)} ms  (budget ${BUDGET_DECODE_RENDER_MS})\n` +
      `input ${cfg.inputMode}   waydeck ${cfg.version}`;
  }
}, 1000);

setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ t: 'ping', t0: performance.now() }));
  }
}, 2000);

/* ---------------- wake lock ---------------- */

let wakeLock = null;
async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
  } catch (e) { /* insecure context or denied — USB mode gives this for free */ }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') acquireWakeLock();
});
acquireWakeLock();

/* ---------------- go ---------------- */

if (!TOKEN) {
  setOverlay('missing session token — open the QR-code URL from the waydeck terminal');
} else {
  connect();
}
