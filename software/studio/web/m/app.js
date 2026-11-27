import { ShutdownButton } from '../shutdown-button.js';
import { ArmatureMark } from '../armature-mark.js';
// Mobile Studio Shell: Session → WebSocket → Header (Brand + E-Stop) → Page → TabBar.
import { html, render, useState, useEffect, useRef, useCallback } from '../vendor/preact-htm.module.js';
import { link, session, login, api, csrfToken } from './link.js';
import { Overview } from './overview.js';
import { VoicePage } from './voice.js';
import { GamepadPage } from './gamepad.js';
import { CameraPage } from './camera.js';
import { MorePage } from './more.js';
import { ModelsPage } from '../models.js';
import { ActionsPage } from '../actions.js';

const STALE_MS = 1500;
const THEME_KEY = 'armature-studio-theme';
function getTheme() {
  try { return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'; } catch { return 'dark'; }
}
function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode */ }
  return t;
}

const ICONS = {
  '/': html`<svg viewBox="0 0 24 24"><path d="M3 10.5 12 4l9 6.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/></svg>`,
  '/voice': html`<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="10" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/></svg>`,
  '/pad': html`<svg viewBox="0 0 24 24"><path d="M7 8h10a5 5 0 0 1 4.9 6l-.6 2.4A2.6 2.6 0 0 1 16 17.6L14.5 16h-5L8 17.6a2.6 2.6 0 0 1-5.3-1.2L2.1 14A5 5 0 0 1 7 8Z"/><path d="M7.5 12.5v2M6.5 13.5h2M16 12.5h.01M17.5 14h.01"/></svg>`,
  '/actions': html`<svg viewBox="0 0 24 24"><path d="M12 3 3 7.5v9L12 21l9-4.5v-9z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>`,
  '/more': html`<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>`,
};

const TABS = [
  { href: '/', label: 'Overview' },
  { href: '/voice', label: 'Voice' },
  { href: '/pad', label: 'Pad' },
  { href: '/actions', label: 'Actions' },
  { href: '/more', label: 'More' },
];

const KNOWN = new Set([...TABS.map((t) => t.href), '/pad', '/more/camera', '/models']);
const routeOf = () => {
  const r = '/' + location.hash.replace(/^#\/?/, '');
  return KNOWN.has(r) ? r : '/';
};

const NICK_KEY = 'duck.nickname';
const hostish = (s) => /^duck-[0-9a-z]{4,}$/i.test(s) || /^[A-Z0-9][A-Z0-9-]{2,}$/.test(s);

function nickStored() {
  try { return localStorage.getItem(NICK_KEY) || ''; } catch { return ''; }
}

function Head({ name, dot, onEstop, busy, onNotice }) {
  return html`<header class="m-head">
    <div class="m-head-in">
      <${ShutdownButton} api=${api} onNotice=${onNotice} />
      <div class="m-duck">
        <${ArmatureMark} size=${18} />
        <span class=${'m-dot ' + dot} aria-hidden="true"></span>
        <span class="m-duck-name">${name}</span>
      </div>
      <span class="m-head-gap"></span>
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
      <button class="m-estop" onClick=${onEstop} disabled=${busy}
        aria-label="E-Stop: relax all servos immediately">E-Stop</button></div>
    </div>
  </header>`;
}

function Login({ onDone }) {
  const [pw, setPw] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setErr(null);
    const r = await login(pw, true);
    setBusy(false);
    if (r.ok) onDone(); else setErr(r.error);
  };
  return html`<form class="m-login" onSubmit=${submit}>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      <${ArmatureMark} size=${24} />
      <h1 style="margin:0">Armature Studio</h1>
    </div>
    <p>Enter the admin password to connect (remembered for 30 days).</p>
    <input type="password" inputmode="text" autocomplete="current-password" placeholder="Admin password"
      value=${pw} onInput=${(e) => setPw(e.currentTarget.value)} autoFocus />
    ${err ? html`<div class="m-banner bad" style="margin-top:12px">${err}</div>` : null}
    <button class="m-btn primary" type="submit" disabled=${busy || !pw}>${busy ? 'Signing in…' : 'Sign In'}</button>
  </form>`;
}

