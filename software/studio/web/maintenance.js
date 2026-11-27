import {html,useEffect,useState} from './vendor/preact-htm.module.js';
export function Maintenance({api}){
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState(''),[archive,setArchive]=useState(''),[preview,setPreview]=useState(null),[password,setPassword]=useState('');
 const receiptKey='duck.maintenance.restore';
 const remember=id=>{try{sessionStorage.setItem(receiptKey,id);}catch{}};
 const forget=()=>{try{sessionStorage.removeItem(receiptKey);}catch{}};
 const recover=async(id,active=()=>true)=>{
   setNote('Verifying restore status...');
   for(let attempt=0;attempt<60 && active();attempt++){
     try{
       const result=await api('/api/maintenance?restore='+id,undefined,3000);
       if(!active())return;
       if(result.status===401){setError('Sign in again to view restore status');return;}
       if(result.id===id && result.phase==='completed'){
         forget();setPreview(null);setArchive('');setNote('Restored '+result.restored+' configs. Pre-restore backup: '+result.backup);return;
       }
       if(result.id===id && result.phase==='failed'){
         forget();setError(result.error||'Restore failed; check maintenance logs');setNote('');return;
       }
     }catch{}
     await new Promise(resolve=>setTimeout(resolve,1000));
   }
   if(active())setNote('Restore confirmation pending. Reconnect and reopen this page to check.');
 };
 useEffect(()=>{
   let active=true;
   let pending;try{pending=sessionStorage.getItem(receiptKey);}catch{}
   if(pending && /^[a-f0-9]{32}$/.test(pending)){
     setBusy(true);recover(pending,()=>active).finally(()=>{if(active)setBusy(false);});
   }
   return ()=>{active=false;};
 },[]);
 const run=async(body)=>{const r=await api('/api/maintenance',body,50000);if(!r.ok)throw Error(r.error||'Operation failed');return r;};
 const download=async()=>{setBusy(true);setError('');try{const r=await fetch('/api/maintenance',{credentials:'same-origin'});if(!r.ok){const d=await r.json();throw Error(d.error||'Export failed');}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='microduck-backup-'+new Date().toISOString().replace(/[:.]/g,'-')+'.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);setNote('Backup generated; check your browser downloads.');}catch(e){setError(e.message);}finally{setBusy(false);}};
 const upload=async(file)=>{setPassword('');setPreview(null);setArchive('');if(!file)return;setBusy(true);setError('');try{if(file.size>16*1024*1024)throw Error('Backup file must not exceed 16 MB');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));const b64=btoa(binary);const r=await run({op:'preview',archive:b64});setArchive(b64);setPreview(r);setNote('Validated; review files before restoring');}catch(e){setError(e.message);}finally{setBusy(false);}};
 const restore=async()=>{
   if(!password||!confirm('Restore selected configs? Support the duck and release torque first. Current configs will be backed up before restarting services.'))return;
   const id=Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
   setPassword('');setBusy(true);setError('');remember(id);
   try{
     try{
       const r=await api('/api/maintenance',{op:'restore',archive,confirmed:true,password,request_id:id},50000);
       if(!r.ok && r.status>=400 && r.status<500){forget();setError(r.error||'Restore request rejected');return;}
     }catch{/* Studio may restart before reply arrives */}
     await recover(id);
   }finally{setBusy(false);}
 };
 const shutdown=async()=>{if(!confirm('Safe shutdown: support the duck first—servos will relax. Wait at least 20s after confirmation before cutting power.'))return;setBusy(true);setError('');try{await run({op:'shutdown',confirmed:true});setNote('Shutdown scheduled (disconnect is expected). Wait at least 20s and confirm the board has halted before cutting power.');}catch(e){setError(e instanceof TypeError||e.name==='AbortError'?'Connection lost before shutdown confirmation. Verify the board has halted before cutting power.':e.message);}finally{setBusy(false);}};
 return html`<section class="m-card m-pad" style="padding:18px;margin:16px 0;text-align:left;overflow-wrap:anywhere" data-maintenance><h3>Shutdown & Backup</h3>
 <p>Closing this browser tab does not power off the duck. Always use Safe Shutdown before cutting power.</p>
 <div style="display:flex;gap:10px;flex-wrap:wrap"><button type="button" class="m-btn btn" disabled=${busy} onClick=${shutdown}>Safe Shutdown</button><button type="button" class="m-btn btn" disabled=${busy} onClick=${download}>Export Backup</button></div>
 <p>Backups include servo/IMU calibration, gamepad mappings, saved connections, camera device tree, ToF startup config, and source code for offline recovery. Keep private (contains API keys); login password and sessions are excluded.</p>
 <label>Import Backup Archive<input style="display:block;width:100%;max-width:100%" aria-label="Import backup archive" type="file" accept=".zip" disabled=${busy} onChange=${e=>upload(e.target.files?.[0])}/></label>
 ${preview?html`<div><p>Source: ${preview.hostname} · ${preview.created}</p><details><summary>Will restore ${preview.files.length} config files</summary><ul>${preview.files.map(n=>html`<li>${n.replace(/^config\//,'/')}</li>`)}</ul></details><p>Import restores configs only; bundled source is not executed. ${preview.skipped&&preview.skipped.length?'Camera device tree is export-only and must be applied manually.':''}</p><label>Confirm Web Login Password<input type="password" autocomplete="current-password" value=${password} onInput=${e=>setPassword(e.target.value)} /></label><button type="button" class="m-btn btn" disabled=${busy||!password} onClick=${restore}>Confirm Restore</button></div>`:null}
 ${busy?html`<p role="status">Processing, do not cut power...</p>`:null}${note?html`<p role="status">${note}</p>`:null}${error?html`<p role="alert">${error}</p>`:null}</section>`;
}
