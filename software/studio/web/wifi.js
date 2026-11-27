// Wi-Fi Provisioning Card & Settings + Duck LAN Addresses (with automatic post-switch discovery).
import { html, useState, useEffect } from './vendor/preact-htm.module.js';
import { ConfirmBox } from './confirm.js';

const CSS = `
.wifi-card { padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.wifi-card h2 { margin: 0; font-size: 17px; font-weight: 600; }
.wifi-settings { padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.wifi-settings h2 { margin: 0; font-size: 15px; font-weight: 600; }
.wifi-lead { margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-2); }
.wifi-last-try { padding: 9px 12px; border-radius: var(--radius); background: var(--danger-soft); box-shadow: inset 0 0 0 1px var(--danger);
  color: var(--danger); font-size: 14px; line-height: 1.55; }
.wifi-list { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.wifi-net, .wifi-manual { border-radius: var(--radius); background: var(--panel-2); border: 1px solid var(--hair-subtle); min-width: 0; overflow: hidden; }
.wifi-net.on { background: var(--accent-soft); border-color: var(--accent); }
.wifi-net-head, .wifi-manual-head { width: 100%; min-height: 48px; padding: 8px 12px; border: 0; background: transparent;
  display: flex; align-items: center; gap: 10px; text-align: left; font-size: 16px; color: var(--text); }
.wifi-manual-head { color: var(--text-2); font-size: 14px; }
.wifi-name { flex: 1; min-width: 0; overflow-wrap: anywhere; line-height: 1.3; }
.wifi-name b { font-weight: 600; }
.wifi-sub { display: block; margin-top: 2px; font-size: 12px; color: var(--text-2); font-weight: 400; }
.wifi-badge { flex: none; font-size: 11px; padding: 1px 7px; border-radius: var(--radius); background: var(--ok-soft); color: var(--ok); border: 1px solid var(--ok); }
.wifi-lock { flex: none; font-size: 13px; opacity: .75; }
.wifi-bars { flex: none; width: 22px; height: 16px; display: inline-flex; align-items: flex-end; gap: 2px; }
.wifi-bars i { flex: 1; border-radius: 1px; background: var(--hair); height: 4px; }
.wifi-bars i:nth-child(2) { height: 7px; }
.wifi-bars i:nth-child(3) { height: 11px; }
.wifi-bars i:nth-child(4) { height: 15px; }
.wifi-bars i.on { background: var(--accent); }
.wifi-row-body { padding: 0 12px 12px; display: flex; flex-direction: column; gap: 8px; }
.wifi-psk-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.wifi-psk, .wifi-manual-name, .wifi-manual-psk { width: 100%; min-width: 0; height: 44px; padding: 0 12px;
  border: 1px solid var(--hair-strong); border-radius: var(--radius); background: var(--panel); color: var(--text); font: inherit; font-size: 16px; }
.wifi-show { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; color: var(--text-2); }
.wifi-btn { min-height: 40px; padding: 0 16px; border: 1px solid var(--hair); border-radius: var(--radius); background: var(--panel-2);
  color: var(--text); font: inherit; font-size: 14px; }
.wifi-btn.primary { background: var(--accent); color: var(--paper); border-color: var(--accent); font-weight: 600; }
.wifi-btn:disabled { opacity: .45; }
.wifi-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.wifi-note { font-size: 13px; color: var(--text-2); line-height: 1.5; }
.wifi-msg { font-size: 13px; line-height: 1.5; color: var(--danger); }
.wifi-switching { padding: 12px 14px; border-radius: var(--radius); background: var(--warn-soft); box-shadow: inset 0 0 0 1px var(--saffron);
  font-size: 14px; line-height: 1.6; }
.wifi-switching b { display: block; margin-bottom: 4px; font-size: 15px; }
.wifi-switching p { margin: 2px 0 0; }
.wifi-now { display: flex; flex-direction: column; gap: 4px; font-size: 14px; line-height: 1.55; }
.wifi-now .big { font-size: 16px; font-weight: 600; }
.wifi-saved-row { display: flex; align-items: center; gap: 10px; min-height: 40px; padding: 4px 0; border-top: 1px solid var(--line); }
.wifi-saved-row .wifi-name { font-size: 15px; }
.wifi-flow { margin-top: 4px; padding-top: 12px; border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 10px; }
.wifi-open { margin: 4px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.6; color: var(--text-2); }
.wifi-open li { overflow-wrap: anywhere; }
.wifi-finding .wifi-find-urls { font-size: 14px; color: var(--text); }
.wifi-finding .wifi-find-urls a { font-weight: 600; }
.duck-addr-row { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 4px 0; border-top: 1px solid var(--line); }
.duck-addr-row .wifi-name { font-size: 15px; }
.duck-addr-row .wifi-name a { overflow-wrap: anywhere; }
@media (max-width: 700px) {
  .wifi-card, .wifi-settings { padding: 14px 14px; }
  .wifi-net-head { min-height: 52px; padding: 10px 12px; }
  .wifi-btn { min-height: 44px; }
  .wifi-psk, .wifi-manual-name, .wifi-manual-psk { height: 46px; }
}
`;
if (typeof document !== 'undefined' && !document.getElementById('wifi-css')) {
  const s = document.createElement('style');
  s.id = 'wifi-css';
  s.textContent = CSS;
  document.head.appendChild(s);
}

