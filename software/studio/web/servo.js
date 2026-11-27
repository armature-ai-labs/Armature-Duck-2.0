// Servo tuning page: full bus takeover, zero calibration, register inspection, and 3D thermal/torque overlay.
// Backend: studio/servo_ops.py.
import { html, Component, useState, useEffect, useRef, useCallback } from './vendor/preact-htm.module.js';
import { Duck3D, JOINT_NAMES } from './duck3d.js';
import { drawServoLabels } from './labels.js';
import { TwoStep } from './confirm.js';
import { Stepper } from './stepper.js';
import { servoReplyStatus } from './startup-status.js';

// Official reference poses (radians, ordered by JOINT_NAMES)
const POSES = {
  zero: { label: 'Zero', rad: Array(15).fill(0) },
  stand: { label: 'Stand', rad: [0, -0.0873, -0.4579, -0.0049, 0.453, 0.3491, 0.3491, 0, 0, 0, 0, 0.0873, 0.4579, 0.0049, -0.453] },
  sit: { label: 'Sit', rad: [0, 0, -0.5236, 1.0472, 0, 0.5, 1.6, 0, 0, 0, 0, 0, 0.5236, -1.0472, 0] },
  fold: { label: 'Fold', rad: [0, 0, 1.57, 1.57, 0, 1, 1, 0, 0, 0, 0, 0, -1.57, -1.57, 0] },
};
const IDS = [20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14];
const ID_OF = Object.fromEntries(JOINT_NAMES.map((n, i) => [n, IDS[i]]));
const NAME_OF = Object.fromEntries(IDS.map((id, i) => [id, JOINT_NAMES[i]]));
const GROUP_OF = (id) => (id >= 30 ? 'H' : id >= 20 ? 'L' : 'R');
const SEL_COLOR = { R: '#E5484D', L: '#2F7CF6', H: '#F5B400' };
const GROUPS = [['L', 'Left Leg', '20–24'], ['R', 'Right Leg', '10–14'], ['H', 'Head & Neck', '30–34']];
const DANGER = {
  5: 'ID: changing causes collisions unless only one servo is connected',
  6: 'Baud Rate: changing will lose communication',
  8: 'Response Level: setting to 0 disables write acks',
  9: 'Min Angle Limit',
  11: 'Max Angle Limit',
  19: 'Protection Switch: disabling removes thermal/overcurrent auto-relax',
  33: 'Operating Mode: wheel/velocity mode spins continuously',
};
const REGS = [
  [41, 'Acceleration', 1, 'SRAM'], [46, 'Running Speed', 2, 'SRAM'], [48, 'Torque Limit', 2, 'SRAM'],
  [50, 'Position P (Active)', 1, 'SRAM'], [51, 'Position D (Active)', 1, 'SRAM'], [21, 'Position P', 1, 'EEPROM'],
  [22, 'Position D', 1, 'EEPROM'], [23, 'Position I', 1, 'EEPROM'], [13, 'Max Temp Limit', 1, 'EEPROM'], [19, 'Protection Flags', 1, 'EEPROM'],
  [28, 'Protection Current', 2, 'EEPROM'], [9, 'Min Angle Limit', 2, 'EEPROM'], [11, 'Max Angle Limit', 2, 'EEPROM'], [8, 'Response Level', 1, 'EEPROM'],
  [33, 'Operating Mode', 1, 'EEPROM'], [5, 'ID', 1, 'EEPROM'], [6, 'Baud Rate', 1, 'EEPROM'],
];

const fmt = (v, d = 0) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '—');
const sign = (v, d = 1) => (typeof v === 'number' ? `${v > 0 ? '+' : ''}${v.toFixed(d)}` : '—');
let calibHandler = null;

// ---------------- 3D ----------------

function ServoStage({ sv, pose, setPose, selected, onSelect }) {
  const boxRef = useRef(null), canvasRef = useRef(null), duckRef = useRef(null);
  const [ready, setReady] = useState(false);
  const [moved, setMoved] = useState(false);
  const [grid, setGrid] = useState(true);
  const [xray, setXray] = useState(false);
  const [thermal, setThermal] = useState(false);
  const [hovered, setHovered] = useState(null);

  useEffect(() => {
    const d = new Duck3D(canvasRef.current, { view: 'front', centerX: 0.5, centerY: 0.52, fill: 0.82 });
    duckRef.current = d;
    d.onUserMove = () => setMoved(true);
    d.onPick = (name) => ID_OF[name] && onSelect(ID_OF[name]);
    d.onHover = (name) => setHovered(name ? (ID_OF[name] ? `#${ID_OF[name]} ${name}` : name) : null);
    d.load('model/').then(() => {
      setReady(true);
      d.setGrid(true);
    }).catch(() => {});
    return () => d.dispose();
  }, []);

  useEffect(() => {
    const d = duckRef.current;
    if (!d || !ready) return;
    if (pose === 'live') {
      if (sv?.diagnostic) return;
      if (!sv?.rows?.length) return;
      d.setJoints(sv.rows.map((r) => (r.raw == null ? 0 : r.dir * (r.raw - 2048) * 2 * Math.PI / 4096)));
    } else {
      d.setJoints(POSES[pose].rad);
    }
    if (sv?.rows?.length) {
      d.setServoTelemetry(sv.rows.map((r) => ({ id: r.id, temp_c: r.temp, torque: r.torque })));
    }
    d.setGravity(null);
  }, [sv, pose, ready]);

  useEffect(() => {
    if (!ready) return;
    duckRef.current.select(selected ? NAME_OF[selected] : null, selected ? SEL_COLOR[GROUP_OF(selected)] : null);
  }, [selected, ready]);

  useEffect(() => {
    if (!ready || pose !== 'zero') return undefined;
    return drawServoLabels(duckRef.current, boxRef.current);
  }, [ready, pose]);

  const toggleGrid = () => { const next = !grid; setGrid(next); duckRef.current?.setGrid(next); };
  const toggleXray = () => { const next = !xray; setXray(next); duckRef.current?.setXray(next); };
  const toggleThermal = () => { const next = !thermal; setThermal(next); duckRef.current?.setThermalOverlay(next); };

  return html`<div class="sv-stage" ref=${boxRef}>
    <canvas ref=${canvasRef} style=${{ visibility: pose === 'live' && sv?.diagnostic ? 'hidden' : 'visible' }}></canvas>
    <div class="sv-poses" role="tablist" aria-label="3D Pose Display">
      ${[['live', 'Live'], ...Object.entries(POSES).map(([k, p]) => [k, p.label])].map(([k, label]) =>
        html`<button role="tab" aria-selected=${pose === k} class=${pose === k ? 'on' : ''} onClick=${(e) => { e.currentTarget.blur(); setPose(k); }}>${label}</button>`)}
    </div>
    ${ready ? html`<div class="sv-hud">
      <button type="button" class=${'hud-btn' + (grid ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleGrid(); }}>Grid</button>
      <button type="button" class=${'hud-btn' + (xray ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleXray(); }}>X-Ray</button>
      <button type="button" class=${'hud-btn' + (thermal ? ' on' : '')} onClick=${(e) => { e.currentTarget.blur(); toggleThermal(); }}>Thermal</button>
      ${hovered ? html`<span class="hud-hover num">${hovered}</span>` : null}
    </div>` : null}
    ${!ready ? html`<span class="sv-loading">Loading 3D model…</span>` : null}
    <span class="sv-note">${pose !== 'live' ? 'Reference pose · not live posture' : sv?.diagnostic ? 'Startup diagnostics: raw feedback only' : 'Drag to orbit · Click servo to select'}</span>
    ${moved ? html`<button class="reset-view" onClick=${(e) => { e.currentTarget.blur(); duckRef.current?.resetView(); setMoved(false); }}>Reset View</button>` : null}
  </div>`;
}

