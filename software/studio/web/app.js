import { ShutdownButton } from './shutdown-button.js';
// Microduck Studio frontend. No build step: Preact + htm (vendor/preact-htm.module.js) run directly in the browser.
//
// Two channels to the Studio backend:
// - HTTP: /api/session, /api/login, /api/logout, /api/password, /api/logs, /api/diag;
// - WebSocket /ws: backend pushes hello / state / log / result; frontend sends hb (heartbeat with page visibility) and cmd.
// Message schema is documented in software/studio/README.md.
import { html, render, Component, useState, useEffect, useRef, useCallback } from './vendor/preact-htm.module.js';
import { ArmatureMark } from './armature-mark.js';
import { Duck3D } from './duck3d.js';
import { ServoPage, HandBackButton } from './servo.js';
import { RuntimeDiagnostics } from './runtime-diagnostics.js';
import { ImuPage } from './imu.js';
import { PadPage, padActivity, padGo } from './pad.js';
import { BatteryCell } from './battery.js';
import { BatterySettings } from './settings-battery.js';
import { MicrophoneSettings } from './settings-microphone.js';
import { AiSettings } from './settings-ai.js';
import { UpdateNotice, UpdateSettings } from './settings-updates.js';
import { AccessGuide, AiSshEntry } from './settings-access.js';
import { TuneSection, ModelSettings } from './tune.js';
import { WifiCard, WifiSettings, DuckAddresses } from './wifi.js';
import { TwoStep } from './confirm.js';
import { ModelsPage } from './models.js';
import { ActionsPage } from './actions.js';
import { CameraPage, TofPage, SensorBandCells } from './sensors.js';
import { imuDiagnosticCopy, startupStageCopy } from './startup-status.js';

const STALE_GRAY_MS = 1000;   // Over 1 s without fresh data: dim telemetry values
const STALE_ALARM_MS = 3000;  // Over 3 s: show disconnect banner; E-Stop remains active while Studio WS is open

// ---------------- Theme ----------------

const THEME_KEY = 'armature-studio-theme';
function getTheme() {
  try { return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'; } catch { return 'dark'; }
}
function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode */ }
  return t;
}

// ---------------- Connection ----------------

class Link {
  constructor() {
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.status = 'connecting';
    this.lastAt = 0;
    this.backoff = 1000;
    this._hb = null;
    this.padCapture = false;
    document.addEventListener('visibilitychange', () => this.heartbeat());
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(m) { for (const fn of this.listeners) fn(m); }
  setStatus(s) { this.status = s; this.emit({ t: '_status', status: s }); }

  connect() {
    clearTimeout(this._retry);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    let opened = false;
    this.setStatus('connecting');
    ws.onopen = () => {
      opened = true;
      this.backoff = 1000;
      this.setStatus('open');
      this.heartbeat();
      clearInterval(this._hb);
      this._hb = setInterval(() => this.heartbeat(), 1000);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;            // Ignore messages from replaced sockets
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      this.lastAt = performance.now();
      // Background tabs throttle setInterval; reply to server hb immediately so we don't appear disconnected
      if (m.t === 'hb') this.heartbeat();
      if (m.t === 'result' && this.pending.has(m.id)) {
        this.pending.get(m.id)(m);
        this.pending.delete(m.id);
      }
      this.emit(m);
    };
    ws.onclose = async (ev) => {
      if (this.ws !== ws) return;            // Ignore close from replaced socket
      clearInterval(this._hb);
      for (const done of this.pending.values()) done({ ok: false, error: 'Disconnected from Studio' });
      this.pending.clear();
      if (ev.code === 4401) { this.setStatus('auth'); return; }   // Session expired
      if (!opened) {
        try { if ((await api('/api/session', undefined, 2000)).status === 401) { this.setStatus('auth'); return; } } catch { /* Studio offline */ }
        if (this.ws !== ws) return;
      }
      this.setStatus('closed');
      this._retry = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  }

  /** Wait until the WebSocket connection to Studio is open */
  whenOpen() {
    if (this.status === 'open') return Promise.resolve();
    return new Promise((resolve) => { const off = this.on((m) => { if (m.t === '_status' && m.status === 'open') { off(); resolve(); } }); });
  }

  reconnect() { this.backoff = 1000; try { this.ws && this.ws.close(); } catch { /* already closed */ } this.connect(); }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) { this.ws.send(JSON.stringify(obj)); return true; }
    return false;
  }

  heartbeat() {
    const route = location.hash.replace(/^#/, '') || '/';
    this.send({ t: 'hb', seq: ++this.seq, visible: document.visibilityState === 'visible', route,
      pad_visible: padActivity.active || this.padCapture,
      diagnostics_visible: !!this.diagnosticsVisible && (route === '/' || route === '/pad') });
  }

  setDiagnosticsVisible(active) {
    if (!!this.diagnosticsVisible === !!active) return;
    this.diagnosticsVisible = !!active; this.heartbeat();
  }

  // Read-only pad capture bypass for model keybinding without taking over drive control
  setPadCapture(active) { this.padCapture = Boolean(active); this.heartbeat(); }

  cmd(name, args = {}, timeoutMs = 15000) {
    const id = `c${++this.seq}`;
    return new Promise((resolve) => {
      if (!this.send({ t: 'cmd', id, name, args })) { resolve({ ok: false, error: 'Disconnected from Studio' }); return; }
      this.pending.set(id, resolve);
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); resolve({ ok: false, error: 'Robot did not respond (timeout)' }); }
      }, timeoutMs);
    });
  }
}

const link = new Link();
padActivity.bind(link);

let csrf = '';   // Provided by /api/session; sent as X-CSRF on mutating POSTs
async function api(path, body, timeoutMs = 10000, signal) {
  const opt = body === undefined ? { credentials: 'same-origin' }
    : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf }, body: JSON.stringify(body) };
  if (path === '/api/microphone' && body === undefined) opt.headers = { 'X-CSRF': csrf };
  const ctl = new AbortController();
  const abort = () => ctl.abort();
  if (signal?.aborted) ctl.abort();
  signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let r;
  let data = {};
  try {
    r = await fetch(path, { ...opt, signal: ctl.signal });
    try { data = await r.json(); } catch (error) { if (ctl.signal.aborted) throw error; }
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  return { status: r.status, ...data };
}

// ---------------- Helpers ----------------

const fmt1 = (v) => (typeof v === 'number' && isFinite(v) ? v.toFixed(1) : '—');

