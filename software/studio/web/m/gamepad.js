// Mobile Gamepad page: dual virtual sticks + action grid.
import { html, useState, useEffect, useRef, useCallback } from '../vendor/preact-htm.module.js';
import { link } from './link.js';
import { ACTION_GROUPS, actionLabel, runAction, nextAction } from './actions.js';

const SEND_MS = 100;
const MODE_HB_MS = 1000;
const AUTO_WALK_MS = 4000;

const MODES = [
  { id: 'drive', label: 'Walk', hint: 'Left stick: forward/strafe · Right stick: turn' },
  { id: 'head', label: 'Head', hint: 'Left stick: pitch/yaw · Right stick: roll' },
];

const TIPS = {
  walking: ['ok', 'Walking: release stick to stop and hold standing'],
  policy: ['ok', 'Ready to walk: push the left stick'],
  holding: ['warn', 'Standing (policy idle): tap "Start Walk" below or push the left stick to auto-enable'],
  sitting: ['warn', 'Sitting: tap Stand first, then Start Walk'],
  limp: ['bad', 'Relaxed: tap Stand to enable torque'],
  starting: ['warn', 'Standing up — wait until settled'],
  homing: ['warn', 'Homing — wait until settled'],
  servo_session: ['bad', 'Servo Tuning active: walking disabled'],
  commissioning: ['bad', 'Uncalibrated: complete setup in desktop Studio first'],
  absent: ['bad', 'robotd is not connected'],
};

function driveTip(st) {
  if (!st) return null;
  if (st.estop && st.estop.state === 'confirmed') return ['bad', 'E-Stop active: tap Stand to re-enable torque'];
  return TIPS[st.posture] || null;
}

function blockedWhy(st) {
  if (!st) return null;
  if (st.estop && st.estop.state === 'confirmed') return 'E-Stop active: tap Stand first';
  switch (st.posture) {
    case 'sitting': return 'Sitting: tap Stand, then Start Walk';
    case 'limp': return 'Relaxed: tap Stand first';
    case 'servo_session': return 'Servo Tuning active: sticks disabled';
    case 'commissioning': return 'Uncalibrated: complete setup in desktop Studio first';
    case 'absent': return 'robotd is not connected';
    default: return null;
  }
}