function GainsLine({ g }) {
  const man = g.manual || [], off = g.differ_on || [];
  return html`<div class="sv-sub sv-gains" title=${g.detail + (g.warn ? '\n' + g.warn : '')}>
    Gains: P${g.kp} · D${g.kd} · Limit ${g.limit} (matches robotd · ${g.source})${
    g.warn ? html` · <span class="sv-warn">Invalid parameter file; using defaults (see Logs)</span>` : null}${
    man.length ? html` · <span class="sv-warn">Manual override: ${man.map((m) => `#${m.id} ${m.what || 'reg'}=${m.value} (addr ${m.addr})`).join(', ')}</span>`
      : off.length ? html` · <span class="sv-warn">${off.map((i) => '#' + i).join(' ')} holding with mismatched gains (relax & re-hold to sync)</span>` : null}
  </div>`;
}

function Why() {
  return html`<section class="board sv-card sv-why">
    <h3>Why Zero Calibration Is Required</h3>
    <ol>
      <li>Each servo contains an encoder (0–4095 ticks per revolution). Splined horns are mounted mechanically with slight angular offsets.</li>
      <li><b>Calibration</b> aligns the robot to the official Zero Pose (legs straight, head level, beak closed) and writes <b>2048</b> as the center reference into each servo.</li>
      <li>Once calibrated, robotd, locomotion policies, and Studio all treat 2048 as 0 rad.</li>
      <li>Uncalibrated offsets cause tilted standing, foot scuffing, or falls during walking.</li>
    </ol>
  </section>`;
}

// ---------------- 15 Servos Table ----------------

function commDot(r) {
  const reply = servoReplyStatus(r);
  if (reply) return html`<span class=${'sv-dot ' + reply.kind} title=${reply.word} aria-label=${reply.word}></span>`;
  const c = r.comm || {};
  const tip = c.asked == null
    ? `Missed ${c.missed} total · ${c.streak} consecutive (robotd)`
    : `Sent ${c.asked} · OK ${c.ok} · Missed ${c.missed} · Max streak ${c.max_streak}${c.alarm ? ` · Alarm ${c.alarm}` : ''}`;
  const kind = !r.fresh || c.streak >= 3 ? 'bad' : c.asked != null && c.missed > 0 && c.missed / Math.max(c.asked, 1) > 0.01 ? 'warn' : 'ok';
  return html`<span class=${'sv-dot ' + kind} title=${tip}></span>`;
}

function tempClass(r) {
  if (typeof r.temp !== 'number' || typeof r.temp_limit !== 'number' || !r.temp_limit) return '';
  if (r.temp >= r.temp_limit) return 'bad';
  if (r.temp >= r.temp_limit - 10) return 'warn';
  return '';
}

