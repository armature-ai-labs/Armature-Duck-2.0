// Model Manager: updates only after server confirmation; installed, selected, and running are distinct states.
import { html, useState, useEffect, useLayoutEffect, useRef } from './vendor/preact-htm.module.js';
import { ConfirmBox } from './confirm.js';
import { sanitizeModelName } from './tune.js';

if (typeof document !== 'undefined' && !document.getElementById('models-css')) {
  const sheet = document.createElement('link');
  sheet.id = 'models-css';
  sheet.rel = 'stylesheet';
  sheet.href = new URL('./models.css', import.meta.url).href;
  document.head.appendChild(sheet);
}

const BUTTONS = [
  { button: 'a', label: 'A' }, { button: 'x', label: 'X' },
  { button: 'lb', label: 'LB' }, { button: 'rb', label: 'RB' },
  { button: 'dpad_down', label: 'D-Pad Down' },
];
const SKILL_SLOTS = ['sitstand', 'ground_pick', 'kick_left', 'kick_right', 'roulade'];
const CAPTURE_ROLES = { south: 'a', west: 'x', lb: 'lb', rb: 'rb', dd: 'dpad_down' };
const taskLabel = (item) => item?.task_label || ({ walk: 'Walk / Hold', sitstand: 'Sit / Stand',
  ground_pick: 'Pick Up', kick_left: 'Left Kick', kick_right: 'Right Kick', roulade: 'Forward Roll',
  stand: 'Standalone Stand' }[item?.suggested_slot || item?.task]) || item?.task || 'Purpose unconfirmed';
const keyLabel = (button) => BUTTONS.find(b => b.button === button)?.label || button;
const itemName = (view, id) => sanitizeModelName(view?.items?.find(item => item.id === id)?.name) || '';
const slotName = (view, slot) => view?.slots?.find(s => s.slot === slot)?.label || slot;
const actionLabel = (view, slot) => {
  const s = view?.slots?.find(s => s.slot === slot);
  const name = itemName(view, s?.model_id);
  return name ? `${s.label} · ${name}` : s?.label || slot || 'Unbound';
};

export function modelState(item) {
  const states = item.installed ? ['Installed'] : ['Missing File'];
  if (item.slots?.length) states.push('Selected');
  if (item.running === true) states.push('Running');
  return states;
}

export function modelSlots(view, item) {
  const allowed = item.allowed_slots || (item.suggested_slot ? [item.suggested_slot] : []);
  return (view?.slots || []).filter(slot => allowed.includes(slot.slot));
}

export const customActions = view => [...(view?.actions || []), ...(view?.manual_actions || [])];

export function bindingChoice(view, button, slot) {
  const current = view?.bindings?.find(b => b.button === button);
  const occupied = Boolean(current?.slot || current?.skill);
  const currentLabel = current?.slot ? actionLabel(view, current.slot) : current?.action_name || current?.skill || 'Unbound';
  if (typeof slot === 'string' && slot.startsWith('skill:')) {
    const skill = slot.slice(6);
    if (!BUTTONS.some(b => b.button === button)) return { valid: false, reason: 'Only A, X, LB, RB, and D-Pad Down are supported.' };
    const target = customActions(view).find(a => a.skill === skill);
    if (!target || target.state !== 'loaded') return { valid: false, reason: 'Install this action first on the Actions page.' };
    const already = current?.skill === skill;
    return { valid: true, already, conflict: occupied && !already, currentLabel, targetLabel: target.name,
      args: { op: 'bind', button, skill } };
  }
  if (!BUTTONS.some(b => b.button === button) || !SKILL_SLOTS.includes(slot)) {
    return { valid: false, reason: 'This slot cannot be bound to an action button; walking uses the stick.' };
  }
  const target = view?.slots?.find(s => s.slot === slot);
  if (!target?.model_id || target.error) return { valid: false, reason: 'Select a valid model for this slot first.' };
  const already = current?.slot === slot;
  return { valid: true, already, conflict: occupied && !already,
    currentLabel, targetLabel: actionLabel(view, slot), args: { op: 'bind', button, slot } };
}

