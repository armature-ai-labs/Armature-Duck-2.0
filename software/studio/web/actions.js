// Actions page: browse skills, trigger on-demand playback, import action packs, and bind gamepad buttons.
import { html, useState, useEffect, useRef } from './vendor/preact-htm.module.js';
import { ConfirmBox } from './confirm.js';
import { actionArgs, actionForm, bindingChoice, createModelReader, operationAvailability } from './models.js';

if (typeof document !== 'undefined' && !document.getElementById('actions-css')) {
  const sheet = document.createElement('link');
  sheet.id = 'actions-css';
  sheet.rel = 'stylesheet';
  sheet.href = new URL('./actions.css', import.meta.url).href;
  document.head.appendChild(sheet);
}

export const KEYS = [
  { button: 'a', label: 'A' }, { button: 'x', label: 'X' }, { button: 'lb', label: 'LB' },
  { button: 'rb', label: 'RB' }, { button: 'dpad_down', label: 'D-Pad Down' },
];
export const BUILTIN = [
  { skill: 'roulade', slot: 'roulade', id: 'roulade', name: 'Forward Roll' },
  { skill: 'kick_left', slot: 'kick_left', id: 'kick_left', name: 'Left Kick' },
  { skill: 'kick_right', slot: 'kick_right', id: 'kick_right', name: 'Right Kick' },
  { skill: 'ground_pick', slot: 'ground_pick', id: 'ground_pick', name: 'Pick Up' },
  { skill: 'sit_toggle', slot: 'sitstand', id: 'sitstand', name: 'Sit / Stand', toggle: true },
];
const STILLS = { roulade: 'roulade', kick_left: 'kick_left', kick_right: 'kick_right', ground_pick: 'ground_pick',
  sit_toggle: 'sitstand', sitstand: 'sitstand', walk: 'walk', crouch: 'crouch' };