function App() {
  const [phase, setPhase] = useState('boot');
  const [hello, setHello] = useState(null);
  const [st, setSt] = useState(null);
  const [stAt, setStAt] = useState(0);
  const [pad, setPad] = useState(null);
  const [conn, setConn] = useState('connecting');
  const [route, setRoute] = useState(routeOf());
  const [now, setNow] = useState(performance.now());
  const [toast, setToast] = useState(null);
  const [estopBusy, setEstopBusy] = useState(false);
  const [theme, setTheme] = useState(getTheme);
  const toastTimer = useRef(null);

  const changeTheme = (t) => setTheme(applyTheme(t));

  useEffect(() => { applyTheme(getTheme()); }, []);

  const say = useCallback((text, bad = false) => {
    setToast({ text, bad });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), bad ? 3600 : 2200);
  }, []);

  useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const r = await session();
        if (dead) return;
        setPhase(r.status === 200 ? 'ready' : r.status === 401 ? 'login' : 'ready');
      } catch { if (!dead) setPhase('ready'); }
    })();
    return () => { dead = true; };
  }, []);

  useEffect(() => {
    if (phase !== 'ready') return;
    const off = link.on((m) => {
      if (m.t === '_status') { setConn(m.status); if (m.status === 'auth') setPhase('login'); return; }
      if (m.t === 'hello') { setHello(m); return; }
      if (m.t === 'state') { setSt(m); setStAt(performance.now()); return; }
      if (m.t === 'pad') { setPad(m); return; }
    });
    link.connect();
    const tick = setInterval(() => setNow(performance.now()), 500);
    return () => { off(); clearInterval(tick); };
  }, [phase]);

  useEffect(() => {
    const onHash = () => setRoute(routeOf());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    link.setRoute(route);
    link.setPadVisible(route === '/pad' || route === '/voice');
  }, [route]);

  const live = conn === 'open' && st && now - stAt < STALE_MS;
  const age = stAt ? now - stAt : Infinity;
  const [nick, setNick] = useState(nickStored());
  const label = hello?.duck?.label;
  const name = nick || (label && !hostish(label) ? label : 'Microduck');
  const rename = useCallback((v) => {
    const clean = String(v || '').trim().slice(0, 12);
    setNick(clean);
    try { clean ? localStorage.setItem(NICK_KEY, clean) : localStorage.removeItem(NICK_KEY); } catch { /* private mode */ }
  }, []);

  const doEstop = useCallback(async () => {
    if (estopBusy) return;
    setEstopBusy(true);
    const r = await link.cmd('relax', {}, 7000);
    setEstopBusy(false);
    if (r.ok) say('E-Stop sent — servos relaxed');
    else say(r.reason || r.error || 'E-Stop failed', true);
  }, [estopBusy, say]);

  if (phase === 'boot') {
    return html`<div class="m-app"><div class="m-empty">Connecting to Microduck…</div></div>`;
  }
  if (phase === 'login') {
    return html`<div class="m-app"><${Login} onDone=${() => { setPhase('ready'); }} /></div>`;
  }

  let dot = 'warn';
  if (conn === 'open') dot = live ? 'on' : 'warn';
  else if (conn === 'closed' || conn === 'auth') dot = 'bad';

  let page = null;
  if (route === '/') page = html`<${Overview} st=${st} pad=${pad} live=${live} age=${age} name=${name} say=${say} />`;
  else if (route === '/voice') page = html`<${VoicePage} st=${st} pad=${pad} live=${live} name=${name} say=${say} />`;
  else if (route === '/pad') page = html`<${GamepadPage} st=${st} live=${live} say=${say} />`;
  else if (route === '/models') page = html`<${ModelsPage} api=${api} csrf=${csrfToken()} link=${link} live=${live} />`;
  else if (route === '/actions') page = html`<${ActionsPage} api=${api} csrf=${csrfToken()} link=${link} plazaUrl="https://fanhao375.github.io/microduck-replica/motion-plaza/" />`;
  else if (route === '/more/camera') page = html`<${CameraPage} live=${live} say=${say} />`;
  else page = html`<${MorePage} hello=${hello} st=${st} conn=${conn} say=${say} nick=${name} rename=${rename} theme=${theme} onThemeChange=${changeTheme} />`;

  return html`<div class="m-app">
    <${Head} name=${name} dot=${dot} onEstop=${doEstop} busy=${estopBusy} onNotice=${say} />
    <main class="m-main">
      ${conn !== 'open' ? html`<div class="m-banner bad">
        <span>${conn === 'closed' ? 'Disconnected from robot' : 'Connecting to robot…'}</span>
        <button class="act" onClick=${() => link.reconnect()}>Reconnect</button>
      </div>` : null}
      ${conn === 'open' && !live ? html`<div class="m-banner">
        <span>${isFinite(age) ? Math.round(age / 1000) + 's since last update' : 'Waiting for telemetry'} — walking pauses automatically.</span>
      </div>` : null}
      ${page}
    </main>
    <nav class="m-tabs" aria-label="Navigation">
      <div class="m-tabs-in">
        ${TABS.map((t) => {
          const on = t.href === route || (t.href === '/more' && route.startsWith('/more/'));
          return html`<a class="m-tab" href=${'#' + t.href} aria-current=${on ? 'page' : null}>
            ${ICONS[t.href]}<span>${t.label}</span>
          </a>`;
        })}
      </div>
    </nav>
    ${toast ? html`<div class=${'m-toast' + (toast.bad ? ' bad' : '')} role="status">${toast.text}</div>` : null}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));

window.addEventListener('unhandledrejection', (e) => { console.warn('Unhandled promise rejection', e.reason); });
