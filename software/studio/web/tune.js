// Overview runtime/tuning cards (read-only telemetry from studio/tune.py) + Settings Model/Body form.
import { html, useState, useEffect } from './vendor/preact-htm.module.js';
import { Stepper } from './stepper.js';

const MODEL_NAME_MAP = {
  '\u884c\u8d70\uff0b\u5012\u5730\u8054\u5408 9600 \u8f6e': 'Walk + Stand Recovery (9600 rounds)',
  '\u4f4e\u5934\u6361\u53d6 10000 \u8f6e \u00b7 \u65e0\u7f29\u653e\u6ee4\u6ce2': 'Ground Pick (10000 rounds · Unfiltered)',
  '\u524d\u6eda\u7ffb 10000 \u8f6e \u00b7 \u65e0\u7f29\u653e\u6ee4\u6ce2': 'Forward Roll (10000 rounds · Unfiltered)',
  '\u53f3\u811a\u8e22\u7403 10000 \u8f6e \u00b7 \u65e0\u7f29\u653e\u6ee4\u6ce2': 'Right Kick (10000 rounds · Unfiltered)',
  '\u5de6\u811a\u8e22\u7403 10000 \u8f6e \u00b7 \u65e0\u7f29\u653e\u6ee4\u6ce2': 'Left Kick (10000 rounds · Unfiltered)',
};

