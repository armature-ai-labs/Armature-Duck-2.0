import { html, useEffect, useRef, useState } from './vendor/preact-htm.module.js';
import { TwoStep } from './confirm.js';

export function AiSshEntry() {
  const go = e => {
    if (location.hash !== '#/settings/ssh') return;
    e.preventDefault();
    const section = document.getElementById('ssh-guide');
    section?.scrollIntoView({ block: 'start' });
    section?.focus({ preventScroll: true });
  };
  return html`<aside class="board ai-ssh-entry" aria-label="AI Assistant SSH Guide">
    <div><b>Need an AI assistant to debug?</b><p>Configure SSH here so your workstation AI coding assistant can inspect the board without a monitor.</p></div>
    <a class="access-button primary" href="#/settings/ssh" onClick=${go}>Connect AI via SSH<span aria-hidden="true"> →</span></a>
  </aside>`;
}

function StepTitle({ number, title, state, titleId }) {
  return html`<div class="access-step-title"><h3 id=${titleId}><span class="access-step-number" aria-hidden="true">${number}</span>${title}</h3>
    <span class=${'access-state' + (state === 'Configured' || state === 'Ready' ? ' done' : '')} data-access-state>${state}</span></div>`;
}

function WebPassword({ api, hasPassword, onSaved, refreshSession }) {
  const [pw, setPw] = useState(''), [again, setAgain] = useState('');
  const [busy, setBusy] = useState(false), [msg, setMsg] = useState(null), [err, setErr] = useState(null);
  const submit = async e => {
    e.preventDefault(); setErr(null); setMsg(null);
    if (pw.length < 6) { setErr('Web login password must be at least 6 characters'); return; }
    if (pw !== again) { setErr('Passwords do not match'); return; }
    setBusy(true);
    try {
      const r = await api('/api/password', { action: 'set', password: pw, confirm: again });
      if (!r.ok) { setErr(r.error || 'Failed to save web password'); return; }
      setPw(''); setAgain('');
      try { await refreshSession(); }
      catch { setMsg('Web password saved. Refresh the page before configuring SSH.'); return; }
      onSaved(); setMsg('Web password saved; this browser stays signed in. Proceed to Step 2.');
    } catch { setErr('Cannot reach the duck; check network and retry'); }
    finally { setBusy(false); }
  };
  const clear = async () => {
    setErr(null); setMsg(null);
    try {
      const r = await api('/api/password', { action: 'clear' });
      if (!r.ok) { setErr(r.error || 'Failed to clear web password'); return; }
      location.reload();
    } catch { setErr('Cannot reach the duck; check network and retry'); }
  };
  return html`<section class="board access-step password-settings" aria-labelledby="web-password-title">
    <${StepTitle} number="1" title="Web Login Password" titleId="web-password-title" state=${hasPassword == null ? 'Loading' : hasPassword ? 'Configured' : 'Not Set'} />
    <p class="access-description">Protects the Studio web console. Required before enabling SSH access from the browser.</p>
    ${hasPassword == null ? html`<p role="status">Reading login status...</p>` : html`
      <details class="access-edit" open=${!hasPassword}>
        <summary>${hasPassword ? 'Configured (expand to change)' : 'Set Web Login Password'}</summary>
        <form onSubmit=${submit}>
          <p class="access-description">Minimum 6 characters. ${hasPassword ? 'Other browsers will need to sign in again.' : ''}</p>
          <label>${hasPassword ? 'New Web Password' : 'Web Password'}
            <input type="password" name="password" autocomplete="new-password" value=${pw} required minLength="6"
              onInput=${e => setPw(e.target.value)} /></label>
          <label>Confirm Web Password
            <input type="password" name="confirm" autocomplete="new-password" value=${again} required minLength="6"
              aria-describedby="web-confirm-help" onInput=${e => setAgain(e.target.value)} /></label>
          <p class="access-help" id="web-confirm-help">Re-enter the same password to confirm; normal sign-in only asks once.</p>
          <div class="access-actions"><button class="access-button primary" type="submit" disabled=${busy || !pw || !again}>
            ${busy ? 'Saving...' : hasPassword ? 'Update Web Password' : 'Save Web Password'}</button>
            ${hasPassword ? html`<${TwoStep} label="Clear Web Password" ask="Confirm Clear?" onConfirm=${clear} />` : null}</div>
        </form>
      </details>`}
    ${err ? html`<p role="alert" class="access-error">${err}</p>` : null}
    ${msg ? html`<p role="status" class="access-help">${msg}</p>` : null}
  </section>`;
}

