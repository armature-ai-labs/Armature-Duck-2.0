import { CertificateDownload } from '../certificate-download.js';
// Mobile More page: Appearance theme, voice source, hotspot, desktop links, and diagnostics.
import { html, useState, useEffect } from '../vendor/preact-htm.module.js';
import { link, api } from './link.js';
import { Maintenance } from '../maintenance.js';

const SRC_KEY = 'duck.voice.source';
const HOT_KEY = 'duck.hotspot';

function loadHotspot() {
  try { return JSON.parse(localStorage.getItem(HOT_KEY) || 'null') || null; } catch { return null; }
}

function saveHotspot(h) {
  try { localStorage.setItem(HOT_KEY, JSON.stringify(h)); } catch { /* private mode */ }
}

const PAGES = [
  { href: '#/more/camera', label: 'Camera', desc: 'Mobile live WebRTC feed' },
  { href: '/#/tof', label: 'ToF 3D', desc: '8×8 depth frustum & matrix' },
  { href: '/#/servos', label: 'Servos', desc: '15-servo status & calibration' },
  { href: '/#/imu', label: 'IMU', desc: 'Attitude & mount calibration' },
  { href: '/#/logs', label: 'Logs', desc: 'Recent Studio events' },
  { href: '/#/settings', label: 'Settings', desc: 'Password, Wi-Fi, updates, AI' },
];

const SOURCES = [
  { id: 'auto', label: 'Auto', desc: 'Use phone mic when HTTPS is available, otherwise robot mic' },
  { id: 'phone', label: 'Phone Mic', desc: 'Requires HTTPS' },
  { id: 'board', label: 'Robot Mic', desc: 'On-device offline recognition' },
];

