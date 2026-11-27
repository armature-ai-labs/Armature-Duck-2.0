// Gamepad & Remote Control section (embedded in Overview at #/pad).
// Backend: studio/pad.py & studio/pad_drive.py.
import { html, useState, useEffect, useRef, useCallback } from './vendor/preact-htm.module.js';
import { Duck3D } from './duck3d.js';
import { InlineCamera } from './sensors.js';
import { MotionStudio } from './motion-studio.js';
import { PadMapping } from './pad-mapping.js';
import { AudioInteraction } from './audio-interaction.js';

const SEND_MS = 100;        // 10 Hz stick stream while held; 0 sent on release (0.5s deadman on robotd)
const WATCH_MS = 2000;
const HOLD_MS = 150;        // Keepalive for held actions/keys (backend times out after 0.45s)
const MODE_HB_MS = 1000;    // Head/body mode heartbeat
const MODE_GRACE_MS = 800;  // Grace period after Y/B mode switch before adopting server snapshot
const DRIVE_HOLD_MS = 3000; // D-Pad Up hold duration to toggle walk/roller
const KEY_STALE_MS = 1000;  // Release stuck keyboard keys if repeat stops for >1s
const BUSY_MS = 3000;       // Post-command busy cooldown for Stand/Walk/Sit
const DZ = 0.1;             // Stick deadzone (matches padd)
const STICK_TRAVEL = 40;    // Virtual stick drag radius in SVG units
const WATCH_SETTLE_MS = 300;
const TAP_MS = 120;
const TAP_MOVE_PX = 8;
const SCROLL_GUARD_MS = 500;
const STICK_GRAB = 30;
let SEQ = 0;
const nextSeq = () => ++SEQ;

// ---------------- Pad Activity Tracking ----------------