const SEC = { open: 'Open', wep: 'WEP', wpa_psk: 'WPA2', wpa3_sae: 'WPA3', enterprise: 'Enterprise' };
const secLabel = (sec) => SEC[sec] || (sec ? 'Encrypted' : '');
const pskFormatOk = (v) => v === '' || (v.length >= 8 && v.length <= 63) || /^[0-9a-fA-F]{64}$/.test(v);
const pskOk = (v) => v !== '' && pskFormatOk(v);

function Bars({ signal }) {
  const n = typeof signal === 'number' ? (signal >= 75 ? 4 : signal >= 50 ? 3 : signal >= 25 ? 2 : 1) : 0;
  return html`<span class="wifi-bars" title=${typeof signal === 'number' ? 'Signal ' + signal : 'Signal unknown'}
    aria-label=${typeof signal === 'number' ? 'Signal ' + signal : 'Signal unknown'}>
    ${[1, 2, 3, 4].map((i) => html`<i class=${i <= n ? 'on' : ''}></i>`)}</span>`;
}

function minutesSince(at) {
  if (typeof at !== 'number') return null;
  return Math.max(1, Math.round((Date.now() / 1000 - at) / 60));
}

export function openLines(net) {
  const host = net.hostname || 'duck-xxxx';
  const proto = typeof location !== 'undefined' && location.protocol === 'https:' ? 'https' : 'http';
  return [
    `Windows: ${net.url_host || `${proto}://${host}:8000/`} (works with proxy enabled)`,
    `iPhone / Mac: ${net.url_local || `${proto}://${host}.local:8000/`}`,
    `Android or fallback: find ${host} IP in your router device list`,
    'If using Clash TUN mode, set LAN (192.168.0.0/16) to Direct',
  ];
}

export const FIND_EVERY_MS = 2000;
export const FIND_GIVEUP_MS = 3 * 60 * 1000;
export const FIND_PROBE_MS = 6000;
export const FIND_SILENT_MS = 5000;

export function findUrls(net) {
  const host = (net && net.hostname) || 'duck-xxxx';
  const proto = typeof location !== 'undefined' && location.protocol === 'https:' ? 'https' : 'http';
  const urls = [(net && net.url_host) || `${proto}://${host}:8000/`, (net && net.url_local) || `${proto}://${host}.local:8000/`];
  return urls.filter((u, i) => urls.indexOf(u) === i);
}

export function probeImage(url, timeoutMs = FIND_PROBE_MS) {
  return new Promise((resolve) => {
    const img = new Image();
    let done = false;
    const end = (ok) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      img.onload = null; img.onerror = null;
      resolve(ok);
    };
    const timer = setTimeout(() => end(false), timeoutMs);
    img.onload = () => end(true);
    img.onerror = () => end(false);
    img.src = `${url}probe.svg?t=${Date.now()}`;
  });
}

export function startFind({ urls, onFound, onGiveUp, probe = probeImage, every = FIND_EVERY_MS, giveUp = FIND_GIVEUP_MS,
  now = () => Date.now(), timers = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) } }) {
  const t0 = now();
  const inFlight = new Set();
  let stopped = false;
  let timer = null;
  const stop = () => {
    stopped = true;
    if (timer != null) timers.clearTimeout(timer);
    timer = null;
  };
  const tick = () => {
    timer = null;
    if (stopped) return;
    if (now() - t0 >= giveUp) { stop(); if (onGiveUp) onGiveUp(); return; }
    for (const url of urls) {
      if (inFlight.has(url)) continue;
      inFlight.add(url);
      Promise.resolve().then(() => probe(url)).then((ok) => {
        inFlight.delete(url);
        if (ok === true && !stopped) { stop(); if (onFound) onFound(url); }
      }, () => { inFlight.delete(url); });
    }
    timer = timers.setTimeout(tick, every);
  };
  tick();
  return { stop, get stopped() { return stopped; } };
}