export function sanitizeModelName(name) {
  if (!name || typeof name !== 'string') return name;
  if (MODEL_NAME_MAP[name]) return MODEL_NAME_MAP[name];
  let res = name;
  for (const [k, v] of Object.entries(MODEL_NAME_MAP)) {
    if (res.includes(k)) res = res.replaceAll(k, v);
  }
  return res.replace(/[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/g, '').trim() || res;
}

const CSS = `
.tune { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; align-items: start; }
@media (min-width: 1100px) { .tune { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } .tune > .tn-run { grid-column: 1 / -1; } }
.tune .card { padding: 14px 16px; }
.tune .tbl { display: grid; grid-template-columns: max-content max-content minmax(0, 1fr); font-size: 13.5px; line-height: 1.6; }
.tune .tbl > * { padding: 7px 18px 7px 0; border-top: 1px solid var(--line); min-width: 0; }
.tune .tbl > :nth-child(-n+3) { border-top: 0; }
.tune .tbl > :nth-child(3n) { padding-right: 0; }
.tune .tbl .k { color: var(--text); }
.tune .tbl .v { font-weight: 600; font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
.tune .tbl .d { color: var(--text-2); }
.tune .tbl .warn { color: var(--warn); }
.tune .tbl .bad { color: var(--danger); }
.tune .tn-tag { display: inline-block; white-space: nowrap; font-size: 11px; line-height: 18px; padding: 0 7px; border-radius: var(--radius); font-weight: 600; font-family: var(--font-mono); background: var(--panel-2); color: var(--text-2); border: 1px solid var(--hair); }
.tune .tn-tag.on { background: var(--ok-soft); color: var(--ok); border-color: var(--ok); }
.tune .tn-tag.err { background: var(--danger-soft); color: var(--danger); border-color: var(--danger); }
.tune .cap { font-size: 13px; color: var(--text-2); margin: 0 0 10px; }
.tune .cap.bad { color: var(--warn); }
.tune h3 { margin: 0 0 10px; font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.tune .summary { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 12px; font-size: 14px; }
.tune .summary .ph { font-weight: 600; }
.tune .summary .ph.warn { color: var(--warn); }
.tune .summary .ph.bad { color: var(--danger); }
.tune .group { margin-top: 12px; }
.tune .group > b { font-size: 12px; color: var(--text-2); font-weight: 600; font-family: var(--font-mono); text-transform: uppercase; letter-spacing: 0.06em; }
.tune .kv { display: grid; grid-template-columns: 165px 1fr; gap: 3px 12px; font-size: 13.5px; line-height: 1.6; }
.tune .kv > span:first-child { color: var(--text-2); }
.tune .kv .warn { color: var(--warn); }
.tune .kv .bad { color: var(--danger); }
.tune .kv .ok { color: var(--ok); }
.tune .row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 14px; font-size: 13.5px; line-height: 1.7; padding: 5px 0; border-top: 1px solid var(--line); }
.tune .row:first-child { border-top: 0; }
.tune .row .k { color: var(--text); font-weight: 500; }
.tune .row .d { color: var(--text-2); }
.tune .row .v { font-weight: 500; }
.tune .row .warn { color: var(--warn); }
.tune .row .bad { color: var(--danger); }
.tune .fold { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
.tune .fold > button { all: unset; cursor: pointer; font-size: 13px; color: var(--text-2); }
.tune .fold > button:hover { color: var(--text); }
.tune .foot { margin-top: 10px; font-size: 12.5px; color: var(--text-2); }
.tune .steps { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.tune .steps .step { display: flex; gap: 8px; line-height: 1.6; }
.tune .steps .mk { width: 16px; text-align: center; font-weight: 700; }
.tune .steps .mk.s { color: var(--ok); }
.tune .steps .mk.f { color: var(--danger); }
.tune .steps .mk.p { color: var(--accent); }
.tune .steps .mk.w { color: var(--text-2); }
.tune .steps .dt { color: var(--text-2); }
.mmod { padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.mmod h2 { margin: 0; font-size: 15px; font-weight: 600; }
.mmod .slot { border-top: 1px solid var(--line); padding-top: 10px; font-size: 13.5px; line-height: 1.7; }
.mmod .slot:first-of-type { border-top: 0; padding-top: 0; }
.mmod .slot .nm { font-weight: 600; }
.mmod .slot .meta { color: var(--text-2); font-size: 12.5px; }
.mmod .slot.err .nm { color: var(--danger); }
.mmod .rol { font-size: 13px; color: var(--text-2); display: flex; flex-wrap: wrap; gap: 4px 16px; }
.mmod .body { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; align-items: center; }
.mmod .body > label, .mmod .body > .k { font-size: 14px; }
.mmod .body .note { grid-column: 2; font-size: 13px; line-height: 1.55; color: var(--text-2); }
.mmod input[type=number], .mmod input[type=text] { width: 120px; height: 32px; padding: 0 8px; border: 1px solid var(--hair-strong); border-radius: var(--radius); background: var(--panel); color: var(--text); font: inherit; font-size: 15px; }
.mmod input[type=text] { width: 100%; max-width: 340px; }
.mmod .btn { height: 34px; padding: 0 16px; border: 1px solid var(--hair); border-radius: var(--radius); background: var(--panel-2); color: var(--text); font: inherit; font-size: 14px; cursor: pointer; }
.mmod .btn.primary { background: var(--accent); color: var(--paper); border-color: var(--accent); font-weight: 600; }
.mmod .btn:disabled { opacity: .45; cursor: default; }
.mmod .msg { font-size: 13px; line-height: 1.55; }
.mmod .msg.bad { color: var(--danger); }
.mmod .msg.ok { color: var(--ok); }
@media (max-width: 560px) {
  .tune .kv { grid-template-columns: 1fr; }
  .tune .kv > span:first-child { color: var(--text); font-weight: 500; }
  .tune .tbl { grid-template-columns: max-content minmax(0, 1fr); }
  .tune .tbl > * { padding-right: 12px; }
  .tune .tbl > :nth-child(-n+3) { border-top: 1px solid var(--line); }
  .tune .tbl > :nth-child(-n+2) { border-top: 0; }
  .tune .tbl > .d { grid-column: 1 / -1; border-top: 0; padding-top: 0; }
  .mmod .body { grid-template-columns: 1fr; }
  .mmod .body .note { grid-column: 1; }
}
`;
if (typeof document !== 'undefined' && !document.getElementById('tune-css')) {
  const s = document.createElement('style');
  s.id = 'tune-css';
  s.textContent = CSS;
  document.head.appendChild(s);
}

const num = (v, d = 1) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '—');
const rnd = (v) => (typeof v === 'number' && isFinite(v) ? String(Math.round(v)) : '—');
const txt = (v) => (v === null || v === undefined || v === '' ? '—' : String(v));