const fmt = v => (v == null ? '' : String(v));
export function actionForm(action) {
  const p = action?.params || action?.suggest || {};
  return { name: p.name || action?.skill || action?.suggest?.name || '', duration: fmt(p.duration),
    command: (p.command || [0, 0, 0]).join(', '), unwind: (p.unwind || [0, 0, 0]).join(', '),
    unwind_s: fmt(p.unwind_s ?? 0), chain: p.chain === true, action_scale: fmt(p.action_scale ?? 1), gain_ratio: fmt(p.gain_ratio ?? 1) };
}

export function actionArgs(form) {
  const num = (text, label, low, high, lowOpen = false) => {
    const v = Number(String(text).trim());
    if (String(text).trim() === '' || !Number.isFinite(v) || v > high || (lowOpen ? v <= low : v < low)) throw new Error(`${label} must be between ${low} and ${high}`);
    return v;
  };
  const triple = (text, label) => {
    const parts = String(text).split(/[,\s]+/).filter(Boolean).map(Number);
    if (parts.length !== 3 || parts.some(v => !Number.isFinite(v) || Math.abs(v) > 2)) throw new Error(`${label} must be 3 numbers between -2 and 2 (e.g. 1, 0, 0)`);
    return parts;
  };
  try {
    const name = String(form.name || '').trim();
    if (!/^[a-z0-9_]{1,32}$/.test(name)) throw new Error('Action name must be 1–32 lowercase letters, digits, or underscores');
    return { ok: true, args: { name, duration: num(form.duration, 'Duration (s)', 0.2, 60),
      command: triple(form.command, 'Command'), unwind: triple(form.unwind, 'Unwind Command'), unwind_s: num(form.unwind_s, 'Unwind Time (s)', 0, 10),
      chain: form.chain === true, action_scale: num(form.action_scale, 'Action Scale', 0, 2, true), gain_ratio: num(form.gain_ratio, 'Gain Ratio', 0.2, 2) } };
  } catch (e) {
    return { ok: false, reason: e.message };
  }
}

export function actionState(action) {
  return { none: 'Not Installed', loaded: 'Installed', failed: 'Load Failed' }[action?.state] || 'Unknown';
}

export function operationAvailability(view) {
  const c = view?.capabilities || {};
  return { import: c.import === true, select: c.select === true,
    rename: c.rename === true, delete: c.delete === true,
    reason: c.reason || view?.error || 'Model manager status unavailable; refresh to retry.' };
}

export function captureKey(previous, live) {
  if (!previous || live?.state !== 'attached' || (live.last_ms != null && live.last_ms > 1500)) return null;
  const pressed = Object.keys(CAPTURE_ROLES).filter(role => live.buttons?.[role]);
  if (pressed.length !== 1 || previous[pressed[0]]) return null;
  return CAPTURE_ROLES[pressed[0]];
}

export function createModelReader(api) {
  let revision = 0;
  return {
    invalidate() { revision += 1; },
    async read() {
      const started = revision;
      try {
        const response = await api('/api/models');
        return started === revision ? response : null;
      } catch (error) {
        if (started === revision) throw error;
        return null;
      }
    },
  };
}

export function captureAllowed(view) {
  const rows = view?.bindings || [];
  const safeSkills = new Set(['', 'ground_pick', 'sit_toggle', 'kick_left', 'kick_right', 'roulade']);
  return rows.length === BUTTONS.length && BUTTONS.every(({ button }) => {
    const matches = rows.filter(row => row.button === button);
    return matches.length === 1 && !matches[0].error && safeSkills.has(matches[0].skill);
  });
}

