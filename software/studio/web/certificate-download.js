import {html,useState} from './vendor/preact-htm.module.js';
export async function downloadCa(){
 const response=await fetch('/ca.crt',{credentials:'same-origin',cache:'no-store'});
 if(!response.ok)throw Error(response.status===404?'No local CA certificate generated on this device':'Certificate read failed (HTTP '+response.status+')');
 const data=await response.arrayBuffer();
 if(data.byteLength<100||data.byteLength>65536)throw Error('Unexpected certificate file size; do not install');
 const blob=new Blob([data],{type:'application/octet-stream'}),url=URL.createObjectURL(blob);
 const a=document.createElement('a');a.href=url;a.download='Microduck-Local-CA.crt';a.style.display='none';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
 return data.byteLength;
}
export function CertificateDownload(){
 const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
 const click=async()=>{setBusy(true);setError('');setMessage('');try{await downloadCa();setMessage('Certificate downloaded: check Microduck-Local-CA.crt in your browser downloads.');}catch(e){setError(e.message||'Download failed; use the direct link.');}finally{setBusy(false);}};
 return html`<div data-certificate-download style="margin:12px 0"><div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center"><button type="button" class="m-btn btn" disabled=${busy} onClick=${click}>${busy?'Reading certificate...':'Download HTTPS CA'}</button><a href="/ca.crt" target="_blank" rel="noopener">Open certificate directly</a></div>${message?html`<p role="status">${message}</p>`:null}${error?html`<p role="alert">${error}</p>`:null}</div>`;
}