function Row({ r, open, selected, onSelect, op, follow, setHint, motion }) {
  const [drag, setDrag] = useState(null);
  const [typed, setTyped] = useState(null);
  const last = useRef(0), release = useRef(null);
  const label = r.en || r.cn || `Servo ${r.id}`;
  const send = (v, force) => {
    if (!open) return;
    if (r.torque !== 1) { setHint(`${r.id} ${label}: torque is off — enable Hold first`); return; }
    const now = performance.now();
    if (!force && now - last.current < 50) return;
    last.current = now;
    op({ op: 'goal', id: r.id, pos: v }, 3000, true);
  };
  const onInput = (e) => { const v = +e.target.value; setDrag(v); clearTimeout(release.current); if (follow) send(v, false); };
  const onDone = (e) => { const v = +e.target.value; send(v, true); clearTimeout(release.current); release.current = setTimeout(() => setDrag(null), 600); };
  const goTyped = () => {
    const text = typed;
    setTyped(null);
    if (text == null || text.trim() === '' || !isFinite(+text) || !open) return;
    if (r.torque !== 1) { setHint(`${r.id} ${label}: torque is off — enable Hold first`); return; }
    const pos = Math.max(0, Math.min(4095, Math.round(+text)));
    const seconds = motion?.seconds ?? 2;
    setDrag(pos); clearTimeout(release.current); release.current = setTimeout(() => setDrag(null), seconds * 1000 + 600);
    op({ op: 'move', goals: { [r.id]: pos }, seconds, mode: motion?.mode, acc: motion?.acc });
  };
  const nudge = (e, dir) => {
    stop(e);
    if (!open) return;
    if (r.torque !== 1) { setHint(`${r.id} ${label}: torque is off — enable Hold first`); return; }
    if (!r.fresh || !Number.isInteger(r.raw)) {
      setHint(`${r.id} ${label}: waiting for live position readback before nudging`);
      return;
    }
    const steps = Math.max(1, Math.round((motion?.nudge ?? 1) * 4096 / 360));
    const pos = Math.max(0, Math.min(4095, r.raw + dir * steps));
    setDrag(pos); clearTimeout(release.current); release.current = setTimeout(() => setDrag(null), 1200);
    op({ op: 'goal', id: r.id, pos }, 3000, true);
  };
  const onKey = (e) => {
    if (e.key === 'Enter') { goTyped(); e.target.blur(); }
    else if (e.key === 'Escape') { setTyped(null); e.target.blur(); }
  };
  const val = drag ?? r.raw ?? 2048;
  const stop = (e) => e.stopPropagation();
  const c = r.comm || {};
  const reply = servoReplyStatus(r);
  return html`<div class=${'sv-row' + (r.fresh ? '' : ' stale') + (selected ? ' sel ' + r.group : '')} data-position=${r.fresh ? r.raw : null} onClick=${() => onSelect(selected ? null : r.id)}>
    <span class=${'sv-id ' + r.group}>${r.id}</span>
    <span class="sv-name" title=${r.en}>${label}</span>
    <input class="sv-slider" type="range" min="0" max="4095" step="1" value=${val} disabled=${!open}
      aria-label=${`${label} target position`} onClick=${stop} onInput=${onInput} onChange=${onDone} />
    <span class="sv-numbox" onClick=${stop}>
      <button class="sv-nudge" type="button" disabled=${!open} aria-label=${`${label} minus ${motion?.nudge ?? 1}°`}
        title=${`Step −${motion?.nudge ?? 1}°`} onClick=${(e) => nudge(e, -1)}>−</button>
      <input class="num r sv-num" type="number" inputmode="numeric" min="0" max="4095" step="1" disabled=${!open}
        value=${typed ?? drag ?? r.raw ?? ''} aria-label=${`${label} target 0–4095`} title="Enter 0–4095 and press Enter; Esc cancels"
        onFocus=${(e) => { setTyped(String(drag ?? r.raw ?? '')); e.target.select(); }}
        onInput=${(e) => setTyped(e.target.value)} onKeyDown=${onKey} onBlur=${() => setTyped(null)} />
      <button class="sv-nudge" type="button" disabled=${!open} aria-label=${`${label} plus ${motion?.nudge ?? 1}°`}
        title=${`Step +${motion?.nudge ?? 1}°`} onClick=${(e) => nudge(e, 1)}>+</button>
    </span>
    <span class="num r sv-deg">${r.deg == null ? '—' : sign(r.deg) + '°'}</span>
    <span class=${'num r ' + tempClass(r)} title=${r.temp_limit ? `Thermal limit ${r.temp_limit} °C` : ''}>${fmt(r.temp)}°C</span>
    <label class=${'sv-switch' + (r.torque === 1 ? ' on' : '')} onClick=${stop} title=${reply ? 'Startup diagnostics: reply status only' : r.torque === 1 ? 'Holding: click to relax' : 'Relaxed: click to hold at current angle'}>
      <input type="checkbox" checked=${r.torque === 1} disabled=${!open}
        onChange=${(e) => op({ op: 'torque', on: e.target.checked ? 1 : 0, ids: [r.id] })} />
      <span>${reply ? reply.word : r.torque === 1 ? 'Hold' : 'Off'}</span>
    </label>
    <button class="sv-mini" disabled=${!open} onClick=${(e) => { stop(e); calibHandler && calibHandler(String(r.id)); }}
      title="Zero calibrate: set current position to 2048 and hold">Zero</button>
    ${commDot(r)}
    ${selected ? html`<div class="sv-more num" onClick=${stop}>
      <span>Voltage <b>${fmt(r.volt, 1)} V</b></span><span>Load <b>${fmt(r.load)}</b></span>
      ${r.current_ma != null ? html`<span>Current <b>${fmt(r.current_ma)} mA</b></span>` : null}
      ${r.current_raw != null ? html`<span>Current Raw <b>${fmt(r.current_raw)}</b></span>` : null}
      ${r.speed_raw != null ? html`<span>Speed Raw <b>${fmt(r.speed_raw)}</b></span>` : null}
      ${r.offset != null ? html`<span title="Register 31: stored zero offset">Offset <b>${r.offset}</b></span>` : null}
      ${r.p_run != null ? html`<span title=${`Active P / D / Limit (RAM 50 / 51 / 48). EEPROM P / D (21 / 22): ${r.p ?? '?'} / ${r.d ?? '?'}`}>P/D/Lim <b class=${r.gains_ok === false ? 'warn' : ''}>${r.p_run}/${r.d_run}/${r.limit ?? '?'}</b></span>`
        : r.p != null ? html`<span title="Position P / D (EEPROM 21 / 22)">P/D <b>${r.p}/${r.d}</b></span>` : null}
      <span>Bus <b>${reply ? reply.word : `Miss ${c.missed ?? 0}${c.asked != null ? ` / ${c.asked}` : ''}`}</b></span>
      ${r.status ? html`<span class="sv-alarm">Status ${r.status}</span>` : null}
      <span class="sv-spacer"></span>
      <span>Direction</span><button class="sv-mini" disabled=${!open} onClick=${() => op({ op: 'set_dir', id: r.id, dir: -r.dir })}
        title="Sign ±1: maps raw encoder direction to 3D model">${r.dir > 0 ? '+1' : '−1'}</button>
    </div>` : null}
  </div>`;
}

const rowSig = (p) => { const r = p.r, c = r.comm || {};
  return [r.raw, r.deg, r.volt, r.temp, r.load, r.current_ma, r.current_raw, r.speed_raw, r.responding, r.torque, r.fresh, r.dir, r.offset, r.p, r.d, r.status,
    r.p_run, r.d_run, r.limit, r.gains_ok, c.missed, c.streak, p.open, p.selected, p.follow,
    p.motion?.seconds, p.motion?.mode, p.motion?.acc, p.motion?.nudge].join('|'); };
class RowPure extends Component {
  shouldComponentUpdate(next) { return rowSig(next) !== rowSig(this.props) || next.op !== this.props.op; }
  render(props) { return html`<${Row} ...${props} />`; }
}

