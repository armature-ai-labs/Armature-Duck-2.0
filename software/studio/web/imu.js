// IMU Calibration & Diagnostics page: live attitude, chip profile selection, coarse & full standing calibration.
// Backend: studio/imu.py & studio/imu_calib.py.
import { html, useState, useEffect, useLayoutEffect, useRef } from './vendor/preact-htm.module.js';
import { Duck3D } from './duck3d.js';
import { TwoStep } from './confirm.js';
import { imuDiagnosticCopy } from './startup-status.js';

const RULES = {
  // ---- Must match studio/imu_calib.py RULES verbatim (verified by E2E tests) ----
  still_min_n: 15,
  still_max_spread_deg: 1.5,
  level_deg: 5,
  ref_min_n: 5,
  ref_spread_deg: 1.5,
  hold_band_deg: 1.8,
  // ---- Frontend-only thresholds (stricter than backend) ----
  still_s: 3.0,
  hold_min_n: 8,
  hold_s: 1.0,
  tilt_go_deg: 12,
  tilt_target_deg: 15,
  tilt_max_deg: 30,
  dir_max_deg: 35,
  stale_ms: 1000,
  dedupe_ms: 40,
  max_step_n: 600,
};

const STEPS = ['still', 'fwd', 'back', 'left', 'right'];
const TILTS = ['fwd', 'back', 'left', 'right'];
// dir: gravity direction relative to body +X (CCW degrees). Used for directional guidance only.
const STEP = {
  still: { cn: 'Still', short: 'Still' },
  fwd: { cn: 'Pitch Forward', short: 'Fwd', dir: 0, how: 'nose down' },
  back: { cn: 'Pitch Back', short: 'Back', dir: 180, how: 'tail down' },
  left: { cn: 'Roll Left', short: 'Left', dir: 90, how: 'robot left side down' },
  right: { cn: 'Roll Right', short: 'Right', dir: -90, how: 'robot right side down' },
};
const DIRV = { fwd: [1, 0, 0], back: [-1, 0, 0], left: [0, 1, 0], right: [0, -1, 0] };

const CHIP_CN = { bus: 'Bus IMU (ID 200)', bno08x: 'BNO085 (I²C)', lsm6dsv16x: 'LSM6DSV16X (I²C)' };
const BUS_BOARD_CN = { ft6: 'FT6', imu_to_dxl: 'IMU-to-DXL', sflp_124_12: 'SFLP 124/12', qinmo: 'Qinmo (Configured)' };
const BUS_PROTOCOL_CN = { feetech: 'Feetech', dxl2: 'Dynamixel 2.0' };
const unsupportedProfile = info => info && info.profile !== undefined && !['legacy', 'qinmo'].includes(info.profile);
const unsupportedTransport = info => info && (!['feetech', 'dxl2'].includes(info.protocol) || unsupportedProfile(info));
function chipLabel(imu, live = true, diagnosticElapsed = 0) {
  if (imu?.source !== 'bus') return CHIP_CN[imu?.source] || imu?.source || '—';
  if (live === false) return CHIP_CN.bus;
  const diagnostic = imuDiagnosticCopy(imu?.diagnostic, live, diagnosticElapsed);
  if (diagnostic.board) return diagnostic.board;
  const info = imu.imu_bus;
  if (imu?.profile_error || unsupportedTransport(info)) return 'IMU Protocol / Profile Unconfirmed';
  return info ? `${BUS_BOARD_CN[info.board] || info.board || 'Bus IMU'} · ${BUS_PROTOCOL_CN[info.protocol] || info.protocol} (ID 200)`
    : CHIP_CN.bus;
}
function imuContext(mode, imu) {
  return JSON.stringify([mode, imu?.source, imu?.source === 'bus' ? imu.imu_bus || null : null,
    imu?.configured_imu_profile ?? null, imu?.metadata_error ?? null, imu?.profile_error ?? null,
    imu?.redo?.session_id || null, !!imu?.redo?.active]);
}

// ---------------- Vector Math (matches imu_calib.py) ----------------

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = (a) => Math.sqrt(dot(a, a));
const unit = (a) => { const n = len(a); return Number.isFinite(n) && n > 1e-9 ? [a[0] / n, a[1] / n, a[2] / n] : null; };
const deg = (r) => r * 180 / Math.PI;
const rad = (d) => d * Math.PI / 180;
const angle = (a, b) => { const ua = unit(a), ub = unit(b); return ua && ub ? deg(Math.acos(Math.max(-1, Math.min(1, dot(ua, ub))))) : null; };
const meanDir = (vs) => (vs.length ? unit(vs.reduce((s, v) => [s[0] + v[0], s[1] + v[1], s[2] + v[2]], [0, 0, 0])) : null);
const spread = (vs) => { const m = meanDir(vs); return m ? Math.max(0, ...vs.map((v) => angle(v, m) ?? 0)) : 0; };
const spreadP90 = (vs) => {
  const m = meanDir(vs);
  if (!m) return 0;
  const ds = vs.map((v) => angle(v, m) ?? 0).sort((a, b) => a - b);
  return ds[Math.floor(0.9 * (ds.length - 1))];
};
const trimmedMeanDir = (vs) => {
  const m = meanDir(vs);
  if (!m) return null;
  const lim = spreadP90(vs);
  return meanDir(vs.filter((v) => (angle(v, m) ?? 0) <= lim + 1e-9)) || m;
};
const okVec = (g) => Array.isArray(g) && g.length === 3 && g.every((x) => typeof x === 'number' && Number.isFinite(x));
const DOWN = [0, 0, -1];

/** Tilt angle (deg) and azimuth relative to g0 (CCW from +X). Matches imu_calib.tilt_of. */
function tiltOf(g0, g) {
  const t = angle(g0, g);
  const up = [-g0[0], -g0[1], -g0[2]];
  const d = sub(g, g0);
  const h = sub(d, up.map((u) => u * dot(d, up)));
  const x0 = sub([1, 0, 0], up.map((u) => u * up[0]));
  if (!unit(h) || !unit(x0)) return { t, d: null };
  const a = unit(x0), b = unit(h);
  return { t, d: deg(Math.atan2(dot(cross(a, b), up), dot(a, b))) };
}

