// WiFi  ( 10b):  configd  hotspot  net.json, Studio ,  Edge Phone.
// :  WiFi  →  Home →  →  → ''→ last-try ''.
//  JSON  stdout,  tests/test_wifi_e2e.py .
//  17:  →'',  2  duck-fake / duck-fake.local  probe.svg
//  ()→ ,  → .
//  WebSocket  (Page.addScriptToEvaluateOnNewDocument, : __cut , 
// )duck-fake ,  CDP Fetch : '', , .
//  ( robotd, Studio,  Edge): 
//   node tests/wifi_e2e.mjs --url http://127.0.0.1:18091/ --cdp 9344 [--shots <>]
// Node >= 22 ( WebSocket), .
import { writeFileSync, mkdirSync } from 'node:fs';
import { createServer } from 'node:net';
import { join } from 'node:path';

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const BASE = arg('url');
const CDP = arg('cdp');
const SHOTS = arg('shots', '');
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const page = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0;
const waiting = new Map();
const errors = [];
//  17:  duck-fake .probeMode = fail: ''found: duck-fake.local , Skip
const probes = [];
let probeMode = 'fail';
const PORT = new URL(BASE).port;
// : '' WebSocket  ( 9 )
const DEAD = await new Promise((ok) => { const s = createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => ok(p)); }); });
const onPaused = (p) => {
  const url = p.request.url;
  probes.push(url);
  const svg = /\/probe\.svg\?t=\d+$/.test(url);
  if (probeMode === 'found' && url.startsWith(`http://duck-fake.local:${PORT}/`)) {
    const body = svg ? '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>' : '<!doctype html><title>found</title><p>found</p>';
    send('Fetch.fulfillRequest', { requestId: p.requestId, responseCode: 200,
      responseHeaders: [{ name: 'Content-Type', value: svg ? 'image/svg+xml' : 'text/html; charset=utf-8' }],
      body: Buffer.from(body).toString('base64') });
  } else {
    send('Fetch.failRequest', { requestId: p.requestId, errorReason: 'NameNotResolved' });
  }
};
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  if (m.method === 'Fetch.requestPaused') onPaused(m.params);
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; waiting.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(`item: ${r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text}`);
  return r.result?.result?.value;
};
const waitFor = async (expr, what, timeout = 12000) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await js(expr)) return true; await sleep(100); }
  const now = await js(`[location.hash, document.querySelector('.overview')?.innerText?.slice(0, 800),
    document.querySelector('.wifi-card')?.innerText?.slice(0, 500), document.querySelector('.wifi-last-try')?.innerText,
    document.querySelector('.wifi-msg')?.innerText].join(' item ')`);
  throw new Error(`${timeout / 1000} item: ${what}.item: ${now} item item: ${JSON.stringify(errors)}`);
};
const setVal = async (sel, value) => js(`(() => {
  const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return false;
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', { bubbles: true })); return e.value;
})()`);
const click = (sel) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return false; e.click(); return true; })()`);
const shot = async (name) => {
  if (!SHOTS) return;
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(SHOTS, name), Buffer.from(r.result.data, 'base64'));
};

await send('Runtime.enable');
await send('Page.enable');
await send('Page.bringToFront');
await send('Emulation.setFocusEmulationEnabled', { enabled: true });
await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
  const Real = window.WebSocket;
  window.__ws = []; window.__cut = false;
  class Cuttable extends Real {
    constructor(url, p) { super(window.__cut ? 'ws://127.0.0.1:${DEAD}/cut' : url, p); window.__ws.push(this); }
  }
  window.WebSocket = Cuttable;
})()` });
await send('Fetch.enable', { patterns: [{ urlPattern: '*duck-fake*', requestStage: 'Request' }] });
await send('Page.navigate', { url: 'about:blank' });
await sleep(250);
await send('Page.navigate', { url: `${BASE}#/` });

