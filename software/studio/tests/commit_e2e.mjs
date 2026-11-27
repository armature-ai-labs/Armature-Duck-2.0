// '' ( 13):  Edge (CDP),  -> .
//  robotd  --mode commissioning --calib factory  ()Studio  tests/test_commit_e2e.py 
//  studio.helper.calib_commit  (PC root ).
//
// --phase factory:  (), ,  /  / TOML .
// --phase mark   :  1 '' (: )→ ✓,  3 '' ( 16).
// --phase imu    : IMU :  ->  ->  ->  verify-imu.
// --phase servo  :  (--servo-sim), .
// --phase commit :  3  motion + control,  3  ✓, .
// --phase fail   : , ''.
// --phase stance0:  IMU ' / ', ,  3  ( 17).
// --phase stance : ''Stand Up, '''E-Stop' ( 17).
//  JSON  stdout,  tests/test_commit_e2e.py .Node >= 22, .
import { mkdirSync, writeFileSync } from 'node:fs';
import { createConnection } from 'node:net';

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const BASE = arg(url);
const CDP = arg('cdp');
const FAKE = Number(arg('fake-port'));
const PHASE = arg(phase, 'factory');
const SHOTS = arg(shots, '');
const CONTROL = arg('control-file', '');
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => process.stderr.write(`[commit-e2e] ${a.join( )}` + '\n');

// :  robotd /  configd  fake.set ()
const fakeRpc = (method, params) => new Promise((resolve, reject) => {
  const s = createConnection({ host: '127.0.0.1', port: FAKE }, () => {
    s.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) + '\n');
  });
  let buf = '', done = false;
  const timer = setTimeout(() => { if (!done) { done = true; s.destroy(); reject(new Error('fake.set item')); } }, 5000);
  s.on(data, (d) => {
    buf += d;
    const i = buf.indexOf('\n');
    if (i < 0 || done) return;
    done = true; clearTimeout(timer);
    const line = buf.slice(0, i); s.end();
    try {
      const m = JSON.parse(line);
      if (m.error) reject(new Error(JSON.stringify(m.error))); else resolve(m.result);
    } catch (e) { reject(e); }
  });
  s.on(error, (e) => { if (!done) { done = true; clearTimeout(timer); reject(e); } });
});
const fakeSet = (params) => fakeRpc('fake.set', params);

const page = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: PUT })).json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0;
const waiting = new Map();
const errors = [];
ws.addEventListener(message, (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id).ok(m); waiting.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === error) errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
  //  ('……'):  overview_e2e '', 
  if (m.method === 'Page.javascriptDialogOpening') { dialogs.push(m.params.message); send('Page.handleJavaScriptDialog', { accept: true }); }
});
const dialogs = [];
const send = (method, params = {}) => new Promise((ok, fail) => {
  const i = ++id;
  const timer = setTimeout(() => {
    waiting.delete(i);
    fail(new Error(`CDP ${method} 15 item`));
  }, 15000);
  waiting.set(i, { ok: (m) => { clearTimeout(timer); ok(m); }, fail: (e) => { clearTimeout(timer); fail(e); } });
  ws.send(JSON.stringify({ id: i, method, params }));
});
ws.addEventListener(close, () => {
  for (const w of waiting.values()) w.fail(new Error('Edge item'));
  waiting.clear();
});
ws.addEventListener(error, () => log('CDP WebSocket item'));
const js = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(`item: ${r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text}`);
  return r.result?.result?.value;
};
const waitFor = async (expr, what, timeout = 12000) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await js(expr)) return true; await sleep(100); }
  const now = await js(`[location.hash, document.querySelector('[data-step="commit"]')?.innerText?.slice(0, 600), document.querySelector('.onb-h')?.innerText, document.querySelector('.imu-coarse')?.innerText?.slice(0, 400), document.querySelector('.sv-sub')?.innerText].join( item )`);
  throw new Error(`${timeout / 1000} item: ${what}.item: ${now}.item: ${errors.slice(-4).join(' item ')}`);
};
const shot = async (name) => {
  if (!SHOTS) return;
  const r = await send('Page.captureScreenshot', { format: png, captureBeyondViewport: false });
  writeFileSync(`${SHOTS}/${name}`, Buffer.from(r.result.data, 'base64'));
};
const clickExact = (sel, text) => js(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})]
  .find((x) => x.textContent.trim() === ${JSON.stringify(text)} && !x.disabled);
  if (!b) return false; b.scrollIntoView({ block: center }); b.click(); return true; })()`);
const clickHas = (sel, text) => js(`(() => { const b = [...document.querySelectorAll(${JSON.stringify(sel)})]
  .find((x) => x.textContent.includes(${JSON.stringify(text)}) && !x.disabled);
  if (!b) return false; b.scrollIntoView({ block: 'center' }); b.click(); return true; })()`);

log('item Edge, item');
await send('Page.enable');
await send('Runtime.enable');
await send('Page.bringToFront');                                  // itemitem: item Studio 1 Hz item, item
await send('Emulation.setFocusEmulationEnabled', { enabled: true });
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: `${BASE}#/` });
await sleep(800);

