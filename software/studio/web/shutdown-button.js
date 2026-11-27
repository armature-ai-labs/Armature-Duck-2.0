import {html,useState} from './vendor/preact-htm.module.js';
export function ShutdownButton({api,onNotice}){
 const [busy,setBusy]=useState(false),[scheduled,setScheduled]=useState(false),[message,setMessage]=useState('');
 const shutdown=async()=>{
  if(busy||scheduled||!confirm('Safe shutdown: support the duck first—servos will relax. Wait at least 20s after confirmation before cutting power.'))return;
  setBusy(true);setMessage('');
  try{const r=await api('/api/maintenance',{op:'shutdown',confirmed:true},50000);if(!r.ok)throw Error(r.error||'Shutdown not confirmed');setScheduled(true);const text='Shutdown scheduled. Wait at least 20s and confirm the board has halted before cutting power.';setMessage(text);onNotice?.(text);}
  catch(e){const text=e instanceof TypeError||e.name==='AbortError'?'Connection lost before shutdown confirmation. Verify the board has halted before cutting power.':e.message||'Shutdown failed; check connection';setMessage(text);onNotice?.(text,true);}
  finally{setBusy(false);}
 };
 return html`<span style="display:inline-flex;flex-shrink:0"><button type="button" class="shutdown-button btn-light" disabled=${busy||scheduled} onClick=${shutdown} aria-label="Safe Shutdown: relax servos and power off">${busy?'Shutting down...':scheduled?'Shutdown Scheduled':'Safe Shutdown'}</button>${message&&!onNotice?html`<span role="status" style="position:fixed;left:16px;right:16px;bottom:20px;z-index:9999;margin:auto;max-width:520px;padding:12px;border-radius:var(--radius);background:var(--panel-2);color:var(--ink);border:1px solid var(--hair-strong);white-space:normal">${message}</span>`:null}</span>`;
}