const still = (key) => (STILLS[key] ? new URL(`./model/stills/${STILLS[key]}.webp`, import.meta.url).href : null);
const keyLabel = (b) => KEYS.find((k) => k.button === b)?.label || b;
const seconds = (v) => (typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v * 10) / 10}s` : null);

export function actionList(view) {
  const binds = view?.bindings || [];
  const keysOf = (skill) => binds.filter((b) => b.skill === skill).map((b) => b.button);
  const loaded = new Set(view?.loaded_skills || []);
  const durations = view?.skill_durations || {};
  const out = [];
  for (const b of BUILTIN) {
    const slot = (view?.slots || []).find((s) => s.slot === b.slot);
    const ready = Boolean(slot?.path) && !slot?.error;
    out.push({ key: 'b:' + b.slot, kind: 'builtin', name: b.name, id: b.id, skill: b.skill, slot: b.slot,
      image: still(b.skill), keys: keysOf(b.skill), ready, state: ready ? 'loaded' : 'missing',
      duration: b.toggle ? 'Toggle' : seconds(durations[b.skill]) ? '~' + seconds(durations[b.skill]) : null,
      error: slot?.error || (ready ? null : 'Action model not installed in image') });
  }
  out.push({ key: 'walk', kind: 'walk', name: 'Walk', id: 'walk', skill: null, image: still('walk'), keys: [],
    ready: true, state: 'loaded', duration: 'Continuous' });
  for (const a of view?.actions || []) {
    out.push({ key: 'a:' + a.id, kind: 'imported', name: a.name, id: a.skill || a.suggest?.name || 'Not installed', skill: a.skill,
      action: a, image: still(a.skill || a.suggest?.name), keys: a.buttons || [], ready: a.state === 'loaded',
      state: a.state, duration: seconds(a.params?.duration ?? a.suggest?.duration), error: a.error });
  }
  for (const m of view?.manual_actions || []) {
    out.push({ key: 'm:' + m.name, kind: 'manual', name: m.name, id: m.name, skill: m.skill, action: m,
      image: still(m.skill), keys: m.buttons || [], ready: m.state === 'loaded' && loaded.has(m.skill),
      state: m.state, duration: seconds(m.params?.duration) });
  }
  return out;
}

export function keyChoice(view, item, button) {
  if (!item || item.kind === 'walk') return { valid: false, reason: 'Walking uses the stick; no button binding needed.' };
  if (!item.ready) return { valid: false, reason: item.kind === 'imported' ? 'Install this action onto the duck before binding.' : 'This action is currently unavailable.' };
  const choice = bindingChoice(view, button, item.kind === 'builtin' ? item.slot : 'skill:' + item.skill);
  if (!choice.valid) return choice;
  const lines = [];
  if (choice.conflict) lines.push(`${keyLabel(button)} is currently bound to: ${keyName(view, button)}`, `Replace with: ${item.name}`);
  if (button === 'x' && item.skill !== 'roulade') lines.push('Holding X repeats the action continuously.');
  return { ...choice, confirm: lines.length ? lines : null, args: { ...choice.args, ...(lines.length ? { replace: true } : {}) } };
}

export function keyName(view, button) {
  const row = (view?.bindings || []).find((b) => b.button === button);
  if (!row) return 'Unavailable';
  if (!row.skill) return 'Unbound';
  const item = actionList(view).find((x) => x.skill === row.skill);
  return item?.name || row.action_name || row.skill;
}

function uploadAction(file, csrf, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const query = new URLSearchParams({ filename: file.name, kind: 'action' });
    xhr.open('POST', `/api/models/import?${query}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-CSRF', csrf || '');
    xhr.timeout = 180000;
    xhr.upload.onprogress = (e) => onProgress(e.lengthComputable ? Math.round(e.loaded / e.total * 100) : null);
    xhr.onload = () => { try { resolve({ status: xhr.status, ...JSON.parse(xhr.responseText) }); } catch { reject(new Error('Invalid server response; refresh to verify import.')); } };
    xhr.onerror = () => reject(new Error('Upload connection lost; refresh and retry.'));
    xhr.ontimeout = () => reject(new Error('Upload or validation timed out; refresh and retry.'));
    xhr.send(file);
  });
}

function Thumb({ item, big }) {
  if (item.image) return html`<img class=${big ? 'ac-stage-img' : 'ac-thumb'} src=${item.image} alt=${big ? `${item.name} preview` : ''} />`;
  return html`<span class=${big ? 'ac-stage-img ac-none' : 'ac-thumb ac-none'} aria-hidden="true">
    <svg viewBox="0 0 40 40"><path d="M11 24a9 8 0 0 1 18 0z" fill="currentColor" opacity=".55"/><rect x="11" y="24" width="18" height="3" rx="1.5" fill="currentColor" opacity=".8"/></svg></span>`;
}

