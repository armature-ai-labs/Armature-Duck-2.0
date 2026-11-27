// Shared action dictionary, policy sequencer, and utterance parser for Mobile Overview, Voice, and Pad.
// Commands route through Studio pad.op (stand / sit / walk / walk_stop / skill / sound / action_start·hold·stop).

export const ACTIONS = {
  stand: { label: 'Stand', op: { op: 'stand' }, cn: 'Stand' },
  walk: { label: 'Start Walk', op: { op: 'walk' }, cn: 'Start Walk' },
  step: { label: 'Step Forward', special: 'step', cn: 'Step Forward' },
  stop: { label: 'Stop', op: { op: 'walk_stop' }, cn: 'Stop & Stand' },
  sit: { label: 'Sit', op: { op: 'sit' }, cn: 'Sit' },
  roulade: { label: 'Roll', op: { op: 'skill', name: 'roulade' }, cn: 'Roll' },
  ground_pick: { label: 'Ground Pick', op: { op: 'skill', name: 'ground_pick' }, cn: 'Ground Pick' },
  chirp: { label: 'Chirp', op: { op: 'sound', tag: 'chirp' }, cn: 'Chirp' },
  greet: { label: 'Greet', op: { op: 'sound', tag: 'greet' }, cn: 'Greet' },
};

const HELD = {
  nod: { id: 'nod', label: 'Nod', cn: 'Nod', ms: 1100 },
  shake: { id: 'shake', label: 'Shake Head', cn: 'Shake Head', ms: 1100 },
  tilt: { id: 'tilt', label: 'Tilt Head', cn: 'Tilt Head', ms: 900 },
  mouth: { id: 'mouth', label: 'Open Beak', cn: 'Open Beak', ms: 900 },
};

const HELD_NEEDS_POLICY = new Set(['nod', 'shake', 'tilt']);

export const ACTION_GROUPS = [
  { title: 'Posture & Walk', keys: ['stand', 'step', 'stop'] },
  { title: 'Sit & Skills', keys: ['sit', 'roulade', 'ground_pick'] },
  { title: 'Sounds', keys: ['chirp', 'greet'] },
  { title: 'Head Motion', keys: ['nod', 'shake', 'tilt'] },
];

export function actionLabel(key) {
  const hit = ACTIONS[key] || HELD[key];
  return (hit && hit.label) || key;
}