function StepMark({ status }) {
  const c = { success: 's', failed: 'f', starting: 'p', waiting: 'w' }[status] || 'w';
  const g = { success: '✓', failed: '✗', starting: '●', waiting: '○' }[status] || '·';
  return html`<span class=${'mk ' + c}>${g}</span>`;
}

function RunCard({ run }) {
  const [open, setOpen] = useState(false);
  const ph = run?.phase || {};
  const auto = ph.kind === 'warn' || ph.kind === 'bad' || ph.error;
  const shown = open || auto;
  const toggle = () => setOpen(!open);
  const hz = run?.loop?.hz;
  const can = run?.can_walk;
  const walk = run?.net?.walk_model;
  const actual = run?.scale?.actual;
  const st = run?.startup;
  return html`<section class="board card tn-run" aria-label="Runtime Status">
    <button style=${'all:unset;display:block;width:100%;cursor:pointer'} onClick=${toggle} aria-expanded=${shown}>
      <div class="summary">
        <span class=${'ph ' + (ph.kind || '')}>${txt(ph.word)}</span>
        <span>·</span>
        <span class="num">${num(hz)} Hz</span>
        ${can !== null ? html`<span>·</span><span>${can ? 'Ready to walk' : 'Walk blocked'}</span>` : null}
        ${walk ? html`<span>·</span><span>Walk ${walk}</span>` : null}
        ${actual !== null && actual !== undefined ? html`<span>·</span><span>Scale ${num(actual, 2)}</span>` : null}
        <span style="color:var(--text-2);margin-left:auto">${shown ? 'Hide ▲' : 'Details ▼'}</span>
      </div>
    </button>
    ${shown ? html`
      ${st && st.known ? html`<div class="group"><b>Startup Sequence</b>
        <div class="steps">${(st.rows || []).map((r) => html`<div class="step">
          <${StepMark} status=${r.status} /><span>${r.name}</span>
          ${r.detail ? html`<span class="dt">${r.detail}</span>` : null}
          ${r.at_s !== null && r.at_s !== undefined ? html`<span class="dt">(+${num(r.at_s)}s)</span>` : null}
        </div>`)}</div>
        ${st.unsupported ? html`<div class="foot">${st.unsupported}</div>` : null}
      </div>` : null}
      <div class="group"><b>Control Loop</b>
        <div class="kv">
          <span>Target / Actual</span><span class="num">${num(run?.loop?.target)} / ${num(hz)} Hz</span>
          <span>Missed Ticks</span><span class="num">${rnd(run?.loop?.missed)}${run?.loop?.missed_per_min != null ? ` (+${rnd(run?.loop?.missed_per_min)}/min)` : ''}</span>
          <span>Last Tick Age</span><span class="num">${num(run?.loop?.last_tick_age_ms, 0)} ms</span>
          <span>Active Policy</span><span>${txt(sanitizeModelName(run?.net?.active_model))}${run?.net?.scale_use_cn ? ` · ${run?.net?.scale_use_cn}` : ''}</span>
          <span>Step Scale</span><span class="num">${num(actual, 2)} = base ${num(run?.scale?.base, 2)} × volt ${num(run?.scale?.volt_mult, 2)}</span>
        </div>
      </div>
      ${run?.guard ? html`<div class="group"><b>Start Guard</b>
        <div class="kv">
          <span>Max Joint Error</span><span class="num">${num(run.guard.max_joint_error_deg, 2)}° / tol ${num(run.guard.joint_tolerance_deg, 2)}°</span>
          <span>Body Tilt</span><span class="num">${num(run.guard.tilt_deg, 2)}° / tol ${num(run.guard.tilt_tolerance_deg, 2)}°</span>
          ${run.guard.reason ? html`<span>Reason</span><span>${run.guard.reason}</span>` : null}
          ${run?.refusal ? html`<span>Last Refusal</span><span class="warn">${run.refusal}</span>` : null}
        </div>
      </div>` : null}
      <div class="group"><b>Hardware</b>
        <div class="kv">
          <span>IMU</span><span>${txt(run?.imu?.source)} · ${run?.imu?.ready === true ? 'Ready' : run?.imu?.ready === false ? 'Not ready' : '—'}${run?.imu?.age_ms !== undefined ? ` · ${num(run?.imu?.age_ms, 0)} ms ago` : ''}${run?.imu?.fusion?.verdict ? ` · Fusion ${run.imu.fusion.verdict}` : ''}</span>
          <span>Bus</span><span>${rnd(run?.bus?.consecutive_errors)} consecutive errors${run?.bus?.startup_failures ? ` · ${rnd(run?.bus?.startup_failures)} startup failures` : ''}</span>
          ${(run?.bus?.worst || []).length ? html`<span>Worst Servos</span><span>${run.bus.worst.map((w) => `${w.name} (to ${w.timeout} · miss ${w.missing})`).join(' · ')}</span>` : null}
          ${run?.cpu_c !== undefined && run?.cpu_c !== null ? html`<span>SoC Temp</span><span class="num">${num(run.cpu_c, 0)} °C</span>` : null}
        </div>
      </div>
      <div class="foot">${txt(run?.infer_note)}</div>
    ` : null}
  </section>`;
}