function useNow(ms = 250) {
  const [now, setNow] = useState(performance.now());
  useEffect(() => { const t = setInterval(() => setNow(performance.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

function Badge({ kind }) {
  return html`<span class=${'badge ' + kind} aria-hidden="true">${kind === 'bad' ? '×' : '!'}</span>`;
}

// ---------------- Top Safety Bar (E-Stop) ----------------

const TEXT_TYPES = ['text', 'password', 'search', 'email', 'number', 'tel', 'url'];
const typing = (el) => !!el && (el.isContentEditable || el.tagName === 'TEXTAREA'
  || (el.tagName === 'INPUT' && TEXT_TYPES.includes((el.type || 'text').toLowerCase())));
const plainSpace = (e) => e.code === 'Space' && !e.ctrlKey && !e.altKey && !e.metaKey;

function EStop({ st, live, open }) {
  const [sentAt, setSentAt] = useState(0);
  const [err, setErr] = useState(null);
  const fire = useCallback(async () => {
    if (performance.now() - sentAt < 200) return;
    setSentAt(performance.now()); setErr(null);
    const r = await link.cmd('relax', {}, 7000);
    if (!r.ok) setErr(r.reason || r.error || 'Command not sent');
  }, [sentAt]);

  useEffect(() => {
    const down = (e) => {
      if (!plainSpace(e) || typing(e.target)) return;
      e.preventDefault(); e.stopPropagation();
      if (!e.repeat) fire();
    };
    const up = (e) => { if (plainSpace(e) && !typing(e.target)) { e.preventDefault(); e.stopPropagation(); } };
    window.addEventListener('keydown', down, true);
    window.addEventListener('keyup', up, true);
    return () => { window.removeEventListener('keydown', down, true); window.removeEventListener('keyup', up, true); };
  }, [fire]);

  const es = st?.estop || {};
  useEffect(() => { if (es.state === 'confirmed') setErr(null); }, [es.state]);
  const click = (e) => { e.currentTarget.blur(); fire(); };
  const note = (text) => (text ? html`<span class="why" role=${err ? 'alert' : undefined}>${text}</span>` : null);
  const fallback = st?.mode === 'commissioning' ? 'Power off only' : 'Use gamepad − or power off';
  if (!open) {
    return html`<span class="estop-wrap">${note(fallback)}
      <button class="estop unavailable" disabled>E-Stop · Offline</button></span>`;
  }
  if (st?.servo_orphan) {
    return html`<span class="estop-wrap">${note(err || 'Servos may still hold torque')}
      <button class="estop unavailable" onClick=${(e) => { e.currentTarget.blur(); location.hash = '#/servos'; }}
        title="Previous servo session left open without takeover: open Servos to take over the bus before relaxing">E-Stop · Take Over Servos</button></span>`;
  }
  if (es.state === 'unavailable') {
    return html`<span class="estop-wrap">${note(err || es.why || fallback)}
      <button class="estop unavailable" onClick=${click}>E-Stop · Unverified</button></span>`;
  }
  if (live && es.state === 'confirmed') {
    return html`<span class="estop-wrap">${note(err)}
      <button class="estop done" onClick=${click} title=${es.why}>Relaxed ✓</button></span>`;
  }
  return html`<span class="estop-wrap">${note(err || (!live ? 'Status stale — still active' : ''))}
    <button class="estop" onClick=${click} title="Disable torque on all 15 servos. Support the robot before pressing.">
      ${es.state === 'pending' ? 'Relaxing…' : 'E-Stop'}<span class="kbd">Space</span></button></span>`;
}

function TopBar({ duck, st, live, age, open, hideEStop, imu }) {
  const calibrationText = live && imu?.job?.phase === 'verify' ? 'Verifying IMU'
    : live && st?.posture === 'homing' && imu?.coarse_sampling?.hold_ready ? 'Standing steady' : null;
  const postureText = st ? (calibrationText || { starting: st.start_wait?.text || (st.starting_long ? 'Stuck starting' : 'Starting'), absent: 'robotd stopped', homing: 'Standing up',
    servo_session: 'Servo Tuning' }[st.posture] || WAS[st.posture] || 'Unknown') : null;
  const modeText = !st ? 'Connecting'
    : st.mode === 'commissioning' ? (st.posture === 'commissioning' ? 'Commissioning' : `Commissioning · ${postureText}`)
    : st.mode === 'motion' && st.posture !== 'absent' && st.posture !== 'servo_session' ? `Motion · ${postureText}`
    : postureText;
  const pct = st?.battery?.pct;
  const net = st?.net;
  const addrTitle = net?.known ? [`IP ${net.ip_now || 'unknown'}`, ...(net.urls || []).map((u) => u.url)].join('\n') : undefined;
  return html`<header class="topbar">
    <${ShutdownButton} api=${api} />
    <div class="duckid">
      <span class="dot" style=${{ background: duck?.color || 'var(--accent)' }}></span>
      <span class="name" title=${addrTitle}>${duck?.label || 'Microduck'}</span>
      ${st?.servo_session ? html`<span class="sv-badge">Servo Tuning</span>` : null}
      <span class="meta num">${live ? '' : `${Math.round(age / 1000)}s ago · `}${modeText}${typeof pct === 'number' ? ` · ${Math.round(pct)}%` : ''}</span>
    </div>
    <span class=${hideEStop ? 'estop-hidden' : ''}><${EStop} st=${st} live=${live} open=${open} /></span>
  </header>`;
}

// ---------------- Servo Session Banner ----------------

function ServoBar({ st, onServos }) {
  const ss = st?.servo_session;
  if (ss) {
    if (onServos) return null;
    const unk = ss.unknown ? ` · ${ss.unknown} torque unreadable` : '';
    return html`<div class="banner warn servo-bar" role="status">
      <b>Servo Tuning · ${ss.holding ?? '?'} holding${unk} · robotd paused (gamepad & walking disabled)</b>
      <span class="sb-acts"><a href="#/servos">Open Servos</a><${HandBackButton} link=${link} /></span>
      <span class="more sb-more">${ss.restored ? 'Restored previous servo session after restart. ' : ''}Use top E-Stop or Space to relax; hand back before starting robotd manually.</span>
      ${st.servo_marker_error ? html`<span class="more sb-more">${st.servo_marker_error}</span>` : null}</div>`;
  }
  if (st?.servo_conflict_s != null) {
    return html`<div class="banner bad servo-bar" role="alert"><b>robotd was started manually and took over the serial bus</b>
      <span class="more sb-more">Studio closed its servo bus cleanly. Always click "Hand Back to robotd" before starting robotd manually.</span></div>`;
  }
  if (st?.servo_orphan) {
    return html`<div class="banner bad servo-bar" role="alert"><b>Servos may still hold torque from a previous session — take over on Servos page to relax</b>
      ${onServos ? null : html`<a href="#/servos">Open Servos</a>`}
      <span class="more sb-more">Could not auto-takeover on restart${st.servo_orphan.why ? ` (${st.servo_orphan.why})` : ''}; power off if urgent.</span></div>`;
  }
  if (st?.servo_handback_s != null) {
    if (st.servo_giveback_error) {
      return html`<div class="banner bad servo-bar" role="alert"><b>Handed back, but robotd failed to start</b>
        <span class="more sb-more">${st.servo_giveback_error}${st.servo_giveback_hint ? `: ${st.servo_giveback_hint}` : ''}</span></div>`;
    }
    return html`<div class="banner warn servo-bar" role="status"><b>Handed back.</b>
      ${st.servo_helper ? html`<span class="more">Starting robotd…</span>`
        : html`<span class="more">Banner clears once robotd reconnects. If stopped manually, run: <code>sudo systemctl start robotd</code></span>`}
      ${st.servo_handback_note ? html`<span class="more sb-more">${st.servo_handback_note}</span>` : null}</div>`;
  }
  return null;
}

// ---------------- Overview ----------------

const WAS = { holding: 'Standing', policy: 'Standing', walking: 'Walking', limp: 'Relaxed', sitting: 'Sitting', homing: 'Standing up' };
function stageCopy(st, live, name, age) {
  if (!st) return { h: `Connecting to ${name}…`, sub: 'Ensure the robot is powered on and on the same Wi-Fi network.' };
  if (!live && st.posture !== 'absent') {
    return { h: `${name} unresponsive for ${Math.round(age / 1000)}s`, sub: `Last posture: ${WAS[st.posture] || 'unknown'}. Control is paused until reconnected.`, stale: true };
  }
  switch (st.posture) {
    case 'absent': return { h: 'robotd is not running', sub: 'May be restarting (~2 s). Note: stopping robotd does not automatically disable servo torque.', stale: true };
    case 'starting':
      if (startupStageCopy(st)) return startupStageCopy(st);
      if (st.starting_long) {
        return { h: 'robotd stuck in "Starting"', act: { href: '#/logs', label: 'View Logs' },
          sub: st.start_error ? `Elapsed ${st.starting_s}s, stalled at: ${st.start_error}. Check Logs for details.`
            : `Elapsed ${st.starting_s}s. Check Logs to inspect the latest robotd startup output.` };
      }
      return { h: 'robotd is starting', sub: st.start_error ? `Stalled at: ${st.start_error}` : 'Usually completes within 10 seconds.' };
    case 'commissioning': return { h: 'Commissioning', sub: 'Initial setup: run IMU coarse calibration, zero servos, then commit to switch to Motion mode.' };
    case 'limp': return { h: `${name} is relaxed`, sub: 'Support the robot, scroll to Remote Control, and click Stand (or press gamepad +).' };
    case 'homing': return { h: 'Standing up…', sub: 'Keep supporting the robot.' };
    case 'holding': return { h: `${name} is standing`, sub: 'Scroll to Remote Control and click Start Walking (or press gamepad +), then use the stick.' };
    case 'policy': return { h: `${name} ready to walk`, sub: 'Push the virtual or physical stick to walk; click Stop (Stand) or press + to hold.' };
    case 'walking': return { h: 'Walking', sub: 'Release the stick to stop and hold standing posture.' };
    case 'sitting': return { h: `${name} is sitting`, sub: 'Click Stand in Remote Control, or press D-Pad Down on the gamepad.' };
    case 'servo_session': return { h: 'Servo Tuning Active', sub: 'robotd is paused (gamepad & walking disabled). Servos hold torque until you click "Hand Back to robotd".' };
    default: return { h: name, sub: 'Unable to read current posture.' };
  }
}

const STAGE_VIEWS = [
  ['front34', '3/4'],
  ['front', 'Front'],
  ['side', 'Side'],
  ['top', 'Top'],
  ['back34', 'Back'],
];

function Stage({ st, live, age, name }) {
  const canvasRef = useRef(null);
  const duckRef = useRef(null);
  const [msg, setMsg] = useState(null);
  const [moved, setMoved] = useState(false);
  const [ready, setReady] = useState(false);
  const [view, setView] = useState('front34');
  const [grid, setGrid] = useState(true);
  const [xray, setXray] = useState(false);
  const [com, setCom] = useState(false);
  const [imuGizmo, setImuGizmo] = useState(false);
  const [hovered, setHovered] = useState(null);

  useEffect(() => {
    const phone = window.innerWidth < 700;
    const d = new Duck3D(canvasRef.current, phone ? { view: 'front34', centerX: 0.5, centerY: 0.34, fill: 0.6, pageScroll: true }
      : { view: 'front34', centerX: 0.61, pageScroll: true });
    duckRef.current = d;
    d.onUserMove = () => setMoved(true);
    d.onHover = (jointName) => setHovered(jointName || null);
    d.load('model/').then(() => {
      setReady(true);
      d.setGrid(true);
    }).catch((e) => setMsg(`3D model failed to load: ${e.message}`));
    return () => d.dispose();
  }, []);

  useEffect(() => {
    if (!duckRef.current || !st) return;
    duckRef.current.setJoints(st.joints);
    duckRef.current.setGravity(st.gravity);
  }, [st]);

  const pickView = (v) => {
    setView(v);
    setMoved(false);
    duckRef.current?.setView(v);
  };
  const toggleGrid = () => { const next = !grid; setGrid(next); duckRef.current?.setGrid(next); };
  const toggleXray = () => { const next = !xray; setXray(next); duckRef.current?.setXray(next); };
  const toggleCom = () => { const next = !com; setCom(next); duckRef.current?.setComOverlay(next); };
  const toggleImu = () => { const next = !imuGizmo; setImuGizmo(next); duckRef.current?.setImuGizmo(next); };

  const c = stageCopy(st, live, name, age);
  const jointsOld = st?.joints_age_ms == null || st.joints_age_ms > 1000;
  return html`<section class="stage stage-ov" aria-label="Robot status">
    <canvas ref=${canvasRef} class=${c.stale || jointsOld ? 'stage-stale' : ''}></canvas>
    ${!live && st ? html`<div class="banner bad stage-banner" role="alert"><${Badge} kind="bad" />
      <b>Cannot reach ${name} · ${isFinite(age) ? Math.round(age / 1000) + 's' : ''}</b><span class="more">Standing robot will pause and hold.</span>
      <a href="#" onClick=${(e) => { e.preventDefault(); link.reconnect(); }}>Reconnect</a></div>` : null}
    <div class="copy">
      <h1>${c.h}</h1>
      <p class="sub">${c.sub}</p>
      ${c.act ? html`<div class="btns"><button class="btn2 stage-act" onClick=${(e) => { e.currentTarget.blur(); location.hash = c.act.href; }}>${c.act.label}</button></div>` : null}
      ${msg ? html`<span class="tip" role="alert">${msg}</span>` : c.tip ? html`<span class="tip">${c.tip}</span>` : null}
    </div>
    ${ready ? html`<div class="stage-hud" role="toolbar" aria-label="3D Viewport Controls">
      <div class="stage-hud-group">
        ${STAGE_VIEWS.map(([k, label]) => html`<button type="button" class=${'hud-btn' + (view === k && !moved ? ' on' : '')}
          onClick=${(e) => { e.currentTarget.blur(); pickView(k); }}>${label}</button>`)}
      </div>
      <div class="stage-hud-group">
        <button type="button" class=${'hud-btn' + (grid ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleGrid(); }} title="Calibration grid & Z-up axes">Grid</button>
        <button type="button" class=${'hud-btn' + (xray ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleXray(); }} title="Translucent shell X-Ray">X-Ray</button>
        <button type="button" class=${'hud-btn' + (com ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleCom(); }} title="Center of Mass plumb-line & support polygon">CoM</button>
        <button type="button" class=${'hud-btn' + (imuGizmo ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleImu(); }} title="Gravity & IMU attitude vector">IMU</button>
      </div>
      ${hovered ? html`<span class="hud-hover num">${hovered}</span>` : null}
    </div>` : null}
    ${c.stale && isFinite(age) ? html`<span class="age num">${Math.round(age / 1000)}s ago</span>` : null}
    ${!ready ? html`<span class="sv-loading">Loading 3D model…</span>` : null}
    ${moved ? html`<button class="reset-view" onClick=${(e) => { e.currentTarget.blur(); duckRef.current?.resetView(); setMoved(false); }}>Reset View</button>` : null}
  </section>`;
}

function boardCell(cpu) {
  const mhz = (k) => (typeof k === 'number' ? Math.round(k / 1000) : '?');
  if (typeof cpu?.c !== 'number') return { big: '—', small: cpu?.why || 'Unavailable', kind: null };
  const studio = cpu.src === 'studio' ? ' · Studio' : '';
  if (cpu.throttled) return { big: Math.round(cpu.c), small: `Throttled: ${mhz(cpu.khz)} / ${mhz(cpu.max_khz)} MHz${studio}`, kind: 'bad' };
  if (cpu.hot) return { big: Math.round(cpu.c), small: `Warm (≥80 °C)${studio}`, kind: 'warn' };
  if (cpu.limited) return { big: Math.round(cpu.c), small: `Capped at ${mhz(cpu.khz)} MHz${studio}`, kind: null };
  if (cpu.throttled == null) return { big: Math.round(cpu.c), small: `Throttle unknown${studio}`, kind: null };
  return { big: Math.round(cpu.c), small: `Normal${studio}`, kind: null };
}

function padCell(st, pd) {
  if (!pd) {
    const p = st?.pad || {};
    return { word: !p.known ? 'Unknown' : p.connected ? 'Connected' : 'Disconnected',
      small: !p.known ? 'Gamepad status unavailable' : p.connected ? `${p.name || 'Gamepad'}${p.estop_ok ? ' · E-Stop OK' : ''}` : 'Pair gamepad' };
  }
  const s = pd.status || {}, lv = pd.live || {}, es = pd.estop || {};
  const attached = lv.state === 'attached';
  const known = !!s.known || attached;
  const connected = !!s.active || attached;
  const name = s.active?.name || (attached ? lv.device?.name : null) || 'Gamepad';
  const esw = { ok: 'E-Stop verified ✓', unknown: 'E-Stop unverified', bad: 'E-Stop failed' }[es.state] || '';
  if (!known) return { word: 'Unknown', small: 'Gamepad unavailable (configd)' };
  if (connected) return { word: 'Connected', small: `${name}${esw ? ` · ${esw}` : ''}` };
  return { word: 'Disconnected', small: (s.pads || []).length ? 'Paired: power on gamepad to connect' : 'Pair gamepad' };
}

function useStickyHeight(name) {
  const ro = useRef(null);
  return useCallback((el) => {
    const root = document.documentElement.style;
    ro.current?.disconnect();
    ro.current = null;
    const put = () => root.setProperty(name, el && getComputedStyle(el).position === 'sticky' ? `${el.offsetHeight}px` : '0px');
    put();
    if (el && typeof ResizeObserver !== 'undefined') {
      ro.current = new ResizeObserver(put);
      ro.current.observe(el);
    }
  }, [name]);
}

function Band({ st, pd, gray, live, diagnosticElapsed = 0, onSame, sensorsActive }) {
  const bandRef = useStickyHeight('--band-h');
  const b = st?.battery || {}, s = st?.servos || {}, imu = st?.imu || {};
  const diagnostic = st?.posture === 'starting' && (imu.source == null || imu.source === 'bus')
    ? imuDiagnosticCopy(imu.diagnostic, live, diagnosticElapsed) : null;
  const imuWord = diagnostic?.word || { ok: 'OK', unverified: 'Unverified', frozen: 'Frozen', missing: 'Missing' }[imu.status] || '—';
  const imuKind = diagnostic ? diagnostic.kind : { unverified: 'warn', frozen: 'bad', missing: 'bad' }[imu.status];
  const cpu = boardCell(st?.cpu);
  const pad = padCell(st, pd);
  return html`<section class=${'band board' + (gray ? ' stale' : '')} aria-label="Telemetry" ref=${bandRef}>
    <${BatteryCell} b=${b} />
    <div>
      <span class="label">Servos</span>
      <span class="num" style="display:flex;align-items:baseline;gap:4px"><span class="big">${s.online ?? '—'}</span><span class="unit">/ ${s.total ?? 15}</span></span>
      <span class="small">${s.hottest ? `Peak ${Math.round(s.hottest.c)} °C · ${s.hottest.name}` : '—'}</span>
    </div>
    <a href="#/imu">
      <span class="label">IMU</span>
      <span class="word">${imuKind ? html`<${Badge} kind=${imuKind} />` : null}${imuWord}</span>
      <span class="small">${diagnostic ? diagnostic.board || 'ID 200 · Read-only' : { unverified: 'Stand first', ok: 'Axis verified' }[imu.status] || 'Calibrate'}</span>
    </a>
    <${SensorBandCells} api=${api} active=${sensorsActive} />
    <a href="#/pad" onClick=${(e) => onSame(e, '#/pad')}>
      <span class="label">Gamepad</span>
      <span class="word">${pad.word}</span>
      <span class="small">${pad.small}</span>
    </a>
    <div class=${'band-cpu' + (cpu.kind ? ` ${cpu.kind}` : '')}>
      <span class="label">SoC</span>
      <span class="num" style="display:flex;align-items:baseline;gap:4px"><span class="big">${cpu.big}</span>${cpu.big !== '—' ? html`<span class="unit">°C</span>` : null}</span>
      <span class="small">${cpu.kind ? html`<${Badge} kind=${cpu.kind} /> ` : null}${cpu.small}</span>
    </div>
  </section>`;
}

// ---------------- Onboarding Checklist ----------------

const ONB_MARK = { ok: '✓', skip: '↷', todo: '●', no: '○', unk: '?' };
const ONB_WORD = { ok: 'Done', skip: 'Skipped', todo: 'Next', no: 'Pending', unk: 'Unavailable' };
const ONB_OPEN_KEY = 'studio.onb.open';
const ONB_FROZEN = 'Servo tuning active: checklist frozen until handed back to robotd';
function fmtWhen(w) {
  if (w == null || w === '') return null;
  let d = null;
  if (typeof w === 'number') d = new Date(w * 1000);
  else if (typeof w === 'string') { const m = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)/.exec(w); if (m) d = new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]); }
  if (!d || isNaN(d.getTime())) return String(w);
  if (d.getFullYear() < 2024) return 'Clock unsynced (offline)';
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function loadOnbOpen() { try { return localStorage.getItem(ONB_OPEN_KEY) === '1'; } catch { return false; } }
function saveOnbOpen(v) { try { localStorage.setItem(ONB_OPEN_KEY, v ? '1' : '0'); } catch { /* private mode */ } }

function Onboard({ st, live }) {
  const ob = st?.onboarding;
  const [msg, setMsg] = useState(null);
  const [open, setOpen] = useState(loadOnbOpen);
  const [cprev, setCprev] = useState(null);
  const [cmsg, setCmsg] = useState(null);
  const [cbusy, setCbusy] = useState(false);
  const [copen, setCopen] = useState(false);
  const job = st?.commit?.job;
  useEffect(() => {
    if (job) setCmsg(null);
  }, [job?.phase, job?.ok, job?.finished]);
  if (!live && job) {
    const title = job.phase === 'running' ? 'Commit to robotd' : (job.ok ? 'Commit to robotd: Done' : 'Commit to robotd: Failed');
    return html`<section class="onb board" data-step="commit" aria-label="Commit progress">
      <div class="onb-h"><b>${title}</b><span class="quiet">robotd is restarting; connection temporarily paused</span></div>
      <div class="onb-commit">
        ${job.phase === 'running'
          ? html`<div class="onb-commit-job" role="status"><span class="onb-spin">⋯</span>${job.msg || 'Writing…'}</div>`
          : html`<div class=${'onb-commit-msg ' + (job.ok ? 'ok' : 'bad')}>${job.reason || job.msg || ''}</div>`}
        ${job.phase !== 'running' && !job.ok ? html`<p class="onb-tip">Previous calibration and mode will be restored automatically.</p>` : null}
      </div>
    </section>`;
  }
  if (!live) return html`<section class="onb board"><div class="onb-h"><b>Onboarding Checklist</b><span class="quiet">Paused while offline</span></div></section>`;
  if (!ob) return html`<section class="onb board"><div class="onb-h"><b>Onboarding Checklist</b><span class="quiet">Evaluating…</span></div></section>`;
  const head = ob.header || {};
  if (head.kind === 'bench') return html`<section class="onb board"><div class="onb-h"><b>Onboarding Checklist</b><span class="quiet">${head.text}</span></div></section>`;
  const done = ob.all_done && head.ok;
  const n = ob.skipped_n || 0;
  const m = ob.marked_n || 0;
  const counts = [n ? `${n} skipped` : '', m ? `${m} marked done` : ''].filter(Boolean).join(', ');
  const doneText = `All 6 onboarding steps complete${counts ? ` (${counts})` : ''}`;
  const toggle = () => { const v = !open; setOpen(v); saveOnbOpen(v); };
  if (done && !open) {
    return html`<section class="onb board onb-done" aria-label="Onboarding Checklist">
      <button class="onb-fold" aria-expanded="false" onClick=${toggle}><span class="onb-mark onb-ok">✓</span><b>${doneText}</b>
        <span class="onb-more">Expand</span></button></section>`;
  }
  const op = async (args, okText) => {
    const r = await link.cmd('onboard.op', args, 8000);
    setMsg(r.ok ? (okText ? { ok: true, text: okText } : null) : { ok: false, text: r.reason || r.error || 'Failed to save' });
  };
  const go = async (a) => {
    setMsg(null);
    if (a.do === 'servos') location.hash = '#/servos';
    else if (a.do === 'imu') location.hash = '#/imu';
    else if (a.do === 'commit') {
      setCopen(true);
      setTimeout(() => document.querySelector('[data-step="commit"] .onb-commit')?.scrollIntoView({ block: 'center' }), 50);
    }
    else if (a.do === 'pad_wizard') padGo('wizard');
    else if (a.do === 'pad_known') padGo('known');
    else if (a.do === 'estop') padGo('estop');
    else if (a.do === 'remote') padGo('remote');
    else if (a.do === 'robotd' || a.do === 'logs') location.hash = '#/logs';
  };
  const commitPreview = async () => {
    if (cprev) { setCprev(null); return; }
    setCmsg(null);
    const r = await link.cmd('calib.op', { op: 'preview' }, 20000);
    if (r.ok) setCprev(r.data || {});
    else setCmsg({ ok: false, text: r.reason || r.error || 'Preview failed' });
  };
  const commitRun = async () => {
    setCmsg(null);
    setCbusy(true);
    try {
      const r = await link.cmd('calib.op', { op: 'commit' }, 20000);
      if (!r.ok) setCmsg({ ok: false, text: r.reason || r.error || 'Commit failed to start' });
      else setCmsg({ ok: true, text: 'Commit started: robotd will restart shortly' });
    } finally {
      setCbusy(false);
    }
  };
  const btn = (a, row, dark) => html`<button class=${dark ? 'btn-dark' : 'btn-light'} onClick=${() => go(a, row)}>${a.label}</button>`;
  const frozen = !!ob.frozen;
  const fa = ob.frozen_ask || {};
  const markBtn = (row, kind, label, cls) => {
    const ask = (row.mark || {})[kind]?.ask || (frozen ? fa[kind] : null);
    const args = { op: kind, step: row.key };
    return ask ? html`<${TwoStep} label=${label} ask=${ask} cls=${'btn-light ' + cls} askCls="btn-light onb-skip-arm"
        onConfirm=${() => op({ ...args, confirmed: true })} />`
      : html`<button class=${'btn-light ' + cls} onClick=${() => op(args)}>${label}</button>`;
  };
  const undoBtn = (row) => {
    const args = { op: row.marked === 'skip' ? 'unskip' : 'undone', step: row.key };
    const label = row.marked === 'skip' ? 'Unskip' : 'Undo';
    return frozen ? html`<${TwoStep} label=${label} ask=${fa.undo || 'Confirm undo?'} cls="btn-light onb-unskip" askCls="btn-light onb-skip-arm"
        onConfirm=${() => op({ ...args, confirmed: true })} />`
      : html`<button class="btn-light onb-unskip" onClick=${() => op(args)}>${label}</button>`;
  };
  return html`<section class=${'onb board' + (ob.frozen ? ' onb-frozen' : '')} aria-label="Onboarding Checklist">
    <div class="onb-h">
      <b>Onboarding Checklist</b>
      ${done ? html`<button class="onb-fold onb-fold-h" aria-expanded="true" onClick=${toggle}><span class="onb-mark onb-ok">✓</span>${doneText}
        <span class="onb-more">Collapse</span></button>`
        : head.ok ? html`<span class="onb-conn"><span class="onb-mark onb-ok">✓</span>Robot connected</span>`
        : html`<span class="onb-conn onb-conn-bad"><${Badge} kind=${head.kind === 'servo' || head.kind === 'connecting' || head.kind === 'bench' ? 'warn' : 'bad'} />
          ${head.text}${head.raw ? html`<small>Raw: ${head.raw}</small>` : null}</span>`}
      ${ob.frozen ? html`<span class="onb-tip onb-noskip">${ONB_FROZEN}</span>` : null}
    </div>
    ${msg ? html`<div class=${'banner ' + (msg.ok ? 'warn' : 'bad')} role="status"><span>${msg.text}</span>
      <a href="#" onClick=${(e) => { e.preventDefault(); setMsg(null); }}>Dismiss</a></div>` : null}
    <ol class="onb-rows">
      ${(ob.rows || []).map((r) => {
        const now = ob.next === r.key;
        const when = fmtWhen(r.when);
        const goA = { do: r.go, label: 'Start' };
        const isCommit = r.key === 'commit' && (r.state === 'todo' || r.state === 'no' || copen || !!job || !!cmsg || !!cprev);
        let acts;
        if (r.marked) {
          acts = html`<button class="btn-light onb-redo" onClick=${() => go(goA, r)}>Redo</button>${undoBtn(r)}`;
        } else if (r.state === 'ok') acts = html`<button class="btn-light onb-redo" onClick=${() => go(goA, r)}>Redo</button>`;
        else {
          const first = (r.actions || []).length ? r.actions : [goA];
          acts = html`${first.map((a, i) => btn(r.found && i === 0 ? { ...a, label: 'Redo' }
            : now && i === 0 ? { ...a, label: 'Start' } : a, r, now && i === 0))}${markBtn(r, 'done', 'Done', 'onb-donebtn')}${markBtn(r, 'skip', 'Skip', 'onb-skip')}`;
        }
        return html`<li class=${'onb-row onb-' + r.state + (now ? ' onb-now' : '')} key=${r.key} data-step=${r.key}>
          <span class=${'onb-mark onb-' + r.state} aria-label=${ONB_WORD[r.state] || 'Unavailable'}>${ONB_MARK[r.state] || '?'}</span>
          <span class="onb-body">
            <span class="onb-title"><b>${r.n} ${r.title}</b>${now ? html`<span class="onb-tag">Next</span>` : null}${when && !r.marked ? html`<span class="onb-when num">${when}</span>` : null}</span>
            <span class="onb-text">${r.text}${r.marked ? ` (${when || 'time unknown'})` : ''}</span>
            ${r.note ? html`<span class="onb-tip">${r.marked ? `Current: ${r.note}` : r.note}</span>` : null}
            ${r.tip && r.state !== 'ok' && r.state !== 'skip' ? html`<span class="onb-tip">Note: ${r.tip}</span>` : null}
            ${isCommit ? html`<div class="onb-commit">
              <div class="onb-commit-desc">${r.desc || ''}</div>
              ${(r.commit?.items || []).length ? html`<ul class="onb-checks">
                ${r.commit.items.map((it) => html`<li class=${it.ok ? 'ok' : 'bad'}>
                  <span class="onb-check-mark" aria-label=${it.ok ? 'OK' : 'Pending'}>${it.ok ? '✓' : '×'}</span>
                  <span class="onb-check-body"><b>${it.label}</b><span>${it.text}</span></span>
                  ${!it.ok && it.action ? html`<button class="btn-light onb-check-go" onClick=${() => go(it.action, r)}>${it.action.label || 'Go'}</button>` : null}
                </li>`)}
              </ul>` : null}
              <div class="onb-commit-acts">
                <button class="btn-light" disabled=${cbusy} onClick=${commitPreview}>${cprev ? 'Hide Preview' : 'Preview Config'}</button>
                <${TwoStep} label="Commit to robotd" ask=${r.commit?.imu_default ? 'Confirm? IMU will use default orientation' : 'Restarts robotd & relaxes servos — support the robot'}
                  cls="btn-dark" disabled=${!r.commit?.ready || cbusy}
                  title=${!r.commit?.ready ? 'Complete all prerequisites above first' : ''} onConfirm=${commitRun} />
              </div>
              ${cmsg ? html`<div class=${'onb-commit-msg ' + (cmsg.ok ? 'ok' : 'bad')}>${cmsg.text}</div>` : null}
              ${job && job.phase === 'running' ? html`<div class="onb-commit-job" role="status"><span class="onb-spin">⋯</span>${job.msg || 'Writing…'}</div>` : null}
              ${job && job.phase !== 'running' && job.reason ? html`<div class=${'onb-commit-msg ' + (job.ok ? 'ok' : 'bad')}>${job.reason}</div>` : null}
              ${cprev ? html`<div class="onb-preview">
                <div class="onb-preview-imu"><b>IMU</b> ${cprev.imu?.label || ''} · ${cprev.imu?.text || ''}${cprev.imu?.default ? ` (default: ${cprev.imu.default})` : ''}</div>
                <table class="onb-preview-table"><thead><tr><th>Joint</th><th>Zero</th><th>Dir</th><th>Limits</th></tr></thead><tbody>
                  ${(cprev.joints || []).map((j) => html`<tr><td>${j.en || j.name}</td><td class="num">${j.zero_raw}</td><td>${j.direction > 0 ? '+1' : '−1'}</td><td class="num">${j.min_deg == null ? 'Unset' : `${j.min_deg}° ~ ${j.max_deg}°`}</td></tr>`)}
                </tbody></table>
                <details class="onb-preview-toml"><summary>Generated Config</summary><pre>${cprev.toml || ''}</pre></details>
              </div>` : null}
            </div>` : null}
          </span>
          <span class="onb-acts">${acts}</span>
        </li>`;
      })}
    </ol>
  </section>`;
}

const TODO_IN_ONB = {
  starting: (ob) => ['starting', 'stuck_calib'].includes(ob.header?.kind),
  absent: (ob) => ob.header?.kind === 'absent',
  servo_session: (ob) => ob.header?.kind === 'servo',
  imu_unverified: (ob) => ['todo', 'no'].includes((ob.rows || []).find((r) => r.key === 'imu')?.state),
};
function Todo({ st, live }) {
  const ob = st?.onboarding;
  const all = (st?.todo || []).filter((it) => !(ob && TODO_IN_ONB[it.key] && TODO_IN_ONB[it.key](ob)));
  const items = all.slice(0, 2);
  const more = all.length - items.length;
  if (!live) return html`<section class="todo board"><span class="quiet">Checks paused while offline</span></section>`;
  if (!items.length) {
    return html`<section class="todo board"><span class="quiet">No active issues</span>
      <span class="when num">${st?.health_age_ms != null ? `Checked ${Math.round(st.health_age_ms / 1000)}s ago` : ''}</span></section>`;
  }
  return html`<section class="todo board list">
    ${items.map((it) => html`<div class="item">
      <${Badge} kind=${it.level === 'error' ? 'bad' : 'warn'} />
      <span class="msg">${it.msg}</span>
      <span class="hint">${it.hint || ''}</span>
    </div>`)}
    ${more > 0 ? html`<a href="#/logs" style="font-size:13px">${more} more ›</a>` : null}
  </section>`;
}

function Overview({ st, live, age, diagnosticElapsed, duck, pd, onSame, tune, sensorsActive, sv, imu, connectionLive, svElapsed, imuElapsed }) {
  const short = duck?.short || 'Microduck';
  const name = /[A-Za-z0-9]$/.test(short) ? short + ' ' : short;
  return html`<div class="overview">
    ${sensorsActive ? html`<${UpdateNotice} api=${api} />` : null}
    ${st?.net?.known && st.net.mode === 'hotspot'
      ? html`<${WifiCard} net=${st.net} link=${link} secure=${st?.auth?.has_password} />` : null}
    <${Stage} st=${st} live=${live} age=${age} name=${name} />
    <${Band} st=${st} pd=${pd} live=${live} diagnosticElapsed=${diagnosticElapsed} gray=${age > STALE_GRAY_MS} onSame=${onSame} sensorsActive=${sensorsActive} />
    <${AiSshEntry} />
    <${Onboard} st=${st} live=${live} />
    <${Todo} st=${st} live=${live} />
    <${PadPage} st=${st} pad=${pd} link=${link} api=${api} previewActive=${sensorsActive} live=${live} />
    <${RuntimeDiagnostics} st=${st} sv=${sv} imu=${imu} link=${link} active=${sensorsActive}
      live=${connectionLive} summaryLive=${live} svElapsed=${svElapsed} imuElapsed=${imuElapsed} />
    <${TuneSection} tune=${tune} />
  </div>`;
}

// ---------------- Logs ----------------

function Logs() {
  const [items, setItems] = useState([]);
  const [onlyBad, setOnlyBad] = useState(false);
  const [cat, setCat] = useState('');
  useEffect(() => {
    api('/api/logs?limit=300').then((r) => setItems(r.items || []));
    return link.on((m) => { if (m.t === 'log') setItems((xs) => [...xs.slice(-499), m.entry]); });
  }, []);
  const shown = items.filter((e) => (!onlyBad || e.level !== 'info') && (!cat || e.cat === cat));
  const cats = [...new Set(items.map((e) => e.cat))];
  const copy = () => navigator.clipboard && navigator.clipboard.writeText(shown.map((e) =>
    `${e.t} [${e.cat}]${e.level === 'info' ? '' : `[${e.level}]`} ${e.msg}${e.step ? ` · ${e.level === 'info' ? 'Step' : 'At'}: ${e.step}` : ''}${e.raw ? ` · Raw: ${e.raw}` : ''}${e.hint ? ` · Hint: ${e.hint}` : ''}`).join('\n'));
  return html`<div style="display:flex;flex-direction:column;gap:12px">
    <div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px">
      <h1 style="margin:0;font-size:28px;font-weight:600;flex:1">Logs</h1>
      <select value=${cat} onChange=${(e) => setCat(e.target.value)} aria-label="Filter by category">
        <option value="">All Categories</option>${cats.map((c) => html`<option value=${c}>${c}</option>`)}
      </select>
      <label style="font-size:14px"><input type="checkbox" checked=${onlyBad} onChange=${(e) => setOnlyBad(e.target.checked)} /> Warnings & Errors only</label>
      <button onClick=${copy} class="btn-light">Copy</button>
      <a class="logs-export" href="/api/diag" style="font-size:14px" title="Includes serial number and LAN IP">Export Diagnostics</a>
    </div>
    <div class="board" style="padding:8px 16px;font-size:14px;line-height:1.7">
      ${shown.length ? shown.slice().reverse().map((e) => html`<div key=${e.n} style="padding:4px 0;border-top:1px solid var(--line)">
        <span class="num" style="color:var(--text-2)">${e.t}</span> <span style="color:var(--text-2)">[${e.cat}]</span>
        ${e.level === 'warn' ? html` <${Badge} kind="warn" />` : e.level === 'error' ? html` <${Badge} kind="bad" />` : ''} ${e.msg}
        ${e.step || e.raw || e.hint ? html`<div style="font-size:13px;color:var(--text-2)">
          ${e.step ? html`${e.level === 'info' ? 'Step' : 'At'}: ${e.step}  ` : ''}${e.raw ? html`Raw: ${e.raw}  ` : ''}${e.hint ? html`Hint: ${e.hint}` : ''}</div>` : null}
      </div>`) : html`<div style="color:var(--text-2)">No log entries yet.</div>`}
    </div>
  </div>`;
}

class LogsPage extends Component {
  shouldComponentUpdate() { return false; }
  render() { return html`<${Logs} />`; }
}

// ---------------- Settings ----------------

const STAGES = [['mid', 'Obsidian (Default)'], ['graphite', 'Graphite'], ['light', 'Warm Paper']];
function applyStage(v) {
  if (v === 'mid') document.documentElement.removeAttribute('data-stage');
  else document.documentElement.setAttribute('data-stage', v);
}
function loadStage() { try { return localStorage.getItem('studio.stage') || 'mid'; } catch { return 'mid'; } }

async function refreshAccessSession() {
  const session = await api('/api/session');
  if (session.status !== 200 || !session.csrf) throw new Error('session unavailable');
  csrf = session.csrf;
  link.reconnect();
}

function Settings({ st, tune, theme, onThemeChange }) {
  const [v, setV] = useState(loadStage());
  const pick = (x) => { setV(x); applyStage(x); try { localStorage.setItem('studio.stage', x); } catch { /* private mode */ } };
  return html`<div style="display:flex;flex-direction:column;gap:16px;max-width:640px">
    <h1 style="margin:0;font-size:28px;font-weight:600">Settings</h1>
    <${AiSshEntry} />
    <${UpdateSettings} api=${api} csrf=${csrf} />
    <${AiSettings} api=${api} />
    <${MicrophoneSettings} api=${api} />
    <div class="board" style="padding:16px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center">
      <span style="font-size:15px;font-weight:600;flex:1">Logs & Diagnostics</span>
      <a class="settings-logs" href="#/logs" style="font-size:15px">Logs</a>
      <a class="settings-diag" href="/api/diag" style="font-size:15px" title="Includes serial number and LAN IP">Export Diagnostics</a>
    </div>
    <${DuckAddresses} net=${st?.net} />
    <${WifiSettings} net=${st?.net} link=${link} />
    <${AccessGuide} api=${api} hasPassword=${st?.auth?.has_password} net=${st?.net} refreshSession=${refreshAccessSession} />
    <${BatterySettings} link=${link} st=${st} />
    <${ModelSettings} tune=${tune} link=${link} />
    <div class="board" style="padding:16px 20px">
      <div style="font-size:15px;font-weight:600;margin-bottom:10px">Appearance & Theme</div>
      <div style="display:flex;gap:10px;margin-bottom:12px">
        <button type="button" class=${theme === 'dark' ? 'btn-dark' : 'btn-light'} onClick=${() => onThemeChange('dark')}>Obsidian Dark</button>
        <button type="button" class=${theme === 'light' ? 'btn-dark' : 'btn-light'} onClick=${() => onThemeChange('light')}>Warm Cream Light</button>
      </div>
      <div style="font-size:14px;font-weight:600;margin-bottom:6px">3D Stage Backdrop</div>
      ${STAGES.map(([k, label]) => html`<label style="display:flex;align-items:center;gap:10px;height:36px">
        <input type="radio" name="stage" checked=${v === k} onChange=${() => pick(k)} /> ${label}</label>`)}
      <div style="font-size:13px;color:var(--text-2);margin-top:4px">Stored locally on this browser.</div>
    </div>
    <button class="btn-light" onClick=${async () => { await api('/api/logout', {}); location.reload(); }}
      style="align-self:flex-start">Sign Out</button>
  </div>`;
}

function Later({ what }) {
  return html`<div class="board" style="padding:24px"><h1 style="margin:0 0 8px;font-size:28px;font-weight:600">${what}</h1>
    <p style="margin:0;color:var(--text-2)">This page is not available yet.</p></div>`;
}

// ---------------- Shell ----------------

const OVERVIEW = (r) => r === '/' || r === '/pad';
function navCur(route, padInView) {
  return (r) => {
    if (r === '/settings') return route === r || route.startsWith(r + '/') ? 'page' : undefined;
    if (r === '/pad') return OVERVIEW(route) && padInView ? 'page' : undefined;
    if (r === '/') return OVERVIEW(route) && !padInView ? 'page' : undefined;
    if (r === '/actions') return route === '/actions' || route === '/models' ? 'page' : undefined;
    return route === r ? 'page' : undefined;
  };
}

function Nav({ route, padInView, onSame, theme, onToggleTheme }) {
  const cur = navCur(route, padInView);
  const a = (href, r, text) => html`<a href=${href} aria-current=${cur(r)} onClick=${(e) => onSame(e, href)}>${text}</a>`;
  return html`<nav class="nav" aria-label="Primary navigation">
    <div class="nav-logo" title="Armature × Microduck Studio">
      <${ArmatureMark} size=${22} />
      <span class="nav-brand">Armature <span class="nav-sep">×</span> Studio</span>
    </div>
    ${a('#/', '/', 'Overview')}
    ${a('#/pad', '/pad', 'Gamepad')}
    ${a('#/camera', '/camera', 'Camera')}
    ${a('#/tof', '/tof', 'ToF 3D')}
    ${a('#/servos', '/servos', 'Servos')}
    ${a('#/imu', '/imu', 'IMU')}
    ${a('#/actions', '/actions', 'Actions')}
    ${a('#/logs', '/logs', 'Logs')}
    <span class="spacer"></span>
    <button type="button" class="theme-toggle" onClick=${onToggleTheme} title=${theme === 'dark' ? 'Switch to Light theme' : 'Switch to Dark theme'}>
      ${theme === 'dark' ? '☀ Light' : '☾ Dark'}
    </button>
    ${a('#/settings', '/settings', 'Settings')}
  </nav>`;
}

function TabBar({ route, padInView, onSame }) {
  const cur = navCur(route, padInView);
  const a = (href, r, text) => html`<a href=${href} aria-current=${cur(r)} onClick=${(e) => onSame(e, href)}>${text}</a>`;
  return html`<nav class="tabbar" aria-label="Mobile navigation">
    ${a('#/', '/', 'Overview')}${a('#/pad', '/pad', 'Pad')}${a('#/camera', '/camera', 'Cam')}${a('#/tof', '/tof', 'ToF')}${a('#/servos', '/servos', 'Servos')}${a('#/imu', '/imu', 'IMU')}${a('#/actions', '/actions', 'Actions')}${a('#/settings', '/settings', 'More')}
  </nav>`;
}

export const leaveGuards = {};
function useRoute() {
  const get = () => (location.hash.replace(/^#/, '') || '/');
  const [r, setR] = useState(get());
  const cur = useRef(r);
  useEffect(() => {
    const f = () => {
      const next = get();
      const guard = leaveGuards[cur.current];
      if (next !== cur.current && guard && !guard()) { history.replaceState(null, '', '#' + cur.current); return; }
      cur.current = next;
      padActivity.setRoute(next);
      setR(next);
      link.heartbeat();
    };
    window.addEventListener('hashchange', f);
    return () => window.removeEventListener('hashchange', f);
  }, []);
  return r;
}

function scrollFor(route) {
  if (route === '/settings/ssh' || route === '/settings/updates') {
    requestAnimationFrame(() => {
      const section = document.getElementById(route === '/settings/ssh' ? 'ssh-guide' : 'software-updates');
      section?.scrollIntoView({ block: 'start' });
      section?.focus({ preventScroll: true });
    });
    return;
  }
  if (route === '/pad') {
    const el = document.getElementById('pad-block');
    if (el) { el.scrollIntoView({ block: 'start' }); return; }
  }
  window.scrollTo(0, 0);
}

const PAD_HEAVY = ['skills', 'actions', 'combos', 'sounds'];

function Main({ duck }) {
  const route = useRoute();
  const [st, setSt] = useState(null);
  const [sv, setSv] = useState(null);
  const [imu, setImu] = useState(null);
  const [tune, setTune] = useState(null);
  const [pd, setPd] = useState(null);
  const [stAt, setStAt] = useState(0);
  const [svAt, setSvAt] = useState(0);
  const [imuAt, setImuAt] = useState(0);
  const imuContext = useRef(null);
  const [status, setStatus] = useState(link.status);
  const [padInView, setPadInView] = useState(padActivity.inView);
  const [navTick, setNavTick] = useState(0);
  const [theme, setTheme] = useState(getTheme);
  const firstScroll = useRef(route === '/pad');
  const now = useNow(250);
  const barRef = useStickyHeight('--bar-h');

  const changeTheme = (t) => setTheme(applyTheme(t));
  const toggleTheme = () => changeTheme(theme === 'dark' ? 'light' : 'dark');

  useEffect(() => {
    const off = link.on((m) => {
      if (m.t === '_status') {
        setStatus(m.status);
        if (m.status !== 'open') { setImu(null); setImuAt(0); }
      }
      else if (m.t === 'state') {
        const context = JSON.stringify([m.mode, m.build, m.imu?.source, m.imu?.configured_imu_profile,
          m.posture === 'starting', m.imu?.context_current, m.imu?.generation]);
        if (imuContext.current !== null && imuContext.current !== context) { setImu(null); setImuAt(0); }
        imuContext.current = context;
        setSt((prev) => (m.onboarding_same && prev ? { ...m, onboarding: prev.onboarding } : m));
        setStAt(performance.now());
      }
      else if (m.t === 'servo') { setSv(m); setSvAt(performance.now()); }
      else if (m.t === 'imu') { setImu(m); setImuAt(performance.now()); }
      else if (m.t === 'tune') setTune(m);
      else if (m.t === 'pad') {
        setPd((prev) => {
          if (!m.lists_same || !prev?.motion) return m;
          const keep = Object.fromEntries(PAD_HEAVY.filter((k) => k in prev.motion).map((k) => [k, prev.motion[k]]));
          return { ...m, motion: { ...m.motion, ...keep } };
        });
      }
    });
    const offAct = padActivity.subscribe(({ inView }) => setPadInView(inView));
    const stop = () => { firstScroll.current = false; };
    window.addEventListener('wheel', stop, { passive: true, once: true });
    window.addEventListener('touchstart', stop, { passive: true, once: true });
    window.addEventListener('keydown', stop, { once: true });
    link.connect();
    return () => { off(); offAct(); };
  }, []);

  useEffect(() => { scrollFor(route); }, [route, navTick]);
  useEffect(() => {
    if (firstScroll.current && st && pd && route === '/pad') {
      firstScroll.current = false;
      requestAnimationFrame(() => scrollFor('/pad'));
    }
  }, [!!st, !!pd]);
  const onSame = (e, href) => {
    if ((location.hash || '#/') === href) { e.preventDefault(); setNavTick((n) => n + 1); }
  };

  if (status === 'auth') { location.reload(); return null; }
  const age = st ? (now - stAt) + (st.robotd_age_ms || 0) : Infinity;
  const live = status === 'open' && age < STALE_ALARM_MS && st?.robotd_ok !== false;
  const overview = html`<div hidden=${!OVERVIEW(route)}><${Overview} st=${st} live=${live} age=${age} diagnosticElapsed=${now - stAt} duck=${duck} pd=${pd} onSame=${onSame} tune=${tune} sensorsActive=${OVERVIEW(route)}
    sv=${sv} imu=${imu} connectionLive=${status === 'open'} svElapsed=${svAt ? Math.max(0, now - svAt) : Infinity} imuElapsed=${imuAt ? Math.max(0, now - imuAt) : Infinity} /></div>`;
  let page = null;
  if (OVERVIEW(route)) page = null;
  else if (route === '/logs') page = html`<${LogsPage} />`;
  else if (route === '/camera') page = html`<${CameraPage} api=${api} />`;
  else if (route === '/tof') page = html`<${TofPage} api=${api} />`;
  else if (route === '/servos') page = html`<${ServoPage} sv=${sv} link=${link} leaveGuards=${leaveGuards} />`;
  else if (['/settings', '/settings/ssh', '/settings/updates'].includes(route)) page = html`<${Settings} st=${st} tune=${tune} theme=${theme} onThemeChange=${changeTheme} />`;
  else if (route === '/models') page = html`<${ModelsPage} api=${api} csrf=${csrf} link=${link} live=${live} />`;
  else if (route === '/actions') page = html`<${ActionsPage} api=${api} csrf=${csrf} link=${link} plazaUrl="https://fanhao375.github.io/microduck-replica/motion-plaza/" />`;
  else if (route === '/imu') page = html`<${ImuPage} st=${st}
    imu=${imu && st?.imu?.diagnostic ? { ...imu, diagnostic: st.imu.diagnostic } : imu}
    link=${link} live=${live} age=${age} diagnosticElapsed=${now - (st?.imu?.diagnostic ? stAt : imuAt)} pd=${pd} />`;
  else page = html`<${Later} what="Page" />`;

  document.title = `${duck?.label || 'Microduck'} · Armature Studio`;
  return html`<div class="shell">
    <${Nav} route=${route} padInView=${padInView} onSame=${onSame} theme=${theme} onToggleTheme=${toggleTheme} />
    <main class="main">
      <${TopBar} duck=${duck} st=${st} live=${live} age=${age} open=${status === 'open'}
        hideEStop=${route === '/servos' && sv?.state === 'open'} imu=${route === '/imu' ? imu : null} />
      <div class="sticky-bar" ref=${barRef}><${ServoBar} st=${st} onServos=${route === '/servos'} /></div>
      ${overview}${page}
    </main>
    <${TabBar} route=${route} padInView=${padInView} onSame=${onSame} />
  </div>`;
}

function Login({ onDone, duck, noPassword }) {
  const [pw, setPw] = useState('');
  const [remember, setRemember] = useState(true);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    let r;
    try { r = await api('/api/login', { password: pw, remember }); } catch { r = { error: 'Cannot reach robot — check network' }; }
    setBusy(false);
    if (r.ok) onDone(); else setErr(r.error || 'Sign-in failed');
  };
  return html`<form class="login board" onSubmit=${submit} autocomplete="on">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
      <${ArmatureMark} size=${26} />
      <h1 style="margin:0">${duck?.label || 'Armature × Microduck Studio'}</h1>
    </div>
    <input type="text" name="username" autocomplete="username" value="admin" hidden readOnly />
    <label>Admin Password
      <input type="password" name="password" autocomplete="current-password" value=${pw} onInput=${(e) => setPw(e.target.value)} required autoFocus />
    </label>
    <label class="row"><input type="checkbox" checked=${remember} onChange=${(e) => setRemember(e.target.checked)} /> Remember me (30 days)</label>
    ${noPassword ? html`<div class="err">No admin password set. Run on robot: <code>python3 -I run.py set-password</code></div>` : null}
    ${err ? html`<div class="err" role="alert">${err}</div>` : null}
    <button type="submit" disabled=${busy || !pw}>Sign In</button>
  </form>`;
}

function App() {
  const [sess, setSess] = useState(undefined);
  const [noPw, setNoPw] = useState(false);
  const [down, setDown] = useState(false);
  const load = () => api('/api/session')
    .then((r) => { setDown(false); csrf = r.csrf || ''; setNoPw(r.has_password === false); setSess(r.status === 200 ? r : null); })
    .catch(() => { setDown(true); setTimeout(load, 2000); });
  useEffect(() => { applyTheme(getTheme()); applyStage(loadStage()); load(); }, []);
  if (sess === undefined) {
    return down ? html`<div class="login board"><h1>Armature × Microduck Studio</h1><p>Cannot reach Studio on robot, retrying…</p></div>` : null;
  }
  if (!sess) return html`<${Login} onDone=${load} duck=${null} noPassword=${noPw} />`;
  return html`<${Main} duck=${sess.duck} />`;
}

render(html`<${App} />`, document.getElementById('app'));