function qRotate(q, v) {
  const [w, x, y, z] = q;
  const r = [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
    [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
    [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]];
  return r.map((row) => dot(row, v));
}

const fmt = (v, d = 1) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(d) : '—');
const sgn = (v, d = 1) => (typeof v === 'number' && Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(d)}` : '—');
const dirDiff = (a, b) => Math.abs(((a - b) % 360 + 540) % 360 - 180);
const names = (ks) => ks.map((k) => STEP[k].cn).join(', ');

// ---------------- Hold & Reference Detection (matches imu_calib.py) ----------------

function runBack(gs, end, band) {
  const last = gs[end];
  const idx = [end];
  let skipped = 0, j = end - 1;
  while (j >= 0) {
    if (angle(gs[j], last) <= band) { idx.push(j); j--; }
    else if (j - 1 >= 0 && angle(gs[j - 1], last) <= band) { skipped++; j--; }
    else break;
  }
  return { idx: idx.sort((a, b) => a - b), skipped };
}

function findHold(gs) {
  if (!gs.length) return { idx: [], skipped: 0 };
  let best = runBack(gs, gs.length - 1, RULES.hold_band_deg);
  if (gs.length >= 2) {
    const alt = runBack(gs, gs.length - 2, RULES.hold_band_deg);
    if (alt.idx.length > best.idx.length + 1) best = alt;
  }
  return best;
}

function findRef(gs, g0, before) {
  const n = RULES.ref_min_n;
  for (let end = before; end >= n; end--) {
    const win = gs.slice(end - n, end);
    if (win.every((v) => (angle(g0, v) ?? 0) <= RULES.level_deg)) {
      const m = meanDir(win);
      if (m && spread(win) <= RULES.ref_spread_deg) return m;
    }
  }
  return null;
}

// ---------------- Sample Collection Wizard ----------------

function freshWizard() {
  return { phase: 'idle', t0: 0, g0: null,
    buf: { still: [], fwd: [], back: [], left: [], right: [] },
    done: { still: false, fwd: false, back: false, left: false, right: false },
    seg: [],
    ref: null, level: 0, cand: null, inZone: false, repeat: null, needLevel: false,
    live: null, progress: 0, hold: 0, paused: null, moved: false, over: false,
    lastKey: null, lastBirth: null, events: [], missing: [], result: null, error: null };
}

const missingSteps = (W) => STEPS.filter((k) => !W.done[k]);
const nextTodo = (W) => TILTS.find((k) => !W.done[k]) || null;

function feed(W, st, live) {
  if (W.phase !== 'collect') return false;
  const g = st?.gravity;
  const fresh = live !== false && st && st.joints_age_ms != null && st.joints_age_ms < RULES.stale_ms;
  if (!okVec(g) || !fresh) { const p = W.paused; W.paused = 'Telemetry paused — waiting for connection (samples preserved)'; return p !== W.paused; }
  const gu = unit(g);
  if (!gu) return false;
  const birth = performance.now() - st.joints_age_ms;
  if (st.state_t_ns != null) {
    if (st.state_t_ns === W.lastKey) return false;
    W.lastKey = st.state_t_ns;
  } else {
    if (W.lastBirth != null && Math.abs(birth - W.lastBirth) < RULES.dedupe_ms) return false;
    W.lastBirth = birth;
  }
  W.paused = null;
  const s = { t: +(birth / 1000 - W.t0).toFixed(3), g: gu.map((x) => +x.toFixed(6)) };
  if (!W.done.still) {
    const b = W.buf.still;
    b.push(s);
    if (spreadP90(b.map((x) => x.g)) > RULES.still_max_spread_deg) { W.buf.still = [s]; W.moved = true; }
    const b2 = W.buf.still;
    W.progress = Math.min(1, (b2[b2.length - 1].t - b2[0].t) / RULES.still_s);
    if (W.progress >= 1 && b2.length >= RULES.still_min_n) {
      W.g0 = trimmedMeanDir(b2.map((x) => x.g));
      W.done.still = true; W.moved = false; W.progress = 0; W.seg = []; W.ref = null;
      finishStep(W, 'still');
    }
    return true;
  }
  const b = W.seg;
  b.push(s);
  if (b.length > RULES.max_step_n) b.splice(0, b.length - RULES.max_step_n);
  const gs = b.map((x) => x.g);
  const n = RULES.ref_min_n;
  const tail = gs.slice(-n);
  const levelNow = tail.length === n && tail.every((v) => angle(W.g0, v) <= RULES.level_deg) && spread(tail) <= RULES.ref_spread_deg;
  if (levelNow) W.ref = meanDir(tail);
  let k = 0;
  for (let i = gs.length - 1; i >= 0 && k < n && angle(W.g0, gs[i]) <= RULES.level_deg; i--) k++;
  W.level = levelNow ? 1 : k / n;
  W.live = tiltOf(W.ref || W.g0, gu);
  W.over = W.live.t > RULES.tilt_max_deg;
  W.hold = 0; W.inZone = false; W.cand = null; W.repeat = null; W.needLevel = false;
  const { idx } = findHold(gs);
  const hold = idx.map((i) => b[i]);
  const hm = meanDir(hold.map((x) => x.g));
  if (!hm) return true;
  const href = findRef(gs, W.g0, idx[0]);
  const ht = tiltOf(href || W.g0, hm);
  if (!(ht.t >= RULES.tilt_go_deg && ht.t <= RULES.tilt_max_deg) || ht.d == null) return true;
  const dirK = TILTS.find((x) => dirDiff(ht.d, STEP[x].dir) <= RULES.dir_max_deg) || null;
  W.cand = dirK;
  if (!dirK) return true;
  if (W.done[dirK]) { W.repeat = dirK; return true; }
  if (!href) { W.needLevel = true; return true; }
  W.inZone = true;
  const span = hold.length > 1 ? hold[hold.length - 1].t - hold[0].t : 0;
  W.hold = Math.min(1, (span + 1e-6) / RULES.hold_s);
  if (hold.length >= RULES.hold_min_n && W.hold >= 1) {
    W.buf[dirK] = b.slice();
    W.done[dirK] = true;
    W.seg = []; W.ref = null; W.hold = 0; W.inZone = false; W.cand = null;
    finishStep(W, dirK);
  }
  return true;
}

function finishStep(W, k) {
  if (!missingSteps(W).length) { W.phase = 'solving'; W.events.push('all'); }
  else W.events.push(k);
}

function startCollect(W, now) {
  Object.assign(W, freshWizard(), { phase: 'collect', t0: now });
}

function endCollect(W) {
  if (W.phase !== 'collect') return;
  const miss = missingSteps(W);
  if (!miss.length) { W.phase = 'solving'; return; }
  W.phase = 'short'; W.missing = miss;
}

function resumeCollect(W) {
  W.phase = 'collect'; W.seg = []; W.ref = null; W.paused = null; W.missing = []; W.result = null; W.error = null;
  if (!W.done.still) { W.buf.still = []; W.progress = 0; }
}

function redoSteps(W, steps) {
  for (const k of steps) { if (k in W.done) { W.done[k] = false; W.buf[k] = []; } }
  resumeCollect(W);
}

function payload(W) {
  return STEPS.flatMap((k) => W.buf[k].map((x) => ({ step: k, t: x.t, g: x.g })));
}

// ---------------- Audio Cues ----------------

const MUTE_KEY = 'studio.imu.mute';
function loadMute() { try { return localStorage.getItem(MUTE_KEY) === '1'; } catch { return false; } }
function saveMute(v) { try { localStorage.setItem(MUTE_KEY, v ? '1' : '0'); } catch { /* private mode */ } }
let actx = null;
function audioUnlock() {
  try {
    const C = window.AudioContext || window.webkitAudioContext;
    if (!C) return;
    actx = actx || new C();
    if (actx.state === 'suspended') actx.resume();
  } catch { actx = null; }
}
function beep(kind) {
  if (!actx) return;
  try {
    const t = actx.currentTime + 0.01;
    const tones = kind === 'all' ? [[880, 0], [1320, 0.13]] : [[kind === 'still' ? 660 : 990, 0]];
    for (const [f, dt] of tones) {
      const o = actx.createOscillator(), gn = actx.createGain();
      o.type = 'sine';
      o.frequency.value = f;
      gn.gain.setValueAtTime(0.0001, t + dt);
      gn.gain.exponentialRampToValueAtTime(0.06, t + dt + 0.012);
      gn.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.11);
      o.connect(gn); gn.connect(actx.destination);
      o.start(t + dt); o.stop(t + dt + 0.12);
    }
  } catch { /* ignore */ }
}

// ---------------- 3D ----------------

const reduceMotion = () => { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; } };

function drawOverlay(d, canvas, svg, arrow, plumb, now) {
  if (!svg) return;
  if (!d || !d.ready || !d.home || !d.blob) { svg.innerHTML = ''; return; }
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  const V = d.camera.position.constructor;
  d.camera.updateMatrixWorld();
  const P = d.blob.position;
  const H = 2 * (d.home.target.z - P.z);
  const pr = (x, y, z) => { const v = new V(x, y, z).project(d.camera); return [(v.x + 1) / 2 * w, (1 - v.y) / 2 * h]; };
  let out = '';
  if (plumb) {
    const [x1, y1] = pr(P.x, P.y, P.z), [x2, y2] = pr(P.x, P.y, P.z + H * 1.12);
    out += `<line class="plumb" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
  }
  if (arrow && DIRV[arrow]) {
    const u = DIRV[arrow], R = H * 1.08;
    const swing = reduceMotion() ? 1 : 0.55 + 0.45 * Math.sin(now / 260);
    const end = 6 + 20 * swing;
    const pts = [];
    for (let a = 0; a <= end + 1e-6; a += 2) {
      const s = Math.sin(rad(a)), c = Math.cos(rad(a));
      pts.push(pr(P.x + R * s * u[0], P.y + R * s * u[1], P.z + R * c));
    }
    if (pts.length >= 2) {
      const [ex, ey] = pts[pts.length - 1], [px, py] = pts[pts.length - 2];
      const dx = ex - px, dy = ey - py, L = Math.hypot(dx, dy) || 1, ux = dx / L, uy = dy / L, sz = Math.max(12, h * 0.03);
      const head = `${ex + ux * sz},${ey + uy * sz} ${ex - uy * sz * 0.6},${ey + ux * sz * 0.6} ${ex + uy * sz * 0.6},${ey - ux * sz * 0.6}`;
      const [gx, gy] = pr(P.x, P.y, P.z);
      out += `<line class="ghost" x1="${gx}" y1="${gy}" x2="${ex}" y2="${ey}"/>`;
      out += `<polyline class="arc" points="${pts.map((p) => p.join(',')).join(' ')}"/><polygon class="head" points="${head}"/>`;
      const lx = Math.min(w - 50, Math.max(50, ex)), ly = Math.min(h - 50, Math.max(120, ey + sz + 26));
      out += `<text class="lbl" x="${lx}" y="${ly}">Tilt ${STEP[arrow].short}</text>`;
    }
  }
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = out;
}

function applyView(d, view) {
  if (typeof view === 'string') { d.setView(view); return; }
  d.setView('front');
  const h = d.home;
  if (!h) return;
  const t = h.target, dist = h.pos.distanceTo(t), az = rad(view.az), el = rad(view.el);
  h.pos.set(t.x + dist * Math.cos(el) * Math.cos(az), t.y + dist * Math.cos(el) * Math.sin(az), t.z + dist * Math.sin(el));
  d.camera.position.copy(h.pos);
  d.controls.target.copy(t);
  d.controls.update();
  d.dirty = true;
}