function FeelCard({ feel, cap }) {
  const [adv, setAdv] = useState(true);
  const walk = feel?.walk || {};
  const vals = walk.values || {};
  const active = feel?.active;
  const pad = feel?.pad || {};
  const cur = pad.current || {};
  const servo = feel?.servo || {};
  const eff = servo.effective || {};
  const row = (k, v, d, kind) => html`<span class="k">${k}</span><span class="v">${v}</span><span class=${'d ' + (kind || '')}>${d || ''}</span>`;
  const wv = (key, label, def) => {
    const x = vals[key] || {};
    const v = x.loaded != null ? x.loaded : x.file;
    const dflt = def != null ? `Default ${num(def, 2)}` : '';
    if (x.file != null && x.loaded != null && x.file !== x.loaded) {
      return row(label, num(v, 2), `${dflt ? `${dflt} · ` : ''}File is ${num(x.file, 2)}, active ${num(x.loaded, 2)} (restart robotd to apply)`, 'warn');
    }
    return row(label, num(v, 2), dflt);
  };
  const pdi = (gs) => (gs || []).map((g) => `P${g.pdi[0]} D${g.pdi[1]} ×${g.n}`).join(' / ') || '—';
  return html`<section class="board card" aria-label="Control Feel">
    <h3>Control Feel</h3>
    <p class=${'cap ' + (cap?.can_edit === false ? '' : 'bad')}>${cap?.why || 'Reading duck configuration...'}</p>
    <div class="tbl">
      ${wv('action_scale', 'Action Scale', walk.default?.action_scale)}
      ${wv('head_lowpass', 'Head Lowpass', walk.default?.head_lowpass)}
      ${wv('legs_lowpass', 'Legs Lowpass', walk.default?.legs_lowpass)}
      ${active ? row('Active Tick', `${num(active.action_scale, 2)} · ${num(active.head_lowpass, 2)} · ${num(active.legs_lowpass, 2)}`,
        `Scale · Head · Legs${active.use ? ` (${active.use})` : ''}`) : null}
      ${row('Mouth Range', `${num(cur.mouth_percent, 0)}%`,
        pad.mouth_warn ? 'Near zero: LT/RT mouth control scales by this factor' : '', pad.mouth_warn ? 'warn' : '')}
      ${row('Head Range', `${num(cur.head_rad, 1)} rad`, '')}
    </div>
    <div class="fold">
      <button onClick=${() => setAdv(!adv)} aria-expanded=${adv}>${adv ? '▾' : '▸'} Servo Gains · Advanced</button>
      ${adv ? html`<div class="tbl" style="margin-top:8px">
        ${row('P', rnd(eff.kp), servo.src ? `Source: ${servo.src}` : '')}
        ${row('D', rnd(eff.kd), '')}
        ${row('Limit', rnd(eff.torque_limit), '')}
        ${servo.written_p ? row('Armed P', servo.written_p.p != null ? rnd(servo.written_p.p) : '—',
          `${servo.written_p.src || ''}${servo.written_p.applied ? ' · Applied' : ''}`) : null}
        ${servo.regs ? row('Pre-arm Regs', '', `RAM ${pdi(servo.regs.ram_before_arm)} · EEPROM ${pdi(servo.regs.eeprom)}`) : null}
        ${(servo.warn || []).map((w) => row('', '', w, 'warn'))}
      </div>` : null}
      ${adv && servo.regs_why ? html`<div class="foot">${servo.regs_why}</div>` : null}
    </div>
  </section>`;
}