function ServoList({ sv, open, selected, onSelect, op, follow, setHint, motion }) {
  const rows = sv.rows || [];
  return html`<section class="board sv-list">
    <div class="sv-row sv-headrow"><span></span><span>Joint</span><span>Position</span><span class="r" title="Enter 0–4095 and press Enter">Raw</span><span class="r">Angle</span>
      <span class="r">Temp</span><span>Torque</span><span></span><span></span></div>
    ${GROUPS.map(([g, label, range]) => {
      const gr = rows.filter((r) => r.group === g);
      const hold = gr.filter((r) => r.torque === 1).length;
      return html`<div class="sv-group">
        <div class="sv-grouphead"><span class=${'sv-gdot ' + g}></span>${label}<span class="sv-small">${range}</span>
          ${hold ? html`<span class="sv-hold-n">${hold} holding</span>` : null}</div>
        ${gr.map((r) => html`<${RowPure} key=${r.id} r=${r} open=${open} selected=${r.id === selected} onSelect=${onSelect}
          op=${op} follow=${follow} setHint=${setHint} motion=${motion} />`)}
      </div>`;
    })}
  </section>`;
}

// ---------------- Quick Controls ----------------

function ServoPick({ selected, onSelect, rows, extra }) {
  return html`<select class="sv-pick" value=${selected ?? ''} onChange=${(e) => onSelect(e.target.value === '' ? null : +e.target.value)}>
    <option value="">Select Servo</option>
    ${GROUPS.map(([g, label]) => html`<optgroup label=${label}>${(rows || []).filter((r) => r.group === g).map((r) =>
      html`<option value=${r.id}>${r.id} · ${r.en || r.cn}</option>`)}</optgroup>`)}
    ${extra?.length ? html`<optgroup label="Other Detected IDs">${extra.map((f) =>
      html`<option value=${f.id}>${f.id} · ${f.kind === 'imu' ? 'IMU Board' : 'Unassigned'}</option>`)}</optgroup>` : null}
  </select>`;
}

function Toolbar({ open, op, link, motion, setMotion, idle, setIdle, follow, setFollow, poses, setHint, syncSliders, main }) {
  const ms = { seconds: motion.seconds, mode: motion.mode, acc: motion.acc };
  const named = (words) => poses.find((p) => words.some((w) => p.name.toLowerCase().includes(w)));
  const goNamed = (words, label, fallbackKey) => {
    const p = named(words);
    if (p) op({ op: 'go_pose', file: p.file, ...ms });
    else if (fallbackKey) op({ op: 'official', key: fallbackKey, ...ms });
    else setHint(`No "${label}" pose saved yet — save one under More`);
  };
  return html`<div class="sv-toolbar">
    <button class="btn-ok" disabled=${!open} onClick=${() => op({ op: 'torque', on: 1 })} title="Match current positions and enable torque on all servos">Torque All On</button>
    <button class="btn-danger" disabled=${!open} onClick=${(e) => { e.currentTarget.blur(); link.cmd('relax', {}, 7000); }}
      title="Relax all 15 servos and verify readback">Torque All Off</button>
    <button class="btn-light" onClick=${syncSliders}>Sync Sliders</button>
    <button class="btn-light" disabled=${!open} onClick=${() => op({ op: 'official', key: 'zero', ...ms })}
      title="Move all servos to 2048 (official zero pose)">Zero Pose (2048)</button>
    <button class="btn-light" disabled=${!open} onClick=${() => goNamed(['stand'], 'Stand', 'stand')}
      title="Saved Stand pose or official Stand keyframe">Stand</button>
    <button class="btn-light" disabled=${!open} onClick=${() => goNamed(['tuck', 'fold'], 'Tuck', null)} title="Saved Tuck pose">Tuck</button>
    <button class="btn-light" disabled=${!open} onClick=${async () => { const r = await op({ op: 'idle', on: !idle }); if (r.ok) setIdle(!idle); }}
      title="Subtle idle head animations (IDs 30–34)">${idle ? '⏹ Stop Idle' : 'Idle Head'}</button>
    <button class="btn-light" disabled=${!open} onClick=${() => op({ op: 'scan', ids: '20-24,30-34,10-14' })}>Rescan</button>
    <button class="btn-stop" disabled=${!open} onClick=${() => op({ op: 'stop' })} title="Stop active motion or idle animation">Stop</button>
    <span class="sv-inline">Duration <${Stepper} value=${motion.seconds} min=${0} max=${30} step=${0.5}
      onChange=${(v) => setMotion({ ...motion, seconds: v === '' ? 0 : v })} unit="s" aria-label="Move duration" /></span>
    <span class="sv-inline" title="Step size for − / + nudge buttons">Step <${Stepper} value=${motion.nudge} min=${0.5} max=${10} step=${0.5}
      onChange=${(v) => setMotion({ ...motion, nudge: v === '' || v <= 0 ? 1 : v })} unit="°" aria-label="Nudge degrees" /></span>
    <label class="sv-inline"><input type="checkbox" checked=${follow} onChange=${(e) => setFollow(e.target.checked)} /> Live Drag</label>
    ${main}
  </div>`;
}

function CalibCard({ open, op, selected, onSelect, rows }) {
  const [ids, setIds] = useState('20-24,30-34,10-14');
  return html`<section class="board sv-card sv-calib">
    <div class="sv-form">
      <span class="sv-label">Single Zero</span>
      <div class="sv-ctrl">
        <${ServoPick} selected=${selected} onSelect=${onSelect} rows=${rows} />
        <button class="btn-dark" disabled=${!open || !selected} onClick=${() => calibHandler && calibHandler(String(selected))}>Zero Selected</button>
        <span class="sv-small">Or click "Zero" on any row</span>
      </div>
      <span class="sv-label">Batch Zero</span>
      <div class="sv-ctrl">
        <input type="text" value=${ids} onInput=${(e) => setIds(e.target.value)} title="Servo IDs to calibrate" class="sv-ids" />
        <button class="btn-dark" disabled=${!open} onClick=${() => calibHandler && calibHandler(ids)}>Calibrate All</button>
        <button class="btn-light" disabled=${!open} onClick=${() => { if (window.confirm('Undo last calibration and restore backed-up offsets?')) op({ op: 'undo' }, 30000); }}>Undo Last</button>
      </div>
      <span></span><p class="sv-small">Position the robot in the official Zero Pose (legs straight, head level, beak closed), then calibrate to write 2048. A preview appears before writing.</p>
    </div>
  </section>`;
}