log(item);
const out = { phase: PHASE };
const card = `document.querySelector('[data-step="commit"] .onb-commit')`;
const snapshot = () => js(`(() => {
  const c = ${card};
  if (!c) return null;
  const checks = [...c.querySelectorAll('.onb-checks li')].map((li) => ({
    ok: li.classList.contains(ok), text: li.innerText.replace(/\\s+/g, ' ').trim() }));
  const act = [...c.querySelectorAll('.onb-commit-acts button')];
  const write = act.find((b) => b.textContent.includes(item));
  return {
    rowState: document.querySelector('[data-step="commit"] .onb-mark')?.getAttribute('aria-label'),
    desc: c.querySelector('.onb-commit-desc')?.innerText,
    checks, bad: checks.filter((x) => !x.ok).length,
    writeDisabled: !!(write && write.disabled),
    job: c.querySelector('.onb-commit-job')?.innerText || null,
    msg: c.querySelector('.onb-commit-msg')?.innerText || null,
    msgs: [...c.querySelectorAll('.onb-commit-msg')].map((e) => e.innerText.replace(/\\s+/g,  ).trim()),
  };
})()`);

// IMU ' / ' ( 17): ,  ( WebSocket.send,  cmd)
const stance = () => js(`(() => { const c = document.querySelector('.imu-stance'); if (!c) return null;
  const b = (sel) => { const e = c.querySelector(sel); return e ? { text: e.textContent.trim(), disabled: e.disabled } : null; };
  return { stand: b('.imu-stand'), relax: b('.imu-relax'),
    now: c.querySelector('.imu-stance-now')?.innerText.replace(/\\s+/g,  ).trim() || '',
    why: c.querySelector('.imu-stance-why')?.innerText.replace(/\\s+/g,  ).trim() || null,
    goCommit: !!c.querySelector('.imu-go-commit'), text: c.innerText.replace(/\\s+/g,  ).trim() }; })()`);
const recordSends = () => js(`(() => { if (window.__sent) { window.__sent.length = 0; return true; } window.__sent = [];
  const orig = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) {
    try { const m = JSON.parse(d); if (m.t === 'cmd') window.__sent.push({ name: m.name, args: m.args }); } catch (e) { /* item JSON */ }
    return orig.call(this, d);
  };
  return true; })()`);
const sentStand = () => js(`(window.__sent || []).filter((m) => m.name === 'pad.op' && m.args && m.args.op === stand)`);
const remoteStand = `[...document.querySelectorAll('.pd-remote .pd-big')].find((x) => x.querySelector(b)?.textContent === 'Stand Up')`;

if (PHASE === 'ssh') {
  // '' SSH  (10-21): , PC root , '', , 
  await js(`location.hash = '#/settings'`);
  await waitFor(`!!document.querySelector('.ssh-settings')`, 'item SSH item', 20000);
  await waitFor(`!/item/.test(document.querySelector('.ssh-settings .ssh-now')?.innerText || '')`, 'SSH item', 20000);
  out.ssh = await js(`(() => { const c = document.querySelector('.ssh-settings');
    return { text: c.innerText.replace(/\\s+/g, ' ').trim(), now: c.querySelector('.ssh-now').innerText.trim(),
      onDisabled: c.querySelector('.ssh-on').disabled, needWebPw: !!c.querySelector('.ssh-need-web-pw'),
      inputs: c.querySelectorAll('input[type=password]').length }; })()`);
  await shot('settings-ssh.png');
}

