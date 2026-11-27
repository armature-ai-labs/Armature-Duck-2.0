import { html, useEffect, useRef, useState } from './vendor/preact-htm.module.js';
import { Maintenance } from './maintenance.js';
import { CertificateDownload } from './certificate-download.js';
import { ARK_URL, blankDraft, draftFrom, isDirty, missingFields, requestMessages } from './ai-ui-state.js';

const CSS = `.ai-settings{padding:18px 20px;min-width:0}.ai-settings h2{margin:0 0 8px;font-size:20px}.ai-settings p{font-size:13px;line-height:1.65;color:var(--text-2);margin:8px 0}.ai-settings .ai-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:14px 0}.ai-settings label{display:flex;flex-direction:column;gap:5px;font-size:13px;min-width:0}.ai-settings input,.ai-settings select,.ai-settings textarea{width:100%;min-width:0;box-sizing:border-box;border:1px solid var(--hair-strong);border-radius:var(--radius);background:var(--panel);color:var(--ink);padding:9px;font:inherit}.ai-settings .ai-wide{grid-column:1/-1}.ai-settings .ai-check{flex-direction:row;align-items:center}.ai-settings .ai-check input{width:auto}.ai-settings .btn{border:1px solid var(--hair);border-radius:var(--radius);padding:9px 15px;min-height:38px;font:inherit;font-size:14px;background:var(--panel-2);color:var(--ink);cursor:pointer}.ai-settings .btn.primary{background:var(--accent);color:var(--paper);border-color:var(--accent);font-weight:600}.ai-settings .btn:disabled{opacity:.4;cursor:default}.ai-settings .btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.ai-actions{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.ai-settings .ai-result{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.ai-settings .ai-chat{border-top:1px solid var(--hair);margin-top:18px;padding-top:12px}.ai-settings .ai-message{white-space:pre-wrap;overflow-wrap:anywhere;border-radius:var(--radius);padding:9px 11px;margin:7px 0;background:var(--panel-2);border:1px solid var(--hair-subtle);font-size:14px}.ai-settings .ai-message strong{font-size:12px;display:block;margin-bottom:3px;color:var(--accent)}.ai-settings [role=alert]{color:var(--danger)}@media(max-width:480px){.ai-settings{padding:16px 12px}.ai-settings .ai-fields{grid-template-columns:1fr}.ai-settings .btn{max-width:100%;white-space:normal}}`;
if (!document.getElementById('ai-settings-css')) {
  const style = document.createElement('style'); style.id = 'ai-settings-css'; style.textContent = CSS; document.head.appendChild(style);
}