function ModelsCard({ models }) {
  const slots = models?.slots || [];
  const rollers = models?.rollers || [];
  const what = (s) => (s.name ? `${sanitizeModelName(s.name)}${s.rounds ? ` · ${s.rounds} steps` : ''}` : s.path ? 'Metadata unreadable' : 'Not installed');
  const rolOn = rollers.filter((r) => r.installed);
  return html`<section class="board card" aria-label="Models">
    <h3>Models <a href="#/models" aria-label="Manage Models" style="font-size:12px;font-weight:400;color:var(--link);text-decoration:none">Manage ›</a></h3>
    <div class="tbl">
      ${slots.map((s) => html`<span class="k">${s.label}</span>
        <span>${s.error ? html`<span class="tn-tag err">Error</span>` : s.active ? html`<span class="tn-tag on">Active</span>`
          : s.loaded ? html`<span class="tn-tag on">Loaded</span>` : html`<span class="tn-tag">${s.path ? 'Unloaded' : 'Empty'}</span>`}</span>
        <span class="d">${what(s)}${s.sha12 ? html` · <span title="SHA-256 prefix">${s.sha12}</span>` : null}
          ${s.error ? html`<br /><span class="bad">${s.error}</span>` : null}
          ${s.exec ? html`<br />Scale ${num(s.exec.action_scale, 2)} · Head ${num(s.exec.head_lowpass, 2)} · Legs ${num(s.exec.legs_lowpass, 2)}` : null}
          ${s.buttons?.length ? html`<br />Buttons: ${s.buttons.join(' ')}` : null}</span>`)}
      ${rollers.length ? html`<span class="k">Roller (${rollers.length})</span>
        <span><span class=${'tn-tag' + (rolOn.length ? ' on' : '')}>${rolOn.length ? `${rolOn.length} installed` : 'Empty'}</span></span>
        <span class="d">${rolOn.length
          ? rollers.map((r) => `${r.label.replace(/^Roller · /, '')} ${r.installed ? (r.rounds ? `${r.rounds} steps` : 'Installed') : 'Empty'}`).join(' · ')
          : 'Requires roller hardware'}</span>` : null}
    </div>
  </section>`;
}

export function TuneSection({ tune }) {
  if (!tune || !tune.ready) return html`<div class="board card tune"><h3>Runtime & Tuning</h3><div class="cap">Reading duck configuration...</div></div>`;
  return html`<div class="tune" id="tune-block">
    <${RunCard} run=${tune.run} />
    <${FeelCard} feel=${tune.feel} cap=${tune.cap} />
    <${ModelsCard} models=${tune.models} />
  </div>`;
}

const BODY_FIELDS = [
  ['total_g', 'Total Mass', 'g'],
  ['trunk_g', 'Trunk', 'g'],
  ['head_g', 'Head', 'g'],
  ['leg_left_g', 'Left Leg', 'g'],
  ['leg_right_g', 'Right Leg', 'g'],
];

