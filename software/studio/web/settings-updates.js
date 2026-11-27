import {html,useEffect,useRef,useState} from './vendor/preact-htm.module.js';
import {discoverUpdateNotice,updateMessage,updateOffer,updateActions,PHASES,nativeMessage,nativeActions} from './updates-ui-state.js';

const CSS=`.update-settings{padding:18px 20px;min-width:0;overflow-wrap:anywhere}.update-settings h2{font-size:20px;margin:0 0 8px}.update-settings p{font-size:13px;line-height:1.65;color:var(--text-2);margin:8px 0}.update-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.update-settings button{border:1px solid var(--hair);border-radius:var(--radius);padding:9px 14px;min-height:38px;font:inherit;font-size:14px;background:var(--panel-2);color:var(--ink);cursor:pointer}.update-settings button.primary{background:var(--accent);color:var(--paper);border-color:var(--accent);font-weight:600}.update-settings button:disabled{opacity:.4;cursor:default}.update-settings button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.update-settings [role=alert]{color:var(--danger)}.update-settings pre{white-space:pre-wrap;font:inherit;font-size:13px;line-height:1.6;max-height:200px;overflow:auto}.update-settings input{max-width:100%;font-size:13px}.update-settings progress{width:100%}@media(max-width:480px){.update-settings{padding:16px 12px}.update-settings button{max-width:100%;white-space:normal}}`;
if(!document.getElementById('updates-css')){
  const style=document.createElement('style');style.id='updates-css';style.textContent=CSS+`
    .update-offer{margin:12px 0;padding:12px 16px;background:var(--panel-2);border:1px solid var(--hair);border-radius:var(--radius)}
    .update-offer h4{margin:0;font-size:16px}.update-offer pre{margin:6px 0}
    .update-settings summary{cursor:pointer;font-size:14px}.update-settings [data-offline]{margin:12px 0}
    .update-settings{scroll-margin-top:calc(72px + var(--bar-h,0px))}
    .update-notice{display:inline-block;align-self:flex-start;padding:6px 0;color:var(--link);font-size:14px}
  `;document.head.appendChild(style);
}

export function UpdateNotice({api}){
  const [version,setVersion]=useState(null);
  useEffect(()=>{
    const controller=new AbortController();
    const {signal}=controller;
    const read=(body,timeout=10000)=>api('/api/updates',body,timeout,signal);
    const pause=ms=>new Promise(resolve=>{
      function done(){clearTimeout(timer);signal.removeEventListener('abort',done);resolve();}
      const timer=setTimeout(done,ms);
      signal.addEventListener('abort',done,{once:true});
    });
    discoverUpdateNotice(read,pause,signal).then(found=>{if(!signal.aborted)setVersion(found);});
    return ()=>controller.abort();
  },[api]);
  return version?html`<a class="update-notice" data-update-notice href="#/settings/updates">New version ${version} available · View Updates</a>`:null;
}