const out = { errors };
await waitFor(`!!document.querySelector('.wifi-card')`, 'item WiFi', 15000);
await waitFor(`!!document.querySelector('.wifi-net[data-ssid="Home"]')`, 'item Home');
out.card = await js(`({ text: document.querySelector('.wifi-card').innerText.replace(/\\s+/g, ' '),
  beforeOnboard: !!(document.querySelector('.wifi-card').compareDocumentPosition(document.querySelector('.onb'))
    & Node.DOCUMENT_POSITION_FOLLOWING),
  ssids: [...document.querySelectorAll('.wifi-net')].map((e) => e.dataset.ssid),
  nameTitle: document.querySelector('.topbar .name')?.title || '',
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth })`);
await shot('wifi-card-phone.png');

await click('.wifi-net[data-ssid="Home"] .wifi-net-head');
await waitFor(`!!document.querySelector('.wifi-psk')`, 'Home item');
await click('.wifi-net[data-ssid="Home-5G"] .wifi-net-head');
await waitFor(`!!document.querySelector('.wifi-net[data-ssid="Home-5G"] .wifi-psk')`, 'item');
out.expanded = await js(`({ pskType: document.querySelector('.wifi-psk')?.type,
  saved: document.querySelector('.wifi-net[data-ssid="Home-5G"] .wifi-badge')?.textContent,
  savedNote: document.querySelector('.wifi-net[data-ssid="Home-5G"] .wifi-note')?.textContent })`);
await click('.wifi-net[data-ssid="Home-5G"] .wifi-net-head');
await click('.wifi-net[data-ssid="Home"] .wifi-net-head');
await waitFor(`!!document.querySelector('.wifi-net[data-ssid="Home"] .wifi-psk')`, 'Home item');
await setVal('.wifi-psk', 'wrongpass');
await click('.wifi-net[data-ssid="Home"] .wifi-connect');
await waitFor(`!!document.querySelector('.wifi-confirm')`, 'item');
out.confirm = await js(`document.querySelector('.wifi-confirm').innerText.replace(/\\s+/g, ' ')`);
await shot('wifi-confirm-phone.png');

await click('.wifi-confirm .btn-danger');
await waitFor(`/item/.test(document.querySelector('.wifi-switching')?.innerText || '')`, 'item', 5000);
out.switching = await js(`document.querySelector('.wifi-switching').innerText.replace(/\\s+/g, ' ')`);
await shot('wifi-switching-phone.png');

await waitFor(`/item'Home'item: item/.test(document.querySelector('.wifi-last-try')?.innerText || '')`, 'last-try item', 15000);
out.lastTry = await js(`({ text: document.querySelector('.wifi-last-try').innerText.replace(/\\s+/g, ' '),
  color: getComputedStyle(document.querySelector('.wifi-last-try')).color,
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth })`);
await shot('wifi-last-try-phone.png');

// Phone'': , 
out.hotspotPasswordHint = await js(`document.querySelector('.wifi-card')?.innerText.includes('item')`);
await js(`[...document.querySelectorAll('.tabbar a')].find((a) => a.textContent.trim() === 'item').click()`);
await waitFor(`location.hash === '#/settings' && !!document.querySelector('.password-settings')`, 'Phoneitem');
out.mobile = await js(`({ navHidden: getComputedStyle(document.querySelector('.nav')).display === 'none',
  hasLogs: !!document.querySelector('.settings-logs'), hasDiag: !!document.querySelector('.settings-diag') })`);
await shot('settings-mobile-links.png');
// '''':  IP  10.42.0.1,  duck-xxxx, duck-xxxx.local, 
await waitFor(`document.querySelectorAll('.duck-addr .duck-addr-row').length >= 3`, 'item');
out.addr = await js(`({ rows: [...document.querySelectorAll('.duck-addr .duck-addr-row')].map((r) => ({ kind: r.dataset.kind,
    url: r.querySelector('a')?.textContent, copy: r.querySelector('.duck-addr-copy')?.textContent.trim() })),
  text: document.querySelector('.duck-addr').innerText.replace(/\\s+/g, ' '),
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth })`);
await click('.duck-addr .duck-addr-row .duck-addr-copy');
await sleep(300);
out.addr.copied = await js(`document.querySelector('.duck-addr .duck-addr-row .duck-addr-copy').textContent.trim()`);
await shot('settings-duck-addr-phone.png');
await click('.settings-logs');
await waitFor(`location.hash === '#/logs' && !!document.querySelector('.logs-export')`, 'item');
out.mobile.logs = await js(`document.querySelector('.logs-export')?.textContent`);
await js(`window.__diagHref = ''; document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a.logs-export');
  if (a) { e.preventDefault(); window.__diagHref = a.getAttribute('href'); }
}, { capture: true, once: true })`);
await click('.logs-export');
out.mobile.diagClicked = await js('window.__diagHref');