if (PHASE === stance0) {
  //  ( 10-21): '',  imu.op hold (fork hold-home), ' ()'
  // ' (E-Stop)',  relax, ' ()'
  await js(`location.hash = '#/imu'`);
  await waitFor(`!!document.querySelector('.imu-stance')`, 'IMU item', 20000);
  await waitFor(`/item (item)/.test(document.querySelector('.imu-stance .imu-stance-now')?.innerText || '')`, 'item (item)', 10000);
  await waitFor(`(() => { const b = document.querySelector('.imu-stance .imu-stand'); return !!b && !b.disabled; })()`,
    'item: item', 10000);
  out.stance = await stance();
  await shot('imu-stance-commissioning.png');
  await recordSends();
  await js(`document.querySelector('.imu-stance .imu-stand').click()`);
  await waitFor(`/item (item)/.test(document.querySelector('.imu-stance .imu-stance-now')?.innerText || '')`, item, 15000);
  out.holdSent = await js(`(window.__sent || []).filter((m) => m.name === 'imu.op')`);
  out.dialogs = dialogs.slice();
  out.holding = await stance();
  await shot('imu-stance-commissioning-holding.png');
  await recordSends();
  await js(`document.querySelector('.imu-stance .imu-relax').click()`);
  await waitFor(`/item (item)/.test(document.querySelector('.imu-stance .imu-stance-now')?.innerText || '')`, item, 15000);
  out.relaxSent = await js(`(window.__sent || []).filter((m) => m.name === 'relax')`);
  out.after = await stance();
}

if (PHASE === stance) {
  // : '', Stand Up'''E-Stop'
  await fakeSet({ mode: motion, calib: 'committed', posture: limp, restart: 0.4 });
  await sleep(3000);       // itemE-Stop 2 item (item), item 1 item: item
  await js(`location.hash = '#/imu'`);
  await waitFor(`!!document.querySelector('.imu-stance')`, 'IMU item', 20000);
  await waitFor(`(() => { const b = document.querySelector('.imu-stance .imu-stand'); return !!b && !b.disabled; })()`,
    'item: item', 20000);
  out.before = await stance();
  await shot('imu-stance-motion.png');
  await recordSends();
  //  (, )
  await js(`new Promise((ok) => { const t0 = Date.now(); const t = setInterval(() => {
    const b = document.querySelector('.imu-stance .imu-stand');
    if (b && !b.disabled) { clearInterval(t); b.click(); ok(true); } else if (Date.now() - t0 > 10000) { clearInterval(t); ok(false); } }, 50); })`);
  // Stand Up: 'Stand Up' ()
  await waitFor(`(window.__sent || []).some((m) => m.name === 'pad.op' && m.args && m.args.op === stand)`, 'itemStand Up', 5000);
  out.standSent = await sentStand();
  await waitFor(`/item: item/.test(document.querySelector('.imu-stance .imu-stance-now')?.innerText || '')`,
    'item, item', 15000);
  out.standing = await stance();
  await fakeSet({ posture: 'limp' });
  await sleep(3000);       // item: item 2 itemStand Up (item 1 item, item), item
  await js(`location.hash = '#/pad'`);
  try {
    await waitFor(`(() => { const b = ${remoteStand}; return !!b && !b.disabled; })()`, 'Stand Up', 20000);
  } catch (e) {
    //  (, ): , , 
    const why = await js(`[...document.querySelectorAll('.pd-remote .pd-big')].map((x) => (x.querySelector('b')?.textContent || '?') + '|' + (x.disabled ? 'disabled' : 'enabled') + '|' + (x.title || '')).join(' ; ')`);
    throw new Error(`${e.message} item item: ${why}`);
  }
  await recordSends();
  await js(`new Promise((ok) => { const t0 = Date.now(); const t = setInterval(() => {
    const b = ${remoteStand};
    if (b && !b.disabled) { clearInterval(t); b.click(); ok(true); } else if (Date.now() - t0 > 10000) { clearInterval(t); ok(false); } }, 50); })`);
  await waitFor(`(window.__sent || []).some((m) => m.name === 'pad.op' && m.args && m.args.op === 'stand')`, 'Stand Up', 5000);
  out.remoteSent = await sentStand();
  await js(`location.hash = '#/imu'`);
  await waitFor(`(() => { const b = document.querySelector('.imu-stance .imu-relax'); return !!b && !b.disabled; })()`, 'E-Stop', 10000);
  await recordSends();
  await js(`document.querySelector('.imu-stance .imu-relax').click()`);
  await waitFor(`(window.__sent || []).some((m) => m.name === 'relax')`, 'E-Stop', 5000);
  out.relaxSent = await js(`(window.__sent || []).filter((m) => m.name === 'relax')`);
}

