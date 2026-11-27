export const PHASES={queued:'Queued',uploading:'Uploading',checking:'Checking release source',downloading:'Downloading',downloaded:'Downloaded',
  preflight:'Checking motion takeover state',verifying:'Verifying signature & package',extracting:'Verifying runtime files',swapping:'Switching version',
  applying:'Starting Studio & voice services',health_gate:'Checking Studio & local voice',committing:'Recording installation result',
  rolling_back:'Rolling back to previous version',recovering:'Verifying saved updater result',completed:'Completed',failed:'Failed',interrupted:'Interrupted; verify result',
  waiting_validation:'Downloaded; awaiting re-verification',rolled_back:'Update failed; rolled back'};
export function updateOffer(data){
  const candidate=data?.candidate;
  return data?.source_state==='available'&&candidate?.version&&candidate.version!==data.installed?candidate:null;
}

export async function discoverUpdateNotice(read,pause,signal){
  const limit=10*60*1000,deadline=performance.now()+limit;
  let waitBudget=limit,delay=1000;
  const remaining=()=>Math.min(waitBudget,deadline-performance.now());
  const usable=result=>!signal.aborted&&result?.ok&&result.available&&result.online_update===true
    &&!result.native?.active&&!result.native?.submitting;
  try{
    if(signal.aborted)return null;
    let status=await read();
    if(!usable(status))return null;
    if(!status.busy)status=await read({op:'check_auto'});
    while(usable(status)&&status.busy&&status.catalog_checking===true){
      const wait=Math.min(delay,remaining());
      if(wait<=0)return null;
      await pause(wait);
      waitBudget-=wait;
      if(signal.aborted||remaining()<=0)return null;
      status=await read(undefined,Math.min(10000,remaining()));
      delay=Math.min(delay*2,30000);
    }
    return usable(status)&&!status.busy&&remaining()>0?updateOffer(status)?.version||null:null;
  }catch{return null;}
}

export function updateMessage(data){
  if(!data)return 'Reading update status...';
  if(!data.available)return 'Software updater service is not installed or unavailable';
  const job=data.job;
  if(job?.phase==='rolled_back')return 'Update failed; rolled back. '+(job.error||'');
  if(job?.phase==='waiting_validation')return PHASES.waiting_validation+'. '+(job.error||'');
  if(job?.phase==='failed'||job?.phase==='interrupted')return (PHASES[job.phase]||'Task incomplete')+': '+(job.error||'Please retry');
  if(data.busy)return PHASES[job?.phase]||'Update in progress';
  if(job?.phase==='completed'&&['applied','already_current'].includes(job.result?.outcome)){
    const version=job.result.to||data.installed;
    if(job.operation==='rollback')return version?`Rolled back Studio / voice to ${version}.`:'Rolled back Studio / voice to previous version.';
    if(job.operation==='install'||job.operation==='online')return version?`Updated Studio / voice to ${version}.`:'Studio / voice updated.';
  }
  if(data.source_state==='installed_unknown')return 'Cannot verify local installed version right now; check again later.';
  if(data.online_update===true&&updateOffer(data))return 'New official release available.';
  if(data.ready)return 'Signature and runtime files verified; ready to install.';
  if(data.source_state==='no_release')return 'No published releases available.';
  if(data.source_state==='network_error')return 'Network check failed; retry later.';
  if(data.source_state==='up_to_date')return 'Software is up to date with the release source.';
  if(data.source_state==='available')return 'Update available; download to verify.';
  return job?.phase==='completed'?'Completed.':'Check for updates or upload an offline package.';
}
export function updateActions(data,pending=false){
  const idle=!!data?.available&&!data.busy&&!pending;
  const nativeBusy=data?.native?.active===true||data?.native?.submitting===true;
  return {check:idle,upload:idle,download:idle&&!!data.candidate&&data.source_state==='available',
    online:idle&&!nativeBusy&&data.online_update===true&&!!data.installed&&!!updateOffer(data),
    validate:idle&&!!data.staged,install:idle&&!nativeBusy&&data.ready===true,
    rollback:idle&&!nativeBusy&&!data?.native?.restorable&&data.can_rollback===true};
}

export const NATIVE_PHASES={prepared:'Backed up; preparing service guard',guarding:'Installing persistent guard',guarded:'Control services isolated; verifying torque off',
  replacing:'Installing robotd',replaced:'robotd replaced; verifying startup',verified_commissioning:'Commissioning runtime verified',
  restoring_normal:'Restoring normal control mode',verified_motion:'Control process verified (torque off)',factory_handoff_pending:'Handing off to initial commissioning',
  completed:'Transaction committed',rolled_back:'Install failed; restored previous robotd',recovering:'Recovering robotd safely',
  old_restored:'Previous robotd restored; verifying runtime',recovery_required:'robotd update incomplete; support duck and recover'};

export function nativeMessage(native){
  if(!native)return 'Reading native robotd status...';
  if(native.active)return (NATIVE_PHASES[native.phase]||'robotd update guard active; verify or recover')+(native.reason?`. ${native.reason}`:'');
  if(native.submitting)return 'Submitting request to root helper; refresh to check live status.';
  if(!native.service_available)return native.reason||'Root helper does not support native updates yet; update Studio/voice first.';
  if(native.bundle_schema==='microduck-controller-payload/1'&&native.supported===true&&native.restorable===true&&!native.current&&native.phase==='completed'&&native.reason)return native.reason;
  if(native.bundle_schema&&native.current&&native.restorable!==true)return 'Companion driver verified. No previous companion backup available on disk.';
  if(native.bundle_schema&&native.current)return 'Companion driver verified. Restore previous companion driver before rolling back Studio / voice.';
  if(native.bundle_schema&&native.phase==='restored')return 'Previous companion driver restored and verified; safe to roll back Studio / voice.';
  if(native.current)return 'Target robotd verified. '+(native.mode==='commissioning'?'Continue IMU selection and orientation verification.':'Verify IMU orientation before walking.');
  if(!native.available)return 'No native robotd update in this package.';
  if(!native.supported)return native.reason||'Current robotd build unverified for auto-install.';
  if(native.phase==='rolled_back')return 'Install failed; restored previous robotd. '+(native.reason||'');
  return native.bundle_schema?'Companion driver update pending; required for gamepad mapping permissions.':'robotd update pending.';
}

export function nativeActions(data,pending=false,confirmed=false){
  const native=data?.native;
  const idle=!!native?.service_available&&!data?.busy&&!native.submitting&&!pending&&confirmed===true;
  return {install:idle&&native.available===true&&native.supported===true&&!native.current&&!native.active&&native.restorable!==true,
    recover:idle&&native.active===true,restore:idle&&native.supported===true&&native.bundle_schema==='microduck-controller-payload/1'
      &&native.restorable===true&&(native.current===true||native.phase==='completed')&&!native.active};
}