function confirmLines(net, ssid, variant) {
  const back = variant === 'switch'
    ? 'If connection fails, this page will disconnect and the hotspot will return so you can check the reason.'
    : `If connection fails, reconnect to ${net.hotspot && net.hotspot.ssid ? net.hotspot.ssid : 'the duck hotspot'} to see the reason.`;
  const first = variant === 'switch'
    ? `Duck will disconnect current Wi-Fi and connect to "${ssid}". This page will disconnect.`
    : `Duck will stop its hotspot and connect to "${ssid}". Your phone will disconnect.`;
  return [first, 'Once connected, join the same Wi-Fi and open:', ...openLines(net).map((l) => `· ${l}`), back];
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fallback */ }
  try {
    const t = document.createElement('textarea');
    t.value = text;
    t.setAttribute('readonly', '');
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.appendChild(t);
    t.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(t);
    return ok;
  } catch { return false; }
}

function LastTry({ net }) {
  const lt = net.last_try;
  if (!lt || lt.ok !== false) return null;
  const why = lt.reason_text || 'Connection failed';
  const ssid = net.hotspot && net.hotspot.ssid ? net.hotspot.ssid : 'duck hotspot';
  return html`<div class="wifi-last-try" role="alert">Last attempt for "${lt.ssid || 'Wi-Fi'}" failed: ${why}
    ${net.mode === 'hotspot' ? html`<div>Hotspot is back; reconnect to ${ssid} to inspect.</div>` : null}</div>`;
}

function Switching({ net, ssid, variant }) {
  return html`<div class="wifi-switching" role="status">
    <b>Switching networks... this page will disconnect</b>
    ${confirmLines(net, ssid || 'Wi-Fi', variant).map((line) => html`<p>${line}</p>`)}
  </div>`;
}

const linkGone = (link) => link.status !== 'open' || (link.lastAt > 0 && performance.now() - link.lastAt > FIND_SILENT_MS);

function Finding({ net, find, variant }) {
  const ssid = find.ssid || 'Wi-Fi';
  if (find.phase === 'timeout') {
    return html`<div class="wifi-switching wifi-finding" role="status" data-phase="timeout">
      <b>Could not find duck automatically after 3 minutes</b>
      ${confirmLines(net, ssid, variant).slice(1).map((line) => html`<p>${line}</p>`)}
    </div>`;
  }
  return html`<div class="wifi-switching wifi-finding" role="status" data-phase="finding">
    <b>Connecting duck to "${ssid}"...</b>
    <p>Switch this device back to "${ssid}"; Studio will redirect automatically when found.</p>
    <ul class="wifi-open wifi-find-urls">${find.urls.map((u) => html`<li><a href=${u}>${u}</a>${/\.local[:/]/.test(u) ? ' (iPhone / Mac)' : ' (Windows)'}</li>`)}</ul>
    <p>Fallback: check ${net.hostname || 'duck-xxxx'} IP in your router.</p>
  </div>`;
}

