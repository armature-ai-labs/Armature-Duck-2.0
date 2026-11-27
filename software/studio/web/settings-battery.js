// Battery Settings panel (2S LiPo / Bench PSU, thresholds, calibration, mute).
import { html, useState, useEffect } from './vendor/preact-htm.module.js';
import { loadMute, saveMute } from './battery.js';
import { Stepper } from './stepper.js';

const f2 = (v) => (typeof v === 'number' && isFinite(v) ? v.toFixed(2) : '—');
const sign = (v) => (typeof v === 'number' && isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}` : '—');
const CLS = { rest: 'Relaxed', light: 'Stand / Sit', heavy: 'Walk / Action' };

const CSS = `
.bset { padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.bset h2 { margin: 0; font-size: 15px; font-weight: 600; }
.bset .now { font-size: 13px; color: var(--text-2); }
.bset .row { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; align-items: center; }
.bset .row > label, .bset .row > .k { font-size: 14px; }
.bset .note { grid-column: 2; font-size: 13px; line-height: 1.55; color: var(--text-2); }
.bset input[type=number] { width: 96px; height: 32px; padding: 0 8px; border: 1px solid var(--hair-strong); border-radius: var(--radius); background: var(--panel); color: var(--ink); font: inherit; font-size: 15px; }
.bset .radio { display: flex; flex-wrap: wrap; gap: 4px 18px; }
.bset .radio label { display: flex; align-items: center; gap: 8px; height: 32px; font-size: 14px; }
.bset .btn { height: 34px; padding: 0 16px; border: 1px solid var(--hair); border-radius: var(--radius); background: var(--panel-2); color: var(--ink); font: inherit; font-size: 14px; cursor: pointer; }
.bset .btn.primary { background: var(--accent); color: var(--paper); border-color: var(--accent); font-weight: 600; }
.bset .btn:disabled { opacity: .45; cursor: default; }
.bset .msg { font-size: 13px; line-height: 1.55; }
.bset .msg.bad { color: var(--danger); }
.bset .msg.ok { color: var(--ok); }
.bset .tips { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.65; color: var(--text-2); }
.bset .cal { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
@media (max-width: 560px) { .bset .row { grid-template-columns: 1fr; } .bset .note { grid-column: 1; } }
`;
if (!document.getElementById('bset-css')) {
  const s = document.createElement('style');
  s.id = 'bset-css';
  s.textContent = CSS;
  document.head.appendChild(s);
}

export function BatterySettings({ link, st }) {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [meter, setMeter] = useState('');
  const [cal, setCal] = useState(null);
  const [muted, setMuted] = useState(loadMute());

  const load = async () => {
    await link.whenOpen();
    const r = await link.cmd('battery.op', { op: 'get' }, 8000);
    if (r.ok) { setData(r.data); setForm({ ...r.data.settings }); } else setMsg({ kind: 'bad', text: `Failed to read battery settings: ${r.reason || r.error}` });
  };
  useEffect(() => { load(); }, []);

  if (!form) {
    return html`<div class="board bset" aria-label="Battery"><h2>Battery</h2><div class="now">${msg ? msg.text : 'Loading...'}</div></div>`;
  }
  const s0 = data.settings, rg = data.ranges || {};
  const bench = form.preset === 'bench';
  const dirty = Object.keys(s0).some((k) => s0[k] !== form[k]);
  const b = st?.battery || {};
  const set = (k, v) => { setForm({ ...form, [k]: v }); setMsg(null); };
  const num = (k, step, unit) => html`<${Stepper} value=${form[k]} min=${rg[k]?.[0]} max=${rg[k]?.[1]} step=${step}
    aria-label=${k} unit=${unit} onChange=${(v) => set(k, v)} />`;

  const save = async (values) => {
    setBusy(true);
    const r = await link.cmd('battery.op', { op: 'set', values }, 8000);
    setBusy(false);
    if (r.ok) { setData(r.data); setForm({ ...r.data.settings }); setMsg({ kind: 'ok', text: 'Saved and applied immediately' }); setCal(null); return true; }
    setMsg({ kind: 'bad', text: `Save failed: ${r.reason || r.error}` });
    return false;
  };
  const measure = async () => {
    const v = Number(meter);
    setCal(null);
    setMsg(null);
    const r = await link.cmd('battery.op', { op: 'calibrate', measured_v: meter === '' ? null : v }, 8000);
    if (!r.ok) { setCal({ ok: false, reason: r.reason || r.error }); return; }
    setCal(r.data);
  };

  const now = b.mode === 'no_data' ? 'Servo voltage unavailable'
    : b.mode === 'estimating' ? 'Estimating...'
      : `Live: ${f2(b.volts)} V (${CLS[b.cls] || 'Unknown'})${bench || b.mode === 'bench' ? '' : `, ~${b.pct ?? '—'}%`}`;
  return html`<div class="board bset" aria-label="Battery">
    <h2>Battery</h2>
    <div class="now num">${data.presets?.[s0.preset] || s0.preset} · ${s0.preset === 'bench' ? 'Voltage Only' : '2S LiPo Table'} · Offset ${sign(s0.cal_offset_v)} V · ${now}</div>

    <div class="row">
      <span class="k">Power Source</span>
      <div class="radio">
        ${Object.entries(data.presets || {}).map(([k, label]) => html`<label>
          <input type="radio" name="bat-preset" value=${k} checked=${form.preset === k} onChange=${() => set('preset', k)} /> ${label}</label>`)}
      </div>
      <div class="note">${bench
        ? 'Bench PSU: displays voltage only (no percentage). Warns if voltage stays below load floor for 30s.'
        : 'HD-1910 max 8.4V (2S LiPo). Estimated from servo voltage (~15% tolerance); conservative before calibration.'}</div>
    </div>

    ${bench ? null : html`
    <div class="row">
      <label>Warn Threshold</label>
      <span>${num('warn_pct', 1, '%')}</span>
      <div class="note">Default ${data.defaults.warn_pct}%.</div>
    </div>
    <div class="row">
      <label>Critical Threshold</label>
      <span>${num('red_pct', 1, '%')}</span>
      <div class="note">Default ${data.defaults.red_pct}% (at least 5% below warn).</div>
    </div>`}
    <div class="row">
      <label>Load Floor</label>
      <span>${num('load_floor_v', 0.1, 'V')}</span>
      <div class="note">Default ${data.defaults.load_floor_v} V (3.45 V/cell). Alerts after 30s below floor.</div>
    </div>
    <div class="row">
      <label>Voltage Offset</label>
      <span>${num('cal_offset_v', 0.01, 'V')}</span>
      <div class="note">Multimeter V − servo bus V (default 0).</div>
    </div>
    <div class="row">
      <span class="k">Calibrate Voltage</span>
      <div class="cal">
        <${Stepper} value=${meter} min=${6} max=${9} step=${0.01} placeholder="7.62" aria-label="Multimeter voltage" unit="V"
          onChange=${(v) => { setMeter(v); setCal(null); }} />
        <button class="btn" type="button" disabled=${meter === ''} onClick=${measure}>Compute</button>
      </div>
      <div class="note">Relax servos for 30s+, measure HAT battery input with a multimeter, and enter here.</div>
      ${cal ? html`<div class=${'note msg ' + (cal.ok ? '' : 'bad')}>${cal.reason}
        ${cal.ok ? html`<div style="margin-top:6px"><button class="btn primary" type="button" disabled=${busy}
          onClick=${() => save({ cal_offset_v: cal.offset })}>${cal.confirm ? 'Confirm & ' : ''}Apply (${sign(cal.offset)} V)</button></div>` : null}</div>` : null}
    </div>

    <div class="row">
      <span class="k">Alert Sound</span>
      <label style="font-size:14px"><input type="checkbox" checked=${!muted}
        onChange=${(e) => { const m = !e.target.checked; setMuted(m); saveMute(m); }} /> Beep in browser on critical/low voltage</label>
      <div class="note">Shares mute state with IMU calibration.</div>
    </div>

    <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px">
      <button class="btn primary" type="button" disabled=${!dirty || busy} onClick=${() => save(form)}>Save</button>
      <button class="btn" type="button" disabled=${busy} onClick=${() => { setForm({ ...data.defaults }); setMsg(null); }}>Reset Defaults</button>
      ${msg ? html`<span class=${'msg ' + msg.kind}>${msg.text}</span>` : null}
    </div>

    <ul class="tips">
      ${bench ? null : html`<li>Warning only: power off and unplug battery when idle to prevent over-discharge.</li>
      <li>Store 2S LiPo at ~7.4–7.6 V when unused.</li>`}
      <li>Settings stored on duck (${data.path}).</li>
    </ul>
  </div>`;
}