export function UpdateSettings({api,csrf}){
  const [data,setData]=useState(null),[pending,setPending]=useState(false),[error,setError]=useState(''),[reconnecting,setReconnecting]=useState(false);
  const [confirmed,setConfirmed]=useState(false),[readError,setReadError]=useState('');
  const mounted=useRef(true),loading=useRef(false),input=useRef(null);
  const submitting=useRef(false),revision=useRef(0),checkedOnEntry=useRef(false);
  const merge=result=>setData(previous=>({...previous,...result,native:result.native??previous?.native}));
  async function refresh(){
    if(loading.current||submitting.current)return;
    const started=revision.current;loading.current=true;
    try{const result=await api('/api/updates');if(!mounted.current||started!==revision.current)return;
      if(!result.ok){setReadError(result.error||'Failed to read update status');setReconnecting(!result.error);return;}
      setData(result);setReadError('');setReconnecting(false);
      if(result.available&&!checkedOnEntry.current){
        checkedOnEntry.current=true;
        if(result.online_update===true&&!result.busy&&!result.native?.active&&!result.native?.submitting)operate('check_auto');
      }
    }catch{if(mounted.current&&started===revision.current)setReconnecting(true);}finally{loading.current=false;}
  }
  useEffect(()=>{mounted.current=true;refresh();const timer=setInterval(refresh,2000);return()=>{mounted.current=false;clearInterval(timer);};},[]);
  async function operate(op,version){
    const native=op==='native_install'||op==='native_recover'||op==='native_restore';
    if(submitting.current||(native&&!confirmed))return;
    submitting.current=true;revision.current++;setPending(true);setError('');
    const body=native?{op,confirmed:true}:op==='online'?{op,version}:{op};
    try{const result=await api('/api/updates',body,20000);if(!mounted.current)return;
      if(result.ok)merge(result);
      else if(result.error)setError(result.error);
      else setReconnecting(true);
    }catch{if(mounted.current)setReconnecting(true);}
    finally{submitting.current=false;if(mounted.current){if(native)setConfirmed(false);setPending(false);refresh();}}
  }
  async function upload(event){
    const file=event.target.files?.[0];if(!file)return;
    if(!file.size||file.size>160*1024*1024){setError('Select a package under 160 MiB');event.target.value='';return;}
    if(submitting.current)return;
    submitting.current=true;revision.current++;setPending(true);setError('');
    try{const response=await fetch('/api/updates/upload',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/octet-stream','X-CSRF':csrf},body:file});
      const result=await response.json();if(!result.ok)throw new Error(result.error||'Upload failed');if(mounted.current)merge(result);
    }catch(reason){if(mounted.current)setError(reason.message||'Upload interrupted; reselect file');}
    finally{event.target.value='';submitting.current=false;if(mounted.current){setPending(false);refresh();}}
  }
  const unavailable=pending||reconnecting||!!readError;
  const actions=updateActions(data,unavailable),job=data?.job,native=data?.native,nativeOps=nativeActions(data,unavailable,confirmed);
  const online=data?.online_update===true;
  const offer=updateOffer(data);
  const manual=html`${data?.staged?html`<p>Staged ${data.staged.version} · ${data.ready?'Verified':'Pending verification'}</p>`:null}
  <div class="update-actions">
    <button type="button" data-update="upload" disabled=${!actions.upload} onClick=${()=>input.current?.click()}>Upload Offline Package</button>
    <input ref=${input} type="file" aria-label="Update package" accept=".tar" style="display:none" disabled=${!actions.upload} onChange=${upload} />
    <button type="button" data-update="validate" disabled=${!actions.validate} onClick=${()=>operate('validate')}>Re-verify</button>
    <button type="button" class=${online?'':'primary'} data-update="install" disabled=${!actions.install} onClick=${()=>operate('install')}>${online?'Install Verified Package':'Install Now'}</button>
  </div>`;
  return html`<section class="board update-settings" id="software-updates" tabindex="-1" data-updates aria-label="Software Updates">
    <h2>Software Updates</h2>
    <h3>1. Studio / Voice Software</h3>
    <p>Installed: <strong data-update-installed>${data?.installed||'Unregistered'}</strong> · Includes Studio web console, root helper, and local voice.</p>
    <p>Exit walking policy before updating (standing or relaxed is fine). Studio and voice restart briefly during install.</p>
    <p role="status" data-update-phase=${job?.phase||'idle'}>${updateMessage(data)}${data?.busy&&typeof job?.percent==='number'?` (${job.percent}%)`:''}</p>
    ${reconnecting?html`<p role="status">Reconnecting to updater service...</p>`:null}
    ${data?.busy&&typeof job?.percent==='number'?html`<progress max="100" value=${job.percent} aria-label=${PHASES[job.phase]||'Update progress'} />`:null}
    ${offer?html`<div class="update-offer" data-update-offer>
      <h4>Available: ${offer.version}</h4><pre>${offer.changelog||'No changelog provided.'}</pre>
      ${online?html`<button type="button" class="primary" data-update="online" disabled=${!actions.online} onClick=${()=>operate('online',offer.version)}>Update Now</button>`:null}
    </div>`:null}
    ${data?.available&&!online?html`<p>Online updates require initial preparation per release notes; offline .tar packages remain supported.</p>`:null}
    ${online&&data.catalog_checked_at>0?html`<p>Last checked · ${new Date(data.catalog_checked_at*1000).toLocaleString('en-US')}</p>`:null}
    <div class="update-actions">
      <button type="button" data-update="check" disabled=${!actions.check} onClick=${()=>operate('check')}>Check Updates</button>
      ${!online?html`<button type="button" data-update="download" disabled=${!actions.download} onClick=${()=>operate('download')}>Download</button>`:null}
      <button type="button" data-update="rollback" disabled=${!actions.rollback} onClick=${()=>operate('rollback')}>Roll Back Studio / Voice</button>
    </div>
    ${online?html`<details data-offline open=${job?.phase==='waiting_validation'}><summary>Offline Package & Cache</summary>${manual}</details>`:manual}
    ${pending?html`<p role="status">Submitting request...</p>`:null}
    ${error||readError?html`<p role="alert">${error||readError} <button type="button" onClick=${()=>{setError('');refresh();}}>Retry</button></p>`:null}
    ${job?.id?html`<p>Job: ${job.id}</p>`:null}
    <hr />
    <div data-native-updates style="scroll-margin-top:72px">
      <h3>2. ${native?.bundle_schema?'Companion Driver Update':'robotd Update (Optional)'}</h3>
      <p>${native?.bundle_schema?'Installs matching padd, robotd, and service permissions for gamepad mapping.':'Used for manual Qinmo IMU profile selection.'} Requires 20261027 baseline.</p>
      <p role="status" data-native-phase=${native?.phase||'unavailable'}>${nativeMessage(native)}</p>
      <p>Runtime: <strong data-native-identity>${native?.supported?'Verified':'Unverified'}</strong>${native?.mode?` · ${native.mode==='motion'?'Motion':native.mode==='commissioning'?'Commissioning':native.mode}`:''}</p>
      ${native?.installed_sha256||native?.transaction_id?html`<details><summary>Diagnostics</summary>${native?.installed_sha256?html`<p>robotd SHA256: ${native.installed_sha256}</p>`:null}${native?.components?.padd?html`<p>padd SHA256: ${native.components.padd.installed_sha256||'Unverified'}</p>`:null}${native?.transaction_id?html`<p>Transaction: ${native.transaction_id}</p>`:null}</details>`:null}
      <p>Support the duck, release torque, and stop all policies, voice, music, and calibration before confirming.</p>
      <label style="display:flex;align-items:flex-start;gap:8px;font-size:13px;line-height:1.6"><input type="checkbox" data-native-confirm checked=${confirmed} onChange=${event=>setConfirmed(event.target.checked)} disabled=${pending||!native?.service_available||!!data?.busy} /><span>I have supported the duck, released torque, and stopped active sessions; proceed with ${native?.bundle_schema?'companion driver':'robotd'} update/restore.</span></label>
      <div class="update-actions">
        <button type="button" class="primary" data-update="native_install" disabled=${!nativeOps.install} onClick=${()=>operate('native_install')}>Install ${native?.bundle_schema?'Companion Driver':'robotd'}</button>
        <button type="button" data-update="native_recover" disabled=${!nativeOps.recover} onClick=${()=>operate('native_recover')}>Recover Incomplete Update</button>
        ${native?.bundle_schema?html`<button type="button" data-update="native_restore" disabled=${!nativeOps.restore} onClick=${()=>operate('native_restore')}>Restore Previous Companion Driver</button>`:null}
      </div>
      ${native?.request_error?html`<p role="alert">${native.request_error}. E-Stop remains available.</p>`:null}
      <p>${native?.bundle_schema?'Failed installs roll back automatically. Restore previous companion driver before rolling back Studio / voice.':'Failed installs roll back robotd automatically.'}</p>
    </div>
  </section>`;
}