// ,  ()
//  (, ),  Node 
out.passwordSet = await js(`(async () => {
  const sess = await (await fetch('/api/session')).json();
  const r = await fetch('/api/password', { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF': sess.csrf },
    body: JSON.stringify({ action: 'set', password: 'phone-secret', confirm: 'phone-secret' }) });
  return r.status;
})()`);
await js(`location.hash = '#/'`);
await waitFor(`!!document.querySelector('.wifi-card') && !document.querySelector('.wifi-card').innerText.includes('item')`,
  'item', 10000);
out.hotspotPasswordHintAfterSet = await js(`document.querySelector('.wifi-card').innerText.includes('item')`);

// ----  17: ,  ----
const cut = () => js(`(() => { window.__cut = true; for (const w of window.__ws) { try { w.close(); } catch (e) { /* item */ } } return true; })()`);
const uncut = () => js(`(() => { window.__cut = false; return true; })()`);
const connectTo = async (ssid, psk) => {
  await click(`.wifi-net[data-ssid="${ssid}"] .wifi-net-head`);
  await waitFor(`!!document.querySelector('.wifi-net[data-ssid="${ssid}"] .wifi-connect')`, `${ssid} item`);
  if (psk) await setVal(`.wifi-net[data-ssid="${ssid}"] .wifi-psk`, psk);
  await waitFor(`!document.querySelector('.wifi-net[data-ssid="${ssid}"] .wifi-connect').disabled`, `${ssid} item'item'item`);
  await click(`.wifi-net[data-ssid="${ssid}"] .wifi-connect`);
  await waitFor(`!!document.querySelector('.wifi-confirm')`, `${ssid} item`);
  await click('.wifi-confirm .btn-danger');
  await waitFor(`!!document.querySelector('.wifi-switching')`, `${ssid} item`, 5000);
};
// A. :  → ,  2  → , , 
probeMode = 'fail';
await connectTo('Ghost', 'whatever1');
await cut();
const mark = probes.length;
await waitFor(`document.querySelector('.wifi-finding')?.dataset.phase === 'finding'`, 'item', 8000);
await sleep(4500);
out.findA = await js(`(() => { const c = document.querySelector('.wifi-finding'); return {
  phase: c.dataset.phase, text: c.innerText.replace(/\\s+/g, ' '),
  links: [...c.querySelectorAll('a')].map((a) => a.getAttribute('href')),
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }; })()`);
out.findA.probes = probes.slice(mark);
await shot('wifi-finding-phone.png');
await uncut();
await waitFor(`!document.querySelector('.wifi-finding') && /item'Ghost'item/.test(document.querySelector('.wifi-last-try')?.innerText || '')`,
  'item: item', 20000);
const atBack = probes.length;
await sleep(4500);
out.findBack = { finding: !!(await js(`!!document.querySelector('.wifi-finding')`)),
  lastTry: await js(`document.querySelector('.wifi-last-try')?.innerText.replace(/\\s+/g, ' ')`),
  probesAtBack: atBack, probesLater: probes.length };
// B. : duck-fake.local  → Skip
probeMode = 'found';
await connectTo('Coffee', '');
await cut();
const end = Date.now() + 15000;
out.found = null;
while (Date.now() < end) {
  let href = null;
  try { href = await js('location.href'); } catch { /* item */ }
  if (href && href.startsWith('http://duck-fake.local')) { out.found = { href, title: await js('document.title') }; break; }
  await sleep(200);
}
if (!out.found) throw new Error(`15 item duck-fake.local: item ${JSON.stringify(probes.slice(-6))}`);

process.stdout.write(JSON.stringify(out));
ws.close();
await fetch(`http://127.0.0.1:${CDP}/json/close/${page.id}`);