function DuckView({ g, joints, stale, arrow, plumb, view = 'front34', children }) {
  const canvasRef = useRef(null), duckRef = useRef(null), svgRef = useRef(null);
  const [ready, setReady] = useState(false);
  const [moved, setMoved] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    const d = new Duck3D(canvasRef.current, { view: 'front34', centerX: 0.5, centerY: 0.56, fill: 0.7 });
    duckRef.current = d;
    d.onUserMove = () => setMoved(true);
    d.load('model/').then(() => {
      setReady(true);
      d.setGrid(true);
      d.setImuGizmo(true);
    }).catch((e) => setErr(`3D model failed to load: ${e.message}`));
    return () => d.dispose();
  }, []);

  useEffect(() => {
    const d = duckRef.current;
    if (!d || !ready) return;
    if (Array.isArray(joints)) d.setJoints(joints);
    d.setGravity(g);
  }, [g, joints, ready]);

  const viewKey = JSON.stringify(view);
  useEffect(() => {
    if (!ready || !duckRef.current) return;
    applyView(duckRef.current, view);
    setMoved(false);
  }, [ready, viewKey]);

  useEffect(() => {
    if (!ready) return undefined;
    if (!arrow && !plumb) { if (svgRef.current) svgRef.current.innerHTML = ''; return undefined; }
    let raf = 0;
    const tick = (now) => { drawOverlay(duckRef.current, canvasRef.current, svgRef.current, arrow, plumb, now || 0); raf = requestAnimationFrame(tick); };
    tick(performance.now());
    return () => cancelAnimationFrame(raf);
  }, [ready, arrow, plumb]);

  return html`<div class="imu-view">
    <canvas ref=${canvasRef} class=${stale ? 'stale' : ''}></canvas>
    <svg class="imu-overlay" ref=${svgRef} aria-hidden="true"></svg>
    ${err ? html`<span class="imu-note" role="alert">${err}</span>` : children}
    ${moved ? html`<button class="reset-view" onClick=${(e) => { e.currentTarget.blur(); duckRef.current?.resetView(); setMoved(false); }}>Reset View</button>` : null}
  </div>`;
}