const ACT = { route: location.hash.replace(/^#/, '') || '/', visible: document.visibilityState === 'visible',
  inView: false, active: false, subs: new Set(), link: null };
function actRecompute(why) {
  const a = (ACT.route === '/' || ACT.route === '/pad') && ACT.visible && ACT.inView;
  const changed = a !== ACT.active;
  ACT.active = a;
  for (const fn of ACT.subs) {
    try { fn({ active: a, inView: ACT.inView, changed }, why); } catch (e) { console.error('padActivity subscriber error', e); }
  }
  if (changed && ACT.link) ACT.link.heartbeat();
}
export const padActivity = {
  get active() { return ACT.active; },
  get inView() { return ACT.inView; },
  bind(link) { ACT.link = link; },
  setRoute(route) { ACT.route = route || '/'; actRecompute(`navigated to #${ACT.route}`); },
  setInView(v) { if (v !== ACT.inView) { ACT.inView = v; actRecompute('scrolled out of pad section'); } },
  subscribe(fn) { ACT.subs.add(fn); return () => ACT.subs.delete(fn); },
};
document.addEventListener('visibilitychange', () => { ACT.visible = document.visibilityState === 'visible'; actRecompute('page hidden'); });
window.__pad = padActivity;

const GO = new Set();
export function padGo(what) { for (const fn of GO) fn(what); }

// Touch scroll guard
let lastScrollAt = -1e9;
window.addEventListener('scroll', () => { lastScrollAt = performance.now(); }, { passive: true, capture: true });
const scrolledRecently = () => performance.now() - lastScrollAt < SCROLL_GUARD_MS;
const isTouch = (e) => e.pointerType === 'touch' || e.pointerType === 'pen';
function touchHold(e, { press, release, tap }) {
  const pid = e.pointerId, x0 = e.clientX, y0 = e.clientY;
  let phase = 'pending';
  const off = () => {
    clearTimeout(timer);
    window.removeEventListener('pointermove', mv, true);
    window.removeEventListener('pointerup', up, true);
    window.removeEventListener('pointercancel', cc, true);
  };
  const mv = (ev) => {
    if (ev.pointerId !== pid || phase !== 'pending') return;
    if (Math.hypot(ev.clientX - x0, ev.clientY - y0) >= TAP_MOVE_PX) { phase = 'gone'; off(); }
  };
  const up = (ev) => {
    if (ev.pointerId !== pid) return;
    const was = phase; phase = 'gone'; off();
    if (was === 'pending' && tap) tap(); else if (was === 'held' && release) release();
  };
  const cc = (ev) => {
    if (ev.pointerId !== pid) return;
    const was = phase; phase = 'gone'; off();
    if (was === 'held' && release) release();
  };
  const timer = setTimeout(() => { if (phase !== 'pending') return; phase = 'held'; window.removeEventListener('pointermove', mv, true); press(); }, TAP_MS);
  window.addEventListener('pointermove', mv, true);
  window.addEventListener('pointerup', up, true);
  window.addEventListener('pointercancel', cc, true);
  return () => { if (phase === 'held' && release) release(); phase = 'gone'; off(); };
}

// ---------------- Controller Layouts & Pairing ----------------

const KINDS = {
  xbox: {
    label: 'Xbox Controller', layout: 'xbox',
    names: { south: 'A', east: 'B', west: 'X', north: 'Y', lb: 'LB', rb: 'RB', lt: 'LT', rt: 'RT', select: 'View', start: 'Menu', home: 'Xbox', l3: 'L3', r3: 'R3' },
    how: ['Press the Xbox button briefly to power on (do not hold).', 'Press the small Sync button near the USB-C port until the Xbox LED flashes rapidly.', 'Xbox controllers remember one host — unpair from PC/phone first if pairing fails.'],
  },
  ps: {
    label: 'PS5 / PS4 Controller', layout: 'ps',
    names: { south: '×', east: '○', west: '□', north: '△', lb: 'L1', rb: 'R1', lt: 'L2', rt: 'R2', select: 'Create', start: 'Options', home: 'PS', l3: 'L3', r3: 'R3' },
    how: ['Hold Create + PS simultaneously until the lightbar flashes rapidly.', 'Verify button mapping in the diagram below after pairing.'],
  },
  switch: {
    label: 'Switch Pro (incl. G50 Pro)', layout: 'switch',
    names: { south: 'B', east: 'A', west: 'Y', north: 'X', lb: 'L', rb: 'R', lt: 'ZL', rt: 'ZR', select: '−', start: '+', home: 'Home', l3: 'L3', r3: 'R3' },
    how: ['For G50 Pro: set rear mode switch to S (Switch mode).', 'Hold the small Sync button near USB-C until the player LEDs cycle.'],
  },
  generic: {
    label: 'Other Controller', layout: 'xbox',
    names: { south: 'Down', east: 'Right', west: 'Left', north: 'Up', lb: 'L1', rb: 'R1', lt: 'L2', rt: 'R2', select: 'Select', start: 'Start', home: 'Home', l3: 'L3', r3: 'R3' },
    how: ['Follow your controller manual to enter Bluetooth pairing mode (rapid LED flash).', 'If it has a mode switch, prefer Switch or X-input over Android mode.'],
  },
};
const kindOf = (k) => KINDS[k] || KINDS.generic;

const ROLLER_NOTE = ' (active in Roller mode)';
const FUNCS = [
  { role: 'ls', group: 'main', name: () => 'Left Stick', does: 'Walk: forward/back & strafe · Head mode: yaw & pitch · Body mode: height/crouch',
    web: 'Web: relative to initial drag origin; centers on release' },
  { role: 'rs', group: 'main', name: () => 'Right Stick', does: 'Walk: turn · Head mode: neck pitch & head roll · Body mode: body tilt' },
  { role: 'start', group: 'main', does: 'Relaxed/Sitting: stand up · Standing: start walking (policy on) · Walking: stop to standing',
    web: 'Web: executes the current posture transition shown below the diagram' },
  { role: 'select', group: 'main', does: 'Short press: E-Stop (relax all servos immediately)', danger: 'Hold 2s: sit down, relax & power off',
    web: 'Web: triggers E-Stop immediately on press (never powers off)' },
  { role: 'dd', group: 'main', name: () => 'D-Pad Down', does: 'Sit ↔ Stand', bind: 'dpad_down', web: 'Web: toggles Sit / Stand' },
  { role: 'south', group: 'skill', does: 'Ground Pick', bind: 'a' },
  { role: 'west', group: 'skill', does: 'Roll (hold for continuous)', bind: 'x' },
  { role: 'lb', group: 'skill', does: 'Left Kick', bind: 'lb' },
  { role: 'rb', group: 'skill', does: 'Right Kick', bind: 'rb' },
  { role: 'rt', group: 'skill', does: 'Open beak & chirp' },
  { role: 'lt', group: 'skill', does: 'Open beak; hold for "wheee"' },
  { role: 'north', group: 'mode', does: 'Head Mode: sticks control head; press again to return to Walk', imu: true },
  { role: 'east', group: 'mode', does: 'Body Mode: sticks tilt/crouch body; press again to return to Walk' },
  { role: 'dr', group: 'other', name: () => 'D-Pad Right', does: 'Reboot all servos (clears overload fault): relaxes first, then press Start to stand',
    web: 'Web: sends servo reboot immediately (relaxes robot first)' },
  { role: 'du', group: 'other', name: () => 'D-Pad Up', does: 'Hold 3s: toggle Walk / Roller mode (requires roller hardware)',
    web: 'Web: requires 3s continuous hold' },
  { role: 'dl', group: 'other', name: () => 'D-Pad Left', does: `Unused in Walk mode${ROLLER_NOTE}`, off: `Unused in Walk mode${ROLLER_NOTE}` },
  { role: 'home', group: 'other', does: 'Unused on robot (controller power button)', off: 'Unused' },
  { role: 'l3', group: 'other', does: `Unused in Walk mode${ROLLER_NOTE}`, off: `Unused in Walk mode${ROLLER_NOTE}` },
  { role: 'r3', group: 'other', does: 'Unused', off: 'Unused' },
];
const FUNC = Object.fromEntries(FUNCS.map((f) => [f.role, f]));
const ROLE_BIND = { south: 'a', west: 'x', lb: 'lb', rb: 'rb', dd: 'dpad_down' };
const ROLE_HELD = { lt: 'lt', rt: 'rt', west: 'x' };
const MODE_CN = { drive: 'Walk', head: 'Head Mode', body: 'Body Mode' };
const DRIVE_CN = { walk: 'Walk', roller: 'Roller' };
const SKILL_CN = { ground_pick: 'Ground Pick', roulade: 'Roll', kick_left: 'Left Kick', kick_right: 'Right Kick', sit_toggle: 'Sit ↔ Stand', roller_stand: 'Roller Stand' };
const DEFAULT_BIND = { a: 'ground_pick', x: 'roulade', lb: 'kick_left', rb: 'kick_right', dpad_down: 'sit_toggle' };
const BIND_ROLE = { a: 'south', x: 'west', lb: 'lb', rb: 'rb', dpad_down: 'dd' };

const STEP_ICON = { ok: '✓', fail: '×', bad: '×', doing: '', todo: '', skip: '–', unk: '?' };
const fmtMs = (ms) => (ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const cx = (...xs) => xs.filter(Boolean).join(' ');

// ---------------- Interactive Gamepad SVG ----------------

const LAYOUT = {
  xbox: { ls: [120, 100], dpad: [158, 152], face: [282, 100], rs: [243, 152], select: [172, 98], start: [228, 98], home: [200, 70] },
  switch: { ls: [120, 100], dpad: [158, 152], face: [282, 100], rs: [243, 152], select: [170, 82], start: [230, 82], home: [214, 112] },
  ps: { ls: [158, 156], dpad: [118, 100], face: [282, 100], rs: [242, 156], select: [150, 64], start: [250, 64], home: [200, 118], touch: true },
};
const NOTES = {
  xbox: { select: [170, 119], start: [236, 119], x: [253, 85], dr: [185, 137, 'start'], dd: [158, 190] },
  switch: { select: [170, 64], start: [230, 64], x: [256, 85], dr: [185, 137, 'start'], dd: [158, 190] },
  ps: { select: [135, 68, 'end'], start: [250, 52], x: [250, 85], dr: [147, 104, 'start'], dd: [104, 134] },
};
const HIT_W = 34, HIT_H = 28;
const BODY = 'M128 44C96 44 70 52 56 74C40 100 26 150 22 190C19 222 38 240 60 240C80 240 92 226 104 206C114 190 124 180 144 180L256 180C276 180 286 190 296 206C308 226 320 240 340 240C362 240 381 222 378 190C374 150 360 100 344 74C330 52 304 44 272 44C240 44 226 52 200 52C174 52 160 44 128 44Z';
const SHOULDER = { l: { x: 80, w: 72 }, r: { x: 248, w: 72 } };
const TRIGGER = { l: { x: 62, w: 62 }, r: { x: 276, w: 62 } };
const SH_Y = 26, SH_H = 24, TR_Y = -10, TR_H = 28;

function GamepadSvg({ kind, live, seen, ctl, keys, learn, mappingView }) {
  const k = kindOf(kind);
  const L = LAYOUT[k.layout] || LAYOUT.xbox;
  const b = live?.buttons || {};
  const ax = live?.axes || {};
  const on = (r) => !!b[r];
  const was = (r) => !!(seen && seen.includes(r));
  const N = NOTES[k.layout] || NOTES.xbox;
  const does = (role) => {
    const row = keys?.[ROLE_BIND[role]];
    if (row && typeof row.does === 'string' && row.does) return row.does;
    const skill = DEFAULT_BIND[ROLE_BIND[role]];
    return SKILL_CN[skill] || skill || '';
  };
  const noteAt = (text, [x, y, anchor = 'middle'], extra) => mappingView ? null : html`<text class=${cx('pd-key-note', extra)} x=${x} y=${y} text-anchor=${anchor} aria-hidden="true">${text}</text>`;
  const act = (r) => {
    if (learn) return { info: { ok: true, does: 'Learn input' }, say: `Learn ${k.names[r] || r}`, props: {
      role: 'button', tabIndex: 0, 'aria-label': `Learn ${k.names[r] || r}`, 'data-role': r,
      onClick: () => learn(r), onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); learn(r); } },
    } };
    if (!ctl) return { info: null, props: {} };
    const info = ctl.info(r);
    const f = FUNC[r];
    const name = f?.name ? f.name() : k.names[r] || r;
    const say = `${name}: ${info.ok ? info.does : info.why}`;
    return { info, say, props: {
      role: 'button', 'aria-label': say, 'aria-disabled': info.ok ? 'false' : 'true', 'data-role': r,
      onPointerDown: (e) => ctl.down(r, e), onPointerUp: (e) => ctl.up(r, e), onPointerCancel: (e) => ctl.up(r, e),
      onLostPointerCapture: (e) => ctl.up(r, e), onContextMenu: (e) => e.preventDefault(),
    } };
  };
  const tip = (a) => (a.info ? html`<title>${a.say}</title>` : null);
  const offc = (a) => a.info && !a.info.ok && !ctl.mirror && 'pd-key-off';
  const cls = (r, a, extra) => cx('pd-key', on(r) && 'pd-on', was(r) && 'pd-seen', r === 'select' && 'pd-estop',
    a.info && 'pd-press', offc(a), extra);
  const scale = (e) => (e.currentTarget.ownerSVGElement || e.currentTarget).getBoundingClientRect().width / 400;
  const stick = (r, [x0, y0], x, y, press) => {
    const t = 12;
    const a = act(r);
    const side = r === 'ls' ? 'l' : 'r';
    const sp = learn ? a.props : ctl ? { role: 'button', 'aria-label': a.say, 'data-role': r, onContextMenu: (e) => e.preventDefault(),
      onPointerDown: (e) => ctl.stickDown(side, e, scale(e) * STICK_TRAVEL, false), onPointerMove: (e) => ctl.stickMove(side, e),
      onPointerUp: (e) => ctl.stickUp(side, e), onPointerCancel: (e) => ctl.stickUp(side, e),
      onLostPointerCapture: (e) => ctl.stickUp(side, e) } : {};
    return html`<g class=${cx('pd-stick', on(press) && 'pd-on', was(r) && 'pd-seen', a.info && 'pd-press', offc(a))} ...${sp}>
      <circle class="pd-stick-base" cx=${x0} cy=${y0} r="27"></circle>
      <circle class="pd-stick-cap" cx=${x0 + clamp(x || 0, -1, 1) * t} cy=${y0 + clamp(y || 0, -1, 1) * t} r="17"></circle>
      <text x=${x0} y=${y0 + 44} class="pd-cap">${r === 'ls' ? 'L' : 'R'}</text>
      ${ctl ? html`<circle class="pd-hit" cx=${x0} cy=${y0} r="34"></circle>` : null}${tip(a)}
    </g>`;
  };
  const face = ([x0, y0]) => [['north', 0, -23], ['south', 0, 23], ['west', -23, 0], ['east', 23, 0]].map(([r, dx, dy]) => {
    const a = act(r);
    return html`<g class=${cls(r, a)} ...${a.props}>
      <circle cx=${x0 + dx} cy=${y0 + dy} r="11.5"></circle>
      <text x=${x0 + dx} y=${y0 + dy + 4}>${k.names[r]}</text>${tip(a)}</g>`;
  });
  const dkey = (r, x, y, w, h) => {
    const a = act(r);
    return html`<g class=${cls(r, a)} ...${a.props}><rect x=${x} y=${y} width=${w} height=${h} rx="3"></rect>${tip(a)}</g>`;
  };
  const dpad = ([x0, y0]) => html`<g>
    ${dkey('du', x0 - 7, y0 - 25, 14, 18)}${dkey('dd', x0 - 7, y0 + 7, 14, 18)}
    ${dkey('dl', x0 - 25, y0 - 7, 18, 14)}${dkey('dr', x0 + 7, y0 - 7, 18, 14)}
    <rect class="pd-dpad-mid" x=${x0 - 7} y=${y0 - 7} width="14" height="14"></rect></g>`;
  const trig = (r, side) => {
    const { x, w } = TRIGGER[side];
    const v = clamp(ax[r] ?? (on(r) ? 1 : 0), 0, 1);
    const pressed = on(r) || v > 0.5;
    const a = act(r);
    return html`<g class=${cx('pd-key', 'pd-trig', pressed && 'pd-on', was(r) && 'pd-seen', v > 0.95 && 'pd-trig-hi', a.info && 'pd-press', offc(a))} ...${a.props}>
      <rect x=${x} y=${TR_Y} width=${w} height=${TR_H} rx="9"></rect>
      ${v > 0.02 ? html`<rect class="pd-fill" x=${x} y=${TR_Y + TR_H * (1 - v)} width=${w} height=${TR_H * v} rx=${v > 0.3 ? 9 : 3}></rect>` : null}
      <text x=${x + w / 2} y=${TR_Y + 18}>${k.names[r]}</text>${tip(a)}</g>`;
  };
  const shoulder = (r, side) => {
    const { x, w } = SHOULDER[side];
    const a = act(r);
    return html`<g class=${cls(r, a, 'pd-shoulder')} ...${a.props}>
      <rect x=${x} y=${SH_Y} width=${w} height=${SH_H} rx="10"></rect>
      <text x=${x + w / 2} y=${SH_Y + 12.5}>${k.names[r]}</text>${tip(a)}</g>`;
  };
  const small = (r, [x0, y0], text) => {
    const a = act(r);
    return html`<g class=${cls(r, a)} ...${a.props}><rect x=${x0 - 13} y=${y0 - 7} width="26" height="14" rx="7"></rect>
      <text x=${x0} y=${y0 + 3.5} class="pd-small">${text}</text>
      ${ctl ? html`<rect class="pd-hit" x=${x0 - HIT_W / 2} y=${y0 - HIT_H / 2} width=${HIT_W} height=${HIT_H} rx="8"></rect>` : null}${tip(a)}</g>`;
  };
  const home = () => {
    const a = act('home');
    return html`<g class=${cls('home', a)} ...${a.props}><circle cx=${L.home[0]} cy=${L.home[1]} r="10"></circle>${tip(a)}</g>`;
  };
  const mode = ctl?.mode && ctl.mode !== 'drive' ? MODE_CN[ctl.mode] : null;
  const pct = (x, y, w, h) => ({ left: `${(x / 400) * 100}%`, top: `${((y + 16) / 266) * 100}%`, width: `${(w / 400) * 100}%`, height: `${(h / 266) * 100}%` });
  const travel = (e) => (e.currentTarget.parentElement.getBoundingClientRect().width / 400) * STICK_TRAVEL;
  const ovStick = (r, [x0, y0]) => {
    const side = r === 'ls' ? 'l' : 'r';
    return html`<span class="pd-ov pd-ov-stick" data-ov=${r} aria-hidden="true" style=${pct(x0 - STICK_GRAB, y0 - STICK_GRAB, STICK_GRAB * 2, STICK_GRAB * 2)}
      onContextMenu=${(e) => e.preventDefault()}
      onPointerDown=${(e) => ctl.stickDown(side, e, travel(e), true)} onPointerMove=${(e) => ctl.stickMove(side, e)}
      onPointerUp=${(e) => ctl.stickUp(side, e)} onPointerCancel=${(e) => ctl.stickUp(side, e)}
      onLostPointerCapture=${(e) => ctl.stickUp(side, e)}></span>`;
  };
  const [sx, sy] = L.select;
  return html`<div class="pd-svgbox">
    <svg class=${cx('pd-svg', ctl && 'pd-svg-live')} viewBox="0 -16 400 266" role=${ctl || learn ? 'group' : 'img'}
    aria-label=${learn ? 'Click a standard button to learn physical controller input (test only)' : ctl ? `${k.label}: interactive virtual gamepad` : `${k.label} diagram`} onContextMenu=${ctl ? (e) => e.preventDefault() : undefined}>
    ${trig('lt', 'l')}${trig('rt', 'r')}
    ${shoulder('lb', 'l')}${shoulder('rb', 'r')}
    <path class="pd-body" d=${BODY}></path>
    ${L.touch ? html`<rect class="pd-touch" x="168" y="54" width="64" height="40" rx="8"></rect>` : null}
    ${mode ? html`<g class="pd-modetag"><rect x="164" y="22" width="72" height="20" rx="10"></rect><text x="200" y="36">${mode}</text></g>` : null}
    ${stick('ls', L.ls, ax.lx, ax.ly, 'l3')}
    ${stick('rs', L.rs, ax.rx, ax.ry, 'r3')}
    ${dpad(L.dpad)}
    ${face(L.face)}
    ${small('select', L.select, mappingView ? 'SEL' : k.names.select.length > 2 ? '' : k.names.select)}
    ${small('start', L.start, mappingView ? 'START' : k.names.start.length > 2 ? '' : k.names.start)}
    ${home()}
    ${noteAt('Beak', [56, 7, 'end'])}
    ${noteAt('Beak · Chirp', [344, 7, 'start'])}
    ${noteAt(does('lb'), [74, 41, 'end'])}
    ${noteAt(does('rb'), [326, 41, 'start'])}
    ${noteAt('Head Mode', [282, 60])}
    ${noteAt('Body Mode', [321, 104, 'start'])}
    ${noteAt(does('south'), [321, 129, 'start'])}
    ${noteAt(does('west'), N.x)}
    ${noteAt('Stand / Walk', N.start, 'pd-key-note-main')}
    ${noteAt('E-Stop (Relax)', N.select, 'pd-key-note-main pd-note-danger')}
    ${noteAt('Reboot Servos', N.dr)}
    ${noteAt(does('dd'), N.dd)}
    </svg>
    ${ctl ? html`${ovStick('ls', L.ls)}${ovStick('rs', L.rs)}
      <span class="pd-ov pd-ov-select" data-ov="select" aria-hidden="true" style=${pct(sx - HIT_W / 2, sy - HIT_H / 2, HIT_W, HIT_H)}
        onContextMenu=${(e) => e.preventDefault()} onPointerDown=${(e) => ctl.down('select', e)}
        onPointerUp=${(e) => ctl.up('select', e)} onPointerCancel=${(e) => ctl.up('select', e)}></span>` : null}
  </div>`;
}