export function sshConnection(ssh, net, hostname) {
  const hostOk = value => typeof value === 'string' && value.length <= 253 &&
    (/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(value) || /^\[?[a-f0-9]*:[a-f0-9:]+\]?$/i.test(value));
  const candidates = [net?.known ? net.ip_now : null, net?.known ? net.ip4 : null, hostname];
  const host = candidates.find(hostOk);
  const user = ssh?.user || 'radxa';
  if (!host || !/^[a-z_][a-z0-9_-]*$/i.test(user)) return null;
  const target = host.replace(/^\[|\]$/g, '');
  const cmd = `ssh ${user}@${target}`;
  const method = ssh?.password_login ? 'SSH system password (I will enter it when prompted)' : 'Registered SSH public key on this machine';
  return { host: target, user, cmd, note: `Connect to my Microduck board via SSH for read-only inspection first.\nHost: ${target}\nUser: ${user}\nPort: 22\nCommand: ${cmd}\nAuth: ${method}\nNote: Web login password is for Studio; SSH uses the system password or authorized key.` };
}

function SshSettings({ api, hasPassword, net }) {
  const [ssh, setSsh] = useState(null), [loading, setLoading] = useState(true), [loadErr, setLoadErr] = useState(null);
  const [pw, setPw] = useState(''), [again, setAgain] = useState(''), [key, setKey] = useState('');
  const [busy, setBusy] = useState(false), [msg, setMsg] = useState(null), [err, setErr] = useState(null);
  const [copyMsg, setCopyMsg] = useState(null);
  const [fallback, setFallback] = useState(null), fallbackRef = useRef(null), alive = useRef(true);
  const load = async () => {
    setLoading(true); setSsh(null); setFallback(null); setCopyMsg(null);
    try {
      const r = await api('/api/ssh', { action: 'status' }, 15000);
      if (!alive.current) return;
      if (r.ok) { setSsh(r.ssh); setLoadErr(null); }
      else setLoadErr(r.error || 'Failed to read SSH status');
    } catch { if (alive.current) setLoadErr('Cannot reach duck; check network and retry'); }
    finally { if (alive.current) setLoading(false); }
  };
  useEffect(() => { alive.current = true; load(); return () => { alive.current = false; }; }, []);
  useEffect(() => { if (fallbackRef.current) { fallbackRef.current.focus(); fallbackRef.current.select(); } }, [fallback]);
  const on = ssh?.password_login === true, keys = ssh?.key_list || [];
  const connection = sshConnection(ssh, net, location.hostname);
  const ready = !loading && ssh?.ssh_active === true && (on || keys.length > 0) && !!connection;
  useEffect(() => { setFallback(null); setCopyMsg(null); }, [ready, connection?.host, connection?.user, on, keys.length]);
  const call = async (body, okText) => {
    setErr(null); setMsg(null); setBusy(true); setFallback(null); setCopyMsg(null);
    try {
      const r = await api('/api/ssh', body, 60000);
      if (!alive.current) return null;
      if (!r.ok) {
        setErr(r.status === 401 ? 'Session expired; refresh and sign in' : r.error || 'Operation failed');
        await load(); return null;
      }
      if (r.ssh) setSsh(r.ssh);
      setMsg(typeof okText === 'function' ? okText(r) : okText); return r;
    } catch { if (alive.current) { setSsh(null); setErr('Cannot reach duck; click Refresh SSH Status'); } return null; }
    finally { if (alive.current) setBusy(false); }
  };
  const submitPw = async e => {
    e.preventDefault(); setErr(null); setMsg(null);
    if (pw.length < 8 || pw.length > 64) { setErr('SSH password must be 8–64 characters'); return; }
    if (pw !== again) { setErr('SSH passwords do not match'); return; }
    const r = await call({ action: 'on', password: pw, confirm: again }, on ? 'SSH system password updated.' : 'SSH password auth enabled; see Step 3.');
    if (r) { setPw(''); setAgain(''); }
  };
  const submitKey = async e => {
    e.preventDefault();
    const r = await call({ action: 'add_key', key }, x => x.added ? 'SSH public key added.' : 'This key is already registered.');
    if (r) setKey('');
  };
  const copy = async value => {
    setCopyMsg(null); setFallback(null);
    try { await navigator.clipboard.writeText(value); if (alive.current) setCopyMsg('Copied to clipboard.'); }
    catch { if (alive.current) { setFallback(value); setCopyMsg('Clipboard blocked; copy from the selected box below.'); } }
  };
  const state = loading ? 'Reading...' : !ssh ? loadErr || 'SSH status unavailable'
    : `${on ? 'Password auth ON' : ssh.password_login === false ? 'Key-only (password auth OFF)' : 'Password auth unknown'}, ${keys.length ? `${keys.length} key(s) registered` : '0 keys registered'}${ssh.ssh_active === false ? '; sshd stopped' : ssh.ssh_active == null ? '; sshd status unknown' : ''}`;
  return html`<div class="ssh-settings">
    <section class="board access-step" aria-labelledby="ssh-step-title">
      <${StepTitle} number="2" title="SSH System Access" titleId="ssh-step-title" state=${loading ? 'Loading' : ready ? 'Ready' : 'Pending'} />
      <p class="access-description">Allows terminal or AI coding assistants to inspect logs and code over SSH. Optional for normal web control.</p>
      <p class="access-help">Factory images accept SSH keys only (no default password). Set a system password or add your public key below.</p>
      <div class="access-status-row"><p class="ssh-now" role="status">Current: ${state}</p>
        <button class="access-button" type="button" data-ssh-refresh disabled=${loading || busy} onClick=${load}>Refresh SSH Status</button></div>
      ${!hasPassword ? html`<p class="ssh-need-web-pw access-notice">Complete Step 1 (Web Login Password) before changing SSH settings.</p>` : null}
      ${err ? html`<p role="alert" class="access-error">${err}</p>` : null}
      ${msg ? html`<p role="status" class="ssh-msg access-help">${msg}</p>` : null}
      <details class="access-edit ssh-pw-edit" open=${!on && keys.length === 0}>
        <summary>${on ? 'Password auth enabled (expand to update)' : keys.length ? 'Public key configured (optional password)' : 'Set SSH System Password'}</summary>
        <form class="ssh-pw" onSubmit=${submitPw}>
          <p class="access-help">8–64 characters (not "microduck"). Sets the Linux password for user ${ssh?.user || 'radxa'} (also used by sudo).</p>
          ${hasPassword ? html`<label>${on ? 'New SSH Password' : 'SSH Password'}
            <input type="password" name="ssh-password" autocomplete="new-password" value=${pw} required minLength="8" maxLength="64" onInput=${e => setPw(e.target.value)} /></label>
            <label>Confirm SSH Password
              <input type="password" name="ssh-confirm" autocomplete="new-password" value=${again} required minLength="8" maxLength="64" aria-describedby="ssh-confirm-help" onInput=${e => setAgain(e.target.value)} /></label>` : null}
          <p class="access-help" id="ssh-confirm-help">Independent from the web login password.</p>
          <div class="access-actions"><button type="submit" class="access-button primary ssh-on" disabled=${busy || loading || !hasPassword || !ssh || !pw || !again}>
            ${busy ? 'Saving...' : on ? 'Update SSH Password' : 'Save & Enable SSH Password'}</button>
            ${on ? html`<${TwoStep} label="Disable SSH Password Auth" ask="Confirm Disable?" onConfirm=${() => call({ action: 'off' }, 'SSH password auth disabled (key-only).')} />` : null}</div>
        </form>
      </details>
      <details class="access-edit ssh-key-options"><summary>Use an SSH Public Key (Optional)</summary>
        <form class="ssh-key" onSubmit=${submitKey}>
          <p class="access-help">Paste your workstation's <code>.pub</code> key below to authenticate without a password.</p>
          <details class="access-key-help"><summary>How to generate an SSH key</summary>
            <p class="access-help">Run <code>ssh-keygen -t ed25519</code> in your terminal, then copy <code>~/.ssh/id_ed25519.pub</code> (never share private keys).</p></details>
          ${keys.length ? html`<ul class="ssh-keys">${keys.map(k => html`<li key=${k.fp}><span>${k.comment || '(no comment)'} <code>${k.type} ${k.fp.slice(0, 19)}…</code></span>
            ${hasPassword ? html`<${TwoStep} label="Remove" ask="Confirm?" onConfirm=${() => call({ action: 'remove_key', fp: k.fp }, 'SSH key removed.')} />` : null}</li>`)}</ul>` : null}
          ${hasPassword ? html`<label>SSH Public Key<textarea name="ssh-key" rows="3" value=${key} placeholder="ssh-ed25519 AAAA... user@host" onInput=${e => setKey(e.target.value)} /></label>` : null}
          <button type="submit" class="access-button ssh-addkey" disabled=${busy || loading || !hasPassword || !key.trim()}>Register Key</button>
        </form>
      </details>
    </section>
    <section class="board access-step access-connection" aria-labelledby="connection-step-title">
      <${StepTitle} number="3" title="Share Connection Details with AI" titleId="connection-step-title" state=${ready ? 'Ready' : 'Waiting on Step 2'} />
      <p class="access-description">Paste these connection details to your AI coding assistant on the same network.</p>
      ${ready ? html`<div data-access-connection>
        <dl class="access-fields"><div><dt>Host</dt><dd>${connection.host}</dd></div><div><dt>User</dt><dd>${connection.user}</dd></div><div><dt>Port</dt><dd>22</dd></div></dl>
        <code class="ssh-cmd access-command">${connection.cmd}</code>
        <div class="access-actions"><button class="access-button primary" type="button" data-copy-ai onClick=${() => copy(connection.note)}>Copy AI Connection Note</button>
          <button class="access-button" type="button" onClick=${() => copy(connection.cmd)}>Copy SSH Command</button></div>
        <p class="access-help">Contains only host, user, and auth method (never passwords).</p>
        <details class="access-edit"><summary>Connect manually via terminal</summary><p class="access-help">Run the command above in Terminal or PowerShell. Password input is hidden as you type.</p></details>
      </div>` : html`<p class="access-notice">${loading ? 'Checking SSH status...' : ssh?.ssh_active === false ? 'SSH service is not running on the board.' : !ssh || ssh.ssh_active == null ? 'SSH status unavailable; refresh Step 2.' : 'Complete Step 2: enable password auth or register an SSH key.'}</p>`}
      ${ready && copyMsg ? html`<p role="status" class="access-copy-message access-help">${copyMsg}</p>` : null}
      ${ready && fallback ? html`<label class="access-copy-fallback">Copy manually
        <textarea ref=${fallbackRef} data-copy-fallback readOnly rows="8" value=${fallback} /></label>` : null}
    </section>
  </div>`;
}

export function AccessGuide({ api, hasPassword, net, refreshSession }) {
  const [savedWeb, setSavedWeb] = useState(false);
  const webReady = hasPassword === true || savedWeb;
  return html`<section id="ssh-guide" class="access-guide" tabIndex="-1" aria-labelledby="access-guide-title">
    <header class="board access-intro"><span class="access-eyebrow">Remote Debugging · SSH</span>
      <h2 id="access-guide-title">Connect AI to Duck (SSH)</h2>
      <p>No monitor needed. Configure web auth and SSH in 3 steps.</p>
      <div class="access-password-map"><div><b>Web Login Password</b><span>Protects Studio console</span></div><span aria-hidden="true">≠</span><div><b>SSH System Password</b><span>Linux & sudo access</span></div></div>
      <p class="access-help">Both passwords are managed independently.</p>
    </header>
    <${WebPassword} api=${api} hasPassword=${webReady ? true : hasPassword} onSaved=${() => setSavedWeb(true)} refreshSession=${refreshSession} />
    <${SshSettings} api=${api} hasPassword=${webReady} net=${net} />
  </section>`;
}