function ModelItem({ item, view, cap, busy, onOp, onConfirm, onBind }) {
  const [renaming, setRenaming] = useState(false);
  const displayName = sanitizeModelName(item.name || '');
  const [name, setName] = useState(displayName);
  const allowed = modelSlots(view, item);
  const [chosen, setChosen] = useState(allowed[0]?.slot || '');
  useEffect(() => { if (!renaming) setName(sanitizeModelName(item.name || '')); }, [item.name, renaming]);
  const selectedSlot = allowed.find(s => s.slot === chosen) || allowed[0];
  const selected = (item.slots || []).map(slot => slotName(view, slot));
  const bound = (item.buttons || []).map(b => keyLabel(typeof b === 'string' ? b : b.button));
  const bindable = (item.slots || []).filter(slot => SKILL_SLOTS.includes(slot));
  const current = selectedSlot?.model_id === item.id;
  const removeReason = item.reason || (item.slots?.length ? 'Currently selected in a slot; switch models or restore built-in first.' : 'Cannot delete right now.');
  return html`<article class="models-item" data-model-id=${item.id} data-model-name=${displayName} aria-label=${`Model ${displayName}`}>
    <div class="models-item-head">
      <div class="models-item-title"><h3 data-model-title>${displayName}</h3><span class="models-source">${item.source === 'builtin' ? 'Built-in' : 'Imported'}</span></div>
      <div class="models-tags">${modelState(item).map(state => html`<span class=${`models-tag ${state === 'Running' ? 'running' : state === 'Selected' ? 'selected' : ''}`}>${state}</span>`)}</div>
    </div>
    <p class="models-purpose">Purpose: ${taskLabel(item)}</p>
    ${selected.length ? html`<p class="models-note">Active in: ${selected.join(', ')}${bound.length ? ` · Buttons: ${bound.join(', ')}` : ''}</p>` : null}
    ${renaming ? html`<form class="models-rename" onSubmit=${async e => {
      e.preventDefault();
      if (await onOp({ op: 'rename', id: item.id, name: name.trim() }, 'Name saved')) setRenaming(false);
    }}>
      <label>Display Name<input type="text" value=${name} maxLength="80" aria-label=${`Model name ${displayName}`} onInput=${e => setName(e.target.value)} disabled=${busy} /></label>
      <button class="models-btn primary" type="submit" disabled=${busy || !cap.rename || !name.trim()}>Save Name</button>
      <button class="models-btn" type="button" disabled=${busy} onClick=${() => setRenaming(false)}>Cancel</button>
    </form>` : null}
    <div class="models-item-actions">
      ${allowed.length > 1 ? html`<label class="models-slot-choice">Slot<select value=${selectedSlot?.slot} aria-label=${`Slot ${displayName}`} onChange=${e => setChosen(e.target.value)} disabled=${busy || !cap.select}>
        ${allowed.map(s => html`<option value=${s.slot}>${s.label}</option>`)}
      </select></label>` : null}
      <button class=${`models-btn ${current ? '' : 'primary'}`} type="button" data-model-action="select" aria-label=${`Use ${displayName}`}
        disabled=${busy || !cap.select || !item.installed || !selectedSlot || current}
        title=${!cap.select ? cap.reason : current ? 'Already selected for this slot' : !selectedSlot ? 'No compatible slot available' : `Assign to ${selectedSlot.label}`}
        onClick=${() => onOp({ op: 'select', id: item.id, slot: selectedSlot.slot }, `Selected "${displayName}" (does not auto-start motion)`)}>${current ? 'Selected' : 'Use Model'}</button>
      <button class="models-btn" type="button" aria-label=${`Rename ${displayName}`} disabled=${busy || !cap.rename} onClick=${() => setRenaming(!renaming)}>Rename</button>
      ${bindable.length ? html`<button class="models-btn" type="button" data-model-action="bind" aria-label=${`Bind ${displayName}`} disabled=${busy || !cap.select}
        onClick=${() => onBind(bindable[0])}>Bind Button</button>` : null}
      ${item.source !== 'builtin' ? html`<button class="models-btn danger" type="button" data-model-action="delete" aria-label=${`Delete ${displayName}`}
        disabled=${busy || !cap.delete || !item.can_delete} title=${!item.can_delete ? removeReason : !cap.delete ? cap.reason : undefined}
        onClick=${() => onConfirm({ title: `Delete "${displayName}"?`, lines: ['You will need to re-import the package to use it again.'],
          label: 'Delete', args: { op: 'delete', id: item.id }, success: 'Model deleted' })}>Delete</button>` : null}
    </div>
    ${item.source !== 'builtin' && !item.can_delete ? html`<p class="models-note">Cannot delete: ${removeReason}</p>` : null}
    ${!item.installed && item.reason ? html`<p class="models-error">${item.reason}</p>` : null}
    <details class="models-details"><summary>Model Details</summary><dl>
      <dt>Purpose</dt><dd>${taskLabel(item)}</dd>
      <dt>Digest</dt><dd>${item.sha12 || item.sha256 || 'N/A'}</dd>
      ${item.path ? html`<dt>Path</dt><dd>${item.path}</dd>` : null}
      ${item.rounds != null ? html`<dt>Steps</dt><dd>${item.rounds}</dd>` : null}
      ${item.commit ? html`<dt>Commit</dt><dd>${item.commit}</dd>` : null}
      <dt>ID</dt><dd>${item.id}</dd>
    </dl></details>
  </article>`;
}