function InstallPanel({ item, busy, cap, onOp, onConfirm, onCancel }) {
  const a = item.action;
  const [form, setForm] = useState(() => actionForm(a));
  const [touched, setTouched] = useState(false);
  const set = (k) => (e) => { setTouched(true); setForm({ ...form, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }); };
  const parsed = actionArgs(form);
  const installed = a.state !== 'none';
  return html`<form class="ac-install" onSubmit=${async (e) => {
    e.preventDefault(); setTouched(true);
    if (!parsed.ok) return;
    const ok = await onOp({ op: 'action_install', id: a.id, ...parsed.args },
      installed ? `Updated parameters for "${item.name}".` : `Installed "${item.name}". Support the duck on first run.`);
    if (ok && onCancel) onCancel();
  }}>
    <strong>${installed ? 'Edit Parameters' : 'Not Installed on Duck'}</strong>
    <div class="ac-fields">
      <label>Action Name (ASCII)<input value=${form.name} maxLength="32" placeholder="e.g. crouch" aria-label="Action name" onInput=${set('name')} disabled=${busy} /></label>
      <label>Duration (s)<input type="number" step="0.1" min="0.2" max="60" value=${form.duration} aria-label="Duration" onInput=${set('duration')} disabled=${busy} /></label>
    </div>
    <details><summary>Advanced (defaults from package)</summary>
      <div class="ac-fields">
        <label>Command (3 numbers)<input value=${form.command} aria-label="Command" onInput=${set('command')} disabled=${busy} /></label>
        <label>Unwind Command<input value=${form.unwind} aria-label="Unwind command" onInput=${set('unwind')} disabled=${busy} /></label>
        <label>Unwind Time (s)<input type="number" step="0.1" min="0" max="10" value=${form.unwind_s} aria-label="Unwind duration" onInput=${set('unwind_s')} disabled=${busy} /></label>
        <label>Action Scale<input type="number" step="0.05" min="0.05" max="2" value=${form.action_scale} aria-label="Action scale" onInput=${set('action_scale')} disabled=${busy} /></label>
        <label>Gain Ratio<input type="number" step="0.05" min="0.2" max="2" value=${form.gain_ratio} aria-label="Gain ratio" onInput=${set('gain_ratio')} disabled=${busy} /></label>
        <label class="ac-check"><input type="checkbox" checked=${form.chain} onChange=${set('chain')} disabled=${busy} />Chain on press while running</label>
      </div>
      <p>Command feeds 3 floats to the policy; toggle actions typically use 1, 0, 0. Scale should match training (usually 1).</p>
    </details>
    ${touched && !parsed.ok ? html`<p class="ac-bad">${parsed.reason}</p>` : null}
    <div class="ac-row">
      <button class="ac-btn blue" type="submit" disabled=${busy || !cap.select || !a.installed} title=${!cap.select ? cap.reason : undefined}>${installed ? 'Save Parameters' : 'Install'}</button>
      ${onCancel ? html`<button class="ac-btn" type="button" disabled=${busy} onClick=${onCancel}>Cancel</button>` : null}
      ${!installed ? html`<button class="ac-btn danger" type="button" disabled=${busy || !cap.delete || !a.can_delete} title=${a.can_delete ? undefined : a.reason}
        onClick=${() => onConfirm({ lines: [`Delete "${item.name}"?`, 'You will need to re-import the package to use it again.'], label: 'Delete',
          args: { op: 'delete', id: a.id }, success: 'Action deleted', after: 'reset' })}>Delete</button>` : null}
    </div>
  </form>`;
}