// ---------------- Button Legend ----------------

function KeyName({ f, k }) {
  const n = f.name ? f.name() : k.names[f.role];
  return html`<span class="pd-chip">${n}</span>`;
}

function Legend({ kind, live, bindings, imuHead }) {
  const k = kindOf(kind);
  const b = live?.buttons || {};
  const ax = live?.axes || {};
  const moved = (r) => (r === 'ls' ? Math.hypot(ax.lx || 0, ax.ly || 0) > 0.25 : r === 'rs' ? Math.hypot(ax.rx || 0, ax.ry || 0) > 0.25
    : r === 'lt' || r === 'rt' ? (ax[r] || 0) > 0.3 : false);
  const bound = (f) => {
    const x = f.bind && (bindings || []).find((y) => y.button === f.bind);
    if (!x || !x.overridden) return null;
    return x.skill ? `Remapped to: ${SKILL_CN[x.skill] || x.skill}${x.error ? ` (${x.error})` : ''}` : 'Disabled';
  };
  const does = (f) => {
    if (f.imu && imuHead) return 'Physical pad: head tracks controller gyro while sticks walk; press again to freeze';
    return bound(f) || f.does;
  };
  const web = (f) => (f.imu ? (imuHead ? 'Web: sticks control head (no gyro on browser)' : imuHead == null ? 'Web: sticks control head' : null)
    : f.off ? `Web: disabled (${f.off})` : f.web);
  const row = (f) => html`<div class=${cx('pd-row', (b[f.role] || moved(f.role)) && 'pd-on')}>
    <${KeyName} f=${f} k=${k} />
    <span class="pd-does">${does(f)}${f.danger ? html`<br /><b class="pd-danger">${f.danger}</b>` : null}
      ${web(f) ? html`<small class="pd-web">${web(f)}</small>` : null}</span>
  </div>`;
  const group = (g) => FUNCS.filter((f) => f.group === g).map(row);
  return html`<div class="pd-legend">
    <div class="pd-legend-h">Primary</div>${group('main')}
    <div class="pd-legend-h">Skills</div>${group('skill')}
    <div class="pd-legend-h">Stick Modes</div>${group('mode')}
    <details class="pd-other"><summary>Other Buttons</summary>${group('other')}</details>
    ${k.layout === 'switch' ? html`<p class="pd-note">Switch A/B positions are swapped relative to Xbox. Functions follow physical position (bottom face button = ${k.names.south}).</p>` : null}
    ${live?.device?.keys_unverified ? html`<p class="pd-note">Xbox X/Y positions are inferred; verify on hardware.</p>` : null}
    ${live?.other?.length ? html`<p class="pd-note">Unmapped inputs pressed: ${live.other.join(', ')}</p>` : null}
  </div>`;
}

// ---------------- Header Summary ----------------

function Summary({ pad }) {
  const s = pad?.status || {};
  const live = pad?.live || {};
  const act = s.active;
  const es = pad?.estop || {};
  const name = act ? act.name || 'Gamepad' : live.state === 'attached' ? live.device?.name || 'Gamepad' : null;
  const mark = es.state === 'ok' ? html`<span class="pd-ok">Verified ✓</span>`
    : es.state === 'unknown' ? html`<span class="pd-unk">Unverified ?</span>` : html`<span class="pd-bad">Failed ×</span>`;
  if (!s.known && live.state !== 'attached') return html`<span class="pd-sum">${s.error ? 'Gamepad status unavailable' : 'Reading gamepad status…'}${' · Pad E-Stop '}${mark}</span>`;
  const tail = name ? ' · Connected' : (s.pads || []).length ? ` · ${(s.pads || []).length} paired, none connected` : '';
  return html`<span class="pd-sum"><b>${name || 'No Gamepad Connected'}</b>${tail}${' · Pad E-Stop '}${mark}</span>`;
}

function EStopChecks({ pad }) {
  const checks = pad?.estop?.checks || [];
  return html`<ul class="pd-checks">${checks.map((c) => {
    const s = c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'unk';
    return html`<li class=${'pd-s-' + s}>
      <span class="pd-mark">${STEP_ICON[s]}</span>
      <span>${c.label}${c.why ? html`<small>${c.why}</small>` : null}</span></li>`;
  })}</ul>`;
}

// ---------------- E-Stop Verification Test ----------------

const TEST_STEPS = [
  ['torque', 'Support the robot and press Start on the gamepad to stand up'],
  ['press', 'Briefly press Select (do not hold 2s, or it will power off)'],
  ['readback', 'Wait for robotd readback confirming torque is disabled'],
];
const TEST_AT = { need_torque: 0, armed: 1, pressed: 1, released: 2 };
const FAIL_AT = { torque: 0, press: 1, early_off: 1, long: 1, pressed_off: 1, pressed_unknown: 1, readback: 2, relax: 2, studio: 2 };
function testStepState(t, i) {
  if (t.state === 'passed') return 'ok';
  if (t.state === 'failed') {
    const f = FAIL_AT[t.failed_at] ?? 2;
    return i < f ? 'ok' : i === f ? 'fail' : 'todo';
  }
  const at = TEST_AT[t.state];
  if (at === undefined) return 'todo';
  return i < at ? 'ok' : i === at ? 'doing' : 'todo';
}

function EStopTest({ pad, op }) {
  const t = pad?.estop_test || { state: 'idle' };
  const ok = pad?.estop?.ok;
  const running = !['idle', 'passed', 'failed'].includes(t.state);
  return html`<section class="board pd-card" id="pd-estop" tabIndex="-1">
    <h3>Gamepad E-Stop Test</h3>
    <${EStopChecks} pad=${pad} />
    <div class="pd-test">
      ${t.state === 'idle' ? html`<p class="pd-small-p">All checks above confirm the software path. Press Select once to verify servos actually relax.</p>` : null}
      ${t.state !== 'idle' ? html`<ol class="pd-steps">${TEST_STEPS.map(([key, text], i) => {
        const s = testStepState(t, i);
        return html`<li class=${'pd-s-' + s}><span class="pd-mark">${STEP_ICON[s] || ''}</span><span>${text}
          ${key === 'press' && t.state === 'pressed' ? html`<small>Held ${t.held_s ?? 0}s… release now</small>` : null}</span></li>`;
      })}</ol>` : null}
      ${t.why && running ? html`<p class="pd-small-p">${t.why}</p>` : null}
      ${t.held_long && running ? html`<div class="banner warn" role="status"><b>Select held too long — release now</b><span class="more">Holding for 2s triggers sit & shutdown and cancels this test.</span></div>` : null}
      ${t.state === 'passed' ? html`<div class="pd-result pd-good">✓ E-Stop verified: Select release to torque-off readback took ${fmtMs(t.ms)}.<br />Press Start to stand again.</div>` : null}
      ${t.state === 'failed' ? html`<div class="pd-result pd-fail"><b>× Failed: ${t.why || 'Unknown reason'}</b>${t.fix ? html`<br />${t.fix}` : null}</div>` : null}
      <div class="pd-btns">
        ${running
          ? html`<button class="btn-light" onClick=${() => op({ op: 'estop_test', action: 'cancel' })}>Cancel Test</button>`
          : html`<button class="btn-dark" id="pd-estop-btn" disabled=${!ok} title=${ok ? '' : 'All prerequisite checks above must pass first'} onClick=${() => op({ op: 'estop_test', action: 'start' })}>${t.state === 'idle' ? 'Run E-Stop Test' : 'Retest E-Stop'}</button>`}
      </div>
    </div>
  </section>`;
}