function Stick({ cap, onChange, onEnd }) {
  const boxRef = useRef(null);
  const knobRef = useRef(null);
  const [active, setActive] = useState(false);
  const idRef = useRef(null);

  const move = (e) => {
    const box = boxRef.current;
    if (!box) return;
    const r = box.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const max = Math.min(r.width, r.height) / 2 * 0.68;
    let dx = e.clientX - cx;
    let dy = e.clientY - cy;
    const len = Math.hypot(dx, dy);
    if (len > max) { dx = dx / len * max; dy = dy / len * max; }
    if (knobRef.current) knobRef.current.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
    onChange(dx / max, dy / max);
  };

  const end = () => {
    idRef.current = null;
    setActive(false);
    if (knobRef.current) knobRef.current.style.transform = 'translate(-50%, -50%)';
    onEnd();
  };

  const down = (e) => {
    e.preventDefault();
    idRef.current = e.pointerId;
    setActive(true);
    try { boxRef.current.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    move(e);
  };

  const up = (e) => {
    if (idRef.current != null && e.pointerId !== idRef.current) return;
    end();
  };

  return html`<div class="m-stick-box">
    <div class=${'m-stick' + (active ? ' active' : '')} ref=${boxRef}
      onPointerDown=${down} onPointerMove=${(e) => { if (idRef.current != null) move(e); }}
      onPointerUp=${up} onPointerCancel=${up} onLostPointerCapture=${up}
      role="application" aria-label=${cap}>
      <div class="m-knob" ref=${knobRef}><i></i></div>
    </div>
    <div class="cap">${cap}</div>
  </div>`;
}

export function GamepadPage({ st, live, say }) {
  const [mode, setMode] = useState('drive');
  const [busy, setBusy] = useState(null);
  const left = useRef({ x: 0, y: 0, on: false });
  const right = useRef({ x: 0, y: 0, on: false });
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const stRef = useRef(st);
  stRef.current = st;
  const poseRef = useRef(null);
  poseRef.current = live ? st?.posture : null;
  const sayRef = useRef(say);
  sayRef.current = say;
  const autoAt = useRef(0);
  const nagged = useRef(false);
  const ctxRef = useRef({ getPosture: () => poseRef.current });

  useEffect(() => {
    link.cmd('pad.op', { op: 'drive', open: true }, 8000);
    autoAt.current = 0;
    return () => {
      poseRef.current = null;
      link.cmd('pad.op', { op: 'drive', on: false }, 8000);
    };
  }, []);

  useEffect(() => {
    let wasOn = false;
    const t = setInterval(() => {
      const on = left.current.on || right.current.on;
      if (on) {
        const why = blockedWhy(stRef.current);
        if (why) {
          if (!nagged.current) { nagged.current = true; sayRef.current(why, true); }
          return;
        }
        nagged.current = false;
        if (poseRef.current === 'holding' && performance.now() - autoAt.current > AUTO_WALK_MS) {
          autoAt.current = performance.now();
          link.cmd('pad.op', { op: 'walk' }, 20000);
        }
        wasOn = true;
        link.cmd('pad.op', {
          op: 'sticks', mode: modeRef.current,
          lx: left.current.x, ly: left.current.y,
          rx: right.current.x, ry: right.current.y,
          seq: link.nextPadSeq(),
        }, 5000);
      } else if (wasOn) {
        wasOn = false;
        link.cmd('pad.op', { op: 'stop', seq: link.nextPadSeq() }, 5000);
      }
    }, SEND_MS);
    return () => { clearInterval(t); link.cmd('pad.op', { op: 'stop', seq: link.nextPadSeq() }, 5000); };
  }, []);

  useEffect(() => {
    link.cmd('pad.op', { op: 'mode', mode, seq: link.nextPadSeq() }, 6000);
    if (mode === 'drive') return undefined;
    const t = setInterval(() => { link.cmd('pad.op', { op: 'mode', mode, hb: true, seq: link.nextPadSeq() }, 6000); }, MODE_HB_MS);
    return () => clearInterval(t);
  }, [mode]);

  const fire = useCallback(async (key) => {
    if (busy) return;
    setBusy(key);
    const out = await runAction(link, key, ctxRef.current);
    setBusy(null);
    autoAt.current = 0;
    if (out.ok) say(out.note || `Sent: ${out.cn}`); else say(out.error || `${out.cn} failed`, true);
  }, [busy, say]);

  const m = MODES.find((x) => x.id === mode) || MODES[0];
  const tip = driveTip(st);
  const hot = nextAction(st);
  const key = (k) => html`<button
    class=${'m-key' + (k === hot ? ' hot' : '') + (busy === k ? ' on' : '')}
    disabled=${Boolean(busy)} onClick=${() => fire(k)}>
    ${actionLabel(k)}
  </button>`;

  return html`<div>
    ${!live ? html`<div class="m-banner">Status stale — sticks may not respond.</div>` : null}
    ${tip ? html`<div class=${'m-banner ' + tip[0]}>${tip[1]}</div>` : null}

    <h2 class="m-h2">Stick Mode</h2>
    <div class="m-seg" role="group" aria-label="Stick mode">
      ${MODES.map((x) => html`<button aria-pressed=${mode === x.id ? 'true' : 'false'} onClick=${() => setMode(x.id)}>${x.label}</button>`)}
    </div>
    <p class="m-tip">${m.hint}</p>

    <div class="m-sticks">
      <${Stick} cap=${mode === 'drive' ? 'Left · Move' : 'Left · Pitch/Yaw'}
        onChange=${(x, y) => { left.current = { x, y, on: true }; }}
        onEnd=${() => { left.current = { x: 0, y: 0, on: false }; }} />
      <${Stick} cap=${mode === 'drive' ? 'Right · Turn' : 'Right · Roll'}
        onChange=${(x, y) => { right.current = { x, y, on: true }; }}
        onEnd=${() => { right.current = { x: 0, y: 0, on: false }; }} />
    </div>

    <div class="m-start-row">
      <button
        class=${'m-key start' + (hot === 'walk' ? ' hot' : '') + (busy === 'walk' ? ' on' : '')}
        disabled=${Boolean(busy)} onClick=${() => fire('walk')}>
        Start Walk<small>enable policy</small>
      </button>
      <button
        class=${'m-key start stop' + (busy === 'stop' ? ' on' : '')}
        disabled=${Boolean(busy)} onClick=${() => fire('stop')}>
        Stop Policy<small>hold standing</small>
      </button>
    </div>

    ${ACTION_GROUPS.map((g) => html`<div>
      <h2 class="m-h2">${g.title}</h2>
      <div class="m-keys">${g.keys.map(key)}</div>
    </div>`)}

    <p class="m-tip">
      Highlighted button indicates the recommended next step. Releasing sticks or losing connection stops motion automatically within 0.5s.
    </p>
  </div>`;
}