if (PHASE === 'mark') {
  // :  15  (hard).Skip, '' ( 10-20)
  const servoRow = `document.querySelector('.onb-row[data-step="servo"]')`;
  const servoCheck = `[...(${card}?.querySelectorAll('.onb-checks li') || [])].find((li) => li.innerText.includes('item'))`;
  await waitFor(`!!${servoRow} && !!${card} && !!(${servoCheck})`, 'item 1 item 3 item', 20000);
  out.before = await js(`({ row: ${servoRow}.querySelector('.onb-mark').getAttribute('aria-label'), check: (${servoCheck}).className })`);
  const doneBtn = `[...${servoRow}.querySelectorAll('button')].find((b) => b.textContent.trim() === 'item')`;
  if (!await js(`(() => { const b = ${doneBtn}; if (!b) return false; b.scrollIntoView({ block: 'center' }); b.click(); return true; })()`)) {
    throw new Error('item');
  }
  await waitFor(`!!${servoRow}.querySelector('.two-step-ask')`, 'item', 5000);
  out.ask = await js(`${servoRow}.querySelector('.two-step-ask').textContent`);
  out.afterOne = await js(`${servoRow}.querySelector('.onb-mark').getAttribute('aria-label')`);   // item
  await shot('commit-mark-servo-ask.png');
  await sleep(900);                                                  // TwoStep item 700 ms item
  await js(`${servoRow}.querySelector('.two-step-ask').click()`);
  await waitFor(`${servoRow}.querySelector('.onb-mark')?.getAttribute('aria-label') === 'item'`, 'item 1 item ✓', 10000);
  await waitFor(`(${servoCheck})?.classList.contains('ok')`, 'item 3 item', 10000);
  out.after = await js(`({ row: ${servoRow}.querySelector('.onb-mark').getAttribute('aria-label'),
    text: ${servoRow}.querySelector('.onb-text').textContent, check: (${servoCheck}).innerText.replace(/\\s+/g, ' ').trim() })`);
  await shot('commit-mark-servo-done.png');
}