export function ActionsPage({ api, csrf, link, plazaUrl }) {
  const [view, setView] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [sel, setSel] = useState('b:roulade');
  const [listen, setListen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [pending, setPending] = useState('');
  const [message, setMessage] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [doing, setDoing] = useState(false);
  const [swap, setSwap] = useState(0);
  const mounted = useRef(true);
  const working = useRef(false);
  const fileInput = useRef(null);
  const confirmArea = useRef(null);
  const reader = useRef(null);
  if (!reader.current) reader.current = createModelReader(api);

  const load = async () => {
    if (working.current) return;
    try {
      const r = await reader.current.read();
      if (!r) return;
      if (!r.ok || !r.data) throw new Error(r.error || 'Failed to load actions.');
      if (mounted.current) { setView(r.data); setLoadError(''); }
    } catch (e) {
      if (mounted.current) setLoadError(e.message || 'Connection failed; refresh to retry.');
    }
  };
  useEffect(() => {
    mounted.current = true; load();
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') load(); }, 5000);
    return () => { mounted.current = false; clearInterval(timer); };
  }, []);

  useEffect(() => { if (confirm) confirmArea.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }); }, [confirm]);

  const cap0 = operationAvailability(view);
  const cap = loadError ? { ...cap0, select: false, delete: false, import: false, reason: loadError } : cap0;
  const busy = Boolean(pending);
  const items = actionList(view);
  const item = items.find((x) => x.key === sel) || items[0];
  const pick = (key) => { if (key !== sel) { setSel(key); setEditing(false); setSwap((n) => n + 1); } };

  const onOp = async (args, success, after) => {
    if (working.current) return false;
    working.current = true; reader.current.invalidate();
    setPending('Saving, waiting for robotd...'); setMessage(null);
    try {
      const r = await api('/api/models/op', args, 60000);
      if (!r.ok || !r.data) throw new Error(r.error || 'Operation failed; refresh to verify state.');
      if (mounted.current) { setView(r.data); setMessage({ kind: 'ok', text: success }); if (after === 'reset') setSel('b:roulade'); }
      return true;
    } catch (e) {
      if (mounted.current) setMessage({ kind: 'bad', text: e.name === 'AbortError' ? 'Confirmation timed out; refresh to verify.' : e.message });
      return false;
    } finally {
      working.current = false;
      if (mounted.current) { setPending(''); setConfirm(null); }
    }
  };
  const ask = (c) => setConfirm(c);

  const doImport = async (file) => {
    if (!file || working.current) return;
    if (!/\.(onnx|zip|tar\.gz|tgz)$/i.test(file.name)) { setMessage({ kind: 'bad', text: 'Select an .onnx, .zip, or .tar.gz action package.' }); return; }
    working.current = true; reader.current.invalidate(); setMessage(null); setPending('Uploading action package...');
    try {
      const before = new Set((view?.actions || []).map((a) => a.id));
      const r = await uploadAction(file, csrf, (p) => mounted.current && setPending(p === 100 ? 'Upload complete; validating model...' : `Uploading action package... ${p ?? ''}%`));
      if (!r.ok || !r.data) throw new Error(r.error || 'Import failed.');
      if (mounted.current) {
        setView(r.data);
        const added = (r.data.actions || []).find((a) => !before.has(a.id));
        if (added) { setSel('a:' + added.id); setSwap((n) => n + 1); }
        setMessage({ kind: 'ok', text: 'Action imported. Verify name and duration, then click Install.' });
      }
    } catch (e) {
      if (mounted.current) setMessage({ kind: 'bad', text: e.message });
    } finally {
      working.current = false;
      if (fileInput.current) fileInput.current.value = '';
      if (mounted.current) setPending('');
    }
  };

  const play = async () => {
    if (!item?.ready || !item.skill || doing) return;
    setDoing(true); setMessage(null);
    const r = await link.cmd('pad.op', { op: 'skill', name: item.skill }, 4000);
    if (mounted.current) {
      setDoing(false);
      setMessage(r.ok ? { kind: 'ok', text: `Executing "${item.name}".` } : { kind: 'bad', text: r.reason || r.error || 'Action rejected by robotd.' });
    }
  };

  const tapKey = (button) => {
    if (!listen) {
      const row = (view?.bindings || []).find((b) => b.button === button);
      const hit = items.find((x) => x.skill && x.skill === row?.skill);
      if (hit) pick(hit.key);
      return;
    }
    const c = keyChoice(view, item, button);
    if (!c.valid) { setMessage({ kind: 'bad', text: c.reason }); return; }
    setListen(false);
    if (c.already) { setMessage({ kind: 'ok', text: `${keyLabel(button)} is already bound to "${item.name}".` }); return; }
    const success = `${keyLabel(button)} bound to "${item.name}"`;
    if (c.confirm) ask({ lines: c.confirm, label: 'Bind', args: c.args, success });
    else onOp(c.args, success);
  };

  const a = item?.action;
  const tags = [];
  if (item) {
    tags.push({ cls: '', text: item.kind === 'builtin' || item.kind === 'walk' ? 'Built-in' : item.kind === 'manual' ? 'Custom' : 'Imported' });
    if (item.duration) tags.push({ cls: '', text: item.duration });
    if (item.kind !== 'walk') tags.push(item.keys.length ? { cls: 'key', text: item.keys.map(keyLabel).join(', ') } : { cls: 'warn', text: 'Unbound' });
    if (item.kind === 'imported' && item.state === 'loaded') tags.push({ cls: 'warn', text: 'New: test supported' });
    if (item.kind === 'imported' && item.state === 'none') tags.push({ cls: 'warn', text: 'Not installed' });
    if (item.state === 'failed') tags.push({ cls: 'bad', text: 'Load failed' });
  }
  const showInstall = item?.kind === 'imported' && (item.state !== 'loaded' || editing);
  const builtinCount = items.filter((x) => x.kind === 'builtin' || x.kind === 'walk').length;
  const importCount = items.length - builtinCount;
  const dockSub = item?.kind === 'walk' ? 'Use the Gamepad stick to walk (press Start to stand, Start again to walk).'
    : !item?.ready ? (item?.error || 'This action is currently unavailable.')
    : listen ? `Click a button below to bind "${item.name}".` : 'Click Play to trigger this action (duck must be standing).';

  return html`<section class="ac-page" aria-label="Actions" aria-busy=${busy}>
    <header class="ac-head">
      <div><h1>Actions</h1><p>Preview skills, trigger on-demand playback, or bind to gamepad buttons.</p></div>
      <div class="ac-row">
        ${plazaUrl ? html`<a class="ac-btn" href=${plazaUrl} target="_blank" rel="noopener">Action Plaza ↗</a>` : null}
        <button class="ac-btn blue" type="button" disabled=${busy || !cap.import} title=${!cap.import ? (view?.capabilities?.import_reason || cap.reason) : undefined}
          onClick=${() => fileInput.current?.click()}>Import Action</button>
        <input ref=${fileInput} type="file" accept=".onnx,.zip,.tar.gz,.tgz" hidden onChange=${(e) => doImport(e.target.files[0])} aria-label="Choose action package" />
      </div>
    </header>
    ${loadError ? html`<div class="ac-note bad" role="alert">${loadError}</div>` : null}
    ${view && !cap.select && !loadError ? html`<div class="ac-note warn" role="status">${cap.reason} (playback works; release torque before editing bindings or installing)</div>` : null}
    ${message ? html`<div class=${`ac-note ${message.kind}`} role=${message.kind === 'bad' ? 'alert' : 'status'}>${message.text}</div>` : null}
    ${pending ? html`<div class="ac-note progress" role="status">${pending}</div>` : null}
    <div ref=${confirmArea}>${confirm ? html`<${ConfirmBox} lines=${confirm.lines} confirmLabel=${confirm.label} cancelLabel="Cancel" busy=${busy}
      onCancel=${() => setConfirm(null)} onConfirm=${() => onOp(confirm.args, confirm.success, confirm.after)} cls="ac-confirm" />` : null}</div>

    <div class="ac-grid">
      <section class="ac-stage" aria-label="Action Stage">
        ${item ? html`<div class=${`ac-stage-top swap${swap % 2}`}>
          <h2>${item.name}<span>${item.id}</span></h2>
          <div class="ac-tags">${tags.map((t) => html`<span class=${'ac-tag ' + t.cls}>${t.text}</span>`)}</div>
          ${item.kind === 'imported' && item.state !== 'none' && !editing ? html`<div class="ac-row ac-stage-acts">
            <button class="ac-btn ghost" type="button" disabled=${busy || !cap.select} onClick=${() => setEditing(true)}>Edit Params</button>
            <button class="ac-btn ghost" type="button" disabled=${busy || !cap.select} onClick=${() => ask({
              lines: [`Uninstall "${item.name}" from the duck?`, item.keys.length ? `${item.keys.map(keyLabel).join(', ')} will revert to built-in bindings.` : 'Package file remains on disk for re-install.'],
              label: 'Uninstall', args: { op: 'action_uninstall', id: a.id }, success: `Uninstalled "${item.name}"` })}>Uninstall</button>
          </div>` : null}
        </div>
        <div class=${`ac-stage-pic swap${swap % 2}`}><${Thumb} item=${item} big=${true} /></div>
        ${showInstall ? html`<${InstallPanel} key=${item.key + (editing ? ':e' : '')} item=${item} busy=${busy} cap=${cap} onOp=${onOp} onConfirm=${ask}
          onCancel=${item.state === 'loaded' ? () => setEditing(false) : null} />` : html`<div class="ac-dock">
          <button class="ac-play" type="button" aria-label=${`Play ${item.name}`} disabled=${!item.ready || !item.skill || doing} onClick=${play}>
            <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M6 3.4v13.2L17 10z" fill="#FFF"/></svg></button>
          <div class="ac-dock-text"><strong>${doing ? 'Sending command...' : 'Play Action'}</strong><span>${dockSub}</span></div>
          ${item.kind !== 'walk' ? html`<button class=${'ac-btn ghost' + (listen ? ' on' : '')} type="button" disabled=${busy || !cap.select || !item.ready}
            title=${!cap.select ? cap.reason : undefined} onClick=${() => setListen(!listen)}>${listen ? 'Cancel' : 'Bind Button'}</button>` : null}
        </div>`}` : html`<p class="ac-dim">Loading actions...</p>`}
      </section>

      <section class="ac-list" aria-label="My Actions">
        <div class="ac-list-head"><h2>My Actions</h2><span>Built-in ${builtinCount} · Imported ${importCount}</span></div>
        ${items.map((x) => html`<button type="button" class=${'ac-item' + (x.key === item?.key ? ' sel' : '')} aria-pressed=${x.key === item?.key ? 'true' : 'false'} onClick=${() => pick(x.key)}>
          <${Thumb} item=${x} />
          <span class="ac-item-text"><span class="ac-item-name">${x.name}${x.kind === 'imported' && x.state === 'loaded' && !x.keys.length ? html`<em>NEW</em>` : null}</span>
            <span class="ac-item-sub">${x.id}${x.duration ? ' · ' + x.duration : ''}</span></span>
          ${x.kind === 'walk' ? html`<span class="ac-pill">Stick</span>`
            : x.state === 'none' ? html`<span class="ac-pill warn">Not Installed</span>`
            : x.state === 'failed' || x.state === 'missing' ? html`<span class="ac-pill bad">Unavailable</span>`
            : x.keys.length ? x.keys.map((k) => html`<span class="ac-cap">${keyLabel(k)}</span>`)
            : html`<span class="ac-pill warn">Unbound</span>`}
        </button>`)}
        <button class="ac-add" type="button" disabled=${busy || !cap.import} onClick=${() => fileInput.current?.click()}>+ Import Action Package</button>
      </section>
    </div>

    <section class="ac-keys" aria-label="Gamepad Bindings">
      <div class="ac-keys-head"><div><h2>Gamepad Bindings</h2>
        <p>${listen ? `Click a button to bind "${item?.name}".` : 'Five action buttons. Click Bind Button above, then click a keycap below.'}</p></div>
        <button class="ac-btn" type="button" disabled=${busy || !cap.select} onClick=${() => ask({ lines: ['Reset all 5 buttons to factory defaults?', 'Installed actions will remain on disk.'],
          label: 'Reset Bindings', args: { op: 'reset_bindings' }, success: 'Reset 5 bindings to defaults' })}>Reset Defaults</button></div>
      <div class="ac-keycaps">${KEYS.map((k) => html`<button type="button" class=${'ac-keycap' + (listen ? ' listen' : '')} aria-label=${`${k.label}: ${keyName(view, k.button)}`}
        disabled=${busy} onClick=${() => tapKey(k.button)}><span class="cap">${k.label}</span><span class="what">${keyName(view, k.button)}</span></button>`)}</div>
    </section>

    <section class="ac-more">
      <div><h2>Walk & Stand Policies</h2><p>Switch locomotion models or restore built-in policies on the Models page.</p></div>
      <a class="ac-btn" href="#/models">Open</a>
    </section>
  </section>`;
}