function ImuStage({ st, stale, corr, parts, collecting, arrow, cue }) {
  const g = okVec(st?.gravity) ? st.gravity : null;
  const joints = st?.joints;
  const after = g && corr ? qRotate(corr, g) : null;
  const tb = g ? angle(g, DOWN) : null, ta = after ? angle(after, DOWN) : null;
  const old = stale || !g;
  const sideways = corr && Math.abs(parts?.pitch_deg ?? 0) >= Math.abs(parts?.roll_deg ?? 0);
  const view = corr ? (sideways ? { az: -80, el: 6 } : 'front') : collecting ? 'back34' : 'front34';
  const noteLive = old ? html`<span class="imu-note">${g ? 'Stale attitude reading' : 'No attitude data'} · Available in Motion mode</span>`
    : html`<span class="imu-note">Live · Body follows IMU gravity</span>`;
  return html`<div class="imu-stage-wrap">
    <div class=${'imu-stage' + (corr ? ' duo' : '')}>
      <${DuckView} key="before" g=${g} joints=${joints} stale=${old} arrow=${corr ? null : arrow} plumb=${!!corr} view=${view}>
        <div class="imu-stage-read num"><span>${corr ? 'Before' : 'Body Tilt (vs Vertical)'}</span><b>${fmt(tb)}°</b></div>
        ${!corr && cue ? html`<span class="imu-cue">${cue}</span>` : null}
        ${corr ? html`<span class="imu-note">Current mount</span>` : noteLive}
      <//>
      ${corr ? html`<${DuckView} key="after" g=${after} joints=${joints} stale=${old} plumb=${true} view=${view}>
        <div class="imu-stage-read num after"><span>After</span><b>${fmt(ta)}°</b></div>
        <span class="imu-note">New mount · unsaved</span>
      <//>` : null}
    </div>
    ${corr ? html`<p class="imu-diff num">Standing tilt: Before <b>${fmt(tb)}°</b> → After <b>${fmt(ta)}°</b> (delta <b>${fmt(g && after ? angle(g, after) : null)}°</b>).
      Dashed line is vertical; ${sideways ? 'side view (primary pitch offset)' : 'front view (primary roll offset)'}.</p>` : null}
  </div>`;
}

// ---------------- Direction Pad ----------------

function DirPad({ W }) {
  const C = 100, R = 80, MAX = 32;
  const r = (t) => Math.min(t, MAX) / MAX * R;
  const pt = (t, d) => [C - r(t) * Math.sin(rad(d)), C - r(t) * Math.cos(rad(d))];
  const collecting = W.phase === 'collect';
  const next = collecting && W.done.still ? nextTodo(W) : null;
  const L = collecting && W.done.still ? W.live : null;
  const p = L && L.t != null ? pt(L.t, L.d ?? 0) : [C, C];
  const ring = (t) => html`<circle cx=${C} cy=${C} r=${r(t)} class="pad-ring" />`;
  return html`<svg class="imu-pad" viewBox="0 0 200 200" role="img"
    aria-label=${`Completed: ${names(STEPS.filter((k) => W.done[k])) || 'none'}`}>
    <circle cx=${C} cy=${C} r=${R + 6} class="pad-bg" />
    ${ring(RULES.tilt_go_deg)}${ring(RULES.tilt_max_deg)}
    ${TILTS.map((k) => {
      const [x, y] = pt(20, STEP[k].dir), [lx, ly] = pt(33, STEP[k].dir);
      const cls = W.done[k] ? 'done' : k === next ? 'next' : (k === W.cand && W.inZone ? 'hot' : '');
      return html`<g class=${'pad-dot ' + cls}><circle cx=${x} cy=${y} r="13" />
        ${W.done[k] ? html`<path d=${`M${x - 5.5},${y} l4,4 l7,-8`} class="tick" />` : null}
        <text x=${lx} y=${ly + 4} class="pad-lbl">${STEP[k].short}</text></g>`;
    })}
    <g class=${'pad-dot center ' + (W.done.still ? 'done' : collecting ? 'next' : '')}>
      <circle cx=${C} cy=${C} r="11" />
      ${W.done.still ? html`<path d=${`M${C - 5},${C} l3.5,3.5 l6.5,-7.5`} class="tick" />` : null}
    </g>
    ${L && W.hold > 0 ? html`<circle cx=${p[0]} cy=${p[1]} r="12" class="pad-hold"
      style=${`stroke-dasharray:${(W.hold * 75.4).toFixed(1)} 76`} transform=${`rotate(-90 ${p[0]} ${p[1]})`} />` : null}
    ${L ? html`<circle cx=${p[0]} cy=${p[1]} r="6" class=${'pad-live' + (W.over ? ' over' : W.inZone ? ' on' : '')} />` : null}
  </svg>`;
}

// ---------------- Side Cards ----------------

function statusOf(st, imu, live) {
  const s = st?.imu?.status;
  const v = imu?.mount?.verified;
  if (imu?.profile_error) return { word: 'IMU Profile Unconfirmed', kind: 'bad', hint: imu.profile_error };
  if (imu?.mount_protocol_mismatch) return { word: 'Recalibration Required', kind: 'warn',
    hint: 'Bus protocol/profile mismatches stored mount orientation: recalibrate and verify below' };
  if (v === false) {
    return { word: 'Unverified', kind: 'warn',
      hint: st?.posture === 'starting' ? 'robotd blocks Motion mode until IMU mounting is verified' : 'Misaligned mount causes walking drift: calibrate first' };
  }
  if (imu?.temporary) return { word: 'Temporary Orientation', kind: 'warn', hint: 'Up axis known, forward axis unverified: stand only, do not walk' };
  if (live !== false && s === 'missing') return { word: 'IMU Missing', kind: 'bad', hint: 'Check module wiring or re-detect chip below' };
  if (live !== false && s === 'frozen') return { word: 'Readings Frozen', kind: 'bad', hint: 'Attitude stopped updating — do not walk' };
  if (v === true || (v == null && s === 'ok' && st?.mode === 'motion')) {
    const when = imu?.stand_calib_at;
    return { word: 'Verified', kind: 'ok', hint: when ? `Last standing calibration: ${when}` : 'robotd verified up & forward axes' };
  }
  return { word: 'Unknown', kind: null, hint: 'Waiting for stored mount orientation from robotd' };
}

function freshText(st, live, age) {
  if (live === false) return { live: false, text: Number.isFinite(age) ? `${Math.round(age / 1000)}s stale` : 'Offline' };
  const a = st?.joints_age_ms;
  if (a == null) return { live: false, text: 'None' };
  if (a >= RULES.stale_ms) return { live: false, text: `${Math.round(a / 1000)}s stale` };
  return { live: true, text: `Live · ${(a / 1000).toFixed(1)}s ago` };
}

function StatusCard({ st, imu, live, age, diagnosticElapsed }) {
  const S = statusOf(st, imu, live);
  const F = freshText(st, live, age);
  const src = imu?.source;
  const guard = st?.guard;
  return html`<section class=${'board imu-card' + (F.live ? '' : ' imu-stale')}>
    <h3>Control & Mount Orientation${F.live ? '' : html` <span class="sv-small">(Attitude: ${F.text})</span>`}</h3>
    <div class="imu-kv">
      <span>Status</span><b class=${'imu-word ' + (S.kind || '')}>${S.kind === 'warn' || S.kind === 'bad' ? html`<span class=${'badge ' + S.kind}>${S.kind === 'bad' ? '×' : '!'}</span>` : S.kind === 'ok' ? '✓ ' : ''}${S.word}</b>
      <span></span><span class="sv-small">${S.hint}</span>
      <span>Sensor</span><b>${chipLabel(imu, live, diagnosticElapsed)}</b>
      ${src === 'bus' ? html`<span>Profile</span><b>${imu?.configured_imu_profile === 'qinmo' ? 'Qinmo (Configured)'
        : imu?.configured_imu_profile === 'legacy' ? 'Auto-detect (Legacy)'
        : imu?.metadata_error || unsupportedTransport(imu?.imu_bus) ? 'Unconfirmed (unsupported diagnostics)'
        : imu?.configured_imu_profile != null ? 'Unsupported profile' : 'Legacy board'}</b>
        <span>Detected</span><b>${imuDiagnosticCopy(imu?.diagnostic, live, diagnosticElapsed).board || (imu?.imu_bus ? chipLabel(imu, live, diagnosticElapsed) : 'Not connected')}</b>
        <span>Gyro Scale</span><b class="num">${imu?.profile_error || unsupportedTransport(imu?.imu_bus) ? 'Unconfirmed'
          : imu?.imu_bus?.profile === 'qinmo' ? 'raw ÷ 16.4 °/s'
          : imu?.imu_bus ? 'raw × 0.0175 °/s' : 'Shown after connect'}</b>` : null}
      <span>Tilt</span><b class="num">${fmt(st?.tilt_deg)}°<small> from vertical; walk limit ≤${guard?.tilt_tolerance_deg ?? 10}°</small></b>
      <span>Stance</span><b class="num">${st?.hold ? html`Max HOME err ${fmt(st.hold.max_error_deg)}°<small> ${st.hold.stage === 'holding' && st.hold.imu_calibration_ready !== false ? 'Steady' : 'Settling'}</small>`
        : guard?.max_joint_error_deg != null ? html`Max HOME err ${fmt(guard.max_joint_error_deg)}°<small> ${imu?.coarse_sampling?.hold_ready ? 'Steady, ready to calibrate. ' : ''}Walk limit ≤${guard.joint_tolerance_deg ?? 5}°</small>` : '—'}</b>
      <span>Telemetry</span><b class=${'num' + (F.live ? '' : ' imu-dim')}>${F.text}${imu?.reader?.errors ? html`<small> (${imu.reader.errors} read errors)</small>` : null}</b>
    </div>
    ${src === 'bus' && imu?.imu_bus?.protocol === 'dxl2' ? html`<p class="sv-small">DXL2 boards omit sample sequence/status flags; freshness is checked by host arrival time and byte changes.</p>` : null}
    ${imu?.mount?.quat ? html`<details class="imu-details"><summary>Mount Quaternion (w, x, y, z)</summary>
      <code class="num">${imu.mount.quat.map((x) => fmt(x, 4)).join(', ')}</code>${imu.mount.verified ? ' · Verified' : ' · Unverified'}
      <p class="sv-small">If mounting changed, recalibrate and verify below.</p></details>` : null}
  </section>`;
}

function DiagnosticCard({ st, imu, live, diagnosticElapsed }) {
  const diagnostic = imu?.diagnostic || st?.imu?.diagnostic;
  if ((imu?.source ?? st?.imu?.source) !== 'bus' || (st?.posture !== 'starting' && !diagnostic?.age_ms
      && diagnostic?.responding == null)) return null;
  const d = imuDiagnosticCopy(diagnostic, live, diagnosticElapsed);
  return html`<section class="board imu-card imu-diagnostic" aria-label="Bus IMU Read-Only Diagnostics">
    <h3>Bus IMU Read-Only Diagnostics</h3>
    <p class=${'imu-word ' + d.kind}>${d.word}</p>
    ${d.board ? html`<p>${d.board}</p>` : null}
    <p class="sv-small">${d.hint}</p>
    ${live !== false && diagnostic?.error ? html`<p class="sv-small">Raw: ${diagnostic.error}</p>` : null}
    <p class="sv-small">Board responses are independent of servos and do not imply verified orientation or motion readiness.</p>
  </section>`;
}

function ChipCard({ imu, link, live, diagnosticElapsed }) {
  const [profile, setProfile] = useState(imu?.configured_imu_profile || 'legacy');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  useEffect(() => { setProfile(imu?.configured_imu_profile || 'legacy'); }, [imu?.configured_imu_profile]);
  const selection = imu?.profile_selection;
  const reason = live === false || link.status !== 'open' ? 'Offline — waiting for connection'
    : imu?.metadata_error || unsupportedTransport(imu?.imu_bus) ? imu?.metadata_error || imu?.profile_error || 'Unsupported IMU profile reported by robotd'
    : !selection?.can_start ? selection?.reason || 'Web profile selection requires an updated robotd' : null;
  const select = async () => {
    setBusy(true); setMessage(null);
    try {
      const reply = await link.cmd('imu.op', { op: 'profile-select', profile, confirmed: true }, 180000);
      setMessage(reply.ok ? 'Profile saved. Reconnecting in Commissioning mode — support the robot and complete mount verification.'
        : reply.reason || reply.error || 'Profile selection failed');
    } finally { setBusy(false); }
  };
  return html`<section class="board imu-card">
    <h3>IMU Hardware <span class="sv-small">Active: ${chipLabel(imu, live, diagnosticElapsed)}</span></h3>
    <p class="sv-small">Auto-detection supports FT6, IMU-to-DXL (Feetech), and SFLP 124/12 (Dynamixel 2.0).</p>
    <p class="sv-small">Qinmo has no unique hardware ID and must be selected explicitly here. Changing boards or mounting requires re-verification.</p>
    ${imu?.source === 'bus' || imu?.source == null ? html`<label class="sv-small">Bus IMU Profile
      <select aria-label="Bus IMU Profile" value=${profile} disabled=${busy || !!imu?.redo?.active}
        onChange=${e => { setProfile(e.target.value); setMessage(null); }}>
        <option value="legacy">Auto-detect (Standard Boards)</option><option value="qinmo">Qinmo · Feetech · raw ÷ 16.4 °/s</option>
      </select></label>
      <div class="imu-actions"><${TwoStep} key=${profile} label="Save Profile & Reconnect"
        ask="Robot supported — confirm reconnect & recalibrate" onConfirm=${select} disabled=${busy || !!reason}
        title=${reason} busyLabel="Saving & reconnecting…" /></div>
      <p class="sv-small">Support the robot and relax servos first. robotd restarts into Commissioning mode; previous mount becomes invalid until verified.</p>
      ${reason ? html`<p class="sv-warn">${reason}</p>` : null}
      ${message ? html`<p role="status" class="sv-small">${message}</p>` : null}` : null}
    <p class="sv-small">I²C modules must be wired before first boot. If added later, run via SSH on the robot:</p>
    <p><code class="num">sudo microduck-firstboot --detect-imu-only --force</code></p>
    <p class="sv-small">Then restart robotd.</p>
  </section>`;
}

function blockReason(st, imu, live) {
  if (!st) return 'Connecting to robot…';
  if (live === false) return 'Offline — waiting for connection';
  if (imu?.profile_error) return imu.profile_error;
  if (imu?.job?.phase === 'verify') return 'IMU verification in progress';
  if (imu?.redo?.active) return 'Recalibration in progress: complete verification or cancel to restore';
  if (st.posture === 'servo_session') return 'Servo Tuning holds the bus — click "Hand Back to robotd" first';
  if (st.posture === 'absent') return 'robotd is not running';
  if (st.posture === 'starting') return `robotd starting${st.start_error ? `: ${st.start_error}` : ''}`;
  if (st.mode === 'commissioning') {
    if (!st.hold) return 'Commissioning relaxed: click "Enable Stance" above first (support the robot)';
    if (st.hold.stage !== 'holding' || st.hold.imu_calibration_ready === false) return 'Waiting for joints to settle';
    if (!okVec(st.gravity) || st.joints_age_ms == null || st.joints_age_ms > RULES.stale_ms) return 'No live attitude data';
    return null;
  }
  if (imu?.mount?.verified === false) return 'Mount unverified: run Coarse Calibration above first';
  if (st.posture === 'limp') return 'Relaxed: click "Enable Stance" above first (support the robot)';
  const calibrationHold = imu?.coarse_sampling?.hold_ready === true;
  if (st.posture === 'homing' && !calibrationHold) return 'Standing up — wait until settled';
  if (st.posture === 'walking') return 'Currently walking — stop first';
  if (st.posture === 'policy') return 'Policy active — press gamepad + to return to Standing';
  if (st.posture === 'sitting') return 'Sitting: click "Enable Stance" above first';
  if (st.posture !== 'holding' && !(st.posture === 'homing' && calibrationHold)) return 'Unable to read current posture';
  if (imu?.coarse_sampling?.live_verify && !calibrationHold) return 'Waiting for joints to settle';
  if (!okVec(st.gravity) || st.joints_age_ms == null || st.joints_age_ms > RULES.stale_ms) return 'No live attitude data';
  if (st.imu?.status === 'missing' || st.imu?.status === 'frozen') return 'IMU telemetry fault — check wiring';
  return null;
}

// ---------------- Stance Enable / Disable ----------------

const POSE_CN = { holding: 'Standing', policy: 'Standing (Policy)', walking: 'Walking', limp: 'Relaxed', homing: 'Standing up',
  sitting: 'Sitting', commissioning: 'Commissioning (Relaxed)', starting: 'robotd starting', absent: 'robotd stopped', servo_session: 'Servo Tuning' };
const HOLD_CN = { holding: 'Commissioning (Holding)', ramping: 'Commissioning (Ramping)' };
const HOLD_CONFIRM = 'Commissioning Stance: all 15 servos will ramp to HOME over 3 seconds and hold.\n'
  + 'Active balance is off in Commissioning mode — support the robot at all times.\nContinue?';

function StanceCard({ st, imu, pd, link, live }) {
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const commissioning = (st?.mode || imu?.mode) === 'commissioning';
  const open = link.status === 'open';
  const hold = st?.hold || imu?.hold || null;
  const calibrationHold = imu?.coarse_sampling?.hold_ready === true;
  const verifying = imu?.job?.phase === 'verify';
  const now = !st ? 'Connecting' : live === false ? 'Offline' : st.posture === 'servo_session' ? POSE_CN.servo_session
    : verifying ? 'Verifying IMU'
    : commissioning && hold ? HOLD_CN[hold.stage] || 'Commissioning (Holding)'
    : st.posture === 'homing' && calibrationHold ? 'Settled, ready to calibrate' : (POSE_CN[st.posture] || 'Unknown');
  const sb = pd?.motion?.buttons?.stand;
  const standWhy = !st || live === false ? 'Offline — waiting for connection'
    : verifying ? 'Verifying — keep supporting robot'
    : imu?.redo?.active && (!commissioning || !['commissioning', 'finish_failed'].includes(imu.redo.phase))
      ? 'Recalibration mode transition in progress'
    : st.posture === 'servo_session' ? 'Servo Tuning holds the bus: click "Hand Back to robotd"'
      : st.posture === 'starting' ? 'robotd is starting'
        : commissioning ? (hold ? (hold.stage === 'holding' ? 'Already holding' : 'Ramping to HOME') : null)
          : st.posture === 'homing' && calibrationHold ? 'Ready for still capture; wait for HOME before walking' : sb && !sb.ok ? sb.why : null;
  const stand = async () => {
    if (commissioning) {
      if (!window.confirm(HOLD_CONFIRM)) return;
      setBusy(true); setMsg(null);
      const r = await link.cmd('imu.op', { op: 'hold' }, 25000);
      setBusy(false);
      setMsg(r.ok ? { ok: true, text: 'Holding HOME: support the robot while calibrating; click Disable (Relax) or E-Stop when done.' }
        : { ok: false, text: r.reason || r.error || 'Failed to hold stance' });
      return;
    }
    if (typeof st?.tilt_deg === 'number' && st.tilt_deg > 30
      && !window.confirm(`Robot is tilted (${Math.round(st.tilt_deg)}°). Legs will apply torque to stand — support it first. Continue?`)) return;
    setBusy(true); setMsg(null);
    const r = await link.cmd('pad.op', { op: 'stand' }, 10000);
    setBusy(false);
    setMsg(r.ok ? { ok: true, text: 'Stand command sent — support the robot until settled.' } : { ok: false, text: r.reason || r.error || 'Stand failed' });
  };
  const relax = async () => {
    setMsg(null);
    const r = await link.cmd('relax', {}, 7000);
    setMsg(r.ok ? { ok: true, text: 'Relax sent — support the robot.' } : { ok: false, text: r.reason || r.error || 'Relax failed — use gamepad − or power off' });
  };
  return html`<section class="board imu-card imu-stance">
    <h3>Stance <span class="sv-small">Must be standing for calibration</span></h3>
    <div class="imu-actions">
      <button class="btn-dark imu-stand" disabled=${!!standWhy || busy} onClick=${stand}
        title=${standWhy || 'Stand up to HOME'}>${busy ? 'Waiting…' : 'Enable Stance'}</button>
      <button class="btn-light imu-relax" disabled=${!open} onClick=${relax}
        title="Relax all 15 servos (same as E-Stop)">Disable (Relax)</button>
      <span class="sv-small imu-stance-now">Current: <b>${now}</b></span>
    </div>
    ${standWhy ? html`<p class="sv-small imu-stance-why">${standWhy}</p>` : null}
    ${imu?.redo?.active ? null : commissioning
      ? html`<p class="sv-small">Commissioning stance locks all 15 joints at HOME without active balance — support the robot.</p>`
      : standWhy ? null
        : html`<p class="sv-small">Enable Stance stands the robot up; Disable relaxes all servos.</p>`}
    ${msg && !verifying ? html`<div class=${'banner ' + (msg.ok ? 'warn' : 'bad')} role="status"><span>${msg.text}</span></div>` : null}
  </section>`;
}

function collectWords(W) {
  if (W.paused) return [W.paused, ''];
  if (!W.done.still) {
    return [W.moved ? 'Movement detected — restarting timer' : 'Place on flat surface, hands off',
      `Capturing 3s standing reference · ${Math.max(0, RULES.still_s * (1 - W.progress)).toFixed(1)}s left`];
  }
  const next = nextTodo(W);
  const n = next ? STEP[next] : null;
  const tip = n ? `Lift torso 2–3 cm, hold level for 0.5s, then tilt ${n.short} (${n.how}) ~15° and hold 1s.` : '';
  if (W.over) return ['Too far — ease back slightly', tip];
  if (W.repeat) return [`${STEP[W.repeat].cn} already recorded`, `Return level, then do ${next ? STEP[next].cn : 'next'}`];
  if (W.needLevel) return ['Hold level first', 'Hold level for 0.5s before each tilt'];
  if (W.inZone && W.cand) return [`${STEP[W.cand].cn}: hold steady…`, 'Keep holding until chime'];
  return [n ? `Next: ${n.cn}` : 'All steps complete', tip];
}

function CollectCard({ st, imu, live, W, act, muted, setMuted }) {
  const block = blockReason(st, imu, live);
  const ph = W.phase;
  const guardErr = st?.guard?.max_joint_error_deg;
  const res = W.result;
  const mute = html`<label class="imu-mute" title="Audio cue on step completion"><input type="checkbox" checked=${muted}
    onChange=${(e) => setMuted(e.target.checked)} /> Mute</label>`;
  let button;
  if (ph === 'collect') button = html`<button class="imu-big rec" onClick=${act.end}><span class="rec-dot"></span>Collecting… (Click to Stop)</button>`;
  else if (ph === 'solving') button = html`<button class="imu-big" disabled>Solving…</button>`;
  else if (ph === 'short') button = html`<button class="imu-big" onClick=${act.resume}>Resume Collection</button>`;
  else button = html`<button class="imu-big" disabled=${!!block} onClick=${act.start}>${ph === 'idle' ? 'Start Collection' : 'Recollect'}</button>`;
  const [say, sub] = ph === 'collect' ? collectWords(W) : ['', ''];
  return html`<section class="board imu-card imu-collect">
    <h3>Standing Calibration <span class="sv-small">~1 min · 4-axis tilt</span>${mute}</h3>
    ${ph === 'idle' ? html`<ol class="imu-howto">
      <li>Enable Stance (no walking) and click "Start Collection".</li>
      <li>Leave the robot standing still on a flat surface for <b>3 seconds</b>.</li>
      <li>Stand behind the robot and tilt <b>Forward, Back, Left, and Right</b> (~15°, hold 1s each, level 0.5s between tilts).</li>
      <li>Each direction lights up and chimes when captured; solving runs automatically once all 4 are done.</li>
    </ol>` : null}
    ${ph === 'collect' || ph === 'solving' || ph === 'short' ? html`<div class="imu-live">
      <${DirPad} W=${W} />
      <div class="imu-live-txt">
        ${ph === 'collect' ? html`<p class="imu-say"><b>${say}</b><br /><span class="sv-small">${sub}</span></p>
          ${!W.done.still ? html`<div class="imu-bar" aria-label="Still progress"><span style=${{ width: `${Math.round(W.progress * 100)}%` }}></span></div>`
            : html`<p class="imu-tilt num"><b>${fmt(W.live?.t)}°</b><span>from level</span></p>`}`
        : ph === 'solving' ? html`<p class="imu-say"><b>Collection complete — solving…</b></p>`
          : html`<p class="imu-say"><b>Missing: ${names(W.missing)}</b><br />
            <span class="sv-small">Completed steps are kept. Click Resume to finish ${names(W.missing)}, or Restart.</span></p>`}
      </div>
    </div>` : null}
    ${ph === 'error' ? html`<div class="banner bad" role="alert"><b>Solve Failed</b><span class="more">${W.error}</span></div>` : null}
    ${ph === 'done' && res && !res.ok ? html`<p class="sv-warn">Calibration did not pass — see Result below.</p>` : null}
    ${block && ph !== 'solving' ? html`<p class="sv-warn">${block}</p>`
      : ph === 'idle' && guardErr != null && guardErr > 5 ? html`<p class="sv-warn">Legs are ${fmt(guardErr)}° off HOME — calibrate servo zero offsets first.</p>` : null}
    <div class="imu-actions">${button}
      ${ph === 'short' || ph === 'collect' ? html`<button class="btn-light" onClick=${act.start} disabled=${!!block && ph !== 'collect'}>Restart</button>` : null}</div>
  </section>`;
}

const GRADE = { good: ['Good', 'ok'], ok: ['Usable', 'warn'], coarse: ['Coarse', 'warn'] };

function ResultCard({ res, imu, link, live, onRedo }) {
  const [checks, setChecks] = useState({});
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState(null);
  useEffect(() => { setChecks({}); setOut(null); }, [res]);
  if (!res) return null;
  const P = res.parts || {}, Q = res.quality || {}, R = res.rules || {};
  const confirms = res.confirm || [];
  const allChecked = confirms.every((_, i) => checks[i]);
  const [gw, gk] = GRADE[Q.grade] || ['—', ''];
  const redo = (res.redo || []).filter((k) => STEP[k]);
  const canSave = live !== false && link.status === 'open' &&
    imu?.coarse_sampling?.live_verify === true && !imu?.coarse_sampling?.reason &&
    !imu?.coarse_sampling?.verify_reason;
  const save = async () => {
    if (res.live_verify) {
      if (!canSave || busy || !allChecked) return;
      setBusy(true);
      setOut(null);
      const r = await link.cmd('imu.op', { op: 'verify', confirmed: true, mount_wxyz: res.mount }, 90000);
      setBusy(false);
      setOut({ ok: r.ok, text: r.ok ? r.data?.note || 'Mount orientation saved' : r.reason || r.error || 'Verification failed' });
      return;
    }
    setOut({ ok: true, text: 'Orientation staged in Studio: go to Onboarding Step 3 to commit to robotd.' });
    location.hash = '#/';
    setTimeout(() => document.querySelector('[data-step="commit"]')?.scrollIntoView({ block: 'center' }), 300);
  };
  const detail = html`<table class="imu-table num">
      <thead><tr><th>Step</th><th class="r">Tilt</th><th class="r">Skew</th><th class="r">Spread</th><th></th></tr></thead>
      <tbody>${STEPS.map((k) => { const s = (res.steps || {})[k] || {}; return html`<tr class=${s.ok === false ? 'bad' : ''}
        title=${s.ref ? `Ref: ${s.ref}${s.ref_offset_deg != null ? ` (${fmt(s.ref_offset_deg)}° off still)` : ''}` : ''}>
        <td>${STEP[k].cn}</td><td class="r">${k === 'still' ? '—' : `${fmt(s.tilt_deg, 0)}°`}</td>
        <td class="r">${s.skew_deg != null ? `${fmt(s.skew_deg, 0)}°` : '—'}</td><td class="r">${s.spread_deg != null ? `${fmt(s.spread_deg)}°` : '—'}</td>
        <td>${s.ok ? '✓' : s.msg || (s.ok === false ? '×' : '')}</td></tr>`; })}</tbody>
    </table>
    ${res.ok && Q.grade !== 'coarse' ? html`<p class="sv-small">Checks: F/B delta ${fmt(Q.fb_pair_deg)}° · L/R delta ${fmt(Q.lr_pair_deg)}° · Cross ${fmt(Q.cross_deg)}° ·
      Max skew ${fmt(Q.max_skew_deg)}° (Good: skew ≤${R.good_skew_deg ?? 10}°, cross ≤${R.good_cross_deg ?? 6}°; Fail: skew >${R.skew_max_deg ?? 20}° or cross >${R.cross_max_deg ?? 12}°)</p>` : null}
    ${res.standard ? html`<p class="sv-small">Orientation: ${res.standard.text}${res.standard.off_deg > 0.5 ? ` (${fmt(res.standard.off_deg)}° offset)` : ''}</p>` : null}
    ${res.mount ? html`<details class="imu-details"><summary>New Mount Quaternion (w, x, y, z)</summary><code class="num">${res.mount.map((x) => fmt(x, 6)).join(', ')}</code></details>` : null}`;
  return html`<section class="board imu-card">
    <h3>Result ${res.ok ? html`<span class=${'imu-grade ' + gk}>${gw}</span>` : html`<span class="imu-grade bad">Failed</span>`}</h3>
    ${!res.ok ? html`<p class="sv-warn">${res.reason}</p>
      ${redo.length ? html`<div class="imu-actions"><button class="btn-dark" onClick=${() => onRedo(redo)}>Redo ${names(redo)}</button>
        <span class="sv-small">Other steps are preserved</span></div>` : null}` : html`
      <div class="imu-parts num">
        <div><span>Pitch</span><b>${sgn(P.pitch_deg)}°</b><small>+ = forward tilt</small></div>
        <div><span>Roll</span><b>${sgn(P.roll_deg)}°</b><small>+ = left tilt</small></div>
        <div><span>Yaw</span><b>${sgn(P.yaw_deg)}°</b><small>Unobservable while standing</small></div>
        <div><span>Total</span><b>${fmt(P.total_deg)}°</b><small>vs current mount</small></div>
      </div>`}
    ${res.check ? html`<p class=${res.check.ok ? 'sv-small imu-check' : 'sv-warn imu-check'}>${res.check.ok
      ? `✓ Within ${fmt(res.check.diff_deg)}° of active mount (standing tilt ${fmt(res.check.tilt_deg)}°): ${res.check.recorded ? 'recorded as Verified in Onboarding' : 'not recorded (see Logs)'}`
      : `Not recorded as verified: ${res.check.why}`}</p>` : null}
    ${(res.notes || []).map((n) => html`<p class="sv-small">· ${n}</p>`)}
    ${res.ok ? html`<details class="imu-details"><summary>Detailed Breakdown</summary>${detail}</details>` : detail}
    ${confirms.map((c, i) => html`<label class="imu-confirm"><input type="checkbox" checked=${!!checks[i]} onChange=${(e) => setChecks({ ...checks, [i]: e.target.checked })} /> ${c}</label>`)}
    ${res.ok && res.live_verify ? html`<div class="imu-save">
      <button class="btn-dark" disabled=${!res.mount || !canSave || !allChecked || busy || out?.ok}
        onClick=${save}>${busy ? 'Verifying…' : out?.ok ? 'Saved' : 'Verify & Save Mount'}</button>
      <span class="sv-small">Hold HOME. Level robot first, click Verify, then tilt torso forward 15–30° within 20s.</span>
    </div>` : res.ok ? html`<div class="imu-save">
      <button class="btn-dark" disabled=${!res.mount} onClick=${save}>Go to Onboarding Step 3</button>
      <span class="sv-small">Staged locally; Step 3 "Commit to robotd" restarts into Motion mode.</span>
    </div>` : null}
    ${!res.mount && res.ok ? html`<p class="sv-warn">Cannot compute new mount (current orientation unknown)</p>` : null}
    ${out ? html`<div class=${'banner ' + (out.ok ? 'warn' : 'bad')} role="status"><b>${out.ok ? 'Saved' : 'Save Failed'}</b><span class="more">${out.text}</span></div>` : null}
  </section>`;
}

// ---------------- Coarse Calibration ----------------

function CoarseCard({ st, imu, link, live }) {
  const [opened, setOpened] = useState(false);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const mode = st?.mode || imu?.mode;
  const redo = imu?.redo || {};
  const context = imuContext(mode, imu);
  useLayoutEffect(() => { setOpened(false); setMsg(null); }, [context]);
  const open = link.status === 'open' && live !== false;
  const commissioning = mode === 'commissioning';
  const verified = imu?.mount?.verified === true;
  const working = !!(imu?.job && !imu.job.done && !imu.job.failed);
  const verifying = working && imu.job.phase === 'verify';
  const hold = st?.hold || imu?.hold || null;
  const sampling = imu?.coarse_sampling || {};
  const inPlace = mode === 'motion' && sampling.available && !redo.active;
  const liveVerify = inPlace && sampling.live_verify === true;
  const localEntry = inPlace || commissioning;
  const needsUnload = inPlace ? sampling.needs_unload !== false : !!redo.active && !!hold;
  const holdReady = hold && hold.stage === 'holding' && (hold.imu_calibration_ready ??
    (Number.isFinite(hold.max_error_deg) && hold.max_error_deg >= 0 && hold.max_error_deg <= 5));
  const captureWhy = inPlace ? sampling.reason : hold && !holdReady ? 'Wait for joints to settle before capturing' : null;
  const canCollect = !imu?.profile_error && (inPlace || commissioning && (!redo.active || ['commissioning', 'finish_failed'].includes(redo.phase)));
  const showSteps = canCollect && (!verified || opened || redo.active);
  const showStart = !showSteps && !redo.active;
  const startWhy = !open ? 'Offline — waiting for connection' : imu?.profile_error ? imu.profile_error : working ? 'Previous operation still running'
    : !localEntry && !redo.can_start ? redo.reason || 'Cannot enter Commissioning mode yet: relax servos first' : null;
  const run = async (op) => {
    setBusy(true); setMsg(null);
    const r = await link.cmd('imu.op', { op, confirmed: true }, 90000);
    setBusy(false);
    setMsg({ ok: r.ok, text: r.ok ? r.data?.note || (op === 'redo-start'
      ? 'Entered Commissioning mode. Enable Stance or support robot relaxed to capture.' : 'Cancelled and restored previous orientation & mode.')
      : r.reason || r.error || 'Operation failed' });
  };
  const start = async () => {
    if (startWhy || busy) return;
    if (localEntry) { setMsg(null); setOpened(true); }
    else await run('redo-start');
  };
  return html`<section class="board imu-card imu-coarse">
    <h3>IMU Mount Orientation <span class="sv-small">${redo.active ? 'Recalibration active' : verified ? 'Saved · re-verifiable' : 'Initial calibration'}</span></h3>
    <p class="sv-small">Capture Still + Forward Tilt to compute mount quaternion, then verify with robotd.</p>
    ${imu?.mount_protocol_mismatch ? html`<p class="sv-warn">Bus protocol mismatches stored mount orientation: recalibrate and verify.</p>` : null}
    ${redo.active ? html`<p class="sv-warn">${canCollect
      ? 'Recalibration active: capture while holding stance or supported relaxed. Relax before verifying or cancelling.'
      : 'Switching calibration session — please wait.'}</p>
      <div class="imu-actions"><${TwoStep} label="Cancel & Restore" ask="Support robot — restore previous mount & mode?"
        disabled=${!open || busy || working || needsUnload}
        onConfirm=${() => run('redo-cancel')} busyLabel="Restoring…" /></div>` : null}
    ${showStart ? html`<div class="imu-actions">${liveVerify
      ? html`<button class="btn-dark" disabled=${!!startWhy || busy} title=${startWhy} onClick=${start}>Recalibrate Mount</button>`
      : html`<${TwoStep} label="Recalibrate Mount" ask=${inPlace ? 'Hold HOME and capture mount orientation?' : commissioning ? 'Support robot and recalibrate?' : 'Support robot & relaxed — restart into Commissioning?'}
          cls="btn-dark" disabled=${!!startWhy || busy} title=${startWhy} onConfirm=${start} busyLabel="Entering Commissioning…" />`}</div>
      <p class="sv-small">${liveVerify
        ? 'Hold HOME stance to capture, verify, and save in-place.'
        : inPlace
        ? 'Capture Still & Forward Tilt in HOME without restarting; relax before final verification.'
        : commissioning
        ? 'Support robot relaxed or enable stance to capture.'
        : 'Click Disable (Relax) and support the robot before starting. Servo zeros and limits are preserved.'}</p>
      ${startWhy ? html`<p class="sv-warn">${startWhy}</p>` : null}` : null}
    ${!verifying && captureWhy && (!showStart || captureWhy !== startWhy) ? html`<p class="sv-warn">${captureWhy}</p>` : null}
    ${showSteps ? html`<${CoarseSteps} key=${context} link=${link} disabled=${!open || busy || working || !!captureWhy}
      redoSession=${!!redo.active} inPlace=${!!inPlace} liveVerify=${liveVerify} needsUnload=${needsUnload}
      verifyWhy=${liveVerify ? sampling.verify_reason : null}
      onBegin=${() => setOpened(true)} onDone=${(text) => setMsg({ ok: true, text })} />` : null}
    ${msg ? html`<div class=${'banner ' + (msg.ok ? 'warn' : 'bad')} role="status"><span>${msg.text}</span></div>` : null}
  </section>`;
}

function CoarseSteps({ link, disabled, redoSession, inPlace, liveVerify, needsUnload, verifyWhy, onBegin, onDone }) {
  const [step, setStep] = useState('idle');
  const [still, setStill] = useState(null);
  const [res, setRes] = useState(null);
  const [checks, setChecks] = useState({});
  const [out, setOut] = useState(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const grab = async (phase, seconds) => {
    if (disabled || busy) return null;
    onBegin();
    setBusy(true); setOut(null);
    const r = await link.cmd('imu.op', { op: 'coarse-capture', phase, seconds }, 15000);
    if (!alive.current) return null;
    setBusy(false);
    if (!r.ok) { setOut({ ok: false, text: r.reason || r.error || 'Capture failed' }); return null; }
    return r.data || {};
  };
  const takeStill = async () => {
    const d = await grab('still', 3);
    if (!d) return;
    setStill(d); setStep('fwd');
    setOut({ ok: true, text: `Captured ${d.n} still samples. Now pitch forward 15–30° and hold.` });
  };
  const takeFwd = async () => {
    const d = await grab('fwd', 3);
    if (!d) return;
    const samples = [...((still && still.samples) || []), ...(d.samples || [])];
    setBusy(true);
    const r = await link.cmd('imu.op', { op: 'solve', mode: 'coarse', frame: 'body', samples }, 20000);
    if (!alive.current) return;
    setBusy(false);
    if (!r.ok || !r.data || r.data.ok !== true) {
      setStep('failed');
      setOut({ ok: false, text: r.reason || (r.data && r.data.reason) || r.error || 'Solve failed' });
      return;
    }
    setRes(r.data); setStep('confirm');
    setOut({ ok: true, text: 'Solved: confirm physical mounting checkboxes below before verifying.' });
  };
  const reset = () => { setStep('idle'); setStill(null); setRes(null); setChecks({}); setOut(null); };
  const verify = async () => {
    if (disabled || busy || needsUnload || verifyWhy || !res?.mount) return;
    setBusy(true); setStep('verifying'); setOut(null);
    const r = await link.cmd('imu.op', { op: 'verify', confirmed: true, mount_wxyz: res.mount }, 90000);
    if (!alive.current) return;
    if (r.ok) onDone(r.data?.note || 'Verified and saved by robotd.');
    setBusy(false);
    if (r.ok) { setStep('done'); setOut({ ok: true, text: (r.data && r.data.note) || 'Verified and written to calibration config' }); }
    else { setStep('failed'); setOut({ ok: false, text: r.reason || r.error || 'Verification failed' }); }
  };
  const confirms = (res && res.confirm) || [];
  const allChecked = confirms.every((_, i) => checks[i]);
  const nowStep = step === 'idle' ? 'still' : step;
  return html`<div class="imu-coarse-steps">
    <p class="sv-small">${liveVerify
      ? 'Hold HOME (stop policy first). Capture Still, then support torso and capture Forward Tilt.'
      : inPlace
      ? 'Hold HOME to capture Still & Forward Tilt; relax servos before final verification.'
      : redoSession
      ? 'Enable Stance or support relaxed to capture; relax before verifying or cancelling.'
      : 'Hold the robot upright for 3s, then pitch the whole torso forward 15–30° for 3s.'}</p>
    ${nowStep === 'still' ? html`<div class="imu-coarse-step">
      <p class="imu-say"><b>1. Hold robot upright and still</b></p>
      <p class="sv-small">Support the torso upright with feet down. Use physical levelness as reference.</p>
      <button class="btn-dark" disabled=${busy || disabled} onClick=${takeStill}>${busy ? 'Capturing…' : 'Capture Still (3s)'}</button>
    </div>` : null}
    ${nowStep === 'fwd' ? html`<div class="imu-coarse-step">
      <p class="imu-say"><b>2. Pitch forward 15–30° and hold</b></p>
      <p class="sv-small">Tilt the entire torso nose-down 15–30° without rolling left/right, and hold 3 seconds.</p>
      <div class="imu-actions">
        <button class="btn-dark" disabled=${busy || disabled} onClick=${takeFwd}>${busy ? 'Capturing / Solving…' : 'Capture Forward (3s)'}</button>
        <button class="btn-light" disabled=${busy || disabled} onClick=${takeStill}>Recapture Still</button>
      </div>
    </div>` : null}
    ${nowStep === 'confirm' && res ? html`<div class="imu-coarse-step">
      <p class="sv-small">Coarse result: ${res.standard ? `${res.standard.text}${res.standard.off_deg > 0.5 ? ` (${fmt(res.standard.off_deg)}° offset)` : ''}` : 'Solved'}.
        Run Standing Calibration afterwards for fine roll/pitch refinement.</p>
      <div class="imu-parts num">
        <div><span>Pitch</span><b>${sgn(res.parts?.pitch_deg)}°</b><small>+ = forward tilt</small></div>
        <div><span>Roll</span><b>${sgn(res.parts?.roll_deg)}°</b><small>+ = left tilt</small></div>
        <div><span>Total Delta</span><b>${fmt(res.parts?.total_deg)}°</b><small>vs previous mount</small></div>
      </div>
      ${(res.notes || []).map((n) => html`<p class="sv-small">· ${n}</p>`)}
      ${confirms.map((c, i) => html`<label class="imu-confirm"><input type="checkbox" checked=${!!checks[i]}
        onChange=${(e) => setChecks({ ...checks, [i]: e.target.checked })} /> ${c}</label>`)}
      <div class="imu-actions">
        <button class="btn-dark" disabled=${busy || disabled || needsUnload || !!verifyWhy || !res.mount || !allChecked} onClick=${verify}>Verify with robotd</button>
        <button class="btn-light" disabled=${busy} onClick=${reset}>Restart</button>
      </div>
      ${verifyWhy ? html`<p class="sv-warn">${verifyWhy}</p>` : null}
      ${(redoSession || inPlace) && !liveVerify ? html`<p class=${needsUnload ? 'sv-warn' : 'sv-small'}>Click Disable (Relax) and support the robot before verifying.</p>` : null}
      <p class="sv-small"><b>Level the robot before clicking Verify</b>, then tilt forward 15–30° within 20s when prompted.</p>
    </div>` : null}
    ${nowStep === 'verifying' ? html`<div class="imu-coarse-step">
      <p class="imu-say"><b>Pitch forward 15–30° within 20 seconds</b></p>
      <p class="sv-small">Support the robot while robotd verifies upright and forward tilt.</p>
    </div>` : null}
    ${nowStep === 'done' ? html`<p class="sv-small">Verified and saved. You can run Standing Calibration below to refine.</p>` : null}
    ${nowStep === 'failed' ? html`<div class="imu-actions">
      <button class="btn-dark" onClick=${reset}>Restart</button>
      <span class="sv-small">Review the error below and retry.</span>
    </div>` : null}
    ${out ? html`<div class=${'banner ' + (out.ok ? 'warn' : 'bad')} role="status"><span>${out.text}</span></div>` : null}
  </div>`;
}

// ---------------- Import IMU Mount ----------------

const IMPORT_MAX_BYTES = 256 * 1024;
function mountFromFile(doc) {
  if (!doc || typeof doc !== 'object') throw new Error('Invalid orientation file');
  const records = Array.isArray(doc.records) ? doc.records.map(r => r?.imu) : [];
  const found = [doc, doc.imu, doc.mount, ...records].filter(m => m && typeof m === 'object' && 'quat' in m);
  if (!found.length) throw new Error('No IMU orientation found in file');
  if (found.length > 1) throw new Error(`File contains ${found.length} orientations; expected 1`);
  const { source, quat } = found[0];
  if (!['bus', 'bno08x', 'lsm6dsv16x'].includes(source)) throw new Error('Missing valid IMU source type');
  if (!Array.isArray(quat) || quat.length !== 4 || !quat.every(Number.isFinite)) {
    throw new Error('Quaternion must contain 4 finite numbers');
  }
  if (Math.abs(Math.hypot(...quat) - 1) > 1e-3) throw new Error('Quaternion is not unit length');
  return { source, quat };
}
function ImportCard({ link, live }) {
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState(null);
  const input = useRef(null);
  const off = live === false || link.status !== 'open';
  const pick = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setOut(null);
    let mount;
    try {
      if (file.size > IMPORT_MAX_BYTES) throw new Error('File too large');
      let doc;
      try { doc = JSON.parse(await file.text()); } catch { throw new Error('Invalid JSON file'); }
      mount = mountFromFile(doc);
    } catch (err) { setOut({ ok: false, text: `Import failed: ${err.message}` }); return; }
    setBusy(true);
    const r = await link.cmd('imu.op', { op: 'import-mount', source: mount.source, quat: mount.quat }, 15000);
    setBusy(false);
    setOut(r.ok
      ? { ok: true, text: `Imported${r.data?.standard?.text ? ` (${r.data.standard.text})` : ''}. Commit in Onboarding Step 3 to apply.` }
      : { ok: false, text: `Import failed: ${r.reason || r.error || 'unknown error'}` });
  };
  return html`<section class="board imu-card imu-import">
    <h3>Import IMU Orientation <span class="sv-small">Load from an existing calibration JSON</span></h3>
    <div class="imu-actions">
      <button class="btn-light imu-import-btn" disabled=${busy || off} onClick=${() => input.current?.click()}>${busy ? 'Importing…' : 'Import IMU JSON…'}</button>
      <input ref=${input} class="imu-import-file" type="file" accept=".json,application/json" hidden onChange=${pick} />
      <span class="sv-small">Must match this robot's physical IMU mounting.</span>
    </div>
    ${out ? html`<p class=${out.ok ? 'sv-small imu-import-out' : 'sv-warn imu-import-out'} role="status">${out.text}
      ${out.ok ? html` <a href="#/" onClick=${() => setTimeout(() => document.querySelector('[data-step="commit"]')?.scrollIntoView({ block: 'center' }), 300)}>Go to Onboarding</a>` : null}</p>` : null}
  </section>`;
}

function JobBanner({ job }) {
  if (!job) return null;
  const kind = job.failed ? 'bad' : 'warn';
  return html`<div class=${'banner imu-job ' + kind} role="status" aria-live="polite">
    <b>${job.title || 'Working'}</b><span class="more">${job.msg || ''}</span>
    ${job.tilt_deg != null ? html`<span class="imu-job-tilt num">${fmt(job.tilt_deg)}°${job.target_deg ? html`<small> / ${job.target_deg}</small>` : null}</span>` : null}
    ${job.hold != null && !job.done ? html`<span class="imu-bar small"><span style=${{ width: `${Math.round(Math.min(1, job.hold) * 100)}%` }}></span></span>` : null}
  </div>`;
}

export const _test = { RULES, STEPS, TILTS, feed, freshWizard, startCollect, endCollect, resumeCollect, redoSteps,
  missingSteps, payload, findHold, findRef, spreadP90, tiltOf, qRotate };

// ---------------- Page ----------------

export function ImuPage({ st, imu, link, live, age, diagnosticElapsed = 0, pd }) {
  const W = useRef(freshWizard());
  const [, bump] = useState(0);
  const redraw = () => bump((n) => n + 1);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [muted, setMutedState] = useState(loadMute);
  const mutedRef = useRef(muted);
  const setMuted = (v) => { mutedRef.current = v; setMutedState(v); saveMute(v); };
  const context = imuContext(st?.mode || imu?.mode, imu);

  useEffect(() => {
    W.current = freshWizard(); setRes(null); setMsg(null); redraw();
  }, [context]);

  useEffect(() => {
    if (!feed(W.current, st, live)) return;
    for (const e of W.current.events.splice(0)) if (!mutedRef.current) beep(e);
    redraw();
  }, [st, live]);

  const phase = W.current.phase;
  useEffect(() => {
    if (phase !== 'solving') return undefined;
    let dead = false;
    const w = W.current;
    const basis = w.mountBasis ? { basis_mount_wxyz: w.mountBasis } : {};
    link.cmd('imu.op', { op: 'solve', frame: 'body', mode: 'full', samples: payload(w), ...basis }, 15000).then((r) => {
      if (dead || W.current !== w) return;
      if (r.ok && r.data) { w.phase = 'done'; w.result = r.data; setRes(r.data); }
      else { w.phase = 'error'; w.error = r.reason || r.error || 'Solve failed'; }
      redraw();
    });
    return () => { dead = true; };
  }, [phase, context]);

  const act = {
    start: () => {
      audioUnlock();
      startCollect(W.current, performance.now() / 1000);
      W.current.mountBasis = imu?.coarse_sampling?.live_verify ? imu.mount?.quat : null;
      setRes(null);
      redraw();
    },
    end: () => { endCollect(W.current); redraw(); },
    resume: () => { audioUnlock(); resumeCollect(W.current); redraw(); },
  };
  const onRedo = (steps) => { audioUnlock(); redoSteps(W.current, steps); setRes(null); redraw(); };

  const w = W.current;
  const S = statusOf(st, imu, live);
  const F = freshText(st, live, age);
  const applied = res?.live_verify && imu?.mount?.verified &&
    res.mount?.every((value, i) => Math.abs(value - imu.mount.quat?.[i]) < 1e-8);
  const arrow = w.phase === 'collect' && w.done.still ? nextTodo(w) : null;
  const cue = w.phase === 'collect' ? (!w.done.still ? 'Hold still for 3s' : arrow ? `Next: ${STEP[arrow].cn} (any order)` : null) : null;
  return html`<div class="imu-page">
    <header class="sv-head">
      <h1>IMU</h1>
      <span class="sv-metrics num">
        <span>Mount <b>${S.word}</b></span>
        <span>Tilt <b>${fmt(st?.tilt_deg)}°</b>${F.live ? '' : ` (${F.text})`}</span>
        <span>Sensor <b>${chipLabel(imu, live, diagnosticElapsed)}</b></span>
      </span>
    </header>
    <div class="sv-sub">IMU calibration aligns the sensor frame to the robot body so locomotion balance stays centered without forward or lateral drift.</div>
    <${JobBanner} job=${imu?.job} />
    ${msg ? html`<div class=${'banner ' + (msg.ok ? 'warn' : 'bad')} role="status"><span>${msg.text}</span>
      <a href="#" onClick=${(e) => { e.preventDefault(); setMsg(null); }}>Dismiss</a></div>` : null}
    <div class="imu-grid">
      <${ImuStage} st=${st} stale=${!F.live} corr=${res?.ok && !applied ? res.correction : null} parts=${res?.parts}
        collecting=${w.phase === 'collect'} arrow=${arrow} cue=${cue} />
      <div class="imu-side">
        <${StatusCard} st=${st} imu=${imu} live=${live} age=${age} diagnosticElapsed=${diagnosticElapsed} />
        <${DiagnosticCard} st=${st} imu=${imu} live=${live} diagnosticElapsed=${diagnosticElapsed} />
        <${CoarseCard} st=${st} imu=${imu} link=${link} live=${live} />
        <${StanceCard} st=${st} imu=${imu} pd=${pd} link=${link} live=${live} />
        <${CollectCard} st=${st} imu=${imu} live=${live} W=${w} act=${act} muted=${muted} setMuted=${setMuted} />
        <${ResultCard} res=${w.phase === 'done' ? res || w.result : null} imu=${imu} link=${link} live=${live} onRedo=${onRedo} />
        <${ImportCard} link=${link} live=${live} />
        <${ChipCard} imu=${imu} link=${link} live=${live} diagnosticElapsed=${diagnosticElapsed} />
      </div>
    </div>
  </div>`;
}