if (PHASE === 'imu-redo') {
  await js(`location.hash = '#/imu'`);
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.trim() === 'item' && !b.disabled)`,
    'item IMU item', 10000);
  out.entryVisible = true;
  out.original = await fakeRpc('robot.busCommand', { action: 'calibration-status' });
  await recordSends();
  await clickExact('.imu-coarse button', 'item');
  out.beforeConfirm = await js(`(window.__sent || []).filter((m) => m.name === 'imu.op')`);
  await waitFor(`!!document.querySelector('.imu-coarse .two-step-ask')`, item, 5000);
  await sleep(900);
  await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
  await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '') &&
    !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes(item) && !b.disabled)`, 'item', 15000);
  out.active = await stance();
  await shot('imu-redo-active.png');
  // Refreshing must recover the persistent session instead of hiding the verified mount.
  await send('Page.reload');
  await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '') &&
    !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes(item) && !b.disabled)`, 'item', 15000);
  await recordSends();
  await fakeSet({ imu_pose: upright });
  await clickHas('.imu-coarse button', item);
  await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '')`, item, 15000);
  await fakeSet({ imu_pose: 'nose_down' });
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes(item) && !b.disabled)`, 'item');
  if (!await clickHas('.imu-coarse button', item)) throw new Error('item');
  await waitFor(`!!document.querySelector('.imu-coarse input[type=checkbox]')`, item, 20000);
  out.verifyInstruction = await js(`document.querySelector('.imu-coarse')?.innerText`);
  await js(`[...document.querySelectorAll('.imu-coarse input[type=checkbox]')].forEach((x) => { if (!x.checked) x.click(); })`);
  await fakeSet({ imu_pose: upright, imu_verify_plan: 'ok' });
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent === item && !b.disabled)`, 'item');
  if (!await clickHas('.imu-coarse button', item)) throw new Error('item');
  await waitFor(`/item/.test(document.querySelector('.topbar')?.innerText || document.querySelector(header)?.innerText || '') &&
    !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.trim() === item && !b.disabled)`, 'item', 45000);
  out.after = await fakeRpc('robot.busCommand', { action: 'calibration-status' });
  out.afterStance = await stance();
  out.motionCommands = await js(`(window.__sent || []).filter((m) => m.name === 'pad.op' || m.name === init || (m.name === 'imu.op' && m.args.op === hold))`);
  await shot('imu-redo-complete.png');
  // A second revalidation can be cancelled and must restore exactly that saved calibration.
  await clickExact('.imu-coarse button', item);
  await sleep(900);
  await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
  await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '')`, item, 15000);
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent === item && !b.disabled)`, 'item');
  if (!await clickExact('.imu-coarse button', item)) throw new Error('item');
  await sleep(900);
  await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
  await waitFor(`/item/.test(document.querySelector('.topbar')?.innerText || '') &&
    !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.trim() === item && !b.disabled)`, 'item', 15000);
  out.cancelled = await fakeRpc('robot.busCommand', { action: 'calibration-status' });
  out.recoveredPhases = [];
  for (const phase of [entering, 'finishing', cancelling]) {
    await clickExact('.imu-coarse button', item);
    await sleep(900);
    await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
    await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '') &&
      !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes(item) && !b.disabled)`, 'item');
    writeFileSync(CONTROL, JSON.stringify({ outcome: ok, redo_phase: phase }));
    await send('Page.reload');
    await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '') &&
      !document.querySelector('.imu-coarse-steps') &&
      !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent === item && !b.disabled)`, `item ${phase} item`, 6000);
    await clickExact('.imu-coarse button', item);
    await sleep(900);
    await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
    await waitFor(`/item/.test(document.querySelector('.topbar')?.innerText || '') &&
      !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent === item && !b.disabled)`, 'item');
    out.recoveredPhases.push(phase);
    writeFileSync(CONTROL, JSON.stringify({ outcome: ok }));
  }
  // Already verified commissioning deployments also retain a direct revalidation entry.
  await fakeSet({ mode: 'commissioning', imu_pose: upright });
  await waitFor(`/item/.test(document.querySelector('.topbar')?.innerText || '') &&
    !![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.trim() === item && !b.disabled)`, 'item', 15000);
  await recordSends();
  await clickExact('.imu-coarse button', item);
  await sleep(900);
  await js(`document.querySelector('.imu-coarse .two-step-ask').click()`);
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes(item))`, 'item', 5000);
  out.commissioningCommands = await js(`(window.__sent || []).filter((m) => m.name === 'imu.op')`);
}

if (PHASE === imu || PHASE === 'imu-hold') {
  log('IMU item: item');
  if (PHASE === 'imu-hold') {
    await fakeSet({ mode: 'commissioning', calib: 'factory', posture: 'commissioning', imu_pose: 'upright', restart: 0.4 });
  }
  await js(`location.hash = '#/imu'`);
  await waitFor(`!!document.querySelector('.imu-coarse')`, 'IMU item', 20000);
  if (PHASE === 'imu-hold') {
    await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes('item') && !b.disabled)`,
      'item', 20000);
  }
  await js(`document.querySelector('.imu-stance .imu-relax').click()`);
  await sleep(500);
  if (PHASE === 'imu-hold') {
    await waitFor(`!!document.querySelector('.imu-stance .imu-stand') && !document.querySelector('.imu-stance .imu-stand').disabled`,
      item, 10000);
    await js(`document.querySelector('.imu-stance .imu-stand').click()`);
    await waitFor(`/item (item)/.test(document.querySelector('.imu-stance .imu-stance-now')?.innerText || '')`,
      item, 15000);
    out.holdBefore = await fakeRpc('robot.busStatus', {});
  }
  out.start = await js(`document.querySelector('.imu-coarse')?.innerText.replace(/\\s+/g, ' ').slice(0, 500)`);
  if (!await clickHas('.imu-coarse button', 'item')) throw new Error('item');
  await waitFor(`/item/.test(document.querySelector('.imu-coarse')?.innerText || '')`, 'item', 20000);
  await fakeSet({ imu_pose: 'nose_down' });
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent.includes('item') && !b.disabled)`,
    'item', 15000);
  if (!await clickHas('.imu-coarse button', 'item')) throw new Error('item');
  await waitFor(`!!document.querySelector('.imu-coarse input[type=checkbox]') || /item/.test(document.querySelector('.imu-coarse')?.innerText || '')`,
    'item', 25000);
  await js(`[...document.querySelectorAll('.imu-coarse input[type=checkbox]')].forEach((x) => { if (!x.checked) x.click(); })`);
  await fakeSet({ imu_verify_plan: 'ok' });        // item: item, item
  await waitFor(`!![...document.querySelectorAll('.imu-coarse button')].find((b) => b.textContent === 'item' && !b.disabled)`, 'item');
  if (!await clickHas('.imu-coarse button', 'item')) throw new Error('item');
  //  verified  true, :  IMU ''.
  await waitFor(`/item/.test(document.querySelector('.sv-metrics')?.innerText || '')`,
    'verify-imu item', 40000);
  out.imu = await js(`[document.querySelector('.sv-metrics')?.innerText || '',
    document.querySelector('.imu-coarse')?.innerText || ''].filter(Boolean).join(' ').replace(/\\s+/g, ' ').slice(0, 700)`);
  if (PHASE === 'imu-hold') {
    out.holdAfter = await fakeRpc('robot.busStatus', {});
    await shot('commit-imu-hold-verified.png');
    // Reset the fake fixture independently of which cards remain mounted after verification.
    await fakeRpc('robot.relax', {});
    for (let attempt = 0; attempt < 50; attempt++) {
      out.cleaned = await fakeRpc('robot.busStatus', {});
      if (out.cleaned.torque_state === off) break;
      await sleep(100);
    }
  } else await shot('commit-imu-verified.png');
}

if (PHASE === servo) {
  log('item: item, item');
  await js(`location.hash = '#/servos'`);
  await waitFor(`/item/.test(document.querySelector('.sv-sub')?.innerText || '')`, 'item (item)', 20000);
  if (!await clickExact('.sv-page button', 'item')) throw new Error('item');
  await waitFor(`!!document.querySelector('.sv-modal')`, 'item', 15000);
  out.planRows = await js(`document.querySelectorAll('.sv-plan tbody tr').length`);
  if (!await clickHas('.sv-modal button', 'item')) throw new Error('item');
  await waitFor(`/item/.test(document.body.innerText)`, '15 item', 40000);
  if (!await clickHas('.sv-handback button', 'item')) throw new Error('item');
  await waitFor(`!!document.querySelector('.sv-handback .two-step-ask')`, 'item', 5000);
  await sleep(900);
  await js(`document.querySelector('.sv-handback .two-step-ask')?.click()`);
  await waitFor(`!/item/.test(document.querySelector('.sv-sub')?.innerText || '')`, 'item', 30000);
  out.servo = await js(`document.querySelector('.sv-sub')?.innerText.replace(/\\s+/g, ' ')`);
  await shot('commit-servo-done.png');
  await js(`location.hash = '#/'`);
}

if (PHASE === 'commit' || PHASE === 'fail') {
  await js(`location.hash = '#/'`);
  await waitFor(`!!${card}`, 'item 3', 20000);
  await waitFor(`(() => { const c = ${card}; return c && c.querySelectorAll('.onb-checks li').length > 0; })()`, 'item', 20000);
  await waitFor(`(() => { const c = ${card}; const w = [...c.querySelectorAll('.onb-commit-acts button')]
    .find((b) => b.textContent.includes('item')); return w && !w.disabled; })()`, 'item', 25000);
  out.ready = await snapshot();
  const writeBtn = `[...document.querySelectorAll('[data-step="commit"] .onb-commit-acts button')]
    .find((b) => b.textContent.includes('item'))`;
  await js(`${writeBtn}.click()`);
  await waitFor(`!!document.querySelector('[data-step="commit"] .two-step-ask')`, 'item', 5000);
  out.confirmText = await js(`document.querySelector('[data-step="commit"] .two-step-ask')?.textContent`);
  await sleep(900);       // TwoStep item 700 ms item
  await js(`document.querySelector('[data-step="commit"] .two-step-ask').click()`);
  // ,  ( 3  ✓, ): , .
  await waitFor(`/item|item/.test(document.querySelector('[data-step="commit"] .onb-commit-msg')?.innerText || '') ||
    !!document.querySelector('[data-step="commit"] .onb-commit-job') ||
    document.querySelector('[data-step="commit"] .onb-mark')?.getAttribute('aria-label') === item`,
    'item', 20000);
  out.started = await snapshot();

  if (PHASE === fail) {
    // :  3 
    let offline = null;
    const end = Date.now() + 25000;
    while (Date.now() < end) {
      const s = await js(`(() => { const c = document.querySelector('[data-step="commit"]'); if (!c) return null;
        return { text: c.innerText.replace(/\\s+/g,  ).slice(0, 600), job: !!c.querySelector('.onb-commit-job'), msg: c.querySelector('.onb-commit-msg')?.innerText || null }; })()`);
      if (s && s.job && /item|item/.test(s.text)) { offline = s; break; }
      await sleep(200);
    }
    out.offline = offline;
    if (!offline) throw new Error('item 3 item');
    await shot('commit-fail-offline.png');
    await waitFor(`(() => { const m = document.querySelector('[data-step="commit"] .onb-commit-msg.bad');
      return /item/.test(m?.innerText || ''); })()`, 'item', 30000);
    // : , ,  3 ''''.
    await waitFor(`document.querySelector('[data-step="commit"] .onb-mark')?.getAttribute('aria-label') === 'item'`,
      'item 3 item', 30000);
    out.wsCommit = await js(`(async () => {
      const w = new WebSocket(location.origin.replace('http', 'ws') + '/ws');
      const m = await new Promise((ok) => {
        const t = setTimeout(() => { try { w.close(); } catch (e) {} ok(null); }, 5000);
        w.onmessage = (e) => { const x = JSON.parse(e.data); if (x.t === 'state') { clearTimeout(t); w.close(); ok(x); } };
      });
      return m ? { robotd_ok: m.robotd_ok, posture: m.posture, mode: m.mode, phase: m.phase,
        job: m.commit && m.commit.job, commit_mode: m.commit && m.commit.mode } : null;
    })()`);
    out.failed = await snapshot();
    await shot('commit-fail-reason.png');
  } else {
    await waitFor(`document.querySelector('[data-step="commit"] .onb-mark')?.getAttribute('aria-label') === 'item'`,
      'item 3 item (item)', 40000);
    await waitFor(`/item/.test(document.querySelector('.topbar .meta')?.innerText || '')`, 'item', 15000);
    out.done = await snapshot();
    out.topbar = await js(`document.querySelector('.topbar .meta')?.innerText`);
    await shot('commit-done.png');
  }
}

if (PHASE === 'factory') {
  await waitFor(`!!${card}`, 'item 3 item', 20000);
  await waitFor(`(() => { const c = ${card}; return c && c.querySelectorAll('.onb-checks li').length > 0; })()`, 'item', 15000);
  out.factory = await snapshot();
  await shot('commit-factory.png');
  await js(`[...document.querySelectorAll('[data-step="commit"] .onb-commit-acts button')]
    .find((b) => b.textContent.includes('item'))?.click()`);
  await waitFor(`!!document.querySelector('[data-step="commit"] .onb-preview')`, 'item', 20000);
  out.preview = await js(`(() => {
    const p = document.querySelector('[data-step="commit"] .onb-preview');
    return { jointRows: p.querySelectorAll('.onb-preview-table tbody tr').length,
      imu: p.querySelector('.onb-preview-imu')?.innerText || '',
      toml: (p.querySelector('pre')?.textContent || '').slice(0, 4000) };
  })()`);
  await shot('commit-preview.png');
}

log('item');
out.errors = errors;
process.stdout.write(JSON.stringify(out));
await new Promise((r) => process.stdout.write('', r));
ws.close();
await fetch(`http://127.0.0.1:${CDP}/json/close/${page.id}`);
