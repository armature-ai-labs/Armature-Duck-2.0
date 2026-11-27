// Mobile Overview: live 3D posture stage and compact telemetry grid.
import { html, useState, useEffect, useRef } from '../vendor/preact-htm.module.js';
import { Duck3D } from '../duck3d.js';
import { postureCopy, readings } from './actions.js';

function Stage({ st, live, name }) {
  const canvasRef = useRef(null);
  const duckRef = useRef(null);
  const [msg, setMsg] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let d;
    try {
      d = new Duck3D(canvasRef.current, { view: 'front34', centerX: 0.5, centerY: 0.44, fill: 0.72, pageScroll: true });
    } catch (e) {
      setMsg(`3D unavailable: ${e.message}`);
      return undefined;
    }
    duckRef.current = d;
    d.load('/model/').then(() => { setReady(true); d.setGrid(true); }).catch((e) => setMsg(`3D model failed: ${e.message}`));
    return () => { try { d.dispose(); } catch { /* already disposed */ } };
  }, []);

  useEffect(() => {
    const d = duckRef.current;
    if (!d || !st) return;
    d.setJoints(st.joints);
    d.setGravity(st.gravity);
  }, [st]);

  const old = st?.joints_age_ms == null || st.joints_age_ms > 1000;
  const c = postureCopy(st, name);
  return html`<section class=${'m-stage' + (live && !old ? '' : ' stale')} aria-label="Robot status">
    <canvas ref=${canvasRef}></canvas>
    ${!ready && !msg ? html`<div class="m-stage-load">Loading 3D model…</div>` : null}
    ${msg ? html`<div class="m-stage-load">${msg}</div>` : null}
    <div class="m-stage-badge"><b>${c.h}</b><span>${live && !old ? 'Live' : 'Stale'}</span></div>
  </section>`;
}

function Cell({ label, big, unit, small, kind, stale }) {
  return html`<div class=${'m-card m-cell' + (kind ? ' ' + kind : '') + (stale ? ' stale' : '')}>
    <span class="label">${label}</span>
    <span class="big num">${big}</span>${unit ? html`<span class="unit">${unit}</span>` : null}
    <span class="small">${small}</span>
  </div>`;
}

export function Overview({ st, live, age, name }) {
  const r = readings(st);
  const c = postureCopy(st, name);
  const old = !live || age > 1000;

  const pctKind = r.pct == null ? null : r.pct <= 15 ? 'bad' : r.pct <= 30 ? 'warn' : null;

  return html`<div>
    <${Stage} st=${st} live=${live} name=${name} />
    <div class="m-caption">
      <h1>${c.h}</h1>
      <p>${c.sub}</p>
      ${st?.start_error ? html`<p class="deal">${st.start_error}</p>` : null}
    </div>

    <h2 class="m-h2">Telemetry</h2>
    <div class="m-cells">
      <${Cell} label="Battery" big=${r.pct ?? '—'} unit=${r.pct != null ? '%' : ''} kind=${pctKind} stale=${old}
        small=${r.volts ? `${r.volts} V` : 'Voltage N/A'} />
      <${Cell} label="Servos" big=${r.online ?? '—'} unit=${`/ ${r.total}`} stale=${old}
        small=${r.hottest ? `Peak ${r.hottest} °C` : 'Temp N/A'} />
      <${Cell} label="IMU" big=${r.imu} stale=${old}
        small=${r.tilt != null ? `Tilt ${r.tilt}°` : 'No IMU data'} kind=${r.imuKind} />
      <${Cell} label="Mode" big=${st?.mode || '—'}
        small=${st?.phase ? `Phase ${st.phase}` : 'No phase reported'} stale=${old} />
    </div>

    ${st?.todo?.length ? html`<div class="m-card m-pad" style="margin-top:12px">
      <div class="m-sub" style="margin-bottom:6px">Active Notices</div>
      <ul class="m-log">${st.todo.slice(0, 4).map((t) => html`<li><span class="what">${typeof t === 'string' ? t : t.text || t.why || ''}</span></li>`)}</ul>
    </div>` : null}

    <p class="m-tip">Open <b>Pad</b> to stand and walk, or use <b>Voice</b> to send spoken commands.
      Tap <b>E-Stop</b> at the top-right at any time to relax all servos.</p>
  </div>`;
}