// ---------------- Pairing Wizard ----------------

function bars(rssi) {
  if (typeof rssi !== 'number') return html`<span class="pd-bars" title="No live RSSI (previously paired)">—</span>`;
  const n = rssi >= -60 ? 3 : rssi >= -75 ? 2 : 1;
  return html`<span class="pd-bars" title=${`Signal ${rssi} dBm`}>${[1, 2, 3].map((i) => html`<i class=${i <= n ? 'pd-on' : ''} style=${{ height: `${4 + i * 3}px` }}></i>`)}</span>`;
}

function DeviceList({ devices, picked, busy, op }) {
  const [showAll, setShowAll] = useState(false);
  const pads = devices.filter((d) => d.pad);
  const others = devices.filter((d) => !d.pad);
  const row = (d) => html`<div class=${cx('pd-dev', picked === d.mac && 'pd-sel')} key=${d.mac}>
    ${bars(d.rssi)}
    <span class="pd-dev-name"><b>${d.name || '(Unnamed)'}</b><small class="num">${d.mac}${d.kind_label ? ` · ${d.kind_label}` : ''}</small></span>
    <span class="pd-tags">
      ${d.pad ? html`<span class="pd-tag">Gamepad</span>` : html`<span class="pd-tag pd-gray">${d.why_not || 'Other device'}</span>`}
      ${d.paired ? html`<span class="pd-tag pd-gray">Paired</span>` : null}
      ${d.connected ? html`<span class="pd-tag pd-gray">Connected</span>` : null}
    </span>
    <button class=${d.pad ? 'btn-dark' : 'btn-light'} disabled=${busy} title=${busy ? 'Scan in progress' : ''} onClick=${() => op({ op: 'pair', mac: d.mac }, 180000)}>Pair</button>
  </div>`;
  return html`<div class="pd-devs">
    ${pads.length ? pads.map(row) : html`<div class="pd-empty">No gamepads found yet. Is the controller LED flashing rapidly?</div>`}
    ${others.length ? html`<button class="pd-link" onClick=${() => setShowAll(!showAll)}>${showAll ? 'Hide' : `Show ${others.length} other Bluetooth device(s)`}</button>` : null}
    ${showAll ? others.map(row) : null}
  </div>`;
}

function Wizard({ w, op, kind, setKind, onClose }) {
  const st = w?.state || 'idle';
  const busy = st === 'running';
  const k = kindOf(kind);
  const steps = w?.steps || [];
  const err = w?.error;
  const cfgBusy = w?.configd_busy_s;
  return html`<section class="board pd-card pd-wizard" id="pd-wizard" tabIndex="-1">
    <div class="pd-card-h"><h3>Pair New Gamepad</h3>
      ${!busy ? html`<button class="pd-link" onClick=${onClose}>Close</button>` : null}</div>
    ${cfgBusy ? html`<div class="banner warn" role="status"><b>configd pairing still active</b><span class="more">Wait ~${cfgBusy}s before starting a new scan.</span></div>` : null}
    ${st === 'idle' ? html`
      <div class="pd-kinds" role="radiogroup" aria-label="Controller type">
        ${Object.entries(KINDS).map(([key, x]) => html`<button role="radio" aria-checked=${kind === key} class=${kind === key ? 'pd-on' : ''} onClick=${() => setKind(key)}>${x.label}</button>`)}
      </div>
      <ol class="pd-how">${k.how.map((s) => html`<li>${s}</li>`)}</ol>
      <div class="pd-btns">
        <button class="btn-dark" disabled=${!!cfgBusy} onClick=${() => op({ op: 'scan', seconds: 12 }, 30000)}>LED Flashing — Start Scan</button>
        <button class="btn-light" disabled=${!!cfgBusy} title="Runs robotctl pad pair automatically" onClick=${() => op({ op: 'pair' }, 180000)}>Auto-Pair</button>
      </div>
      <p class="pd-small-p">Scan takes ~12s (Wi-Fi may stutter briefly on shared radio). Cannot scan while walking.</p>
      ${w.helper_available === false ? html`<p class="pd-small-p"><b>Helper unavailable</b>: Studio cannot pause btd for pairing. Run <code>sudo robotctl pad pair</code> via SSH.</p>`
        : w.helper_available === true ? html`<p class="pd-small-p">Uses <code>robotctl pad pair</code> via privileged helper.</p>`
        : html`<p class="pd-small-p">Uses <code>robotctl pad pair</code> when helper is installed.</p>`}
      ` : html`
      <ol class="pd-steps">${steps.map((s) => html`<li class=${'pd-s-' + s.state}>
        <span class="pd-mark">${STEP_ICON[s.state] || ''}</span>
        <span>${s.label}${s.note ? html`<small>${s.note}</small>` : null}</span></li>`)}</ol>
      ${w.phase === 'scan' || w.phase === 'pick' ? html`<${DeviceList} devices=${w.devices || []} picked=${w.picked} busy=${busy} op=${op} />` : null}
      ${err ? html`<div class="pd-err" role="alert">
        <b>${err.msg}</b>
        ${err.fix ? html`<div>Fix: ${err.fix}</div>` : null}
        ${err.raw ? html`<details><summary>Raw output</summary><code>${err.raw}</code></details>` : null}
      </div>` : null}
      ${w.phase === 'keys' && st !== 'done' ? html`<p class="pd-small-p">Press every button and sweep both sticks to verify input mapping.</p>` : null}
      ${st === 'done' ? html`<div class="pd-result pd-good">✓ Paired: ${w.result_name || 'Gamepad'}. It will reconnect automatically after reboot.</div>` : null}
      <div class="pd-btns">
        ${busy ? html`<button class="btn-light" onClick=${() => op({ op: 'cancel' })}>Cancel</button>` : null}
        ${w.phase === 'pick' && !busy ? html`<button class="btn-light" disabled=${!!cfgBusy} onClick=${() => op({ op: 'scan', seconds: 12 }, 30000)}>Scan Again</button>` : null}
        ${w.phase === 'keys' && st !== 'done' ? html`<button class="btn-dark" onClick=${() => op({ op: 'keys_done' })}>All Inputs Verified</button>` : null}
        ${st === 'failed' ? html`<button class="btn-dark" disabled=${!!cfgBusy} onClick=${() => op(w.retry || { op: 'scan', seconds: 12 }, 180000)}>Retry</button>` : null}
        ${!busy ? html`<button class="btn-light" onClick=${() => op({ op: 'wizard_reset' })}>${st === 'done' ? 'Done' : 'Start Over'}</button>` : null}
      </div>
      ${w.via ? html`<p class="pd-small-p">Paired via ${w.via}${w.elapsed_s != null ? ` · ${w.elapsed_s.toFixed(0)}s` : ''}</p>` : null}`}
  </section>`;
}

// ---------------- Known Gamepads ----------------

function KnownPads({ pad, op }) {
  const pads = pad?.status?.pads || [];
  if (!pads.length) return null;
  const forget = (p) => {
    const lines = [`Forget ${p.name || p.mac}?`, ''];
    if (p.connected) lines.push('Currently connected: forgetting will disconnect it immediately and stop the robot. Support the robot first.', '');
    lines.push('Removes pairing from the robot only; re-enter pairing mode on the controller to pair again.');
    if (window.confirm(lines.join('\n'))) op({ op: 'forget', mac: p.mac });
  };
  return html`<section class="board pd-card" id="pd-known" tabIndex="-1">
    <h3>Paired Gamepads</h3>
    ${pads.map((p) => html`<div class="pd-dev" key=${p.mac}>
      <span class=${cx('pd-dot', p.connected && 'pd-on')}></span>
      <span class="pd-dev-name"><b>${p.name || '(Unnamed)'}</b><small class="num">${p.mac}</small></span>
      <span class="pd-tags">
        <span class="pd-tag pd-gray">${p.connected ? 'Connected' : 'Offline'}</span>
        ${p.trusted ? null : html`<span class="pd-tag pd-warn" title="Works now, but won't auto-reconnect after reboot">Untrusted</span>`}
      </span>
      ${p.trusted ? null : html`<button class="btn-light" onClick=${() => op({ op: 'pair', mac: p.mac }, 180000)}>Trust</button>`}
      <button class="btn-light" onClick=${() => forget(p)}>Forget</button>
    </div>`)}
    <p class="pd-small-p">Offline controllers reconnect automatically when powered on.</p>
  </section>`;
}

// ---------------- Virtual Gamepad Controller ----------------

const TEXT_TYPES = ['text', 'password', 'search', 'email', 'number', 'tel', 'url'];
const typing = (el) => !!el && (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT'
  || (el.tagName === 'INPUT' && TEXT_TYPES.includes((el.type || 'text').toLowerCase())));
const KEYMAP = { KeyW: ['ly', -1], KeyS: ['ly', 1], KeyA: ['lx', -1], KeyD: ['lx', 1], KeyQ: ['rx', -1], KeyE: ['rx', 1] };
const ZERO = { lx: 0, ly: 0, rx: 0, ry: 0 };
const dz = (v) => (Math.abs(v) < DZ ? 0 : v);
const MIRROR_WHY = 'Physical gamepad connected: diagram mirrors live hardware input (Select still triggers E-Stop)';

function readout(mode, f, lim, head) {
  const LX = dz(f.lx), LY = -dz(f.ly), RX = dz(f.rx), RY = -dz(f.ry);
  if (mode === 'head') return [['Yaw', -LX * head, 'rad'], ['Pitch', -LY * head, 'rad'], ['Neck', RY * head, 'rad'], ['Roll', RX * head, 'rad']];
  if (mode === 'body') {
    return [['Height', LY * (LY >= 0 ? 1.0 : 2.5), 'cm'], ['Pitch', clamp(RY * 0.2618, -0.26, 0.26), 'rad'],
      ['Roll', clamp(RX * 0.2618, -0.26, 0.26), 'rad']];
  }
  return [['Fwd', LY * lim.v, 'm/s'], ['Strafe', -LX * lim.v, 'm/s'], ['Turn', -RX * lim.w, 'rad/s']];
}

