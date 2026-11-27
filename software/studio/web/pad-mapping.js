import { html, useEffect, useRef, useState } from './vendor/preact-htm.module.js';

const BUTTONS=[['south','A (South)'],['east','B (East)'],['west','X (West)'],['north','Y (North)'],['lb','LB'],['rb','RB'],
  ['select','Select / Back'],['start','Start'],['du','D-Pad Up'],['dd','D-Pad Down'],['dl','D-Pad Left'],['dr','D-Pad Right'],
  ['l3','L3 (Left Stick)'],['r3','R3 (Right Stick)'],['home','Home']];
const AXES=[['lx','Left Stick X'],['ly','Left Stick Y'],['rx','Right Stick X'],['ry','Right Stick Y'],['lt','LT'],['rt','RT']];
const ALL=[...BUTTONS,...AXES], NAMES=Object.fromEntries(ALL), REQUIRED=new Set(['select','start','lx','ly','rx','ry']);
const instruction=role=>{
  if(['lx','rx'].includes(role))return 'Push fully right, then fully left, release to center, and click Confirm Axis.';
  if(['ly','ry'].includes(role))return 'Push fully down, then fully up, release to center, and click Confirm Axis.';
  if(['lt','rt'].includes(role))return 'Pull trigger fully, release completely, and click Confirm Axis.';
  return `Press and release the physical button for "${NAMES[role]}".`;
};