export function WifiFlow({ net, link, variant = 'card' }) {
  const [scan, setScan] = useState({ networks: [], loading: true, error: null, cached: false, scan_at: null });
  const [sel, setSel] = useState(null);
  const [psk, setPsk] = useState('');
  const [show, setShow] = useState(false);
  const [manual, setManual] = useState(false);
  const [manualName, setManualName] = useState('');
  const [manualPsk, setManualPsk] = useState('');
  const [manualShow, setManualShow] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [trying, setTrying] = useState(null);
  const [switching, setSwitching] = useState(false);
  const [startedAt, setStartedAt] = useState(0);
  const [msg, setMsg] = useState(null);
  const [find, setFind] = useState(null);
  const [linkUp, setLinkUp] = useState(link.status === 'open');
  useEffect(() => link.on((m) => { if (m.t === '_status') setLinkUp(m.status === 'open'); }), []);
  const gone = !linkUp || linkGone(link);

  const rescan = async () => {
    setScan((s) => ({ ...s, loading: true, error: null }));
    const r = await link.cmd('net.op', { op: 'scan' }, 25000);
    if (!r.ok) {
      setScan({ networks: [], loading: false, error: r.reason || r.error || 'Failed to scan Wi-Fi', cached: false, scan_at: null });
      return;
    }
    const d = r.data || {};
    setScan({ networks: Array.isArray(d.networks) ? d.networks : [], loading: false, error: null,
      cached: !!d.cached, scan_at: d.scan_at || null });
  };
  useEffect(() => {
    let alive = true;
    (async () => {
      await link.whenOpen();
      if (alive) await rescan();
    })();
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!switching && !find) return;
    const lt = net.last_try;
    if ((lt && typeof lt.at === 'number' && startedAt && lt.at * 1000 >= startedAt - 2000)
      || (!net.busy && startedAt && Date.now() - startedAt > 12000)) { setSwitching(false); setFind(null); }
  }, [net.busy, net.last_try ? net.last_try.at : null]);

  useEffect(() => {
    if (!find) return;
    if (gone && find.phase === 'wait') setFind({ ...find, phase: 'finding' });
    else if (!gone && find.phase !== 'wait') setFind({ ...find, phase: 'wait' });
  }, [gone, find]);
  useEffect(() => {
    if (!find || find.phase !== 'finding') return undefined;
    const f = startFind({
      urls: find.urls,
      onFound: (url) => { if (linkGone(link)) location.href = url; },
      onGiveUp: () => setFind((x) => (x ? { ...x, phase: 'timeout' } : x)),
    });
    return () => f.stop();
  }, [find ? find.phase : null]);

  useEffect(() => {
    if (!confirm) return;
    const e = document.querySelector('.wifi-confirm-wrap');
    if (e && e.scrollIntoView) e.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [confirm]);

  const pick = (n) => {
    if (sel === n.ssid) { setSel(null); setPsk(''); return; }
    setSel(n.ssid); setPsk(''); setShow(false); setMsg(null);
  };
  const ask = (ssid, pass, locked = false) => {
    if (!ssid) { setMsg('Enter Wi-Fi SSID first'); return; }
    if (locked && !pass) { setMsg('This network requires a password'); return; }
    if (!pskFormatOk(pass)) { setMsg('Password must be 8–63 characters or 64 hex digits'); return; }
    setMsg(null);
    setConfirm({ ssid, psk: pass });
  };
  const start = async () => {
    if (!confirm) return;
    const { ssid, psk: pass } = confirm;
    setConfirm(null); setTrying(ssid); setStartedAt(Date.now()); setSwitching(true); setMsg(null);
    const wasOpen = link.status === 'open';
    setFind({ ssid, urls: findUrls(net), phase: 'wait' });
    const r = await link.cmd('net.op', { op: 'connect', ssid, psk: pass }, 20000);
    if (!r.ok && (r.reason || !wasOpen || link.status === 'open')) {
      setSwitching(false); setFind(null); setMsg(r.reason || r.error || 'Failed to send connect request');
    }
  };

  const nets = scan.networks || [];
  const ago = scan.cached ? minutesSince(scan.scan_at) : null;
  const busy = switching || net.busy === 'connecting' || (!!find && find.phase !== 'wait');
  return html`<div class="wifi-flow">
    ${find && find.phase !== 'wait' ? html`<${Finding} net=${net} find=${find} variant=${variant} />`
      : busy ? html`<${Switching} net=${net} ssid=${trying || (net.last_try && net.last_try.ssid)} variant=${variant} />` : null}
    ${scan.error ? html`<div class="wifi-msg">${scan.error}</div>` : null}
    <div class="wifi-list">
      ${nets.map((n) => {
        const on = sel === n.ssid;
        const open = n.security === 'open';
        const needs = !open;
        const can = open || pskOk(psk);
        return html`<div class=${'wifi-net' + (on ? ' on' : '')} data-ssid=${n.ssid}>
          <button class="wifi-net-head" type="button" aria-expanded=${on ? 'true' : 'false'} onClick=${() => pick(n)}>
            <${Bars} signal=${n.signal} />
            <span class="wifi-name"><b>${n.ssid}</b><span class="wifi-sub">${secLabel(n.security)}${n.saved ? ' · Saved' : ''}</span></span>
            ${n.saved ? html`<span class="wifi-badge">Saved</span>` : null}
            ${open ? null : html`<span class="wifi-lock" title="${secLabel(n.security)}">🔒</span>`}
          </button>
          ${on ? html`<div class="wifi-row-body">
            ${needs ? html`<div class="wifi-psk-row">
              <input class="wifi-psk" type=${show ? 'text' : 'password'} value=${psk} autocomplete="new-password"
                placeholder="Wi-Fi password" onInput=${(e) => setPsk(e.target.value)} />
              <label class="wifi-show"><input type="checkbox" checked=${show} onChange=${(e) => setShow(e.target.checked)} /> Show</label>
            </div>${n.saved ? html`<div class="wifi-note">Saved network; configd requires re-entering the password when switching.</div>` : null}`
              : html`<div class="wifi-note">Open network (no password required).</div>`}
            <div class="wifi-actions"><button class="wifi-btn primary wifi-connect" type="button" disabled=${!can || busy}
              onClick=${() => ask(n.ssid, needs ? psk : '', needs)}>Connect</button></div>
          </div>` : null}
        </div>`;
      })}
    </div>
    <div class="wifi-manual">
      <button class="wifi-manual-head" type="button" aria-expanded=${manual ? 'true' : 'false'} onClick=${() => setManual(!manual)}>
        ${manual ? 'Hide manual entry' : 'Other network (enter SSID)...'}
      </button>
      ${manual ? html`<div class="wifi-row-body">
        <input class="wifi-manual-name" value=${manualName} placeholder="Network SSID" onInput=${(e) => setManualName(e.target.value)} />
        <input class="wifi-manual-psk" type=${manualShow ? 'text' : 'password'} value=${manualPsk}
          placeholder="Password (leave blank if open)" autocomplete="new-password" onInput=${(e) => setManualPsk(e.target.value)} />
        <label class="wifi-show"><input type="checkbox" checked=${manualShow} onChange=${(e) => setManualShow(e.target.checked)} /> Show</label>
        <div class="wifi-actions"><button class="wifi-btn primary wifi-manual-connect" type="button" disabled=${busy || !manualName}
          onClick=${() => ask(manualName, manualPsk)}>Connect</button></div>
      </div>` : null}
    </div>
    <div class="wifi-actions">
      <button class="wifi-btn wifi-rescan" type="button" disabled=${scan.loading || busy} onClick=${rescan}>
        ${scan.loading ? 'Scanning...' : 'Rescan'}</button>
      ${ago ? html`<span class="wifi-note">Scanned ${ago}m ago</span>` : null}
    </div>
    ${msg ? html`<div class="wifi-msg">${msg}</div>` : null}
    ${confirm ? html`<div class="wifi-confirm-wrap"><${ConfirmBox} cls="wifi-confirm" lines=${confirmLines(net, confirm.ssid, variant)}
      confirmLabel=${'Switch to "' + confirm.ssid + '"'} onCancel=${() => setConfirm(null)} onConfirm=${start} /></div>` : null}
  </div>`;
}