export function ModelSettings({ tune, link }) {
  const [body, setBody] = useState(null);
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    await link.whenOpen();
    const r = await link.cmd('tune.op', { op: 'body' }, 8000);
    if (r.ok) {
      const b = r.data || {};
      setBody(b);
      setForm(Object.fromEntries(BODY_FIELDS.map(([k]) => [k, b[k] ?? '']).concat([['foot', b.foot ?? ''], ['note', b.note ?? '']])));
    } else {
      setMsg({ kind: 'bad', text: `Failed to read mass profile: ${r.reason || r.error}` });
    }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true);
    const b = {};
    for (const [k] of BODY_FIELDS) {
      const v = form[k];
      b[k] = (v === '' || v === null || v === undefined) ? null : Number(v);
    }
    b.foot = (form.foot || '').trim() || null;
    b.note = (form.note || '').trim() || null;
    const r = await link.cmd('tune.op', { op: 'save_body', body: b }, 8000);
    setBusy(false);
    if (r.ok) { setBody(r.data); setMsg({ kind: 'ok', text: 'Saved to Studio state (included in profile export)' }); }
    else setMsg({ kind: 'bad', text: `Save failed: ${r.reason || r.error}` });
  };

  const slots = tune?.models?.slots || [];
  const rollers = tune?.models?.rollers || [];
  return html`<div class="board mmod" aria-label="Models & Hardware Profile">
    <h2>Models & Hardware Profile</h2>
    <a href="#/models" style="font-size:13px;color:var(--link)">Open Model Manager to import policies or bind buttons ›</a>
    ${slots.map((s) => html`<div class=${'slot' + (s.error ? ' err' : '')}>
      <div><span class="nm">${s.label}</span>${s.active ? ' · Active' : s.loaded ? ' · Loaded' : ' · Empty'}${s.rounds ? ` · ${s.rounds} steps` : ''}</div>
      <div class="meta">${s.name ? s.name : '—'}${s.task ? ` · ${s.task}` : ''}${s.sha12 ? ` · ${s.sha12}` : ''}${s.commit ? ` · ${s.commit}` : ''}</div>
      ${s.exec ? html`<div class="meta">Execution: scale ${num(s.exec.action_scale, 2)} · head ${num(s.exec.head_lowpass, 2)} · legs ${num(s.exec.legs_lowpass, 2)}</div>` : null}
      ${s.error ? html`<div class="meta" style="color:var(--danger)">${s.error}</div>` : null}
    </div>`)}
    ${rollers.length ? html`<div class="rol">${rollers.map((r) => html`<span>${r.label}: ${r.installed ? (r.rounds ? `${r.rounds} steps` : 'Installed') : 'Empty'}</span>`)}</div>` : null}

    <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px">
      <a class="btn primary" href="/api/profile" style="display:inline-flex;align-items:center;text-decoration:none">Export Duck Profile (JSON)</a>
      <span class="msg" style="color:var(--text-2)">Includes IMU orientation, servo zeros, P/D/limit gains, walk policy, and mass specs</span>
    </div>

    <div class="body">
      ${BODY_FIELDS.map(([k, label, unit]) => html`
        <label>${label}</label>
        <span style="display:flex;align-items:center;gap:8px">
          <${Stepper} value=${form ? form[k] : ''} min=${0} max=${5000} step=${0.1}
            aria-label=${label} unit=${unit} onChange=${(v) => setForm({ ...form, [k]: v })} />
        </span>`)}
      <span class="k">Foot Material</span>
      <span><input type="text" maxlength="40" value=${form ? form.foot : ''} aria-label="Foot material"
        onInput=${(e) => setForm({ ...form, foot: e.target.value })} /></span>
      <span class="k">Notes</span>
      <span><input type="text" maxlength="200" value=${form ? form.note : ''} aria-label="Notes"
        onInput=${(e) => setForm({ ...form, note: e.target.value })} /></span>
      <span></span>
      <div class="note">Unmeasured fields are exported as null. Approximate values help policy training.</div>
    </div>

    <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px">
      <button class="btn primary" type="button" disabled=${!form || busy} onClick=${save}>Save Mass Profile</button>
      ${msg ? html`<span class=${'msg ' + msg.kind}>${msg.text}</span>` : null}
    </div>
  </div>`;
}