function useVpad({ pad, st, link }) {
  const v = pad?.virtual || {};
  const mirror = !!v.mirror;
  const d = v.driver;
  const locked = !!(d && !d.mine);
  const usable = !!pad?.motion && !mirror && !locked;
  const [axes, setAxes] = useState(ZERO);
  const [pressedView, setPressedView] = useState(() => new Set());
  const [heldView, setHeldView] = useState(() => new Set());
  const [mode, setModeView] = useState('drive');
  const [msg, setMsg] = useState(null);
  const [kbOn, setKbOn] = useState(false);
  const R = useRef(null);
  if (!R.current) {
    R.current = { sticks: { l: { pid: null, x: 0, y: 0 }, r: { pid: null, x: 0, y: 0 } }, kb: new Map(), held: new Set(),
      pressed: new Set(), mode: 'drive', modeAt: 0, lastNonZero: false, timer: null, keysTimer: null, alive: true };
  }
  const S = R.current;
  Object.assign(S, { usable, pad, st, mirror, locked, d });

  const say = (text, bad = true) => setMsg(text ? { text, bad, at: performance.now() } : null);
  const cmd = (args, t = 2000) => link.cmd('pad.op', args, t);
  const whyNot = () => (S.mirror ? MIRROR_WHY : S.locked ? `${S.d?.by || 'Another tab'} is driving — wait or click Take Over`
    : 'Waiting for robot state');
  const press = (role) => { S.pressed.add(role); setPressedView(new Set(S.pressed)); };
  const unpress = (role) => { if (S.pressed.delete(role)) setPressedView(new Set(S.pressed)); };

  const frame = () => {
    const f = { lx: S.sticks.l.x, ly: S.sticks.l.y, rx: S.sticks.r.x, ry: S.sticks.r.y };
    if (S.mode === 'drive') for (const code of S.kb.keys()) { const [axis, val] = KEYMAP[code]; f[axis] = val; }
    return f;
  };
  const isZero = (f) => Math.max(Math.abs(f.lx), Math.abs(f.ly), Math.abs(f.rx), Math.abs(f.ry)) < DZ;
  const r3 = (x) => Math.round(x * 1000) / 1000 || 0;
  const send = async () => {
    const f = frame();
    if (isZero(f) || !S.usable) { kick(); return; }
    S.lastNonZero = true;
    const sentMode = S.mode;
    const r = await cmd({ op: 'sticks', mode: sentMode, lx: r3(f.lx), ly: r3(f.ly), rx: r3(f.rx), ry: r3(f.ry), seq: nextSeq() });
    if (!S.alive) return;
    if (!r.ok) { say(r.reason || r.error || 'Send failed'); return; }
    const dd = r.data || {};
    if (dd.dropped && dd.mode && dd.mode !== S.mode && sentMode === S.mode && performance.now() - S.modeAt > MODE_GRACE_MS) adopt(dd.mode, dd.dropped);
    else if (dd.dropped && S.mode !== 'drive') say(dd.dropped, false);
  };
  function kick() {
    const f = frame();
    setAxes(f);
    if (isZero(f) || !S.usable) {
      if (S.timer) { clearInterval(S.timer); S.timer = null; }
      if (S.lastNonZero) { S.lastNonZero = false; cmd({ op: 'stop', seq: nextSeq() }); }
      return;
    }
    if (!S.timer) { S.timer = setInterval(send, SEND_MS); send(); }
  }
  const voidSticks = () => {
    for (const side of ['l', 'r']) { const s = S.sticks[side]; if (s.pid != null) s.void = true; s.x = 0; s.y = 0; }
    S.kb.clear();
    kick();
  };
  const focusZone = (e) => {
    const z = e.currentTarget?.closest && e.currentTarget.closest('.pd-zone');
    if (z && document.activeElement !== z) z.focus({ preventScroll: true });
  };
  const stickDown = (side, e, k, center = true) => {
    if (e.button > 0) return;
    if (isTouch(e) && (!center || scrolledRecently())) return;
    if (!ACT.active) return;
    e.preventDefault(); focusZone(e);
    if (!S.usable) { say(whyNot(), false); return; }
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    S.sticks[side] = { pid: e.pointerId, ox: e.clientX, oy: e.clientY, k: Math.max(8, k), x: 0, y: 0, void: false };
  };
  const stickMove = (side, e) => {
    const s = S.sticks[side];
    if (s.pid !== e.pointerId || s.void) return;
    let x = (e.clientX - s.ox) / s.k, y = (e.clientY - s.oy) / s.k;
    const n = Math.hypot(x, y);
    if (n > 1) { x /= n; y /= n; }
    s.x = x; s.y = y;
    kick();
  };
  const stickUp = (side, e) => {
    if (S.sticks[side].pid !== e.pointerId) return;
    S.sticks[side] = { pid: null, x: 0, y: 0 };
    kick();
  };

  const sendKeys = async (down = []) => {
    const r = await cmd({ op: 'keys', held: [...S.held], down, seq: nextSeq() });
    if (!S.alive) return;
    const drop = (k) => { S.held.delete(k); unpress(k === 'x' ? 'west' : k); };
    if (!r.ok) { down.forEach(drop); say(r.reason || r.error || 'Key send failed'); }
    else if (r.data?.ended?.length) { r.data.ended.forEach(drop); say('Held key stopped by backend — release and press again', false); }
    setHeldView(new Set(S.held));
    if (!S.held.size && S.keysTimer) { clearInterval(S.keysTimer); S.keysTimer = null; }
  };
  const holdKey = (k) => {
    if (!ACT.active) return;
    S.held.add(k); setHeldView(new Set(S.held));
    sendKeys([k]);
    if (!S.keysTimer) S.keysTimer = setInterval(() => { if (S.held.size) sendKeys(); }, HOLD_MS);
  };
  const releaseKey = (k) => { if (!S.held.delete(k)) return; setHeldView(new Set(S.held)); sendKeys(); };

  function adopt(serverMode, why) {
    S.mode = serverMode; S.modeAt = performance.now(); setModeView(serverMode);
    voidSticks();
    if (why) say(why, false);
    if (serverMode === 'drive') cmd({ op: 'mode', mode: 'drive', seq: nextSeq() });
  }
  const setMode = async (next, why) => {
    if (next === S.mode) return;
    S.mode = next; S.modeAt = performance.now(); setModeView(next);
    voidSticks();
    const r = await cmd({ op: 'mode', mode: next, seq: nextSeq() });
    if (!S.alive) return;
    if (!r.ok) { say(r.reason || r.error || 'Mode switch failed'); if (S.mode === next && next !== 'drive') adopt('drive'); return; }
    if (r.data?.dropped && r.data.mode && r.data.mode !== S.mode) { adopt(r.data.mode, r.data.dropped); return; }
    if (r.data?.note) say(r.data.note, false);
    else if (why) say(`Returned to Walk (${why})`, false);
  };
  const beatMode = async () => {
    const r = await cmd({ op: 'mode', mode: S.mode, hb: true });
    if (S.alive && r.ok && r.data?.mode && r.data.mode !== S.mode && performance.now() - S.modeAt > MODE_GRACE_MS) adopt(r.data.mode, r.data.dropped);
  };

  function releaseAll(why, keep) {
    if (S.touchOff) { const f = S.touchOff; S.touchOff = null; f(); }
    if (S.driveTimer) { clearTimeout(S.driveTimer); S.driveTimer = null; }
    S.kb.clear();
    for (const side of ['l', 'r']) { const s = S.sticks[side]; if (s.pid != null) s.void = true; s.x = 0; s.y = 0; }
    if (S.held.size) { S.held.clear(); setHeldView(new Set()); sendKeys(); }
    S.pressed.clear(); if (keep) S.pressed.add(keep); setPressedView(new Set(S.pressed));
    if (S.mode !== 'drive') setMode('drive', why);
    kick();
  }

  const estop = (fromCard) => {
    const p = link.cmd('relax', {}, 7000);
    if (fromCard === true) releaseAll('E-Stop button pressed');
    else releaseAll('Select (E-Stop) pressed', 'select');
    p.then((r) => { if (S.alive) say(r.ok ? '✓ E-Stop sent — support the robot' : (r.reason || r.error || 'Relax failed — use gamepad − or power off'), !r.ok); });
  };
  const startKey = async () => {
    const s = S.pad?.motion?.start;
    if (!s) return;
    const tilt = S.st?.tilt_deg;
    if (s.op === 'stand' && typeof tilt === 'number' && tilt > 30
      && !window.confirm(`Robot is tilted (${Math.round(tilt)}°). Legs will apply torque to stand — support it first. Continue?`)) { unpress('start'); return; }
    const r = await cmd({ op: 'start', expect: s.op, src: 'svg' }, 10000);
    if (S.alive) say(r.ok ? `✓ Start → ${s.label}${r.data?.reason ? `: ${r.data.reason}` : ''}` : (r.reason || r.error || 'Failed'), !r.ok);
  };
  const rebootKey = async () => {
    releaseAll('D-Pad Right pressed (servo reboot)', 'dr');
    const r = await cmd({ op: 'reboot', src: 'svg' }, 7000);
    if (S.alive) say(r.ok ? '✓ Servo reboot sent — support robot, then press Start to stand' : (r.reason || r.error || 'Failed'), !r.ok);
  };
  const driveHold = () => {
    const target = S.pad?.motion?.drive_mode?.target;
    clearTimeout(S.driveTimer);
    say(`Hold 3s to switch to ${DRIVE_CN[target] || 'next'} mode…`, false);
    S.driveTimer = setTimeout(async () => {
      S.driveTimer = null;
      if (!S.pressed.has('du')) return;
      const r = await cmd({ op: 'drive_mode', expect: target, src: 'svg' }, 10000);
      if (!S.alive) return;
      if (!r.ok) { say(r.reason || r.error || 'Failed'); return; }
      say(r.data?.reason ? `✓ ${r.data.reason}` : `✓ Switched to ${DRIVE_CN[target]} mode`, false);
    }, DRIVE_HOLD_MS);
  };
  const oneShot = async (role) => {
    const key = ROLE_BIND[role];
    const row = S.pad?.motion?.keys?.[key];
    const r = await cmd({ op: 'key', key, src: 'svg' }, 10000);
    if (!S.alive) return;
    if (!r.ok) { say(r.reason || r.error || 'Failed'); if (key === 'x') { releaseKey('x'); unpress('west'); } } else say(`✓ ${row ? row.does : key}`, false);
  };
  const info = (role) => {
    if (role === 'select') return { ok: true, does: 'E-Stop (Relax): disables all servo torque immediately' };
    const f = FUNC[role];
    if (f?.off) return { ok: false, why: f.off };
    const mm = S.pad?.motion || {};
    if (role === 'dr') {
      const x = mm.reboot;
      if (!x || S.mirror) return { ok: false, why: whyNot() };
      return x.ok ? { ok: true, does: x.does } : { ok: false, why: x.why };
    }
    if (role === 'start') {
      const s = mm.start;
      if (!s || S.mirror) return { ok: false, why: whyNot() };
      if (S.locked && s.op !== 'walk_stop') return { ok: false, why: whyNot() };
      return s.ok ? { ok: true, does: `Press: ${s.label}` } : { ok: false, why: s.why };
    }
    if (!S.usable) return { ok: false, why: whyNot() };
    if (role === 'du') {
      const x = mm.drive_mode;
      if (!x) return { ok: false, why: 'Drive mode unknown' };
      return x.ok ? { ok: true, does: x.does } : { ok: false, why: x.why };
    }
    if (role === 'north') return { ok: true, does: S.mode === 'head' ? 'Return to Walk' : 'Head Mode: sticks control head' };
    if (role === 'east') return { ok: true, does: S.mode === 'body' ? 'Return to Walk' : 'Body Mode: sticks tilt/crouch body' };
    if (role === 'ls' || role === 'rs') {
      const t = { drive: ['Walk: forward/strafe', 'Turn'], head: ['Yaw/Pitch head', 'Neck pitch/roll'], body: ['Height/Crouch', 'Body tilt'] }[S.mode];
      return { ok: true, does: `Drag: ${t[role === 'ls' ? 0 : 1]}` };
    }
    if (role === 'lt' || role === 'rt') {
      const t = mm.triggers || {};
      if (!t.ok) return { ok: false, why: t.why || 'Unavailable' };
      return { ok: true, does: (role === 'lt' ? 'Hold: open beak & wheee' : 'Hold: open beak & chirp') + (t.note ? ` (${t.note})` : '') };
    }
    const bk = ROLE_BIND[role];
    if (bk) {
      const row = mm.keys?.[bk];
      if (!row) return { ok: false, why: 'Binding unavailable' };
      return row.ok ? { ok: true, does: row.does + (role === 'west' ? ' (hold to repeat)' : '') } : { ok: false, why: row.why };
    }
    return { ok: false, why: 'Unused' };
  };
  const fire = (role) => {
    if (!ACT.active) return;
    const x = info(role);
    if (!x.ok) { say(x.why, false); return; }
    press(role);
    if (role === 'start') { startKey(); return; }
    if (role === 'dr') { rebootKey(); return; }
    if (role === 'du') { driveHold(); return; }
    if (role === 'north' || role === 'east') { const want = role === 'north' ? 'head' : 'body'; setMode(S.mode === want ? 'drive' : want); return; }
    if (ROLE_HELD[role]) holdKey(ROLE_HELD[role]);
    if (ROLE_BIND[role]) oneShot(role);
  };
  const down = (role, e) => {
    if (e.button > 0) return;
    if (role === 'select') {
      e.preventDefault();
      try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      estop(); return;
    }
    if (isTouch(e)) {
      if (scrolledRecently() || !ACT.active) return;
      if (S.touchOff) S.touchOff();
      S.touchOff = touchHold(e, { press: () => fire(role), release: () => up(role), tap: () => { fire(role); up(role); } });
      return;
    }
    e.preventDefault(); focusZone(e);
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    fire(role);
  };
  const up = (role) => {
    if (!S.pressed.has(role)) return;
    unpress(role);
    if (ROLE_HELD[role]) releaseKey(ROLE_HELD[role]);
    if (role === 'du' && S.driveTimer) { clearTimeout(S.driveTimer); S.driveTimer = null; say('Release before 3s: mode unchanged', false); }
  };

  const zoneProps = {
    tabIndex: 0,
    onKeyDown: (e) => {
      const km = KEYMAP[e.code];
      if (!km || typing(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
      if (!ACT.active) return;
      e.preventDefault();
      if (e.repeat && !S.kb.has(e.code)) return;
      if (S.mode !== 'drive') { say('Keyboard disabled in Head/Body mode — use sticks', false); return; }
      if (!S.usable) { say(whyNot(), false); return; }
      const had = S.kb.has(e.code);
      S.kb.set(e.code, performance.now());
      if (!had) kick();
    },
    onKeyUp: (e) => { if (S.kb.delete(e.code)) kick(); },
    onFocus: () => setKbOn(true),
    onBlur: (e) => {
      const to = e.relatedTarget;
      if (to && to.closest && to.closest('.pd-zone')) return;
      setKbOn(false);
      if (S.kb.size) { S.kb.clear(); kick(); }
    },
  };

  useEffect(() => {
    S.alive = true;
    const open = () => link.whenOpen().then(() => { if (S.alive && ACT.active) cmd({ op: 'drive', open: true, seq: nextSeq() }, 3000); });
    const leave = (why) => {
      releaseAll(why);
      link.cmd('pad.op', { op: 'drive', on: false, seq: nextSeq(), why }, 3000);
    };
    const offAct = padActivity.subscribe(({ active, changed }, why) => {
      if (!changed) return;
      if (active) open(); else leave(why);
    });
    if (ACT.active) open();
    const offLink = link.on((m) => { if (m.t === '_status' && m.status === 'open' && ACT.active) open(); });
    const blur = () => releaseAll('window blur');
    const vis = () => { if (document.visibilityState !== 'visible') releaseAll('page hidden'); };
    const keyCap = (e) => { if (e.code === 'Space' || e.code === 'Escape') releaseAll(e.code === 'Space' ? 'Space E-Stop' : 'Esc pressed'); };
    const stale = setInterval(() => {
      const now = performance.now();
      let changed = false;
      for (const [c, at] of S.kb) if (now - at > KEY_STALE_MS) { S.kb.delete(c); changed = true; }
      if (changed) kick();
    }, 200);
    const beat = setInterval(() => { if (S.mode !== 'drive' && document.visibilityState === 'visible') beatMode(); }, MODE_HB_MS);
    window.addEventListener('blur', blur);
    document.addEventListener('visibilitychange', vis);
    window.addEventListener('keydown', keyCap, true);
    return () => {
      releaseAll('pad unmounted');
      S.alive = false;
      offAct(); offLink();
      clearInterval(stale); clearInterval(beat); clearInterval(S.timer); clearInterval(S.keysTimer);
      S.timer = S.keysTimer = null;
      window.removeEventListener('blur', blur);
      document.removeEventListener('visibilitychange', vis);
      window.removeEventListener('keydown', keyCap, true);
      link.cmd('pad.op', { op: 'drive', on: false, seq: nextSeq() }, 3000);
    };
  }, []);
  useEffect(() => { if (!usable) releaseAll(mirror ? 'physical pad connected' : locked ? 'another tab driving' : 'unavailable'); }, [usable]);
  useEffect(() => {
    const sm = v.mode;
    if (!sm || sm === S.mode || performance.now() - S.modeAt < MODE_GRACE_MS) return;
    adopt(sm, v.mode_exit ? `Exited ${MODE_CN[S.mode]}: ${v.mode_exit.why}` : null);
  });
  useEffect(() => {
    if (!msg) return undefined;
    const t = setTimeout(() => setMsg(null), msg.bad ? 8000 : 4000);
    return () => clearTimeout(t);
  }, [msg]);

  const liveWeb = { buttons: Object.fromEntries([...pressedView].map((r) => [r, true])),
    axes: { ...axes, lt: heldView.has('lt') ? 1 : 0, rt: heldView.has('rt') ? 1 : 0 } };
  return { mode, axes, msg, kbOn, mirror, locked, usable, liveWeb, zoneProps, info, down, up, stickDown, stickMove, stickUp, say,
    estop: () => estop(true) };
}

function HoldButton({ item, link, onErr, lit, locked, quiet }) {
  const [held, setHeld] = useState(false);
  const timer = useRef(null);
  const alive = useRef(false);
  const pend = useRef(null);
  const seq = useRef(0);
  const stop = useCallback(() => {
    if (pend.current) { const f = pend.current; pend.current = null; f(); }
    if (!alive.current) return;
    alive.current = false; setHeld(false);
    clearInterval(timer.current); timer.current = null;
    link.cmd('pad.op', { op: 'action_stop' }, 3000);
  }, []);
  const down = (e) => {
    if (e.button > 0) return;
    if (isTouch(e)) {
      if (scrolledRecently() || !ACT.active || !item.ok || locked) return;
      if (pend.current) pend.current();
      pend.current = touchHold(e, { press: () => { pend.current = null; start(); }, release: stop });
      return;
    }
    start(e);
  };
  const start = async (e) => {
    if (e) { e.preventDefault(); try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ } }
    if (alive.current || !item.ok || locked || !ACT.active) return;
    const my = ++seq.current;
    alive.current = true; setHeld(true);
    const r = await link.cmd('pad.op', { op: 'action_start', id: item.id, press: my }, 4000);
    if (my !== seq.current) return;
    if (!r.ok) { onErr(r.reason || r.error || 'Action failed'); alive.current = false; setHeld(false); return; }
    if (!alive.current) { link.cmd('pad.op', { op: 'action_stop' }, 3000); return; }
    clearInterval(timer.current);
    const iv = setInterval(async () => {
      if (my !== seq.current || !alive.current) { clearInterval(iv); return; }
      const h = await link.cmd('pad.op', { op: 'action_hold', press: my }, 2000);
      if (my === seq.current && alive.current && h.ok && h.data && h.data.running === null) {
        alive.current = false; setHeld(false); clearInterval(iv);
      }
    }, HOLD_MS);
    timer.current = iv;
  };
  useEffect(() => {
    const vis = () => { if (document.visibilityState !== 'visible') stop(); };
    const offAct = padActivity.subscribe(({ active, changed }) => { if (changed && !active) stop(); });
    window.addEventListener('blur', stop);
    document.addEventListener('visibilitychange', vis);
    return () => { offAct(); window.removeEventListener('blur', stop); document.removeEventListener('visibilitychange', vis); stop(); };
  }, []);
  return html`<button class=${cx('pd-act', 'pd-hold', held && 'pd-held', lit && 'pd-lit')} disabled=${!item.ok || locked}
    title=${item.ok ? `Hold to run: ${item.text}` : item.why}
    onPointerDown=${down} onPointerUp=${stop} onPointerCancel=${stop} onLostPointerCapture=${stop}
    onKeyDown=${(e) => { if (e.key === 'Enter' && !e.repeat) start(); }} onKeyUp=${(e) => { if (e.key === 'Enter') stop(); }}
    onContextMenu=${(e) => e.preventDefault()}>
    <b>${item.name}</b><small>${item.ok || quiet ? item.text : item.why}</small></button>`;
}

function VpadBar({ vp, pad }) {
  const m = pad?.motion || {};
  const v = pad?.virtual || {};
  const lim = v.limits || {};
  const s = m.start;
  if (vp.mirror) return html`<p class="pd-small-p pd-vbar">${MIRROR_WHY}.</p>`;
  return html`<div class="pd-vbar">
    <span>Start action: <b>${s ? (s.ok ? s.label : 'Unavailable') : '—'}</b>${s && !s.ok ? html`<small> (${s.why})</small>` : null}</span>
    <span>Stick mode: <b>${MODE_CN[vp.mode]}</b>${vp.mode !== 'drive' && pad?.motion && !m.driving ? html`<small> (policy off — sticks idle)</small>` : null}</span>
    <span class="pd-kbhint">${vp.kbOn ? 'Keyboard active' : 'Click pad to focus keyboard'}</span>
    ${vp.msg ? html`<span class=${cx('pd-vmsg', vp.msg.bad && 'pd-bad-t')} role=${vp.msg.bad ? 'alert' : 'status'}>${vp.msg.text}</span>` : null}
  </div>
  <p class="pd-small-p pd-vbar-hint">Keyboard: click the pad diagram, then use W/S (fwd/back), A/D (strafe), Q/E (turn). Space = E-Stop · Esc = release all. Max ${(lim.v ?? 0.3).toFixed(2)} m/s, ${(lim.w ?? 1.5).toFixed(1)} rad/s (${lim.src || 'default'}), head ${(v.inputs?.head ?? 2.5).toFixed(1)} rad (${v.inputs?.src || 'default'}).</p>`;
}

function Remote({ st, pad, link, vp }) {
  const [err, setErr] = useState(null);
  const [busyUntil, setBusyUntil] = useState(0);
  const [busyPose, setBusyPose] = useState(null);
  const v = pad?.virtual || {};
  const m = pad?.motion || {};
  const live = pad?.live || {};
  const d = v.driver;
  const locked = !!(d && !d.mine);
  const mirror = !!v.mirror;
  const lb = live.buttons || {};
  const lax = live.axes || {};
  const binds = Object.fromEntries((pad?.bindings || []).map((b) => [b.button, b.skill]));
  const bindOf = (btn) => (btn in binds ? binds[btn] : DEFAULT_BIND[btn]);
  const physSkill = (name) => mirror && Object.entries(BIND_ROLE).some(([btn, role]) => bindOf(btn) === name && lb[role]);
  const now = performance.now();
  const busy = now < busyUntil && m.posture === busyPose;
  useEffect(() => { if (busyUntil) { const t = setTimeout(() => setBusyUntil(0), Math.max(0, busyUntil - performance.now())); return () => clearTimeout(t); } return undefined; }, [busyUntil]);

  const op = async (args, timeout = 8000) => {
    setErr(null);
    const r = await link.cmd('pad.op', args, timeout);
    if (!r.ok) setErr(r.reason || r.error || 'Command failed');
    return r;
  };
  const takeOver = async () => {
    if (!window.confirm(`${d?.by || 'Another tab'} is currently driving. Taking over will disable its controls immediately. Continue?`)) return;
    await op({ op: 'drive', on: true, force: true, seq: nextSeq() });
  };

  const press = async (b) => {
    if (b.op === 'stand' && typeof st?.tilt_deg === 'number' && st.tilt_deg > 30
      && !window.confirm(`Robot is tilted (${Math.round(st.tilt_deg)}°). Support it before standing. Continue?`)) return;
    setBusyUntil(performance.now() + BUSY_MS); setBusyPose(m.posture);
    const r = await op({ op: b.op }, 10000);
    if (!r.ok) setBusyUntil(0);
  };
  const B = m.buttons || {};
  const mainBtn = (b, extra) => {
    if (!b) return null;
    const lit = mirror && ((b.op === 'stand' || b.op === 'walk' || b.op === 'walk_stop') ? lb.start : b.op === 'sit' ? (bindOf('dpad_down') === 'sit_toggle' && lb.dd) : false);
    const off = !b.ok || (b.op !== 'walk_stop' && (locked || busy));
    return html`<button class=${cx('pd-big', extra, lit && 'pd-lit')} disabled=${off} onClick=${() => press(b)}
      title=${b.ok ? (b.call ? `RPC: ${b.call}` : '') : b.why}>
      <b>${b.label}</b><small>${busy ? 'Waiting…' : b.ok ? (b.note || (b.op === 'walk_stop' ? 'Stop and hold standing' : '')) : b.why}</small></button>`;
  };
  const last = m.last;
  const la = m.last_action;
  const common = (items) => {
    const xs = items || [];
    return xs.length && xs.every((x) => !x.ok && x.why === xs[0].why) ? xs[0].why : null;
  };
  const skWhy = common(m.skills), actWhy = common(m.actions), comboWhy = common(m.combos);
  const why = (text) => (text ? html`<p class="pd-small-p pd-why">${text}</p>` : null);
  return html`<section class="board pd-card pd-remote" id="pd-remote" tabIndex="-1">
    <div class="pd-card-h">
      <h3>Remote Control</h3>
      <span class="pd-pose">Posture: <b>${m.posture_cn || '—'}</b></span>
      <span class="sv-spacer"></span>
      ${d ? html`<span class=${cx('pd-tag', !d.mine && 'pd-warn')}>${d.mine ? 'This tab driving' : `${d.by || 'Another tab'} driving`}</span>` : null}
      ${locked ? html`<button class="btn-light" onClick=${takeOver}>Take Over</button>` : null}
      <button class="estop pd-relax" title="Disable torque on all 15 servos (E-Stop)"
        onClick=${(e) => { e.currentTarget.blur(); vp.estop(); }}>E-Stop</button>
    </div>
    <p class="pd-small-p pd-leave-note">Scrolling away or switching tabs automatically releases all sticks and held actions.</p>
    ${err ? html`<div class="banner bad" role="alert"><b>${err}</b><a href="#" onClick=${(e) => { e.preventDefault(); setErr(null); }}>Dismiss</a></div>` : null}
    ${locked ? html`<div class="banner warn" role="status"><b>${d.by || 'Another tab'} is driving</b><span class="more">Only one client may drive at a time. Wait 6s after release or click Take Over. Stop & E-Stop are always available.</span></div>` : null}
    ${v.pad_unknown ? html`<div class="banner warn" role="status"><b>Physical gamepad status unknown</b><span class="more">${v.pad_unknown}. Physical controllers override virtual sticks.</span></div>` : null}
    ${mirror ? html`<div class="banner warn" role="status"><b>Physical gamepad connected: diagram in read-only mirror mode</b><span class="more">Stand, Walk, Sit, Skills, Sounds, and E-Stop remain active.</span></div>` : null}

    <div class="pd-sec">
      <div class="pd-sec-h">Posture & Locomotion</div>
      ${m.posture === 'servo_session' || st?.posture === 'servo_session' ? html`<p class="pd-small-p pd-why">Servo Tuning active: robotd paused (click "Hand Back to robotd" in top banner).</p>` : null}
      <div class="pd-bigs">${mainBtn(B.stand)}${mainBtn(B.walk, B.walk?.op === 'walk_stop' ? 'pd-stopbtn' : '')}${mainBtn(B.sit)}</div>
      ${last ? html`<p class=${cx('pd-small-p', !last.ok && 'pd-bad-t')}>${last.ok ? '✓' : '×'} ${last.what}${last.by ? ` (${last.by})` : ''}: ${last.ok ? (last.reason || 'Acknowledged') : last.reason}</p>` : null}
    </div>

    <${AudioInteraction} pad=${pad} link=${link} activity=${padActivity} />
    <${MotionStudio} pad=${pad} link=${link} activity=${padActivity} />

    <div class="pd-sec">
      <div class="pd-sec-h">Skills<small>One-shot official skills</small></div>
      ${why(skWhy)}
      <div class="pd-acts">
        ${(m.skills || []).map((s) => html`<button class=${cx('pd-act', physSkill(s.name) && 'pd-lit')} disabled=${!s.ok || locked}
          title=${s.ok ? `robot.do ${s.name}` : s.why} onClick=${() => op({ op: 'skill', name: s.name })}>
          <b>${s.label}</b><small>${s.ok || skWhy ? '' : s.why}</small></button>`)}
        ${!(m.skills || []).length ? html`<p class="pd-small-p">${m.skills_why || 'No skills configured'}</p>` : null}
      </div>
      <div class="pd-sec-h">Sounds</div>
      <div class="pd-acts pd-sounds">
        ${(m.sounds || []).map((s) => html`<button class=${cx('pd-act', 'pd-chipbtn', mirror && s.tag === 'chirp' && (lax.rt || 0) > 0.3 && 'pd-lit')} disabled=${!s.ok}
          title=${s.ok ? `robot.sound ${s.tag}` : s.why} onClick=${() => op({ op: 'sound', tag: s.tag })}>${s.label}</button>`)}
      </div>
      <div class="pd-sec-h">Actions<small>Hold to run, release to stop</small></div>
      ${why(actWhy)}
      <div class="pd-acts">
        ${(m.actions || []).map((a) => html`<${HoldButton} key=${a.id} item=${a} link=${link} onErr=${setErr} locked=${locked} quiet=${!!actWhy}
          lit=${mirror && a.id === 'mouth' && Math.max(lax.lt || 0, lax.rt || 0) > 0.1} />`)}
      </div>
      <div class="pd-sec-h">Combos<small>Hold to run sequence; stops on release or E-Stop</small></div>
      ${comboWhy && comboWhy !== actWhy ? why(comboWhy) : null}
      <div class="pd-acts">
        ${(m.combos || []).map((a) => html`<${HoldButton} key=${a.id} item=${a} link=${link} onErr=${setErr} locked=${locked} quiet=${!!comboWhy} />`)}
      </div>
      ${m.running ? html`<p class="pd-small-p">Running "${m.running.name}": step ${m.running.step + 1} / ${m.running.steps}${m.running.by ? ` (${m.running.by})` : ''}</p>`
        : la ? html`<p class=${cx('pd-small-p', !la.ok && 'pd-bad-t')}>"${la.name}" ${la.result}</p>` : null}
    </div>
    <p class="pd-small-p">Safety: deadman timeout stops motion within 0.5s if connection drops; single-client lock prevents conflicting inputs.</p>
  </section>`;
}

// ---------------- Live 3D Pad Preview ----------------

function PadDuck({ st, pad, vp, live }) {
  const canvasRef = useRef(null);
  const duckRef = useRef(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    const d = new Duck3D(canvasRef.current, { view: 'front34', centerX: 0.5, centerY: 0.5, fill: 0.86, pageScroll: true });
    duckRef.current = d;
    d.load('model/').then(() => d.setGrid(true)).catch((e) => setErr(`3D model failed to load: ${e.message}`));
    return () => d.dispose();
  }, []);
  useEffect(() => {
    if (!duckRef.current || !st) return;
    duckRef.current.setJoints(st.joints);
    duckRef.current.setGravity(st.gravity);
  }, [st]);
  const v = pad?.virtual || {};
  const ax = pad?.live?.axes || {};
  const mirror = vp.mirror;
  const f = mirror ? { lx: ax.lx || 0, ly: ax.ly || 0, rx: ax.rx || 0, ry: ax.ry || 0 } : vp.axes;
  const rows = readout(mirror ? 'drive' : vp.mode, f, v.limits || { v: 0.3, w: 1.5 }, v.inputs?.head ?? 2.5);
  const pct = st?.battery?.pct;
  const old = !live || st?.joints_age_ms == null || st.joints_age_ms > 1000;
  return html`<section class="board pd-card pd-3d" aria-label="Live Robot Posture">
    <div class="pd-3d-stage"><canvas ref=${canvasRef} class=${old ? 'stage-stale' : ''}></canvas>
      ${err ? html`<span class="sv-loading">${err}</span>` : null}</div>
    <p class="pd-small-p pd-3d-line num">${pad?.motion?.posture_cn || '—'} · Battery ${typeof pct === 'number' ? `${Math.round(pct)}%` : '—'}
      ${rows.map(([n, val, u]) => ` · ${n} ${(Math.round(val * 100) / 100 || 0).toFixed(2)} ${u}`).join('')}${old ? ' · Stale' : ''}</p>
  </section>`;
}