export function WifiCard({ net, link, secure }) {
  if (!net || !net.known || net.mode !== 'hotspot') return null;
  return html`<section class="board wifi-card" aria-label="Connect Wi-Fi">
    <h2>Connect Wi-Fi</h2>
    <p class="wifi-lead">Currently on duck hotspot (${net.hotspot && net.hotspot.ssid ? net.hotspot.ssid : 'Microduck'}).
      Select your Wi-Fi network below, then reconnect your device to the same network:</p>
    <ul class="wifi-open">${openLines(net).map((l) => html`<li>${l}</li>`)}</ul>
    <${LastTry} net=${net} />
    <${WifiFlow} net=${net} link=${link} />
    ${secure === false ? html`<div class="wifi-note" role="note">Hotspot password is public; set a web login password on the More page before outdoor demos.</div>` : null}
  </section>`;
}

export function WifiSettings({ net, link }) {
  const [flow, setFlow] = useState(false);
  const [scan, setScan] = useState(null);
  const [forget, setForget] = useState(null);
  const [msg, setMsg] = useState(null);
  const load = async () => {
    const r = await link.cmd('net.op', { op: 'scan' }, 25000);
    setScan(r.ok ? (r.data || { networks: [] }) : { networks: [], error: r.reason || r.error });
  };
  useEffect(() => {
    if (!net || !net.known) return undefined;
    let alive = true;
    (async () => { await link.whenOpen(); if (alive) await load(); })();
    return () => { alive = false; };
  }, [net && net.known]);
  if (!net || !net.known) return null;
  const saved = (scan && scan.networks ? scan.networks : []).filter((n) => n.saved);
  const doForget = async (ssid) => {
    setForget(null); setMsg(null);
    const r = await link.cmd('net.op', { op: 'forget', ssid }, 15000);
    if (!r.ok) { setMsg(r.reason || r.error || 'Forget failed'); return; }
    setMsg(r.data && r.data.removed === false ? `"${ssid}" was not saved` : `Forgot "${ssid}"`);
    load();
  };
  const current = net.mode === 'hotspot' ? 'Duck Hotspot' : (net.ssid || 'Disconnected');
  return html`<div class="board wifi-settings" aria-label="Wi-Fi Settings">
    <h2>Wi-Fi</h2>
    <${LastTry} net=${net} />
    <div class="wifi-now">
      <div class="big">Current: ${current}</div>
      ${net.mode === 'hotspot' ? html`<div>Hotspot ${net.hotspot && net.hotspot.ssid ? net.hotspot.ssid : '—'} · Password ${net.hotspot && net.hotspot.psk ? net.hotspot.psk : '—'}</div>`
        : net.mode === 'wifi' ? html`<div class="wifi-actions"><${Bars} signal=${net.signal} /><span>Signal ${net.signal ?? '—'}</span><span>IP ${net.ip4 || '—'}</span></div>`
          : html`<div>Not connected to Wi-Fi.</div>`}
      <div class="wifi-note">See Duck Addresses above for Studio URLs.</div>
    </div>
    <div class="wifi-actions"><button class="wifi-btn primary" type="button" onClick=${() => setFlow(!flow)}>
      ${flow ? 'Hide' : 'Switch Wi-Fi'}</button></div>
    ${flow ? html`<div class="wifi-flow"><${WifiFlow} net=${net} link=${link} variant="switch" /></div>` : null}
    <div>
      <div class="wifi-note">Saved Networks</div>
      ${scan && scan.error ? html`<div class="wifi-msg">${scan.error}</div>` : null}
      ${saved.length ? saved.map((n) => html`<div class="wifi-saved-row">
        <span class="wifi-name">${n.ssid}</span>
        ${n.ssid === net.ssid ? html`<span class="wifi-badge">Active</span>` : null}
        <button class="wifi-btn wifi-forget" type="button" data-ssid=${n.ssid}
          onClick=${() => n.ssid === net.ssid ? setForget(n.ssid) : doForget(n.ssid)}>Forget</button>
      </div>`) : html`<div class="wifi-note">${scan ? 'No saved networks.' : 'Loading...'}</div>`}
    </div>
    ${msg ? html`<div class="wifi-note">${msg}</div>` : null}
    ${forget ? html`<div class="wifi-confirm-wrap"><${ConfirmBox} cls="wifi-confirm"
      lines=${[`Forgetting "${forget}" will disconnect the duck; hotspot will start after ~1m if no other Wi-Fi is available.`]}
      confirmLabel="Forget" onCancel=${() => setForget(null)} onConfirm=${() => doForget(forget)} /></div>` : null}
  </div>`;
}