// ---------------- More (Collapsed) ----------------

function MoreArea({ open, op, motion, setMotion, follow, setFollow, poses, setPoses, selected, onSelect, sv, dirs, link }) {
  const [name, setName] = useState('');
  const ms = { seconds: motion.seconds, mode: motion.mode, acc: motion.acc };
  return html`<details class="board sv-more-area">
    <summary>More: Servo ID Assignment, Motion Profile, Saved Poses, Registers & Logs</summary>
    <div class="sv-more-grid">
      <section class="sv-form">
        <span class="sv-label">Official Poses</span>
        <div class="sv-ctrl">
          ${Object.entries(POSES).map(([k, p]) => html`<button class="btn-light" disabled=${!open} onClick=${() => {
            if (k === 'fold' && !window.confirm('Fold pose: knees bend 88°. Do not run if zero offsets are uncalibrated. Continue?')) return;
            op({ op: 'official', key: k, ...ms });
          }}>${p.label}</button>`)}
        </div>
        <span class="sv-label">Motion Mode</span>
        <div class="sv-ctrl"><select value=${motion.mode} onChange=${(e) => setMotion({ ...motion, mode: e.target.value })}>
          <option value="profile">Profile (servo trapezoidal curve)</option><option value="stream">Stream (host-interpolated)</option></select>
          <span class="sv-inline">Accel <${Stepper} value=${motion.acc} min=${1} max=${254}
            onChange=${(v) => setMotion({ ...motion, acc: v === '' ? 0 : v })} aria-label="Acceleration" /></span></div>
        <span class="sv-label">Saved Poses</span>
        <div class="sv-ctrl sv-chips">
          ${poses.length ? poses.map((p) => html`<span class="sv-chip"><button disabled=${!open} onClick=${() => op({ op: 'go_pose', file: p.file, ...ms })} title=${p.time}>${p.name}</button>
            <button class="x" disabled=${!open} title="Delete pose" onClick=${async () => { if (!window.confirm(`Delete pose "${p.name}"?`)) return; const r = await op({ op: 'delete_pose', file: p.file }); if (r.ok) setPoses(r.data.poses); }}>×</button></span>`)
            : html`<span class="sv-small">None saved</span>`}
        </div>
        <span></span>
        <div class="sv-ctrl"><input type="text" placeholder="Pose name (e.g. Tuck)" value=${name} onInput=${(e) => setName(e.target.value)} />
          <button class="btn-light" disabled=${!open || !name.trim()} onClick=${async () => { const r = await op({ op: 'save_pose', name }); if (r.ok) { setPoses(r.data.poses); setName(''); } }}>Save Current Pose</button></div>
      </section>
      <${Tools} open=${open} op=${op} selected=${selected} onSelect=${onSelect} sv=${sv} dirs=${dirs} />
    </div>
    <${LogBox} link=${link} />
  </details>`;
}

function Numbering({ open, op, rows, found, setFound, missing, setMissing }) {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [note, setNote] = useState(null);
  const cn = Object.fromEntries((rows || []).map((r) => [r.id, r.en || r.cn]));
  const fresh = (found || []).filter((f) => f.kind === 'servo' && !f.known);
  const find = async (keepNote) => {
    const r = await op({ op: 'find' }, 40000);
    if (!r.ok || !r.data) return null;
    const list = r.data.found, lack = r.data.missing;
    setFound(list); setMissing(lack);
    const first = list.find((f) => f.kind === 'servo' && !f.known);
    setFrom(first ? String(first.id) : ''); setTo(lack.length ? String(lack[0]) : '');
    if (!keepNote) setNote(null);
    return r.data;
  };
  const write = async () => {
    const a = +from, b = +to;
    if (!window.confirm(`Reassign ID ${a} → ${b} (${cn[b] || 'custom'})? Ensure only one servo with ID ${a} is on the bus.`)) return;
    const r = await op({ op: 'set_id', old: a, new: b }, 10000);
    if (!r.ok) return;
    const ok = r.data?.ok;
    const after = await find(true);
    const left = after ? after.missing : [];
    setNote(ok ? `Assigned ID ${b}${cn[b] ? ` · ${cn[b]}` : ''}. ${left.length ? `Remaining (${left.length}): ${left.join(', ')}. Swap next servo and click Find Servos.` : 'All 15 servo IDs present.'}`
      : `Written, but ID ${b} did not respond to ping. Click Find Servos to verify.`);
  };
  return html`<section class="sv-form sv-numbering">
    <span class="sv-label">Assign IDs</span>
    <div class="sv-ctrl">
      <button class="btn-dark sv-find" disabled=${!open} onClick=${() => find(false)}>Find Servos</button>
      <span class="sv-small">Factory servos default to ID 1. Connect one new servo at a time to assign its target ID.</span>
    </div>
    ${found ? html`<span class="sv-label">Detected</span>
    <div class="sv-ctrl sv-chips sv-found">
      ${found.length ? found.map((f) => html`<span class=${'sv-chip' + (f.kind === 'servo' && !f.known ? ' new' : '')} data-id=${f.id}>
        ${f.id} · ${f.kind === 'imu' ? 'IMU Board' : f.known ? (cn[f.id] || f.joint) : 'Unassigned'}</span>`)
        : html`<span class="sv-small">No servos detected — check bus cable and battery.</span>`}
    </div>
    ${fresh.length > 1 ? html`<span></span><p class="sv-warn">${fresh.length} unassigned servos detected. Ensure no duplicate IDs are connected simultaneously.</p>` : null}
    <span class="sv-label">Assign To</span>
    <div class="sv-ctrl">
      <select class="sv-from" value=${from} onChange=${(e) => setFrom(e.target.value)}>
        <option value="">Source ID</option>
        ${found.filter((f) => f.kind === 'servo').map((f) => html`<option value=${f.id}>${f.id} · ${f.known ? (cn[f.id] || f.joint) : 'Unassigned'}</option>`)}
      </select>
      <span>→</span>
      <select class="sv-to" value=${to} onChange=${(e) => setTo(e.target.value)}>
        <option value="">Target joint</option>
        ${GROUPS.map(([g, label]) => html`<optgroup label=${label}>${(rows || []).filter((r) => r.group === g).map((r) =>
          html`<option value=${r.id} disabled=${!missing.includes(r.id)}>${r.id} · ${r.en || r.cn}${missing.includes(r.id) ? '' : ' (present)'}</option>`)}</optgroup>`)}
      </select>
      <button class="btn-danger sv-write-id" disabled=${!open || from === '' || to === '' || from === to} onClick=${write}>Write ID</button>
    </div>` : null}
    ${note ? html`<span></span><p class="sv-out sv-number-note">${note}</p>` : null}
  </section>`;
}

