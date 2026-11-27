// Mobile controller WebSocket & HTTP transport layer (mirrors web/app.js protocol).

export const link = {
  ws: null,
  seq: 0,
  padSeq: 0,
  pending: new Map(),
  listeners: new Set(),
  status: 'idle',        // idle / connecting / open / closed / auth
  backoff: 1000,
  route: '/',
  padVisible: false,
  padCapture: false,
  _hb: null,

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
  emit(m) { for (const fn of this.listeners) fn(m); },
  setStatus(s) { this.status = s; this.emit({ t: '_status', status: s }); },

  connect() {
    clearTimeout(this._retry);
    clearInterval(this._hb);
    try { this.ws && this.ws.close(); } catch { /* already closed */ }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    let opened = false;
    this.setStatus('connecting');
    ws.onopen = () => {
      opened = true;
      this.backoff = 1000;
      this.setStatus('open');
      this.heartbeat();
      this._hb = setInterval(() => this.heartbeat(), 1000);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.t === 'hb') this.heartbeat();
      if (m.t === 'result' && this.pending.has(m.id)) {
        this.pending.get(m.id)(m);
        this.pending.delete(m.id);
      }
      this.emit(m);
    };
    ws.onclose = async (ev) => {
      if (this.ws !== ws) return;
      clearInterval(this._hb);
      for (const done of this.pending.values()) done({ ok: false, error: 'Disconnected from robot' });
      this.pending.clear();
      if (ev.code === 4401) { this.setStatus('auth'); return; }
      if (!opened) {
        try {
          const r = await api('/api/session', undefined, 2000);
          if (r.status === 401) { this.setStatus('auth'); return; }
        } catch { /* Studio offline */ }
        if (this.ws !== ws) return;
      }
      this.setStatus('closed');
      this._retry = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  },

  reconnect() { this.backoff = 1000; this.connect(); },

  whenOpen() {
    if (this.status === 'open') return Promise.resolve();
    return new Promise((resolve) => {
      const off = this.on((m) => { if (m.t === '_status' && m.status === 'open') { off(); resolve(); } });
      setTimeout(() => { off(); resolve(); }, 6000);
    });
  },

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) { this.ws.send(JSON.stringify(obj)); return true; }
    return false;
  },

  setRoute(route) { this.route = route; this.heartbeat(); },
  setPadVisible(v) { this.padVisible = Boolean(v); this.heartbeat(); },
  setPadCapture(v) { this.padCapture = Boolean(v); this.heartbeat(); },
  nextPadSeq() { return ++this.padSeq; },

  heartbeat() {
    this.send({
      t: 'hb', seq: ++this.seq,
      visible: document.visibilityState === 'visible',
      route: this.route,
      pad_visible: this.padVisible || this.padCapture,
    });
  },

  cmd(name, args = {}, timeoutMs = 15000) {
    const id = `m${++this.seq}`;
    return new Promise((resolve) => {
      if (!this.send({ t: 'cmd', id, name, args })) { resolve({ ok: false, error: 'Disconnected from robot' }); return; }
      this.pending.set(id, resolve);
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); resolve({ ok: false, error: 'Robot did not respond (timeout)' }); }
      }, timeoutMs);
    });
  },
};

let csrf = '';

export async function api(path, body, timeoutMs = 10000) {
  const opt = body === undefined
    ? { credentials: 'same-origin' }
    : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf }, body: JSON.stringify(body) };
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let r, data = {};
  try {
    r = await fetch(path, { ...opt, signal: ctl.signal });
    try { data = await r.json(); } catch { /* non-JSON (e.g. 204) */ }
  } finally { clearTimeout(timer); }
  return { status: r.status, ...data };
}

export function setCsrf(value) { csrf = value || ''; }

export function csrfToken() { return csrf; }

export async function session() {
  const r = await api('/api/session', undefined, 4000);
  if (r.status === 200) setCsrf(r.csrf);
  return r;
}

export async function login(password, remember = true) {
  const r = await api('/api/login', { password, remember }, 8000);
  if (r.status === 200 || r.ok) { const s = await session(); return s.status === 200 ? { ok: true } : { ok: false, error: 'Sign-in did not persist' }; }
  return { ok: false, error: r.error || 'Incorrect password' };
}