export function nextAction(st) {
  if (!st) return null;
  if (st.estop && st.estop.state === 'confirmed') return 'stand';
  switch (st.posture) {
    case 'holding': return 'walk';
    case 'sitting': return 'stand';
    case 'limp': return 'stand';
    case 'walking': return 'stop';
    default: return null;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pushStick(link, { x = 0, y = 0 } = {}, timeout) {
  return link.cmd('pad.op', {
    op: 'sticks', mode: 'drive', lx: x, ly: y, rx: 0, ry: 0, seq: link.nextPadSeq(),
  }, timeout);
}

const whyOf = (r) => String((r && (r.reason || r.error || r.data?.dropped)) || '');
const good = (r) => Boolean(r) && r.ok !== false;
const isReady = (r) => !r?.data?.dropped && (good(r) || /ready to walk|already walking/i.test(whyOf(r)));
const BUSY_WHY = /not ready|wait|settling|standing up|homing|home|stale|upright|support|busy|not respond/i;
const DRIVING = new Set(['policy', 'walking']);

async function waitDriving(ctx, ms, check) {
  const get = ctx && ctx.getPosture;
  if (typeof get !== 'function') { await sleep(Math.min(ms, 1800)); return null; }
  const t0 = performance.now();
  while (performance.now() - t0 < ms) {
    check?.();
    if (DRIVING.has(get())) return true;
    await sleep(150);
  }
  return false;
}

async function ensurePolicy(link, ctx, tries = 4, check) {
  for (let i = 0; i < tries; i++) {
    check?.();
    const r = await link.cmd('pad.op', { op: 'walk' }, 20000);
    check?.();
    if (isReady(r)) return { ok: true, already: !good(r) };
    const why = whyOf(r);
    if (BUSY_WHY.test(why)) { await sleep(900); continue; }
    if (/sit/i.test(why)) {
      const s = await link.cmd('pad.op', { op: 'stand' }, 25000);
      check?.();
      if (!good(s) && !/already standing/i.test(whyOf(s))) return { ok: false, error: whyOf(s) || 'Failed to stand' };
      await waitDriving(ctx, 15000, check);
      const p = await ensurePolicy(link, ctx, tries - i - 1, check);
      return p.ok ? { ...p, stood: true } : p;
    }
    return { ok: false, error: why || 'Failed to start walk policy' };
  }
  return { ok: false, error: 'Start Walk timed out — please retry' };
}

const activeSteps = new WeakMap();

function forgetStep(link, step) {
  if (activeSteps.get(link) === step) activeSteps.delete(link);
}

function startStep(link, ctx) {
  const previous = activeSteps.get(link);
  if (previous) previous.cancelled = 'replaced';
  const step = { cancelled: null };
  activeSteps.set(link, step);
  const run = async () => {
    if (previous) await previous.done;
    return stepOnce(link, ctx, step);
  };
  step.done = run().finally(() => forgetStep(link, step));
  return step.done;
}

function stopAction(link, op) {
  const previous = activeSteps.get(link);
  if (previous) previous.cancelled = 'stop';
  const result = Promise.resolve().then(() => link.cmd('pad.op', op, 20000));
  const stop = { done: Promise.allSettled([previous?.done, result]) };
  activeSteps.set(link, stop);
  stop.done.then(() => forgetStep(link, stop));
  return result;
}

async function stepOnce(link, ctx, step, power = 0.55) {
  const poseError = 'Walking policy not active yet — wait until settled.';
  const cancelled = 'Step cancelled.';
  const check = () => {
    if (step.cancelled) throw new Error(cancelled);
    if (ctx?.getPosture?.() == null) throw new Error(poseError);
  };
  let error = null;
  let attempted = false;
  let moved = false;
  let stood = false;
  try {
    check();
    const policy = await ensurePolicy(link, ctx, 4, check);
    check();
    if (!policy.ok) throw new Error(policy.error);
    stood = policy.stood === true;
    if (await waitDriving(ctx, 5000, check) !== true) throw new Error(poseError);
    check();
    await sleep(stood ? 150 : 220);
    check();
    const deadline = performance.now() + 850;
    while (performance.now() < deadline) {
      check();
      if (!DRIVING.has(ctx.getPosture())) throw new Error(poseError);
      const remaining = deadline - performance.now();
      if (remaining <= 0) break;
      attempted = true;
      const frame = await pushStick(link, { y: -power }, Math.min(500, Math.ceil(remaining)));
      check();
      if (frame?.ok !== true || frame.data?.dropped || !(frame.data?.sent?.[0] > 0)) {
        throw new Error(whyOf(frame) || 'Forward stick frame failed.');
      }
      moved = true;
      const pause = Math.min(95, deadline - performance.now());
      if (pause > 0) await sleep(pause);
    }
    check();
    if (!moved) throw new Error('Forward stick frame failed.');
  } catch (err) {
    error = err?.message || 'Forward stick frame failed.';
  } finally {
    if (attempted && step.cancelled !== 'stop') {
      try {
        const stop = await link.cmd('pad.op', { op: 'stop', seq: link.nextPadSeq(), owned_only: true }, 5000);
        if (stop?.ok !== true || stop.data?.dropped || stop.data?.sent !== true) {
          error ||= whyOf(stop) || 'Stop confirmation missing.';
        }
      } catch (err) {
        error ||= err?.message || 'Stop confirmation missing.';
      }
    }
  }
  if (step.cancelled) error ||= cancelled;
  return { ok: !error, cn: 'Step Forward', error, note: !error && stood ? 'Stood up and stepped forward' : null };
}

async function sitSmart(link, ctx) {
  for (let i = 0; i < 4; i++) {
    const r = await link.cmd('pad.op', { op: 'sit' }, 25000);
    const why = whyOf(r);
    if (good(r) || /already sitting/i.test(why)) {
      return { ok: true, cn: 'Sit', error: null, note: /already sitting/i.test(why) ? 'Already sitting' : null };
    }
    if (BUSY_WHY.test(why)) { await sleep(900); continue; }
    if (!/policy/i.test(why)) return { ok: false, cn: 'Sit', error: why };
    const p = await ensurePolicy(link, ctx);
    if (!p.ok) return { ok: false, cn: 'Sit', error: p.error };
    await waitDriving(ctx, 6000);
  }
  return { ok: false, cn: 'Sit', error: 'Sit timed out — please retry' };
}

let press = 0;

export async function runAction(link, key, ctx) {
  const hit = ACTIONS[key];
  if (hit) {
    if (hit.special === 'step') return startStep(link, ctx);
    if (key === 'sit') return sitSmart(link, ctx);
    if (key === 'walk') {
      const p = await ensurePolicy(link, ctx);
      if (!p.ok) return { ok: false, cn: 'Start Walk', error: p.error };
      return { ok: true, cn: 'Start Walk', error: null,
        note: p.already ? 'Already ready to walk — push left stick' : (p.stood ? 'Stood up and ready to walk' : 'Ready to walk — push left stick') };
    }
    const r = key === 'stop' ? await stopAction(link, hit.op) : await link.cmd('pad.op', hit.op, 20000);
    const ok = good(r) && !r.data?.dropped;
    return { ok, cn: hit.cn, error: ok ? null : whyOf(r) };
  }
  const held = HELD[key];
  if (!held) return { ok: false, cn: key, error: 'Unknown action' };
  if (HELD_NEEDS_POLICY.has(key)) {
    const p = await ensurePolicy(link, ctx);
    if (!p.ok) return { ok: false, cn: held.cn, error: p.error };
  }
  const tag = ++press;
  const tries = HELD_NEEDS_POLICY.has(key) ? 3 : 1;
  for (let i = 0; i < tries; i++) {
    const start = await link.cmd('pad.op', { op: 'action_start', id: held.id, press: tag }, 8000);
    if (start.ok !== false) break;
    const why = whyOf(start);
    if (i < tries - 1 && (BUSY_WHY.test(why) || /ready to walk|start walk|policy/i.test(why))) {
      await waitDriving(ctx, 2000);
      continue;
    }
    return { ok: false, cn: held.cn, error: why };
  }
  const t0 = performance.now();
  const hb = setInterval(() => { link.cmd('pad.op', { op: 'action_hold', press: tag }, 8000); }, 150);
  await new Promise((r) => setTimeout(r, held.ms));
  clearInterval(hb);
  await link.cmd('pad.op', { op: 'action_stop' }, 8000);
  return { ok: true, cn: held.cn, error: null, ms: performance.now() - t0 };
}

// ---------------- Audio Toggle ----------------

export const PAD_AREA_WHY = /pad area|remote|disconnected/i;

export function openPadArea(link) {
  return link.cmd('pad.op', { op: 'drive', open: true }, 8000);
}

export async function setAudio(link, values, tries = 2) {
  for (let i = 0; i < tries; i++) {
    const r = await link.cmd('pad.op', { op: 'audio', ...values }, 12000);
    if (r.ok !== false) return r;
    if (!PAD_AREA_WHY.test(String(r.reason || r.error || '')) || i === tries - 1) return r;
    await openPadArea(link);
  }
  return { ok: false, reason: 'Failed to toggle robot audio listening' };
}

// ---------------- Utterance → Action ----------------

const ADDRESS = /^(?:hey\s+)?(?:microduck|ducky|duck)[,\s.!?:]*/i;

const RULES = [
  ['stop', /^(?:stop(?:\s+walking)?|halt|freeze|hold)$/i],
  ['stand', /^(?:stand(?:\s+up)?|get\s+up|rise)$/i],
  ['sit', /^(?:sit(?:\s+down)?)$/i],
  ['roulade', /^(?:roll(?:\s+over)?|roulade|do\s+a\s+roll)$/i],
  ['ground_pick', /^(?:(?:ground\s+)?pick(?:\s+up)?|look\s+down)$/i],
  ['step', /^(?:step(?:\s+forward)?|take\s+a\s+step|walk\s+a\s+step)$/i],
  ['walk', /^(?:walk|start\s+walking|go|forward)$/i],
  ['nod', /^(?:nod(?:\s+head)?)$/i],
  ['shake', /^(?:shake(?:\s+head)?)$/i],
  ['tilt', /^(?:tilt(?:\s+head)?)$/i],
  ['mouth', /^(?:open\s+(?:beak|mouth)|beak)$/i],
  ['chirp', /^(?:chirp|quack|speak)$/i],
  ['greet', /^(?:greet|hello|hi|say\s+hi)$/i],
];

export const CMD_CN = {
  stand: 'Stand', sit: 'Sit', walk: 'Start Walk', step: 'Step Forward', stop: 'Stop & Stand', roulade: 'Roll',
  ground_pick: 'Ground Pick', nod: 'Nod', shake: 'Shake Head', tilt: 'Tilt Head', mouth: 'Open Beak',
  chirp: 'Chirp', greet: 'Greet',
};

function tidy(text) {
  return String(text || '').trim().replace(/\s+/g, ' ').replace(/[.,!?]+$/g, '');
}

export function parseUtterance(text, { requireWake = false } = {}) {
  const raw = tidy(text);
  const m = raw.match(ADDRESS);
  const wake = Boolean(m);
  const body = wake ? raw.slice(m[0].length).trim() : raw;
  const said = body || raw;
  if (!body) return { cmd: null, wake, said };
  if (requireWake && !wake) return { cmd: null, wake, said };
  const phrase = body.replace(/^please\s+/i, '').trim();
  for (const [cmd, re] of RULES) {
    if (re.test(phrase)) return { cmd, wake, said };
  }
  return { cmd: null, wake, said };
}

// ---------------- Posture Copy ----------------

export function postureCopy(st, name) {
  const p = st?.posture;
  const sleepy = st?.estop?.state === 'confirmed';
  if (!st) return { h: name, sub: 'Waiting for robot telemetry.' };
  if (sleepy) return { h: `${name} is relaxed`, sub: 'Servos have no torque. Tap Stand in Pad to re-enable.' };
  switch (p) {
    case 'walking': return { h: 'Walking', sub: 'Release the stick to stop and hold standing.' };
    case 'policy': return { h: `${name} ready to walk`, sub: 'Open Pad to drive or tap Step Forward.' };
    case 'sitting': return { h: `${name} is sitting`, sub: 'Tap Stand or say "stand up".' };
    case 'starting': return { h: `${name} is standing up`, sub: 'Hands off until settled.' };
    case 'limp': return { h: `${name} is relaxed`, sub: 'Servos are off; tap Stand to enable.' };
    case 'homing': return { h: `${name} is homing`, sub: 'Waiting for joints to settle.' };
    case 'servo_session': return { h: 'Servo Tuning Active', sub: 'robotd is paused; walking is disabled.' };
    case 'commissioning': return { h: `${name} uncalibrated`, sub: 'Complete onboarding in desktop Studio.' };
    case 'absent': return { h: `${name} offline`, sub: 'robotd is not connected.' };
    default: return { h: name, sub: 'Unable to read current posture.' };
  }
}

export function readings(st) {
  const b = st?.battery || {};
  const s = st?.servos || {};
  const imu = st?.imu || {};
  return {
    pct: typeof b.pct === 'number' ? Math.round(b.pct) : null,
    volts: typeof b.volts === 'number' ? b.volts.toFixed(2) : null,
    online: typeof s.online === 'number' ? s.online : null,
    total: typeof s.total === 'number' ? s.total : 15,
    hottest: s.hottest && typeof s.hottest.c === 'number' ? Math.round(s.hottest.c) : null,
    imu: { ok: 'OK', unverified: 'Unverified', frozen: 'Frozen', missing: 'Missing' }[imu.status] || '—',
    imuKind: { unverified: 'warn', frozen: 'bad', missing: 'bad' }[imu.status] || null,
    tilt: typeof st?.tilt_deg === 'number' ? Math.round(st.tilt_deg) : null,
  };
}