// ---------------- Pad Section ----------------

const TOPBAR_PX = 56;
const TABBAR_PX = 83;

export function PadPage({ st, pad, link, api, previewActive = false, live: duckLive = true }) {
  const [msg, setMsg] = useState(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [mappingOpen, setMappingOpen] = useState(false);
  const [kind, setKind] = useState('xbox');
  const [visualFits, setVisualFits] = useState(true);
  const rootRef = useRef(null);
  const s = pad?.status || {};
  const live = pad?.live || {};
  const w = pad?.wizard || { state: 'idle' };
  const showWizard = wizardOpen || w.state !== 'idle';
  const activeKind = live.device?.kind || s.active?.kind || kind;
  const vp = useVpad({ pad, st, link });
  const shown = vp.mirror || live.state === 'attached' ? live : vp.liveWeb;

  useEffect(() => {
    const left = rootRef.current?.querySelector('.pd-left');
    if (!left) return;
    let frame = null;
    const measure = () => {
      frame = null;
      const height = left.getBoundingClientRect().height;
      const top = parseFloat(getComputedStyle(left).top);
      if (height > 0 && Number.isFinite(top)) setVisualFits(height + top + 16 <= innerHeight);
    };
    const schedule = () => { if (frame == null) frame = requestAnimationFrame(measure); };
    const observer = new ResizeObserver(schedule);
    for (const el of [left, document.querySelector('.band'), document.querySelector('.sticky-bar')]) {
      if (el) observer.observe(el);
    }
    window.addEventListener('resize', schedule);
    schedule();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); window.removeEventListener('resize', schedule); };
  }, []);

  const op = useCallback(async (args, timeout = 15000) => {
    const r = await link.cmd('pad.op', args, timeout);
    if (!r.ok) setMsg(r.reason || r.error || 'Operation failed'); else setMsg(null);
    return r;
  }, []);

  useEffect(() => {
    let io = null;
    const mq = window.matchMedia('(max-width: 700px)');
    const seen = new Map();
    const build = () => {
      if (io) io.disconnect();
      seen.clear();
      io = new IntersectionObserver((es) => {
        for (const e of es) seen.set(e.target, e.isIntersecting);
        padActivity.setInView([...seen.values()].some(Boolean));
      }, { rootMargin: `-${TOPBAR_PX}px 0px -${mq.matches ? TABBAR_PX : 0}px 0px`, threshold: 0 });
      const root = rootRef.current;
      if (root) for (const el of root.querySelectorAll(':scope > .pd-head, .pd-live, #pd-remote, :scope > .pd-more')) io.observe(el);
    };
    build();
    if (mq.addEventListener) mq.addEventListener('change', build);
    return () => {
      if (io) io.disconnect();
      if (mq.removeEventListener) mq.removeEventListener('change', build);
      padActivity.setInView(false);
    };
  }, []);

  useEffect(() => {
    let since = ACT.active ? performance.now() : null;
    let t0 = null;
    const tick = () => {
      if (ACT.active && since != null && performance.now() - since >= WATCH_SETTLE_MS) link.cmd('pad.op', { op: 'watch' }, 3000);
    };
    const arm = () => { clearTimeout(t0); t0 = setTimeout(tick, WATCH_SETTLE_MS + 20); };
    const offAct = padActivity.subscribe(({ active, changed }) => {
      if (!changed) return;
      if (active) { since = performance.now(); arm(); } else { since = null; clearTimeout(t0); }
    });
    if (since != null) arm();
    const iv = setInterval(tick, WATCH_MS);
    const offLink = link.on((m) => { if (m.t === '_status' && m.status === 'open') arm(); });
    return () => { offAct(); offLink(); clearInterval(iv); clearTimeout(t0); };
  }, []);

  useEffect(() => {
    const fn = (what) => {
      if (what === 'wizard') setWizardOpen(true);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const el = document.getElementById({ wizard: 'pd-wizard', estop: 'pd-estop', known: 'pd-known', remote: 'pd-remote' }[what]);
        if (!el) return;
        el.scrollIntoView({ block: 'center' });
        const btn = what === 'estop' ? document.getElementById('pd-estop-btn') : null;
        try { (btn && !btn.disabled ? btn : el).focus({ preventScroll: true }); } catch { /* ignore */ }
      }));
    };
    GO.add(fn);
    return () => GO.delete(fn);
  }, []);
  useEffect(() => { if (w.state === 'idle' && !wizardOpen) setMsg(null); }, [w.state]);

  const connected = !!s.active || live.state === 'attached';
  const tapTag = { attached: 'Live', waiting: 'Waiting', absent: 'No input', denied: 'Permission denied', error: 'Read error', connecting: 'Connecting', off: '' }[live.state] || '';
  return html`<section class="pd-page" id="pad-block" ref=${rootRef} aria-label="Gamepad & Remote">
    <header class="pd-head">
      <h2>Gamepad</h2>
      <${Summary} pad=${pad} />
      <span class="sv-spacer"></span>
      <button class="btn-light" type="button" data-map-open onClick=${() => { setMappingOpen(true); requestAnimationFrame(() => document.querySelector('.pad-mapping')?.scrollIntoView({ block: 'start' })); }}>Remap / Calibrate</button>
      ${!showWizard ? html`<button class="btn-dark" onClick=${() => setWizardOpen(true)}>${connected ? 'Pair Another' : 'Pair Gamepad'}</button>` : null}
    </header>
    ${mappingOpen ? html`<${PadMapping} mapping=${pad?.mapping} link=${link} active=${previewActive} Diagram=${GamepadSvg} onClose=${() => setMappingOpen(false)} />` : null}
    ${msg ? html`<div class="banner bad" role="alert"><b>${msg}</b><a href="#" onClick=${(e) => { e.preventDefault(); setMsg(null); }}>Dismiss</a></div>` : null}
    ${s.error && live.state !== 'attached' ? html`<div class="banner warn" role="status"><b>Cannot read configd gamepad state</b><span class="more">${s.error}</span></div>` : null}
    ${s.netpad ? html`<div class="banner warn" role="status"><b>UDP Netpad enabled</b><span class="more">padd is reading UDP gamepad input; Bluetooth controllers are bypassed. Disable [netpad] in config to use Bluetooth.</span></div>` : null}
    ${s.known && s.driver && s.driver !== 'active' ? html`<div class="banner bad" role="alert"><b>padd not running (${s.driver})</b><span class="more">padd only runs while robotd is active.</span></div>` : null}
    ${st?.posture === 'servo_session' ? html`<div class="banner warn" role="status"><b>Servo Tuning Active</b><span class="more">robotd is paused; gamepad and walking are disabled until handed back.</span></div>` : null}
    <div class="pd-main">
      <section class="board pd-card pd-live">
        <div class="pd-card-h"><h3>${live.device?.name || s.active?.name || `${kindOf(activeKind).label}${vp.mirror ? '' : ' (Virtual)'}`}</h3>
          <span class="pd-livetag">${tapTag}${live.last_ms != null && live.state === 'attached' ? ` · ${fmtMs(live.last_ms)} ago` : ''}</span></div>
        <div class=${cx('pd-zone', 'pd-svgzone', vp.kbOn && 'pd-kb')} ...${vp.zoneProps} aria-label="Virtual Gamepad (click to focus keyboard W/S/A/D/Q/E)">
          <${GamepadSvg} kind=${activeKind} live=${shown} seen=${w.phase === 'keys' ? w.seen : null} ctl=${vp} keys=${pad?.motion?.keys} />
        </div>
        <${VpadBar} vp=${vp} pad=${pad} />
        ${['error', 'absent', 'denied'].includes(live.state) ? html`<p class="pd-small-p">${live.why || 'Input unavailable'}</p>` : null}
        ${!connected && live.state !== 'attached' ? html`<p class="pd-small-p">No physical controller connected — click or drag directly on the virtual diagram above.</p>` : null}
        <details class="pd-legend-box">
          <summary>Button Reference</summary>
          <${Legend} kind=${activeKind} live=${shown} bindings=${pad?.bindings} imuHead=${pad?.virtual?.pad_imu_head} />
        </details>
      </section>
      <div class=${cx('pd-left', !visualFits && 'pd-left-overflow')}><${PadDuck} st=${st} pad=${pad} vp=${vp} live=${duckLive} />
        <${InlineCamera} api=${api} active=${previewActive} /></div>
      <${Remote} st=${st} pad=${pad} link=${link} vp=${vp} />
    </div>
    <div class="pd-more">
      ${showWizard ? html`<${Wizard} w=${w} op=${op} kind=${kind} setKind=${setKind} onClose=${() => { setWizardOpen(false); op({ op: 'wizard_reset' }); }} />` : null}
      <${EStopTest} pad=${pad} op=${op} />
      <${KnownPads} pad=${pad} op=${op} />
    </div>
  </section>`;
}