function BindingRow({ binding, view, busy, cap, preset, onBind, onOp }) {
  const candidates = (view.slots || []).filter(s => SKILL_SLOTS.includes(s.slot) && s.model_id && !s.error);
  const extra = customActions(view).filter(a => a.state === 'loaded' && a.skill);
  const own = binding.slot || (extra.some(a => a.skill === binding.skill) ? 'skill:' + binding.skill : '');
  const [slot, setSlot] = useState(own);
  const button = binding.button;
  useEffect(() => { setSlot(own); }, [own]);
  useEffect(() => { if (preset?.slot) setSlot(preset.slot); }, [preset?.tick]);
  const choice = bindingChoice(view, button, slot);
  const current = binding.slot ? actionLabel(view, binding.slot) : binding.action_name || binding.skill || 'Unbound';
  return html`<div class="models-binding" data-button=${button}>
    <div class="models-binding-current"><kbd>${keyLabel(button)}</kbd><span>${current}</span></div>
    <div class="models-binding-controls">
      <select value=${slot} aria-label=${`Bind action for ${keyLabel(button)}`} disabled=${busy || !cap.select} onChange=${e => setSlot(e.target.value)}>
        <option value="">Choose action...</option>${candidates.map(s => html`<option value=${s.slot}>${actionLabel(view, s.slot)}</option>`)}
        ${extra.map(a => html`<option value=${'skill:' + a.skill}>Action · ${a.name}</option>`)}
      </select>
      <button class="models-btn" type="button" data-model-action="bind" aria-label=${`Bind ${keyLabel(button)}`} disabled=${busy || !cap.select || !choice.valid || choice.already}
        onClick=${() => onBind(button, slot)}>${choice.already ? 'Bound' : 'Bind'}</button>
      <button class="models-btn" type="button" aria-label=${`Unbind ${keyLabel(button)}`} disabled=${busy || !cap.select || !(binding.slot || binding.skill)}
        onClick=${() => onOp({ op: 'unbind', button }, `Unbound ${keyLabel(button)}`)}>Unbind</button>
    </div>
  </div>`;
}

