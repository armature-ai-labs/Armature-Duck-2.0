// Inline overview diagnostics. Visibility requests cached snapshots, never UART ownership.
import {html, useState, useEffect, useRef} from './vendor/preact-htm.module.js';
import {ServoMonitor, ImuHealth} from './communication-health.js';

export function RuntimeDiagnostics({st, sv, imu, link, active = false, live = false,
  summaryLive = live && st?.robotd_ok !== false, svElapsed = 0, imuElapsed = 0}) {
  const [open, setOpen] = useState(false), [tab, setTab] = useState('servos');
  const [selected, setSelected] = useState(null);
  const [inView, setInView] = useState(false);
  const [visible, setVisible] = useState(document.visibilityState === 'visible');
  const box = useRef(null);
  useEffect(() => {
    const visibility = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', visibility);
    let observer;
    const measure = () => { const r=box.current?.getBoundingClientRect();setInView(!!r && r.bottom>0 && r.top<innerHeight); };
    if (typeof IntersectionObserver === 'function') {
      observer = new IntersectionObserver(entries => setInView(entries.some(e=>e.isIntersecting)));
      observer.observe(box.current);
    } else {
      measure();window.addEventListener('scroll', measure, {passive:true});window.addEventListener('resize', measure);
    }
    return () => {observer?.disconnect();document.removeEventListener('visibilitychange',visibility);
      window.removeEventListener('scroll',measure);window.removeEventListener('resize',measure);};
  }, []);
  useEffect(() => {
    link.setDiagnosticsVisible?.(!!(open && active && inView && visible));
    return () => link.setDiagnosticsVisible?.(false);
  }, [open, active, inView, visible, link]);
  const imuWord=({ok:'OK',unverified:'Unverified',frozen:'Frozen',missing:'Missing'})[st?.imu?.status] || 'Unverified';
  return html`<section class="board runtime-diagnostics" id="runtime-diagnostics" aria-label="Diagnostics" ref=${box}>
    <button type="button" class="runtime-diag-toggle" data-diagnostics-toggle aria-expanded=${open} onClick=${()=>setOpen(!open)}>
      <span class="runtime-diag-title"><b>Diagnostics</b><small>Servos & IMU · Read-only</small></span>
      <span class=${'runtime-diag-summary'+(summaryLive?'':' stale')}>
        <span>Servos <b>${summaryLive?st?.servos?.online ?? '—':'—'} / ${st?.servos?.total ?? 15}</b></span>
        <span>IMU <b>${summaryLive?imuWord:live?'Stale':'Connecting'}</b></span>
      </span><span class="runtime-diag-expand">${open?'Collapse':'Expand'} ${open?'▴':'▾'}</span>
    </button>
    ${open ? html`<div class="runtime-diag-body">
      <div class="runtime-diag-toolbar"><div class="comm-view-tabs" role="tablist" aria-label="Diagnostic Tabs">
        <button role="tab" aria-selected=${tab==='servos'} onClick=${()=>setTab('servos')}>Servo Diagnostics</button>
        <button role="tab" aria-selected=${tab==='imu'} onClick=${()=>setTab('imu')}>IMU Diagnostics</button>
      </div><a href=${tab==='servos'?'#/servos':'#/imu'}>${tab==='servos'?'Open Servo Calibration':'Open IMU Calibration'} →</a></div>
      ${tab==='servos' ? html`<div class="runtime-diag-servos">
        <${ServoMonitor} sv=${sv} selected=${selected} onSelect=${setSelected} live=${live} elapsed=${svElapsed} />
      </div>` : html`<${ImuHealth} imu=${imu} live=${live} elapsed=${imuElapsed} />`}
    </div>`:null}
  </section>`;
}