export function PadMapping({ mapping, link, active, Diagram, onClose }) {
  const [view,setView]=useState(mapping||{}),[error,setError]=useState(null),[busy,setBusy]=useState(false);
  const [name,setName]=useState(''),[reset,setReset]=useState(false),[automatic,setAutomatic]=useState(false);
  const current=useRef(view),closed=useRef(false),oldCapture=useRef(null),activeRef=useRef(active);
  current.current=view;
  activeRef.current=active;
  useEffect(()=>{if(mapping)setView(mapping)},[mapping]);
  const call=async(action,extra={})=>{
    setError(null);setBusy(true);
    try{
      const r=await link.cmd('pad.op',{op:'mapping',action,...extra},8000);
      if(closed.current)return null;
      if(!r.ok){setError(r.reason||r.error||'Operation failed; retry');return null}
      if(r.data)setView(r.data);
      return r.data;
    }catch{if(!closed.current)setError('Disconnected from duck; reconnect and retry');return null}
    finally{if(!closed.current)setBusy(false)}
  };
  const learn=(role,auto=false)=>{if(!auto)setAutomatic(false);return call('learn',{role})};
  const begin=async()=>{
    setReset(false);const next=await call('begin');
    if(next?.mine){setName(next.profile?.name||next.device?.name||'My Gamepad');setAutomatic(true);await learn('south',true)}
  };
  const nextRole=role=>{
    const index=ALL.findIndex(([r])=>r===role);
    if(index>=0&&index+1<ALL.length)learn(ALL[index+1][0],true);else setAutomatic(false);
  };
  useEffect(()=>{
    const previous=oldCapture.current;
    if(automatic&&previous&&!view.capture&&view.draft?.[previous])nextRole(previous);
    oldCapture.current=view.capture?.role||null;
  },[view.capture?.role,view.draft]);
  useEffect(()=>{
    closed.current=false;
    call('status');
    const cancel=()=>{
      if(current.current.mine)call('cancel');
      setAutomatic(false);
    };
    const visible=()=>{if(document.visibilityState!=='visible')cancel()};
    const timer=setInterval(()=>{
      if(!activeRef.current||document.visibilityState!=='visible')return;
      const action=current.current.mine?'heartbeat':'status';
      link.cmd('pad.op',{op:'mapping',action},4000).then(r=>{
        if(closed.current)return;
        if(r.ok&&r.data)setView(r.data);
      }).catch(()=>{});
    },1000);
    document.addEventListener('visibilitychange',visible);
    return()=>{cancel();closed.current=true;clearInterval(timer);document.removeEventListener('visibilitychange',visible)};
  },[]);
  useEffect(()=>{
    if(!active&&view.mine){setAutomatic(false);call('cancel')}
  },[active,view.mine]);
  const close=async()=>{setAutomatic(false);if(view.mine)await call('cancel');onClose()};
  const draft=view.mine?view.draft||{}:view.profile?.bindings||{};
  const capture=view.capture,role=capture?.role;
  const shown=view.mine?(view.preview||{buttons:{},axes:{}}):view.mapped||{buttons:{},axes:{}};
  const canLearn=view.mine&&!busy;
  const disabled=busy||!view.available||view.output_blocked||!!view.owned;
  const skip=async target=>{const r=await call('skip',{role:target});if(r&&automatic)nextRole(target)};
  const row=([target,label])=>html`<div class=${'map-control-row'+(AXES.some(([r])=>r===target)?' map-axis-row':'')+(role===target?' selected':'')}>
    <button type="button" class="map-target" data-map-role=${target} data-bound=${!!draft[target]}
      disabled=${!canLearn} onClick=${()=>learn(target)} aria-pressed=${role===target}>
      <span>${label}${REQUIRED.has(target)?html`<small>Required</small>`:null}</span><span class="map-binding">${draft[target]?'Mapped':'Unset'}</span></button>
    ${canLearn&&draft[target]?html`<button type="button" class="map-text-button" data-map-clear=${target} aria-label=${'Clear '+label}
      onClick=${()=>{setAutomatic(false);call('clear',{role:target})}}>Clear</button>`:null}
    ${canLearn&&draft[target]?.kind==='axis'?html`<button type="button" class="map-text-button" onClick=${()=>call('invert',{role:target})}>${draft[target].invert?'Inverted ✓':'Invert'}</button>`:null}
    ${canLearn&&draft[target]?.kind==='axis'?html`<label class="map-axis-deadzone"><span>Deadzone</span><select data-map-deadzone=${target}
      aria-label=${label+' deadzone'} disabled=${busy} value=${String(draft[target].deadzone)}
      onChange=${e=>call('deadzone',{role:target,value:Number(e.currentTarget.value)})}>
      ${[.02,.04,.08,.12,.18,.25,.3].map(value=>html`<option value=${String(value)}>${Math.round(value*100)}%</option>`)}</select></label>`:null}
  </div>`;
  return html`<section class="board pad-mapping" data-mine=${!!view.mine} aria-labelledby="pad-mapping-title">
    <header class="map-header"><div><span class="map-eyebrow">Physical Gamepad · Input Learning</span><h3 id="pad-mapping-title">Remap / Calibrate</h3></div>
      <button type="button" class="map-button" disabled=${busy} onClick=${close}>${view.mine?'Cancel & Close':'Close'}</button></header>
    <p class="map-lead">Click a standard button on the diagram, then press the corresponding physical button on your controller.</p>
    <div class="map-device" data-map-profile><b>${view.device?.name||'Waiting for gamepad'}</b><span>${view.profile?'Saved: '+view.profile.name:'Default mapping · No custom profile saved'}</span></div>
    ${view.output_blocked?html`<p class="map-paused" role="status" data-map-blocked>Gamepad motion output paused. ${view.mine?'Learning/testing input only.':'Release all buttons and center sticks.'} Web E-Stop remains active.</p>`:null}
    ${error||view.error?html`<p class="map-error" role="alert">${error||view.error}</p>`:null}
    ${view.message?html`<p class="map-message" data-map-message role="status">${view.message}</p>`:null}
    ${!view.mine?html`<div class="map-start"><p>Stop walking (Stand) before starting input learning. Saved profiles load automatically on reconnect.</p>
      <button class="map-button primary" type="button" data-map-begin disabled=${disabled} onClick=${begin}>${view.profile?'Edit Saved Mapping':'Start Button Learning'}</button>
      ${view.owned?html`<p>Another tab is currently mapping this controller.</p>`:null}
      ${!view.available?html`<button class="map-button" type="button" disabled=${busy} onClick=${()=>call('status')}>Retry Gamepad Service</button>`:null}</div>`:null}
    <div class="map-layout"><div class="map-visual">
      <div class="map-diagram"><${Diagram} live=${shown} kind="xbox" mappingView=${true} learn=${canLearn?target=>learn(target==='ls'?'lx':target==='rs'?'rx':target):null} /></div>
      <p class="map-help">Diagram uses standard Xbox layout (A bottom, B right, X left, Y top). Click sticks for horizontal axis; select vertical axes & L3/R3 on the right.</p>
      ${capture?html`<div class="map-capture" role="status" data-map-capture data-role=${role}>
        <span>Learning</span><b>${NAMES[role]}</b><p>${instruction(role)}</p>
        <small>${capture.state==='sweeping'?'Axis detected; sweep both ends, center, and confirm.':capture.state==='error'?'Click the target again to retry.':'Waiting for controller input...'}</small>
        <div class="map-actions">${AXES.some(([r])=>r===role)?html`<button type="button" class="map-button primary" data-map-confirm-axis disabled=${busy||capture.code==null||capture.state==='error'} onClick=${()=>call('confirm_axis')}>Confirm Axis</button>`:null}
          ${!REQUIRED.has(role)?html`<button type="button" class="map-button" disabled=${busy} onClick=${()=>skip(role)}>Skip</button>`:null}
          <button type="button" class="map-button" disabled=${busy} onClick=${()=>learn(role)}>Retry</button></div>
      </div>`:view.mine?html`<p class="map-help">Mapped controls highlight on the diagram. Test sticks and buttons to verify.</p>`:null}
    </div><div class="map-controls"><h4>Buttons</h4><div class="map-button-grid">${BUTTONS.map(row)}</div>
      <h4>Sticks & Triggers</h4>${AXES.map(row)}
      ${view.mine?html`<p class="map-help">Deadzone suppresses stick drift at center; Invert flips axis direction.</p>`:null}
    </div></div>
    ${view.mine?html`<footer class="map-save"><label>Profile Name<input type="text" maxLength="60" value=${name} placeholder="e.g. GameSir G8" onInput=${e=>setName(e.target.value)} /></label>
      <p class="map-help">Select, Start, and all 4 stick axes are required. Release all inputs after saving and re-test E-Stop.</p>
      ${view.required_missing?.length?html`<p class="map-help">Required remaining: ${view.required_missing.map(r=>NAMES[r]).join(', ')}</p>`:null}
      <div class="map-actions"><button type="button" class="map-button primary" data-map-save disabled=${busy||!!capture||!!view.required_missing?.length||!name.trim()}
        onClick=${()=>{setAutomatic(false);call('save',{name})}}>Save & Apply</button>
        <button class="map-button" type="button" data-map-cancel disabled=${busy} onClick=${()=>{setAutomatic(false);call('cancel')}}>Cancel</button>
        <button class="map-text-button" type="button" data-map-reset disabled=${busy} onClick=${()=>setReset(true)}>Reset Default</button></div>
      ${reset?html`<div class="map-reset"><p>Delete custom mapping and restore default detection?</p><button type="button" class="map-button" type="button" data-map-reset-confirm disabled=${busy}
        onClick=${()=>{setAutomatic(false);setReset(false);call('reset')}}>Confirm Reset</button><button type="button" class="map-button" type="button" onClick=${()=>setReset(false)}>Keep Profile</button></div>`:null}
    </footer>`:null}
  </section>`;
}