export function MorePage({ hello, st, conn, say, nick, rename, theme, onThemeChange }) {
  const [src, setSrc] = useState('auto');
  const [draft, setDraft] = useState('');
  useEffect(() => { try { setSrc(localStorage.getItem(SRC_KEY) || 'auto'); } catch { /* private mode */ } }, []);
  const pick = (id) => {
    setSrc(id);
    try { localStorage.setItem(SRC_KEY, id); } catch { /* private mode */ }
    say(`Voice source set to "${SOURCES.find((s) => s.id === id).label}"`);
  };
  const saveName = (e) => {
    e.preventDefault();
    rename(draft);
    setDraft('');
    say(draft.trim() ? `Renamed to "${draft.trim()}"` : 'Reset name to "Microduck"');
  };

  const net = st?.net;
  const hs = net?.hotspot;
  const [hot, setHot] = useState(loadHotspot);
  const [outdoorAsk, setOutdoorAsk] = useState(false);
  const [netBusy, setNetBusy] = useState(false);
  const [netNote, setNetNote] = useState(null);
  useEffect(() => {
    if (hs && hs.ssid) {
      const clean = { ssid: hs.ssid, psk: hs.psk || 'microduck', ip: hs.ip || '10.42.0.1',
                      url: net?.url_hotspot || '' };
      setHot(clean);
      saveHotspot(clean);
    }
  }, [hs?.ssid, hs?.psk, hs?.ip, net?.url_hotspot]);

  const netBusyNow = netBusy || !!net?.busy;
  const netOp = async (op) => {
    if (netBusyNow) return;
    setNetBusy(true);
    setNetNote(null);
    const r = await link.cmd('net.op', { op }, 45000);
    setNetBusy(false);
    if (r.ok) {
      if (op === 'hotspot-up') setOutdoorAsk(false);
      setNetNote(r.reason || 'Operation started');
      return;
    }
    const dropped = r.error === 'Disconnected from robot' || r.error === 'Robot did not respond (timeout)';
    if (dropped) {
      if (op === 'hotspot-up') setOutdoorAsk(false);
      setNetNote(op === 'hotspot-up'
        ? 'Connection dropped — hotspot is starting. Follow the 3 steps below to reconnect.'
        : 'Connection dropped — hotspot is closing. Reconnect your phone to home Wi-Fi.');
      return;
    }
    setNetNote(r.reason || r.error || 'Command failed');
  };

  const secure = window.isSecureContext !== false;
  const version = hello?.version || '—';
  const duck = hello?.duck || {};
  const hotspot = hs || hot;
  const hotspotSsid = hotspot?.ssid || 'Microduck-xxxx';
  const hotspotPsk = hotspot?.psk || 'microduck';
  const hotspotUrl = net?.url_hotspot || hotspot?.url
    || `${location.protocol}//${hotspot?.ip || '10.42.0.1'}:8000/m`;
  const atHotspot = net?.mode === 'hotspot';

  return html`<div>
    <h2 class="m-h2">Appearance</h2>
    <div class="m-card m-pad">
      <div class="m-sub" style="margin-bottom:8px">Armature Studio Theme</div>
      <div class="m-seg" role="group" aria-label="Theme">
        <button aria-pressed=${theme === 'dark' ? 'true' : 'false'} onClick=${() => onThemeChange('dark')}>Obsidian Dark</button>
        <button aria-pressed=${theme === 'light' ? 'true' : 'false'} onClick=${() => onThemeChange('light')}>Warm Cream Light</button>
      </div>
    </div>

    <h2 class="m-h2">Robot Nickname</h2>
    <div class="m-card m-pad">
      <div class="m-sub">Current name: "${nick}" (stored locally on this phone).</div>
      <form class="m-type" onSubmit=${saveName}>
        <input placeholder="New name (blank resets to Microduck)" value=${draft} maxlength="12"
          onInput=${(e) => setDraft(e.currentTarget.value)} />
        <button class="m-btn primary" type="submit">Save</button>
      </form>
    </div>

    <h2 class="m-h2">Voice Input</h2>
    <div class="m-card">
      <ul class="m-list">
        ${SOURCES.map((s) => html`<li>
          <button class="row" style="width:100%;text-align:left" onClick=${() => pick(s.id)}>
            <span style="flex:1">
              <span style="display:block">${s.label}</span>
              <span style="display:block;font-size:12px;color:var(--text-3)">${s.desc}</span>
            </span>
            <span class="chev">${src === s.id ? '✓' : ''}</span>
          </button>
        </li>`)}
      </ul>
    </div>
    ${!secure ? html`<div class="m-banner">
      HTTP mode blocks browser microphone access. Install the HTTPS certificate below and open via HTTPS, or use the robot mic / text input.
    </div>` : null}

    <h2 class="m-h2">HTTPS Certificate (for Phone Mic)</h2>
    <div class="m-card m-pad">
      <div class="m-sub">Trust the robot's local HTTPS CA to enable Phone Mic in your mobile browser.</div>
      <${CertificateDownload} />
      <ol class="m-tip" style="padding-left:18px;margin:10px 0 0">
        <li>iOS: Download → Settings → Profile Downloaded → Install; then Settings → General → About → Certificate Trust Settings → enable Microduck Local CA.</li>
        <li>Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate.</li>
        <li>Reload this page via HTTPS.</li>
      </ol>
    </div>

    ${net?.known ? html`
      <h2 class="m-h2">Outdoor Hotspot</h2>
      <div class="m-card m-pad">
        <div class="m-sub">
          ${atHotspot ? 'Connected to the robot hotspot.'
            : net.mode === 'wifi' ? `Connected to Wi-Fi${net.ssid ? ` (${net.ssid})` : ''}.`
            : 'Robot is not connected to Wi-Fi.'}
          Enable hotspot mode outdoors when no router is available.
        </div>
        <div class="m-kv"><span class="k">SSID</span><span class="v">${hotspotSsid}</span></div>
        <div class="m-kv"><span class="k">Password</span><span class="v">${hotspotPsk}</span></div>
        <div class="m-kv"><span class="k">Mobile URL</span><span class="v">${hotspotUrl}</span></div>
        ${atHotspot ? html`
          <div class="m-btns" style="margin-top:12px">
            <button class="m-btn" onClick=${() => netOp('hotspot-down')} disabled=${netBusyNow}>Disable Hotspot & Rejoin Wi-Fi</button>
          </div>
          <p class="m-tip">After disabling, reconnect your phone to your home Wi-Fi.</p>
        ` : outdoorAsk ? html`
          <div class="m-banner">
            <b>Your connection will drop; reconnect in 3 steps:</b>
            <ol style="padding-left:18px;margin:8px 0 0">
              <li>Join Wi-Fi "${hotspotSsid}" (password: ${hotspotPsk})</li>
              <li>Keep connection if warned "No Internet" and pause cellular data if needed</li>
              <li>Open ${hotspotUrl}</li>
            </ol>
          </div>
          <div class="m-btns" style="margin-top:12px">
            <button class="m-btn primary" onClick=${() => netOp('hotspot-up')} disabled=${netBusyNow}>Confirm Hotspot</button>
            <button class="m-btn" onClick=${() => setOutdoorAsk(false)} disabled=${netBusyNow}>Cancel</button>
          </div>
        ` : html`
          <div class="m-btns" style="margin-top:12px">
            <button class="m-btn primary" onClick=${() => setOutdoorAsk(true)} disabled=${netBusyNow}>Enable Hotspot (drops current Wi-Fi)</button>
          </div>
        `}
        ${netNote ? html`<div class="m-banner" style="margin-top:12px">${netNote}</div>` : null}
        <p class="m-tip">Set an admin password in Settings before public demos.</p>
      </div>
    ` : null}

    <h2 class="m-h2">Engineering Pages</h2>
    <div class="m-card">
      <ul class="m-list">
        ${PAGES.map((p) => html`<li><a href=${p.href}>
          <span>${p.label}</span><span class="desc">${p.desc}</span><span class="chev">›</span>
        </a></li>`)}
      </ul>
    </div>

    <${Maintenance} api=${api} />
    <h2 class="m-h2">Device Info</h2>
    <div class="m-card">
      <div class="m-kv"><span class="k">Nickname</span><span class="v">${nick}</span></div>
      <div class="m-kv"><span class="k">Hostname</span><span class="v">${duck.label || '—'}</span></div>
      <div class="m-kv"><span class="k">Studio Version</span><span class="v">${version}</span></div>
      <div class="m-kv"><span class="k">WebSocket</span><span class="v">${conn === 'open' ? 'Connected' : conn === 'closed' ? 'Disconnected' : 'Connecting'}</span></div>
      <div class="m-kv"><span class="k">Posture</span><span class="v">${st?.posture || '—'}</span></div>
      <div class="m-kv"><span class="k">Host</span><span class="v">${location.host}</span></div>
    </div>
    <div class="m-btns" style="margin-top:12px">
      <button class="m-btn" onClick=${() => link.reconnect()}>Reconnect</button>
      <a class="m-btn" href="/">Open Desktop Studio</a>
    </div>

    <p class="m-tip" style="margin-top:22px">
      E-Stop is always available at the top-right. After relaxing, tap Stand to re-enable servo torque.
    </p>
  </div>`;
}