export function ModelsPage({ api, csrf, link, live }) {
  const [view, setView] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState('');
  const [message, setMessage] = useState(null);
  const [fileState, setFile] = useState(null);
  const [nameState, setName] = useState('');
  const [upload, setUpload] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [preset, setPreset] = useState(null);
  const [capture, setCapture] = useState(null);
  const [captureNote, setCaptureNote] = useState('');
  const [seconds, setSeconds] = useState(20);
  const [stableBindings, setStableBindings] = useState('');
  const mounted = useRef(true);
  const working = useRef(false);
  const refreshing = useRef(false);
  const fileInput = useRef(null);
  const request = useRef(null);
  const reader = useRef(null);
  if (!reader.current) reader.current = createModelReader(api);
  const confirmArea = useRef(null);
  const bindingsArea = useRef(null);

  const load = async (quiet = false) => {
    if (refreshing.current || working.current) return;
    refreshing.current = true;
    if (!quiet) setLoading(true);
    try {
      const r = await reader.current.read();
      if (!r) return;
      if (!r.ok || !r.data) throw new Error(r.error || (r.status === 401 ? 'Session expired; sign in again.' : 'Failed to load model manager.'));
      if (mounted.current) { setView(r.data); setLoadError(''); }
    } catch (e) {
      if (mounted.current) setLoadError(e.name === 'AbortError' ? 'Timed out loading models; refresh.' : e.message || 'Connection failed; refresh.');
    } finally {
      refreshing.current = false;
      if (mounted.current) setLoading(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    load();
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') load(true); }, 5000);
    return () => { mounted.current = false; clearInterval(timer); request.current?.abort(); };
  }, []);
  useEffect(() => {
    if (confirm) { confirmArea.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
  }, [confirm]);

  const captureFingerprint = JSON.stringify((view?.bindings || []).map(({ button, skill, error }) => [button, skill, error]));
  const safeCapture = captureAllowed(view);
  useEffect(() => {
    if (!safeCapture) { setStableBindings(''); return undefined; }
    const timer = setTimeout(() => setStableBindings(captureFingerprint), 2000);
    return () => clearTimeout(timer);
  }, [captureFingerprint, safeCapture]);
  const rawCap = { ...operationAvailability(view), capture: safeCapture && stableBindings === captureFingerprint };
  const cap = loadError ? { ...rawCap, import: false, select: false, rename: false, delete: false, reason: loadError } : rawCap;
  const busy = Boolean(pending) || Boolean(capture);
  const onOp = async (args, success) => {
    if (working.current) return false;
    setCapture(null);
    reader.current.invalidate();
    working.current = true;
    setPending('Saving and confirming...');
    setMessage(null);
    try {
      const r = await api('/api/models/op', args, 60000);
      if (!r.ok || !r.data) throw new Error(r.error || 'Operation failed; refresh to verify.');
      if (mounted.current) { setView(r.data); setLoadError(''); setMessage({ kind: 'ok', text: success }); }
      return true;
    } catch (e) {
      if (mounted.current) setMessage({ kind: 'bad', text: e.name === 'AbortError' ? 'Confirmation timed out; refresh.' : e.message || 'Connection failed; refresh.' });
      return false;
    } finally {
      working.current = false;
      if (mounted.current) { setPending(''); setConfirm(null); }
    }
  };

  const doImport = async (e) => {
    e.preventDefault();
    const file = fileState, name = nameState;
    if (working.current || !cap.import || !file) return;
    setMessage(null);
    if (!/\.(zip|tar\.gz|tgz)$/i.test(file.name)) {
      setMessage({ kind: 'bad', text: 'Select a .zip or .tar.gz model package. Standalone .onnx actions can be imported on the Actions page.' });
      return;
    }
    working.current = true;
    reader.current.invalidate();
    setPending('Uploading model package...');
    setUpload(0);
    try {
      const response = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        request.current = xhr;
        const query = new URLSearchParams({ filename: file.name });
        if (name.trim()) query.set('name', name.trim());
        xhr.open('POST', `/api/models/import?${query}`);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.setRequestHeader('X-CSRF', csrf || '');
        xhr.timeout = 180000;
        xhr.upload.onprogress = event => {
          if (!mounted.current) return;
          const percent = event.lengthComputable ? Math.round(event.loaded / event.total * 100) : null;
          setUpload(percent);
          setPending(percent === 100 ? 'Upload complete; validating & installing...' : 'Uploading model package...');
        };
        xhr.upload.onload = () => { if (mounted.current) { setUpload(100); setPending('Upload complete; validating & installing...'); } };
        xhr.onload = () => {
          try { resolve({ status: xhr.status, ...JSON.parse(xhr.responseText) }); }
          catch { reject(new Error('Invalid server response; refresh to verify install.')); }
        };
        xhr.onerror = () => reject(new Error('Upload connection lost; refresh and retry.'));
        xhr.ontimeout = () => reject(new Error('Upload or validation timed out; refresh and retry.'));
        xhr.onabort = () => reject(new Error('Upload cancelled.'));
        xhr.send(file);
      });
      if (!response.ok || !response.data) throw new Error(response.error || 'Model import failed.');
      if (mounted.current) {
        setView(response.data); setLoadError(''); setFile(null); setName('');
        if (fileInput.current) fileInput.current.value = '';
        setMessage({ kind: 'ok', text: 'Model installed. Click "Use Model" to activate it in a slot.' });
      }
    } catch (err) {
      if (mounted.current) setMessage({ kind: 'bad', text: err.message || 'Model import failed.' });
    } finally {
      working.current = false; request.current = null;
      if (mounted.current) { setPending(''); setUpload(null); }
    }
  };

  const bind = (button, slot) => {
    const choice = bindingChoice(view, button, slot);
    if (!choice.valid) { setMessage({ kind: 'bad', text: choice.reason }); return; }
    if (choice.already) return;
    if (choice.conflict) {
      setConfirm({ title: `${keyLabel(button)} is already bound`,
        lines: [`Current: ${choice.currentLabel}`, `Replace with: ${choice.targetLabel}`, 'Previous action will no longer trigger on this button.'],
        label: 'Replace Binding', args: { ...choice.args, replace: true }, success: `Updated ${keyLabel(button)} binding` });
    } else onOp(choice.args, `Bound ${keyLabel(button)} to "${choice.targetLabel}"`);
  };
  const jumpBind = slot => {
    if (!cap.select || !live || !link) {
      setMessage({ kind: 'bad', text: cap.reason || 'Connect duck and release torque first.' });
      return;
    }
    setPreset({ slot, tick: Date.now() });
    bindingsArea.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    if (!cap.capture) {
      setMessage({ kind: 'warn', text: safeCapture ? 'Refreshing gamepad config; retry Bind Button or use the dropdown below.' : 'Use the dropdown below to assign button bindings.' });
      bindingsArea.current?.querySelector('select')?.focus({ preventScroll: true });
      return;
    }
    setCapture({ slot, started: Date.now() });
    setCaptureNote('Waiting for gamepad input... release all buttons, then press an action button.');
    setSeconds(20);
    bindingsArea.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };
  useLayoutEffect(() => {
    if (!capture || !link) return undefined;
    let previous = null;
    let released = false;
    let done = false;
    const cancel = text => {
      if (done) return;
      done = true;
      setCapture(null);
      if (text) setMessage({ kind: 'bad', text });
    };
    const visible = () => { if (document.visibilityState !== 'visible') cancel('Button capture cancelled (tab hidden).'); };
    const watch = () => {
      if (!done && document.visibilityState === 'visible' && link.status === 'open') link.cmd('pad.op', { op: 'watch' }, 3000);
    };
    link.setPadCapture(true);
    watch();
    const off = link.on(message => {
      if (done) return;
      if (message.t === '_status' && message.status !== 'open') { cancel('Button capture cancelled (disconnected).'); return; }
      if (message.t === 'state' && (message.estop?.state !== 'confirmed' || message.robotd_ok === false)) {
        cancel('Button capture cancelled (confirm torque is OFF).'); return;
      }
      if (message.t !== 'pad') return;
      const input = message.live;
      if (input?.state !== 'attached') {
        previous = null; released = false;
        setCaptureNote('No physical gamepad detected; connect controller or use dropdown below.');
        return;
      }
      const buttons = input.buttons || {};
      if (!released) {
        previous = { ...buttons };
        released = !Object.values(buttons).some(Boolean);
        setCaptureNote(released ? 'Press A, X, LB, RB, or D-Pad Down now.' : 'Release all buttons on the gamepad first.');
        return;
      }
      const key = captureKey(previous, input);
      previous = { ...buttons };
      if (key) {
        done = true;
        setCapture(null);
        link.setPadCapture(false);
        bind(key, capture.slot);
      }
    });
    const timer = setInterval(() => {
      const left = Math.max(0, 20 - Math.floor((Date.now() - capture.started) / 1000));
      setSeconds(left);
      if (!left) cancel('Button capture timed out. Retry or use the dropdown below.');
    }, 250);
    const watchTimer = setInterval(watch, 2000);
    document.addEventListener('visibilitychange', visible);
    return () => { done = true; off(); clearInterval(timer); clearInterval(watchTimer); document.removeEventListener('visibilitychange', visible); link.setPadCapture(false); };
  }, [capture?.started]);
  useEffect(() => {
    if (capture && (!cap.select || !cap.capture || !live)) {
      setCapture(null);
      setMessage({ kind: 'bad', text: 'Button capture cancelled; keep connected with torque OFF.' });
    }
  }, [cap.select, cap.capture, live]);
  const items = view?.items || [];
  const groups = [['imported', 'Imported'], ['builtin', 'Built-in']];

  return html`<section class="models-page" aria-label="Model Manager" aria-busy=${busy || loading} data-capture-ready=${cap.capture ? 'true' : 'false'}>
    <header class="models-heading"><div><h1>Model Manager</h1><p>Import trained policies and assign them to control slots and gamepad buttons.</p></div>
      <button class="models-btn" type="button" aria-label="Refresh models" disabled=${busy || loading} onClick=${() => load()}>${loading ? 'Loading...' : 'Refresh'}</button>
    </header>
    ${loadError || view?.error ? html`<div class="models-notice bad" role="alert">${loadError || view.error}</div>` : null}
    ${view && !cap.select && cap.reason !== (loadError || view.error) ? html`<div class="models-notice warn" role="status">${cap.reason}</div>` : null}
    ${message ? html`<div class=${`models-notice ${message.kind}`} role=${message.kind === 'bad' ? 'alert' : 'status'}>${message.text}</div>` : null}
    ${busy ? html`<div class="models-notice progress" role="status">${pending}${upload != null && upload < 100 ? ` ${upload}%` : ''}
      ${upload != null ? html`<progress max="100" value=${upload} aria-label="Model upload progress"></progress>` : null}</div>` : null}
    <div ref=${confirmArea}>${confirm ? html`<${ConfirmBox} lines=${[confirm.title, ...confirm.lines]} confirmLabel=${confirm.label} cancelLabel="Cancel"
      busy=${busy} onCancel=${() => setConfirm(null)} onConfirm=${() => onOp(confirm.args, confirm.success)} cls="models-confirm" />` : null}</div>

    <section class="board models-panel" aria-label="Import Model">
      <h2>Import Model</h2><p class="models-note">Select a .zip or .tar.gz model package. Built-in models remain intact for easy rollback.</p>
      <form class="models-import" onSubmit=${doImport}>
        <label class="models-file-field">Model Package<input ref=${fileInput} type="file" accept=".zip,.tar.gz,.tgz" aria-label="Import model package" disabled=${busy || !cap.import}
          onChange=${e => { setFile(e.target.files[0] || null); setMessage(null); }} /></label>
        <label>Display Name (optional)<input type="text" value=${nameState} maxLength="80" placeholder="Leave blank to use package name" aria-label="Model name" disabled=${busy || !cap.import}
          onInput=${e => setName(e.target.value)} /></label>
        <button class="models-btn primary" type="submit" aria-label="Import Model" disabled=${busy || !cap.import || !fileState}>Import Model</button>
      </form>
      ${view && !cap.import ? html`<p class="models-note models-warning">Import unavailable: ${view.capabilities?.import_reason || cap.reason}</p>` : null}
      <details class="models-details"><summary>Required package contents</summary><p>Bundle policy.onnx, manifest.json, deployment-contract.json (and optional deployment-execution.json) from the same training export.</p></details>
    </section>

    ${!view && loading ? html`<div class="board models-panel models-note">Loading model library...</div>` : null}
    ${view ? groups.map(([source, label]) => {
      const group = items.filter(item => item.source === source);
      return html`<section class="board models-panel" aria-label=${label}>
        <div class="models-section-head"><h2>${label}</h2><span class="models-count">${group.length}</span></div>
        ${group.length ? html`<div class="models-list">${group.map(item => html`<${ModelItem} key=${item.id} item=${item} view=${view} cap=${cap} busy=${busy}
          onOp=${onOp} onConfirm=${setConfirm} onBind=${jumpBind} />`)}</div>` : html`<p class="models-note">${source === 'imported' ? 'No imported models yet.' : 'No built-in models found.'}</p>`}
      </section>`;
    }) : null}

    ${view ? html`<section class="board models-panel" aria-label="Active Slots" data-model-slots>
      <h2>Active Slots</h2><p class="models-note">"Selected" indicates the model assigned to a slot; switching models does not arm torque.</p>
      <div class="models-slot-list">${(view.slots || []).map(slot => html`<div class="models-slot" data-slot=${slot.slot}>
        <div><strong>${slot.label}</strong><p class="models-note">${itemName(view, slot.model_id) || (slot.path ? 'Unregistered model path' : 'Not installed')}${slot.error ? html`<br /><span class="models-error">${slot.error}</span>` : null}</p></div>
        <button class="models-btn" type="button" data-model-action="restore" aria-label=${`Restore default ${slot.label}`} disabled=${busy || !cap.select || !slot.default_id || slot.is_default || slot.default_id === slot.model_id}
          title=${!cap.select ? cap.reason : !slot.default_id ? 'No default saved for this slot' : slot.is_default ? 'Already using default' : undefined}
          onClick=${() => setConfirm({ title: slot.default_disabled ? `Restore default disabled state for "${slot.label}"?` : `Restore built-in model for "${slot.label}"?`,
            lines: [slot.default_disabled ? 'This slot had no model by default. Imported models stay in your library.' : 'Imported models remain in your library; button bindings stay intact.'],
            label: slot.default_disabled ? 'Disable Slot' : 'Restore Built-in', args: { op: 'restore', slot: slot.slot },
            success: `Restored ${slot.label}` })}>${slot.default_disabled ? 'Disable' : 'Restore Built-in'}</button>
      </div>`)}</div>
    </section>` : null}

    ${view ? html`<section class="board models-panel" aria-label="More Actions"><h2>Custom Actions</h2>
      <p class="models-note">Import and bind standalone ONNX actions on the <a href="#/actions">Actions page</a>.</p></section>` : null}

    ${view ? html`<section ref=${bindingsArea} class="board models-panel models-bindings" aria-label="Gamepad Bindings" id="models-bindings">
      <div class="models-section-head"><h2>Gamepad Bindings</h2><button class="models-btn" type="button" aria-label="Reset default bindings" disabled=${busy || !cap.select}
        onClick=${() => setConfirm({ title: 'Reset all 5 buttons to defaults?', lines: ['A, X, LB, RB, and D-Pad Down will revert to default actions. Selected models stay unchanged.'],
          label: 'Reset Defaults', args: { op: 'reset_bindings' }, success: 'Reset bindings to defaults' })}>Reset Defaults</button></div>
      <p class="models-note">Assign a model to a slot first, then bind a gamepad button. Walking always uses the stick.</p>
      ${capture ? html`<div class="models-capture" role="status" aria-label="Waiting for gamepad button"><strong>Binding "${slotName(view, capture.slot)}" · ${seconds}s</strong>
        <p>${captureNote}</p><button class="models-btn" type="button" aria-label="Cancel capture" onClick=${() => setCapture(null)}>Cancel Capture</button></div>` : null}
      ${preset ? html`<p class="models-bind-hint" role="status">Selected "${slotName(view, preset.slot)}": choose a button below and click Bind.</p>` : null}
      ${BUTTONS.map(button => html`<${BindingRow} key=${button.button} binding=${{ ...button, ...(view.bindings || []).find(b => b.button === button.button) }}
        view=${view} busy=${busy} cap=${cap} preset=${preset} onBind=${bind} onOp=${onOp} />`)}
    </section>` : null}
  </section>`;
}