function Tools({ open, op, selected, onSelect, sv, dirs }) {
  const [found, setFound] = useState(null);
  const [missing, setMissing] = useState([]);
  const [other, setOther] = useState(null);
  const extra = (found || []).filter((f) => !f.known);
  const sel = other ?? selected ?? 20;
  const pick = (id) => { if (extra.some((f) => f.id === id)) setOther(id); else { setOther(null); onSelect(id ?? 20); } };
  const [newId, setNewId] = useState('');
  const [reg, setReg] = useState(41);
  const [size, setSize] = useState(1);
  const [val, setVal] = useState('');
  const [unlock, setUnlock] = useState(false);
  const [scanIds, setScanIds] = useState('20-24,30-34,10-14');
  const [out, setOut] = useState(null);
  const online = new Set((sv.rows || []).filter((r) => r.fresh).map((r) => r.id));
  const run = async (args, t) => { const r = await op(args, t); if (r.ok && r.data) setOut({ op: args.op, ...r.data }); };
  const copyDirs = () => {
    const line = 'directions = [' + IDS.map((id) => ((dirs[id] ?? -1) < 0 ? -1 : 1)).join(', ') + ']';
    navigator.clipboard?.writeText(line);
    setOut({ op: 'note', text: `Copied: ${line} (robotd.toml [bus.feetech] directions, order 20–24, 30–34, 10–14)` });
  };
  return html`<${Numbering} open=${open} op=${op} rows=${sv.rows} found=${found} setFound=${setFound} missing=${missing} setMissing=${setMissing} />
  <section class="sv-form">
    <span class="sv-label">Target</span>
    <div class="sv-ctrl"><${ServoPick} selected=${sel} onSelect=${pick} rows=${sv.rows} extra=${extra} /></div>
    <span class="sv-label">Advanced</span>
    <div class="sv-ctrl">
      <button class="btn-light" disabled=${!open} onClick=${() => run({ op: 'dump', id: sel })}>Dump Registers</button>
      <button class="btn-light" disabled=${!open} onClick=${() => { if (window.confirm(`Reboot servo #${sel} (disables torque briefly)?`)) run({ op: 'reboot', id: sel }); }}>Reboot</button>
      <button class="btn-light" onClick=${copyDirs}>Copy Directions</button>
    </div>
    <span class="sv-label">Change ID</span>
    <div class="sv-ctrl"><span>${sel} →</span>
      <select value=${newId} onChange=${(e) => setNewId(e.target.value)}>
        <option value="">New ID</option>
        ${Array.from({ length: 254 }, (_, i) => i).filter((i) => i !== sel).map((i) => html`<option value=${i}>${i}${online.has(i) ? ' (in use)' : ''}</option>`)}
      </select>
      <button class="btn-danger" disabled=${!open || newId === ''} onClick=${() => {
        if (online.has(+newId)) { window.alert(`ID ${newId} is already in use on the bus.`); return; }
        if (window.confirm(`Change ID ${sel} → ${newId}? Isolate this servo on the bus if possible.`)) run({ op: 'set_id', old: sel, new: +newId });
      }}>Write ID</button></div>
    <span class="sv-label">Write Reg</span>
    <div class="sv-ctrl"><select value=${reg} onChange=${(e) => { const r = REGS.find((x) => x[0] === +e.target.value); setReg(+e.target.value); setSize(r ? r[2] : 1); setUnlock(r ? r[3] === 'EEPROM' : false); }}>
        ${REGS.map(([a, n, s, area]) => html`<option value=${a}>${a} · ${n} (${s}B ${area})</option>`)}</select>
      <input type="number" value=${val} onInput=${(e) => setVal(e.target.value)} placeholder="Val" style="width:80px" />
      <label class="sv-inline"><input type="checkbox" checked=${unlock} onChange=${(e) => setUnlock(e.target.checked)} /> Unlock</label>
      <button class=${DANGER[reg] ? 'btn-danger' : 'btn-light'} disabled=${!open || val === ''} onClick=${() => {
        if (DANGER[reg] && !window.confirm(`Danger: ${DANGER[reg]}\nWrite ${val} to register ${reg} on servo #${sel}?`)) return;
        run({ op: 'write_reg', id: sel, addr: +reg, size, value: +val, unlock });
      }}>Write</button></div>
    ${DANGER[reg] ? html`<span></span><p class="sv-warn">Register ${reg}: ${DANGER[reg]}</p>` : null}
    <span class="sv-label">Bus Scan</span>
    <div class="sv-ctrl">
      <button class="btn-light" disabled=${!open} onClick=${() => run({ op: 'timing', n: 200 }, 30000)}>Timing ×200</button>
      <button class="btn-light" disabled=${!open} onClick=${() => run({ op: 'scan_all' }, 30000)}>Scan 0–253</button>
      <input type="text" value=${scanIds} onInput=${(e) => setScanIds(e.target.value)} style="width:140px" />
      <button class="btn-light" disabled=${!open} onClick=${() => run({ op: 'scan', ids: scanIds })}>Scan</button>
    </div>
    ${out ? html`<span></span><${ToolOut} out=${out} />` : null}
  </section>`;
}

function ToolOut({ out }) {
  if (out.op === 'note') return html`<p class="sv-small">${out.text}</p>`;
  if (out.op === 'timing') return html`<p class="sv-out num">Timing: ${out.n}×${out.ids} servos · min ${out.min_ms} ms · avg ${out.avg_ms} ms · p99 ${out.p99_ms} ms · max ${out.max_ms} ms · fails ${out.fails}</p>`;
  if (out.op === 'scan' || out.op === 'scan_all') return html`<p class="sv-out">Online (${out.found.length}): ${out.found.join(', ') || 'none'}${out.lost?.length ? `; missing: ${out.lost.join(', ')}` : ''}</p>`;
  if (out.op === 'set_id') return html`<p class="sv-out">${out.ok ? 'ID updated and ping verified' : 'New ID did not respond to ping'}</p>`;
  if (out.op === 'write_reg') return html`<p class="sv-out">Old ${out.old} → Readback ${out.new}${out.same ? ' ✓' : ' (mismatch)'}</p>`;
  if (out.op === 'dump') {
    const md = ['| Addr | Name | Area | Value |', '|---|---|---|---|', ...out.rows.map((r) => `| ${r.addr} | ${r.name} | ${r.area} | ${r.value} |`)].join('\n');
    const download = () => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: 'application/json' }));
      a.download = `HD-1910-id${out.id}-regs-${new Date().toISOString().slice(0, 10)}.json`; a.click();
    };
    return html`<div class="sv-out">
      <div class="sv-ctrl"><b>#${out.id} Registers</b><button class="btn-light" onClick=${download}>Download JSON</button>
        <button class="btn-light" onClick=${() => navigator.clipboard?.writeText(md)}>Copy Markdown</button></div>
      <div class="sv-dump">${out.rows.map((r) => html`<span class=${DANGER[r.addr] ? 'danger' : ''}><i>${r.addr}</i> ${r.name} <b class="num">${r.value}</b></span>`)}</div>
    </div>`;
  }
  return null;
}

function LogBox({ link }) {
  const [items, setItems] = useState([]);
  useEffect(() => link.on((m) => {
    const cat = String(m.entry?.cat || '').toLowerCase();
    if (m.t === 'log' && ['bus', 'calib', 'calibration', 'safety', 'cmd', 'command', 'servo'].includes(cat)) {
      setItems((xs) => [m.entry, ...xs].slice(0, 60));
    }
  }), []);
  return html`<div class="sv-logwrap">
    <div class="sv-label">Logs <span class="sv-small">(bus, calibration, safety; full logs in Logs tab)</span></div>
    <div class="sv-log">${items.length ? items.map((e) => html`<div key=${e.n} class=${e.level}><span class="num">${e.t}</span> [${e.cat}] ${e.msg}${e.raw ? html` <small>${e.raw}</small>` : null}</div>`)
      : html`<span class="sv-small">No entries yet</span>`}</div>
  </div>`;
}

function CalibDialog({ plan, onOk, onCancel }) {
  const big = plan.rows.filter((r) => r.diff_deg != null && Math.abs(r.diff_deg) > plan.big_deg);
  return html`<div class="sv-modal" role="dialog" aria-modal="true">
    <div class="board sv-modal-box">
      <h2>Zero Calibration (2048)</h2>
      <p class="sv-small">Align robot to official Zero Pose (legs straight, head level, beak closed). Torque disables briefly per servo, then re-enables.${plan.sim ? ' (Simulated mode)' : ''}</p>
      <table class="sv-plan"><thead><tr><th>ID</th><th class="r">Current</th><th class="r">Target</th><th class="r">Delta</th></tr></thead>
        <tbody>${plan.rows.map((r) => html`<tr class=${big.includes(r) ? 'big' : ''}><td>${r.id}</td><td class="r num">${fmt(r.pos)}</td>
          <td class="r num">2048</td><td class="r num">${r.diff_deg == null ? 'N/A' : sign(r.diff_deg) + '°'}</td></tr>`)}</tbody></table>
      ${big.length ? html`<p class="sv-warn">${big.length} servo(s) differ by more than ${plan.big_deg}°. Verify mechanical horn alignment and direction sign before confirming.</p>` : null}
      <div class="sv-modal-btns"><button class="btn-light" onClick=${onCancel}>Cancel</button>
        <button class="btn-dark" onClick=${() => { if (!big.length || window.confirm(`${big.length} servo(s) exceed ${plan.big_deg}° delta. Proceed with calibration?`)) onOk(); }}>Confirm & Calibrate (${plan.rows.length})</button></div>
    </div>
  </div>`;
}

// ---------------- Page ----------------

const HANDBACK_ASK = 'Hand back: relaxes all 15 servos immediately — support the robot first';
export function HandBackButton({ link }) {
  const [err, setErr] = useState(null);
  const confirm = async () => {
    setErr(null);
    const r = await link.cmd('servo.close', {}, 90000);
    if (!r.ok) setErr(r.reason || r.error || 'Handback failed');
  };
  return html`<span class="sv-handback"><${TwoStep} label="Hand Back to robotd" ask=${HANDBACK_ASK} busyLabel="Handing back…" onConfirm=${confirm} />${
    err ? html`<span class="sv-warn">${err}</span>` : null}</span>`;
}

export function ServoPage({ sv, link }) {
  const [pose, setPose] = useState('live');
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [hint, setHint] = useState(null);
  const [motion, setMotion] = useState({ seconds: 2, mode: 'profile', acc: 30, nudge: 1 });
  const [follow, setFollow] = useState(true);
  const [poses, setPoses] = useState([]);
  const [idle, setIdle] = useState(false);
  const [plan, setPlan] = useState(null);
  const [syncKey, setSyncKey] = useState(0);
  const open = sv?.state === 'open';
  const syncSliders = useCallback(() => { setSyncKey((k) => k + 1); setHint('Sliders synced to live servo positions'); }, []);
  const dirs = Object.fromEntries((sv?.rows || []).map((r) => [r.id, r.dir]));

  const onSelect = useCallback((id) => setSelected(id), []);
  const op = useCallback(async (args, timeout = 15000, quiet = false) => {
    const r = await link.cmd('servo.op', args, timeout);
    if (!r.ok) setMsg(r.reason || r.error || 'Operation failed');
    else if (!quiet) setMsg(null);
    return r;
  }, []);
  const run = async (name, args = {}) => {
    setBusy(true); setMsg(null);
    const r = await link.cmd(name, args, 15000);
    setBusy(false);
    if (!r.ok) setMsg(r.reason || r.error || 'Operation failed');
  };

  const [ports, setPorts] = useState(null);
  const [pick, setPick] = useState('');
  const loadPorts = async () => {
    const r = await link.cmd('servo.ports', {}, 5000);
    if (r.ok) { setPorts(r.data); setPick((p) => p || (r.data.sim ? 'sim' : r.data.port || '')); }
    return r.ok ? r.data : null;
  };
  useEffect(() => {
    let alive = true;
    link.whenOpen().then(() => loadPorts()).then((d) => { if (alive && d && (d.port || d.sim)) run('servo.open'); });
    return () => { alive = false; link.cmd('servo.close', { if_idle: true }, 15000); };
  }, []);
  calibHandler = useCallback(async (ids) => {
    const text = Array.isArray(ids) ? ids.join(',') : String(ids);
    const r = await op({ op: 'calib_plan', ids: text });
    if (r.ok) setPlan({ ...r.data, ids: text });
  }, []);
  useEffect(() => { if (open) op({ op: 'poses' }).then((r) => r.ok && setPoses(r.data.poses)); else setIdle(false); }, [open]);
  useEffect(() => { if (!hint) return undefined; const t = setTimeout(() => setHint(null), 4000); return () => clearTimeout(t); }, [hint]);

  const dr = sv?.drop_rate;
  return html`<div class="sv-page">
    <header class="sv-head">
      <div class="sv-titles">
        <div class="sv-titleline">
          <h1>Servos</h1>
          ${sv?.rows ? html`<div class="sv-stats num">
            <div><b>${sv.online ?? '—'}<small>/${sv.total}</small></b><span>Online</span></div>
            <div><b>${fmt(sv.avg_volts, 1)}<small> V</small></b><span>Avg Voltage</span></div>
            <div><b>${open ? fmt(sv.hz, 0) : '—'}<small> Hz</small></b><span>Rate</span></div>
            <div><b>${open && dr != null ? (dr * 100).toFixed(1) : '—'}<small> %</small></b><span>Drop</span></div>
            <div><b>${sv.holding ?? '—'}</b><span>Holding</span></div>
          </div>` : null}
        </div>
        <div class="sv-sub">${open
          ? html`<b class="sv-live">Servo Tuning</b> · ${sv.port} · robotd paused, gamepad disabled${sv.latched ? ' · All relaxed' : ''} · Torque stays active on page leave
            <${HandBackButton} link=${link} />`
          : busy ? 'Taking over servo bus…'
            : html`Read-only${sv?.diagnostic ? ' · Startup diagnostics · Raw feedback' : ''}`}
        <span class="sv-port">Port
          <select value=${pick} onFocus=${loadPorts} onChange=${(e) => setPick(e.target.value)} disabled=${open}
            title=${open ? 'Hand back to robotd before changing serial port' : ''}>
            <option value="">Select port…</option>
            ${(ports?.ports || []).map((p) => html`<option value=${p.device}>${p.device}${p.desc ? ` · ${p.desc}` : ''}</option>`)}
            ${ports?.port && !(ports.ports || []).some((p) => p.device === ports.port) ? html`<option value=${ports.port}>${ports.port}</option>` : null}
            <option value="sim">Simulator (no hardware)</option>
          </select>
          <button class="btn-light" disabled=${open || busy || !pick} onClick=${() => run('servo.open', { port: pick })}
            title=${open ? 'Hand back to robotd before changing port' : ''}>${open ? 'Switch' : 'Connect'}</button>
        </span></div>
        ${open && sv.gains ? html`<${GainsLine} g=${sv.gains} />` : null}
      </div>
      <div class="sv-actions">
        <${Toolbar} open=${open} op=${op} link=${link} motion=${motion} setMotion=${setMotion} idle=${idle} setIdle=${setIdle}
          follow=${follow} setFollow=${setFollow} poses=${poses} setHint=${setHint} syncSliders=${syncSliders}
          main=${null} />
      </div>
    </header>
    ${msg || sv?.error ? html`<div class="banner bad" role="alert"><b>${msg || 'Cannot open servo bus'}</b><span class="more">${msg ? '' : sv?.error || ''}</span>
      <a href="#" onClick=${(e) => { e.preventDefault(); setMsg(null); }}>Dismiss</a></div>` : null}
    ${sv?.marker_error ? html`<div class="banner warn" role="status"><b>Session marker write failed</b>
      <span class="more">${sv.marker_error}</span></div>` : null}
    ${hint ? html`<div class="banner warn" role="status"><span>${hint}</span></div>` : null}
    <div class="sv-main">
      <div class="sv-left">
        <${ServoStage} sv=${sv} pose=${pose} setPose=${setPose} selected=${selected} onSelect=${onSelect} />
        ${pose === 'zero' ? html`<${Why} />` : null}
      </div>
      <div class="sv-right">
        <${CalibCard} open=${open} op=${op} selected=${selected} onSelect=${onSelect} rows=${sv?.rows} />
        <${ServoList} key=${'list' + syncKey} sv=${sv || { rows: [] }} open=${open} selected=${selected} onSelect=${onSelect} op=${op} follow=${follow} setHint=${setHint} motion=${motion} />
      </div>
    </div>
    <${MoreArea} open=${open} op=${op} motion=${motion} setMotion=${setMotion} follow=${follow} setFollow=${setFollow}
      poses=${poses} setPoses=${setPoses} selected=${selected} onSelect=${onSelect} sv=${sv || { rows: [] }} dirs=${dirs} link=${link} />
    ${plan ? html`<${CalibDialog} plan=${plan} onCancel=${() => setPlan(null)} onOk=${async () => {
      const ids = plan.ids; setPlan(null);
      const r = await op({ op: 'calibrate', ids }, 60000);
      if (r.ok) {
        const d = r.data, bad = Object.keys(d.bad || {});
        setHint(`Calibration done: ${d.ok.length} succeeded${bad.length ? `, ${bad.join(', ')} failed (see Logs)` : ''}; calibrated servos now holding`);
      }
    }} />` : null}
  </div>`;
}
