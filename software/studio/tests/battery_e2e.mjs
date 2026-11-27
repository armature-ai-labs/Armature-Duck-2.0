//  (battery-plan.md v2 §5, §4):  Edge (CDP),  Studio ( robotd  + , 
// Studio  20 ), , , ,  / , 
// '', . JSON  stdout,  tests/test_battery_e2e.py .
//  ( robotd, Studio (STUDIO_BATTERY_SPEED=20),  Edge): 
//   node tests/battery_e2e.mjs --url http://127.0.0.1:18103/ --cdp 9353 --fake 17056 [--shots <>]
// Node ≥ 22 ( WebSocket), .
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import net from 'node:net';

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const BASE = arg('url');
const CDP = arg('cdp');
const FAKE = Number(arg('fake'));
const SHOTS = arg('shots', '');
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => process.stderr.write(`[battery-e2e] ${a.join(' ')}\n`);

const fakeSet = (params) => new Promise((resolve, reject) => {
  const s = net.connect(FAKE, '127.0.0.1', () => s.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'fake.set', params })}\n`));
  let buf = '';
  s.on('data', (d) => { buf += d; if (buf.includes('\n')) { s.end(); resolve(JSON.parse(buf)); } });
  s.on('error', reject);
});
const studioLogs = async () => ((await (await fetch(`${BASE}api/logs?limit=500`)).json()).items || []).map((e) => e.msg);

// ---------------- CDP ----------------
const page = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0;
const waiting = new Map();
const errors = [];
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; waiting.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(`item: ${r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text}`);
  return r.result?.result?.value;
};
const cellText = () => js(`(document.querySelector('.bat-cell')?.innerText || '').replace(/\\s+/g, ' ').trim()`);
const waitFor = async (expr, what, timeout = 15000, every = 100) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await js(expr)) return true; await sleep(every); }
  throw new Error(`${timeout / 1000} item: ${what}.item: ${await cellText()} item item: ${await js(`document.querySelector('.todo')?.innerText?.replace(/\\s+/g, ' ')`)}`);
};
await send('Runtime.enable');
await send('Page.enable');
let VIEW = { w: 1440, h: 900, mobile: false };
async function setView(w, h, mobile) {
  VIEW = { w, h, mobile };
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile });
  await send('Emulation.setTouchEmulationEnabled', { enabled: !!mobile, maxTouchPoints: mobile ? 5 : 1 });
}
async function open(hash, w = 1440, h = 900, mobile = false) {
  await send('Page.bringToFront');
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await setView(w, h, mobile);
  await send('Page.navigate', { url: `${BASE}${hash}` });
}
// : sels  (, )
async function shot(name, ...sels) {
  if (!SHOTS) return;
  const r = await js(`(() => { const rs = ${JSON.stringify(sels)}.map((s) => document.querySelector(s)).filter(Boolean).map((e) => e.getBoundingClientRect());
    if (!rs.length) return null;
    const l = Math.min(...rs.map((r) => r.left)), t = Math.min(...rs.map((r) => r.top)), R = Math.max(...rs.map((r) => r.right)), B = Math.max(...rs.map((r) => r.bottom));
    return { x: l + scrollX - 8, y: t + scrollY - 8, width: R - l + 16, height: B - t + 16 }; })()`);
  const res = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, ...(r ? { clip: { ...r, scale: 1 } } : {}) });
  writeFileSync(join(SHOTS, name), Buffer.from(res.result.data, 'base64'));
  log('item', name);
}
const mode = (m) => `document.querySelector('.bat-cell')?.dataset.mode === ${JSON.stringify(m)}`;
const todoText = () => js(`(document.querySelector('.todo')?.innerText || '').replace(/\\s+/g, ' ')`);
//  20 :  10  ( 0.5 ).' 5 ', 
async function restart(battery) {
  await fakeSet({ battery: { gone: true } });
  await waitFor(mode('no_data'), 'item', 8000);
  await fakeSet({ battery: { gone: false, sag: 0, ...battery } });
}

const out = { errors, steps: {} };

// ---- 1. :  7.62 V ----
// Studio may have accumulated the recorded fixture's 7.54 V while Edge was
// starting. Explicitly clear that history before testing the scripted 7.62 V.
await fakeSet({ battery: { gone: true } });
await open('#/');
await waitFor(`!!document.querySelector('.bat-cell') && !!document.querySelector('.band-cpu')`, 'item');
await waitFor(mode('no_data'), 'item', 8000);
await fakeSet({ battery: { v: 7.62, sag: 0, gone: false } });
await waitFor(mode('calm'), 'item');
await sleep(400);
out.steps.calm = { text: await cellText(), cls: await js(`document.querySelector('.bat-cell').className`) };
await shot('battery-calm.png', '.band');

// ---- 2.  ----
await js(`document.querySelector('.bat-hit').click()`);
await waitFor(`!!document.querySelector('.bat-pop')`, 'item');
out.steps.detail = { lines: await js(`[...document.querySelectorAll('.bat-pop li')].map((l) => l.textContent)`),
  expanded: await js(`document.querySelector('.bat-hit').getAttribute('aria-expanded')`) };
await shot('battery-detail.png', '.band', '.bat-pop');
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
await waitFor(`!document.querySelector('.bat-pop')`, 'item Esc item');

// ---- 3.  → '' ----
await fakeSet({ battery: { gone: true } });
await waitFor(mode('no_data'), 'item', 8000);
out.steps.nodata = { text: await cellText() };
await shot('battery-nodata.png', '.band');
await fakeSet({ battery: { gone: false, v: 7.40 } });
await waitFor(mode('estimating'), 'item', 8000, 40);
out.steps.estimating = { text: await cellText(), todo: await todoText() };
await shot('battery-estimating.png', '.band');

// ---- 4. : 7.40 V ( 28%) ----
await waitFor(`document.querySelector('.bat-cell')?.classList.contains('warn')`, 'item', 15000);
out.steps.warn = { text: await cellText(), todo: await todoText() };
await shot('battery-warn.png', '.band', '.todo');

// ---- 5. : 7.15 V ( 9%) ----
await restart({ v: 7.15 });
await waitFor(`document.querySelector('.bat-cell')?.classList.contains('bad') && /item/.test(document.querySelector('.todo')?.innerText || '')`, 'item', 15000);
out.steps.red = { text: await cellText(), todo: await todoText() };
await shot('battery-red.png', '.band', '.todo');

// ---- 6. :  6.8 V  ----
await restart({ v: 7.60 });
await waitFor(mode('calm'), 'item');
await fakeSet({ battery: { sag: 0.8 } });
await waitFor(`/item/.test(document.querySelector('.todo')?.innerText || '')`, 'item', 15000);
out.steps.lowvolt = { text: await cellText(), todo: await todoText(), badge: await js(`!!document.querySelector('.bat-cell .badge.bad')`) };
await shot('battery-lowvolt.png', '.band', '.todo');
await fakeSet({ battery: { sag: 0 } });

// ---- 7. Phone:  +  ----
await restart({ v: 7.62 });
await open('#/', 390, 844, true);
await waitFor(mode('calm'), 'Phoneitem');
await js(`document.querySelector('.bat-hit').click()`);
await waitFor(`!!document.querySelector('.bat-pop')`, 'Phoneitem');
out.steps.mobile = await js(`({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
  popRight: Math.round(document.querySelector('.bat-pop').getBoundingClientRect().right) })`);
await shot('battery-mobile.png', '.band', '.bat-pop');

// ---- 8. :  ----
await open('#/settings');
await waitFor(`!!document.querySelector('.bset input[name=bat-preset]')`, 'item');
await sleep(300);
out.steps.settings = { text: await js(`document.querySelector('.bset').innerText.replace(/\\s+/g, ' ')`) };
await shot('settings-battery.png', '.bset');
const setNum = (label, v) => js(`(() => { const e = document.querySelector('.bset input[aria-label="${label}"]'); e.focus();
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; set.call(e, ${JSON.stringify(String(v))});
  e.dispatchEvent(new Event('input', { bubbles: true }));
  e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); return e.value; })()`);
const clickBtn = (text) => js(`(() => { const b = [...document.querySelectorAll('.bset button')].find((x) => x.textContent.trim().startsWith(${JSON.stringify(text)}));
  if (!b || b.disabled) return false; b.click(); return true; })()`);
const bmsg = () => js(`[...document.querySelectorAll('.bset .msg')].map((e) => e.textContent.replace(/\\s+/g, ' ')).join(' | ')`);
// :  28% ( 30%  5%)
await setNum('red_pct', 28);
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText)`, 'item');
out.steps.bad = { msg: await bmsg() };
await setNum('red_pct', 20);
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText)`, 'item');
out.steps.saved = { msg: await bmsg(), logs: (await studioLogs()).filter((m) => m.startsWith('item')) };
// : E-Stop ( robotd )
await sleep(2500);
await setNum('item', 7.80);
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText) || /itemE-Stop/.test(document.querySelector('.bset').innerText)`, 'item');
out.steps.calc = { msg: await bmsg(), text: await js(`document.querySelector('.bset').innerText.replace(/\\s+/g, ' ')`) };
await shot('settings-calibrate.png', '.bset');
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText) && /item \\+0\\.18 V/.test(document.querySelector('.bset').innerText)`, 'item', 8000);
out.steps.calibrated = { now: await js(`document.querySelector('.bset .now').textContent`) };

// ---- 9.  ----
await js(`document.querySelector('.bset input[value=bench]').click()`);
await sleep(200);
out.steps.benchForm = { text: await js(`document.querySelector('.bset').innerText.replace(/\\s+/g, ' ')`) };
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText) && /item (item) · item/.test(document.querySelector('.bset .now').textContent)`, 'item');
await shot('settings-bench.png', '.bset');
await open('#/');
await waitFor(mode('bench'), 'item');
await sleep(400);
out.steps.bench = { text: await cellText(), todo: await todoText() };
// Phone (, ): 
await js(`document.querySelector('.bat-pop') || document.querySelector('.bat-hit').click()`);
await waitFor(`!!document.querySelector('.bat-pop')`, 'item');
out.steps.bench.lines = await js(`[...document.querySelectorAll('.bat-pop li')].map((l) => l.textContent)`);
await shot('battery-bench.png', '.band', '.bat-pop');
// :  2S  ()
await open('#/settings');
await waitFor(`!!document.querySelector('.bset input[value="2s_liion"]')`, 'item');
await js(`document.querySelector('.bset input[value="2s_liion"]').click()`);
await sleep(200);
await clickBtn('item');
await waitFor(`/item/.test(document.querySelector('.bset').innerText)`, 'item 2S item');
out.logs = (await studioLogs()).filter((m) => /item|item|item/.test(m)).slice(-40);

process.stdout.write(JSON.stringify(out));
ws.close();
await fetch(`http://127.0.0.1:${CDP}/json/close/${page.id}`);