export function DuckAddresses({ net }) {
  const [copied, setCopied] = useState(null);
  useEffect(() => {
    if (!copied) return undefined;
    const t = setTimeout(() => setCopied(null), 1500);
    return () => clearTimeout(t);
  }, [copied]);
  if (!net || !net.known) return null;
  const urls = net.urls || [];
  const hasIp = urls.some((u) => u.kind === 'ip');
  const https = net.url_local?.startsWith('https:');
  const copy = async (url) => setCopied({ url, ok: await copyText(url) });
  return html`<div class="board wifi-settings duck-addr" aria-label="Duck Addresses">
    <h2>Duck Addresses</h2>
    <p class="wifi-lead">${net.mode === 'hotspot' ? 'On duck hotspot: use the hotspot IP on your phone.'
      : 'On the same Wi-Fi network, pick the URL that works best for your OS:'}</p>
    ${urls.map((u) => html`<div class="duck-addr-row" data-kind=${u.kind}>
      <span class="wifi-name"><a href=${u.url}>${u.url}</a><span class="wifi-sub">${u.note}</span></span>
      <button class="wifi-btn duck-addr-copy" type="button" onClick=${() => copy(u.url)}>
        ${copied && copied.url === u.url ? (copied.ok ? 'Copied' : 'Copy failed; long-press link') : 'Copy'}</button>
    </div>`)}
    ${hasIp || https ? null
      : html`<div class="wifi-note">IP unknown; look up ${net.hostname || 'duck-xxxx'} in your router.</div>`}
    <div class="wifi-note">${https && !hasIp
      ? 'Use the hostname URL for HTTPS.'
      : 'Use the IP address on Android if .local does not resolve.'} Set LAN (192.168.0.0/16) to Direct if using a TUN proxy.</div>
  </div>`;
}