export function AiSettings({ api }) {
  const [data,setData] = useState(null), [form,setForm] = useState(blankDraft);
  const [selected,setSelected] = useState(''), [busy,setBusy] = useState(false);
  const [error,setError] = useState(''), [notice,setNotice] = useState(''), [tested,setTested] = useState(false);
  const [history,setHistory] = useState([]), [input,setInput] = useState('');
  const alive = useRef(false), controller = useRef(null), pending = useRef(false);
  const clearConversation = () => {setHistory([]);setInput('');setTested(false);setNotice('');};
  const apply = (response, id) => {
    setData(response);setSelected(id || '');
    setForm(draftFrom(response.connections.find(item=>item.id === id)));
  };
  const run = async (body, done) => {
    if (pending.current) return;
    pending.current = true;setBusy(true);setError('');
    const ctl = new AbortController();controller.current = ctl;
    try {
      const response = await api('/api/ai',body,24000,ctl.signal);
      if (!alive.current) return;
      if (!response.ok) {
        if (response.code === 'stale') clearConversation();
        setError(response.error || 'AI request failed');return;
      }
      done(response);
    } catch (error) {
      if (alive.current) setError(error?.name === 'AbortError' ? 'AI request timed out or cancelled' : 'Connection lost; retry');
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  };
  useEffect(()=>{
    alive.current = true;
    run(undefined,response=>apply(response,response.current_id));
    return ()=>{alive.current = false;controller.current?.abort();};
  },[]);
  const saved = data?.connections.find(item=>item.id === selected);
  const dirty = isDirty(form,saved), missing = missingFields(form,saved);
  const ready = !!saved?.ready && !dirty && !busy;
  const edit = (key,value) => {setForm({...form,[key]:value});setError('');clearConversation();};
  const choose = id => {setSelected(id);setForm(draftFrom(data.connections.find(item=>item.id === id)));setError('');clearConversation();};
  const save = () => run({op:'save',connection:form},response=>{apply(response,response.saved_id);clearConversation();setNotice('Connection saved locally (provider not contacted)');});
  const select = () => run({op:'select',id:selected},response=>{apply(response,selected);clearConversation();setNotice('Set as active connection');});
  const remove = () => run({op:'delete',id:selected},response=>{apply(response,'');clearConversation();setNotice('Connection deleted');});
  const test = () => {setTested(false);setNotice('');run({op:'test',id:selected,revision:saved.revision},response=>{
    if (response.id === selected && response.revision === saved.revision) {setTested(true);setNotice(response.text);}
  });};
  const send = () => {
    const content = input.trim();if (!content || !ready) return;
    const messages = requestMessages(history,content);
    run({op:'chat',id:selected,revision:saved.revision,messages},response=>{
      if (response.id === selected && response.revision === saved.revision) {
        setHistory([...messages,{role:'assistant',content:response.text}].slice(-10));setInput('');
      }
    });
  };
  return html`<section id="ai-settings" class="board ai-settings" data-ai-settings aria-label="AI Chat Settings">
    <h3>Phone HTTPS Certificate</h3>
    <${CertificateDownload} />
    <${Maintenance} api=${api} />
    <h2>AI Chat</h2>
    <p>Configure a provider and model for text testing. Audio/voice pipelines are configured separately and do not drive motion here.</p>
    ${error ? html`<p role="alert">${error}</p>` : null}
    ${!data ? html`<p>${busy ? 'Loading AI settings...' : 'AI settings unavailable. Set a web login password and refresh.'}</p>` : html`
    <label>Saved Connections<select aria-label="Saved connections" value=${selected} disabled=${busy} onChange=${e=>choose(e.target.value)}>
      <option value="">New Connection</option>${data.connections.map(item=>html`<option value=${item.id}>${item.name || 'Untitled Draft'}${item.id === data.current_id ? ' (Active)' : ''}</option>`)}
    </select></label>
    <p data-ai-current>Active: ${data.connections.find(item=>item.id === data.current_id)?.name || 'None'} · ${data.connections.length}/10 saved</p>
    <div class="ai-actions"><button type="button" class="btn" data-ai="new" disabled=${busy || data.connections.length >= 10} onClick=${()=>choose('')}>New Connection</button></div>
    <fieldset disabled=${busy} style="border:0;padding:0;margin:0;min-width:0" data-ai-connection=${selected || ''}>
      <div class="ai-fields">
        <label>Connection Name<input aria-label="Connection name" maxlength="80" value=${form.name} onInput=${e=>edit('name',e.target.value)} placeholder="e.g. Doubao Text" /></label>
        <label>Provider<select aria-label="Provider" value=${form.provider} onChange=${e=>{
          const provider=e.target.value;setForm({...form,provider,base_url:provider === 'doubao_ark' ? ARK_URL : '',api_key:'',clear_key:false});setError('');clearConversation();
        }}><option value="doubao_ark">Doubao Ark (Text)</option><option value="openai_compatible">OpenAI-Compatible API</option></select></label>
        <label class="ai-wide">Base URL<input aria-label="Base URL" maxlength="500" value=${form.base_url} onInput=${e=>edit('base_url',e.target.value)} placeholder="https://api.example.com/v1" spellcheck="false" /></label>
        <label class="ai-wide">Model ID<input aria-label="Model ID" maxlength="200" value=${form.model} onInput=${e=>edit('model',e.target.value)} placeholder="Model ID or endpoint ID" spellcheck="false" /></label>
        <label class="ai-wide">API Key<input type="password" aria-label="API Key" value=${form.api_key} maxlength="4096" autocomplete="new-password" onInput=${e=>edit('api_key',e.target.value)} placeholder=${saved?.key_configured ? 'Saved; leave blank to keep existing key' : 'Not configured (drafts can be saved)'} /></label>
        <label class="ai-check ai-wide"><input type="checkbox" checked=${form.clear_key} onChange=${e=>edit('clear_key',e.target.checked)} />Clear saved API Key</label>
        <label class="ai-check ai-wide"><input aria-label="No API Key required (local service only)" type="checkbox" checked=${form.auth_mode === 'none'} onChange=${e=>edit('auth_mode',e.target.checked ? 'none' : 'bearer')} />No API Key required (local service only)</label>
      </div>
    </fieldset>
    <p>API keys are stored only on the duck and are never echoed to browser storage, logs, or diagnostic bundles.</p>
    ${missing.length ? html`<p data-ai-missing>Missing: ${missing.join(', ')}. Complete and save before testing or chatting.</p>` : dirty ? html`<p>Unsaved changes; save before testing.</p>` : null}
    <div class="ai-actions">
      <button class="btn primary" type="button" data-ai="save" disabled=${busy || !dirty || (!saved && data.connections.length >= 10)} onClick=${save}>Save Connection</button>
      <button class="btn" type="button" data-ai="select" disabled=${!ready || data.current_id === selected} onClick=${select}>Set Active</button>
      <button class="btn" type="button" data-ai="delete" disabled=${busy || !saved} onClick=${remove}>Delete</button>
      <button class="btn" type="button" data-ai="test" disabled=${!ready} onClick=${test}>Test Connection</button>
    </div>
    <p>Saving does not contact the provider; Test Connection sends a short prompt.</p>
    ${busy ? html`<p role="status">Working...</p>` : null}
    ${notice ? html`<p class="ai-result" role="status" data-ai-tested=${tested}>${tested ? 'Test OK: ' : ''}${notice}</p>` : null}
    <div class="ai-chat"><strong>Text Test Chat</strong><p>In-memory only (max 5 turns, 12,000 chars). Cleared on switch, edit, or leaving this page.</p>
      <div aria-live="polite" data-ai-history>${history.map(message=>html`<div class="ai-message"><strong>${message.role === 'user' ? 'You' : 'Model'}</strong>${message.content}</div>`)}</div>
      <label>Message<textarea aria-label="Chat message" rows="3" maxlength="2000" disabled=${busy} value=${input} onInput=${e=>setInput(e.target.value)} placeholder="Type a test message after testing connection" /></label>
      <div class="ai-actions"><button class="btn primary" type="button" data-ai="send" disabled=${!ready || !input.trim()} onClick=${send}>Send</button><button class="btn" type="button" data-ai="clear" disabled=${busy || (!history.length && !input)} onClick=${()=>{setHistory([]);setInput('');}}>Clear Chat</button></div>
    </div>
    <p>Note: Realtime voice WebSocket endpoints are configured separately from this text API form.</p>`}
  </section>`;
}
