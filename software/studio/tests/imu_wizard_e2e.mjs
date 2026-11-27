// IMU :  robot.state  web/imu.js  (, ), 
// , , ,  JSON  stdout,  tests/test_imu_wizard_e2e.py  Python  imu_calib.solve .
//
// : node tests/imu_wizard_e2e.mjs    (Node ≥ 20.6)
// 3D (duck3d.js → three.js), .
import { register } from 'node:module';

register('data:text/javascript,' + encodeURIComponent(
  "export async function resolve(s, c, next) {"
  + " if (s.endsWith('duck3d.js')) return { url: 'data:text/javascript,export class Duck3D {}', shortCircuit: true };"
  + " return next(s, c); }"));
const { _test: T } = await import(new URL('../web/imu.js', import.meta.url).href);

let NOW = 0;
performance.now = () => NOW;
let seed = 7;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; };

// ---- :  x  y  z  =  ( 3°  + 4° ) ----
const rad = (d) => d * Math.PI / 180;
const rot = (axis, d) => {                                   // item d item (Rodrigues)
  const [x, y, z] = axis, c = Math.cos(rad(d)), s = Math.sin(rad(d)), C = 1 - c;
  return [[c + x * x * C, x * y * C - z * s, x * z * C + y * s], [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
    [z * x * C - y * s, z * y * C + x * s, c + z * z * C]];
};
const mv = (m, v) => m.map((r) => r[0] * v[0] + r[1] * v[1] + r[2] * v[2]);
const mm = (a, b) => a.map((r) => [0, 1, 2].map((j) => r[0] * b[0][j] + r[1] * b[1][j] + r[2] * b[2][j]));
const tr = (m) => [0, 1, 2].map((i) => [0, 1, 2].map((j) => m[j][i]));
const I = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
const ERR = mm(rot([0, 0, 1], 4), rot([0, 1, 0], -3));        // item: item = ERR · item
const AX = { fwd: [0, 1, 0], back: [0, -1, 0], left: [-1, 0, 0], right: [1, 0, 0] };   // item = item +y item (item)
const reading = (R_wb, noise = 0.1) => mv(ERR, mv(tr(R_wb), [0, 0, -1])).map((x) => x + rnd() * rad(noise) * 2);
const tilted = (k, d, hand = I, twist = 0) => mm(rot(mv(rot([0, 0, 1], twist), AX[k]), d), hand);
const hand = (d, dir) => rot([Math.cos(rad(dir)), Math.sin(rad(dir)), 0], d);

/** item'item × item'→ item */
function script(parts) {
  const out = [];
  for (const [R, n, extra] of parts) for (let i = 0; i < n; i++) out.push({ g: reading(R), ...(extra || {}) });
  return out;
}

/** item: item 0.8 s (item)→ 0.8 s item → (item)→ item 1.5 s (item)→ 0.8 s item */
function tiltParts(k, { tilt = 16, handDeg = 3, handDir = 40, overshoot = 0, spike = false, twist = 0 } = {}) {
  const H = hand(handDeg, handDir), parts = [[H, 8]];
  for (let j = 1; j <= 8; j++) parts.push([tilted(k, tilt * j / 8, H, twist), 1]);
  if (overshoot) parts.push([tilted(k, tilt + overshoot, H, twist), 2], [tilted(k, tilt + overshoot / 2, H, twist), 1]);
  const hold = Array.from({ length: 15 }, () => [tilted(k, tilt, H, twist), 1]);
  if (spike) hold[6] = [tilted(k, tilt + 15, H, twist), 1];
  parts.push(...hold);
  for (let j = 7; j >= 0; j--) parts.push([tilted(k, tilt * j / 8, H, twist), 1]);
  return parts;
}
const stillParts = (spikes = false) => (spikes ? [[I, 8], [tilted('left', 12), 1], [I, 12], [tilted('fwd', 12), 1], [I, 13]] : [[I, 35]]);
const session = (order = ['fwd', 'back', 'left', 'right'], opts = {}, stillSpike = false) =>
  [...stillParts(stillSpike), ...order.flatMap((k, i) => tiltParts(opts.as?.[k] || k, { handDir: 40 + 90 * i, ...opts }))];

/** item, item.opts.dup: item (item state_t_ns)itemopts.noKey: item state_t_ns, item'item'item. */
function drive(frames, opts = {}, W = null) {
  if (!W) { W = T.freshWizard(); NOW = 0; T.startCollect(W, 0); }
  feedAll(W, frames, opts);
  return W;
}
let TNS = 1e12;
function feedAll(W, frames, opts = {}) {
  for (const f of frames) {
    if (W.phase !== 'collect') break;
    NOW += 100; TNS += 1e8;
    const st = { gravity: f.g, joints_age_ms: 20 };
    if (!opts.noKey) st.state_t_ns = TNS;
    T.feed(W, st, f.live);
    W.pushed = (W.pushed || 0) + 1;
    if (opts.dup) {
      NOW += 30;                                                    // 30 ms item Studio item
      T.feed(W, opts.noKey ? { gravity: f.g, joints_age_ms: 50 } : { ...st }, f.live);
      NOW -= 30;
    }
  }
  return W;
}
const report = (W) => ({ phase: W.phase, done: { ...W.done }, missing: W.missing.slice(), pushed: W.pushed || 0, paused: W.paused,
  counts: Object.fromEntries(T.STEPS.map((k) => [k, W.buf[k].length])), seg: W.seg.length, events: W.events.slice(),
  samples: T.payload(W) });

const out = { err: ERR };
out.normal = report(drive(script(session()), { dup: true }));
out.normal_nokey = report(drive(script(session()), { dup: true, noKey: true }));
out.any_order = report(drive(script(session(['right', 'back', 'left', 'fwd']))));
out.overshoot = report(drive(script(session(undefined, { overshoot: 5 }))));
out.spike = report(drive(script(session(undefined, { spike: true }))));
out.still_spike = report(drive(script(session(undefined, {}, true))));
//  4.5° ( 5° , '')
out.hand_offset = report(drive(script(session(undefined, { handDeg: 4.5, tilt: 13 }))));
out.too_small = report(drive(script(session(undefined, { tilt: 9 }))));
out.offline = report(drive(script([[I, 35, { live: false }]])));

// : ,  →  → ,  → 
{
  const W = drive(script(session(undefined, { as: { back: 'fwd' } })));
  const before = report(W);
  T.endCollect(W);
  const short = { phase: W.phase, missing: W.missing.slice() };
  T.resumeCollect(W);
  feedAll(W, script(tiltParts('back')));
  out.back_wrong = { before, short, after: report(W) };
}
// : , , 
{
  const W = drive(script([...stillParts(), ...tiltParts('fwd')]));
  T.endCollect(W);
  out.short = report(W);
}
//  ( redo: ["left"]): , 
{
  const W = drive(script(session()));
  const fwdBefore = JSON.stringify(W.buf.fwd);
  T.redoSteps(W, ['left']);
  const mid = { phase: W.phase, done: { ...W.done }, left: W.buf.left.length };
  feedAll(W, script(tiltParts('left', { tilt: 18 })));
  out.redo = { mid, after: report(W), fwd_kept: JSON.stringify(W.buf.fwd) === fwdBefore };
}
process.stdout.write(JSON.stringify(out));
