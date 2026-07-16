/* waydeck browser client.
 *
 * One WebSocket carries everything: binary video frames (12-byte header:
 * u8 type, u8 flags, u16 reserved, f64 server-time-ms) and JSON text for
 * handshake, input and ping/pong. Video path is negotiated: H.264 via
 * WebCodecs when this page runs in a secure context (USB/localhost or TLS),
 * JPEG + createImageBitmap otherwise.
 */
'use strict';

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

// stats
let framesRendered = 0;
let bytesReceived = 0;
let lastFrameServerTs = 0;
let renderLatencyMs = 0;
let rttMs = 0;
let clockOffsetMs = 0; // performance.now() + offset ≈ server epoch ms
let fps = 0;
let mbps = 0;

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
      break;
    case 'pong': {
      const now = performance.now();
      rttMs = now - msg.t0;
      clockOffsetMs = msg.t1 + rttMs / 2 - now;
      break;
    }
    case 'error':
      setOverlay(msg.msg);
      break;
  }
}

/* ---------------- video: h264 / webcodecs ---------------- */

function setupDecoder(codec) {
  waitingKey = true;
  decoder = new VideoDecoder({
    output: (frame) => { paint(frame); frame.close(); },
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
}

/* ---------------- video: jpeg fallback ---------------- */

let pendingJpeg = null;
let jpegBusy = false;

function decodeJpeg(payload) {
  pendingJpeg = payload;
  if (jpegBusy) return; // latest-wins: this one is picked up when current finishes
  jpegBusy = true;
  const pump = () => {
    const data = pendingJpeg;
    pendingJpeg = null;
    createImageBitmap(new Blob([data], { type: 'image/jpeg' }))
      .then((bmp) => {
        paint(bmp);
        bmp.close();
        if (pendingJpeg) pump(); else jpegBusy = false;
      })
      .catch(() => { jpegBusy = false; });
  };
  pump();
}

/* ---------------- shared frame path ---------------- */

function onBinary(buf) {
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 1) return; // not a video frame
  const key = (dv.getUint8(1) & 1) !== 0;
  lastFrameServerTs = dv.getFloat64(4, true);
  bytesReceived += buf.byteLength;
  const payload = new Uint8Array(buf, 12);

  if (cfg && cfg.transport === 'h264' && decoder) {
    if (waitingKey && !key) return;
    waitingKey = false;
    try {
      decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta',
        timestamp: performance.now() * 1000,
        data: payload,
      }));
    } catch (e) {
      console.error(e);
      ws.close();
    }
  } else {
    decodeJpeg(payload);
  }
}

function paint(source) {
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  framesRendered += 1;
  if (clockOffsetMs && lastFrameServerTs) {
    renderLatencyMs = performance.now() + clockOffsetMs - lastFrameServerTs;
  }
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

setInterval(() => {
  fps = framesRendered; framesRendered = 0;
  mbps = (bytesReceived * 8) / 1e6; bytesReceived = 0;
  if (!hud.classList.contains('hidden') && cfg) {
    hud.textContent =
      `${cfg.transport}${cfg.transport === 'h264' ? ` (${cfg.codec})` : ''}  ${cfg.width}×${cfg.height}\n` +
      `fps ${fps}   ${mbps.toFixed(1)} Mb/s\n` +
      `rtt ${rttMs.toFixed(0)} ms   e2e ~${renderLatencyMs.toFixed(0)} ms\n` +
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
