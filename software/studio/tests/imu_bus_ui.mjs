import assert from 'node:assert/strict';

const [base, cdp] = process.argv.slice(2);
const target = await (await fetch(`http://127.0.0.1:${cdp}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
let id = 0;
const pending = new Map(), errors = [];
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
  if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const key = ++id;
  const timer = setTimeout(() => { pending.delete(key); reject(Error(`CDP timeout: ${method}`)); }, 10000);
  pending.set(key, message => { clearTimeout(timer); message.error ? reject(Error(JSON.stringify(message.error))) : resolve(message.result); });
  ws.send(JSON.stringify({ id: key, method, params }));
});
const js = async expression => {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result?.value;
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const wait = async (expression, label) => {
  for (let i = 0; i < 100; i++) { if (await js(expression)) return; await pause(50); }
  throw Error(`${label}\n${await js('document.body.innerText')}`);
};
try {
  await send('Runtime.enable');
  await send('Page.navigate', { url: base });
  await wait('document.readyState === "complete"', 'static page');
  await js(`(async () => {
    const { h, render } = await import('/vendor/preact-htm.module.js');
    const { ImuPage } = await import('/imu.js');
    document.body.innerHTML = '<main id="fixture"></main>';
    const root = document.querySelector('#fixture');
    window.calls = []; window.replies = [];
    window.props = { live: true, link: { status: 'open', cmd: (name, args) => {
      calls.push({name, args}); return new Promise(resolve => replies.push(resolve));
    } }, st: { mode: 'commissioning', posture: 'holding', gravity: [0, 0, -1], joints_age_ms: 5,
      imu: {status: 'ok'}, hold: {stage: 'holding', max_error_deg: 0.4} },
      imu: { source: 'bus', mode: 'commissioning', mount: {quat: [1,0,0,0], verified: true},
        imu_bus: {protocol: 'feetech', board: 'ft6', identity: [1,2,3]}, redo: {} } };
    window.draw = () => render(h(ImuPage, props), root);
    window.clickText = text => {
      const button = [...root.querySelectorAll('button')].find(b => b.textContent.trim() === text);
      if (!button || button.disabled) throw Error('Missing enabled button: ' + text);
      button.click();
    };
    draw();
  })()`);
  await wait('document.querySelector(".imu-page")', 'IMU page');
  assert.equal(await js(`!!document.querySelector('.imu-diagnostic')`), false, 'normal control without startup evidence keeps its normal health display');
  // The actual IMU component must show a diagnostic board even while all motion
  // samples and servo responses are unavailable; it cannot call this calibrated.
  await js(`window.savedProps=structuredClone({st:props.st,imu:props.imu});
    props.st={mode:'motion',posture:'starting',imu:{status:'missing'},servos:{online:0,total:15},
      joints_age_ms:null,gravity:null,guard:null};
    props.imu={mode:'motion',source:'bus',ready:false,mount:null,imu_bus:null,redo:{},
      configured_imu_profile:'legacy',profile_selection:{can_start:false},
      diagnostic:{status:'identified',responding:true,age_ms:20,sample_valid:true,sensor_ready:true,
        sample_fresh:null,info:{protocol:'feetech',board:'imu_to_dxl',identity:[1],profile:'legacy'}}}; draw()`);
  await wait(`document.querySelector('.imu-diagnostic')`, 'independent diagnostic shown');
  assert.match(await js(`document.querySelector('.imu-diagnostic').innerText`), /item.*item/);
  assert.match(await js(`document.querySelector('.imu-diagnostic').innerText`), /IMU-to-DXL.*Feetech.*200/);
  assert.match(await js(`document.querySelector('.imu-diagnostic').innerText`), /item.*item.*item/);
  assert.doesNotMatch(await js(`document.querySelector('.sv-metrics').innerText`), /item/);
  assert.equal(await js(`!!document.querySelector('.imu-big:not([disabled])')`), false);
  for (const [status, expected] of [['data_wait','item'], ['no_response','No Reply'],
    ['stale','item'], ['unknown','itemUnknown']]) {
    await js(`props.imu={...props.imu,diagnostic:{...props.imu.diagnostic,status:${JSON.stringify(status)},responding:null,info:null}};draw()`);
    await wait(`document.querySelector('.imu-diagnostic').innerText.includes(${JSON.stringify(expected)})`, 'diagnostic '+status);
    assert.doesNotMatch(await js(`document.querySelector('.imu-diagnostic').innerText`), /IMU-to-DXL/);
  }
  await js(`props.imu={...props.imu,diagnostic:{status:'identified',info:{protocol:'feetech',board:'imu_to_dxl'}}}; props.live=false;draw()`);
  await wait(`document.querySelector('.imu-diagnostic').innerText.includes('itemUnknown')`, 'browser disconnect invalidation');
  assert.doesNotMatch(await js(`document.querySelector('.sv-metrics').innerText`), /IMU-to-DXL/);
  await js(`props.live=true;props.diagnosticElapsed=200;
    props.imu={...props.imu,diagnostic:{status:'identified',age_ms:2800,info:{protocol:'feetech',board:'imu_to_dxl'}}};draw()`);
  await wait(`document.querySelector('.imu-diagnostic').innerText.includes('item')`, 'browser receipt age added');
  assert.doesNotMatch(await js(`document.querySelector('.sv-metrics').innerText`), /IMU-to-DXL/);
  await js(`props.diagnosticElapsed=0;props.imu={...props.imu,diagnostic:{status:'unknown',responding:true,info:null}};draw()`);
  await wait(`document.querySelector('.imu-diagnostic').innerText.includes('Responding · item')`, 'unknown identity response');
  await js(`props.st=savedProps.st;props.imu=savedProps.imu;props.live=true;props.diagnosticElapsed=0;draw()`);
  await pause(120);
  assert.match(await js('document.querySelector(".imu-side").innerText'), /FT6.*Feetech/);
  assert.doesNotMatch(await js('document.body.innerText'), /item FT6/);
  assert.match(await js('document.body.innerText'), /item FT6, IMU-to-DXL (Feetech)itemSFLP 124\/12 (Dynamixel 2\.0)/);
  for (const [protocol, board, label] of [['feetech', 'imu_to_dxl', 'IMU-to-DXL'], ['dxl2', 'sflp_124_12', 'SFLP 124/12']]) {
    for (const mode of ['motion', 'commissioning']) {
      await js(`props.st = {...props.st, mode:${JSON.stringify(mode)}};
        props.imu = {...props.imu, mode:${JSON.stringify(mode)},
        imu_bus: {protocol:${JSON.stringify(protocol)}, board:${JSON.stringify(board)}, identity:[1,2,3]},
        mount: {...props.imu.mount, verified:${protocol === 'feetech'}}, mount_protocol_mismatch:${protocol === 'dxl2'}}; draw()`);
      const protocolLabel = protocol === 'feetech' ? 'Feetech' : 'Dynamixel 2.0';
      await wait(`document.querySelector('.sv-metrics').innerText.includes(${JSON.stringify(`${label} · ${protocolLabel}`)})`, `${mode} board label ${board}`);
    }
    const text = await js('document.body.innerText');
    if (protocol === 'dxl2') {
      assert.match(text, /item/);
      assert.doesNotMatch(await js('document.querySelector(".sv-metrics").innerText'), /item/);
      assert.match(text, /item.*item.*item.*item/);
      assert.match(text, /item.*item.*item/);
    } else {
      assert.match(await js('document.querySelector(".sv-metrics").innerText'), /item/);
      assert.doesNotMatch(text, /item/);
    }
  }
  // A delayed capture belongs to the original board and must not advance its replacement.
  await js(`clickText('item (3 item)')`);
  await wait('calls.length === 1', 'coarse capture request');
  await js(`props.imu = {...props.imu, imu_bus: {...props.imu.imu_bus, identity:[9,9,9]}}; draw()`);
  await pause(120);
  await js(`replies.shift()({ok:true, data:{n:30, samples:[{step:'still',g:[0,0,-1]}]}})`);
  await pause(120);
  assert.equal(await js(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item (3 item)')`), false);
  assert.equal(await js(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item (3 item)'&&!b.disabled)`), true);
  // Standing collection also resets when only transport identity changes.
  await js(`props.st = {...props.st, mode:'motion', hold:null}; props.imu = {...props.imu, mode:'motion',
    mount:{quat:[1,0,0,0],verified:true}, mount_protocol_mismatch:false}; draw()`);
  await pause(120);
  await js(`clickText('item')`);
  await wait('document.querySelector(".imu-big.rec")', 'standing collection started');
  await js(`props.imu = {...props.imu, imu_bus:{...props.imu.imu_bus, identity:[8,8,8]}}; draw()`);
  await wait('!document.querySelector(".imu-big.rec")', 'standing collection reset');
  assert.equal(await js(`document.querySelector('.imu-big').textContent.trim()`), 'item');
  // A conflicting secondary diagnostic leaves the chosen board unchanged, but
  // its validity transition must discard collection and stay discarded on recovery.
  await js(`clickText('item')`);
  await wait('document.querySelector(".imu-big.rec")', 'collection before diagnostic conflict');
  await js(`props.imu={...props.imu,metadata_error:'item IMU item',
    profile_error:'item IMU item',mount:{...props.imu.mount,verified:false}}; draw()`);
  await wait('!document.querySelector(".imu-big.rec")', 'conflicting metadata resets collection');
  await js(`props.imu={...props.imu,metadata_error:null,profile_error:null,
    mount:{...props.imu.mount,verified:true}}; draw()`);
  await pause(120);
  assert.equal(await js(`!!document.querySelector('.imu-big.rec')`),false);
  assert.equal(await js(`document.querySelector('.imu-big').textContent.trim()`),'item');
  // Same wire and identity, different explicitly configured data profile.
  await js(`props.imu = {...props.imu, configured_imu_profile:'qinmo', profile_selection:{can_start:true},
    imu_bus:{protocol:'feetech',board:'qinmo',identity:[],profile:'qinmo',gyro_dps_per_lsb:1/16.4,identification:'configured_profile'}}; draw()`);
  await wait('document.querySelector(".imu-side").innerText.includes("Qinmo")', 'Qinmo label');
  await pause(120); // let the configured-profile state synchronize before user input
  assert.match(await js('document.body.innerText'), /item.*Qinmo|Qinmo.*item/);
  assert.match(await js('document.body.innerText'), /16\.4/);
  assert.match(await js('document.body.innerText'), /item.*item|item.*item/);
  assert.match(await js('document.body.innerText'), /item/);
  assert.match(await js('document.body.innerText'), /item/);
  await js(`const select=document.querySelector('select[aria-label="item IMU item"]'); select.value='legacy'; select.dispatchEvent(new Event('change',{bubbles:true}));`);
  await pause(100);
  await js(`clickText('item')`);
  await pause(750);
  await js(`clickText('item, item')`);
  await wait('calls.some(c=>c.args.op==="profile-select")', 'profile selection command');
  assert.deepEqual(await js(`calls.find(c=>c.args.op==='profile-select').args`), {op:'profile-select',profile:'legacy',confirmed:true});
  await js(`replies.shift()({ok:true,data:{}})`);
  await pause(100);
  // Failed discovery still leaves the selector available through configured diagnostics.
  await js(`props.imu={...props.imu,imu_bus:null}; draw()`);
  await wait(`document.body.innerText.includes('item')`, 'configured versus effective missing');
  await js(`props.imu={...props.imu, configured_imu_profile:null,profile_selection:{can_start:false,reason:'item'}}; draw()`);
  await wait(`document.body.innerText.includes('item')`, 'old daemon fallback');
  await js(`props.imu={...props.imu,imu_bus:{protocol:'feetech',board:'ft6',identity:[1],profile:'future'},
    profile_error:'item IMU item',mount:{quat:[1,0,0,0],verified:false}}; draw()`);
  await wait(`document.querySelector('.imu-side').innerText.includes('IMU item')`, 'unknown profile stays visible and unsafe');
  assert.match(await js(`document.querySelector('.imu-side').innerText`), /item IMU item/);
  assert.doesNotMatch(await js(`document.querySelector('.imu-side').innerText`), /item × 0\.0175|item/);
  for (const raw of [
    {imu_bus:{protocol:'future',profile:'legacy'},configured_imu_profile:'legacy'},
    {imu_bus:{protocol:'feetech',profile:'legacy'},configured_imu_profile:'future'},
    {imu_bus:null,configured_imu_profile:null},
  ]) {
    await js(`props.imu={...props.imu,...${JSON.stringify(raw)},profile_selection:{can_start:true},
      metadata_error:'item IMU item / item',profile_error:'item IMU item / item'}; draw()`);
    await pause(100);
    assert.match(await js(`document.querySelector('.imu-side').innerText`), /item/);
    assert.doesNotMatch(await js(`document.querySelector('.imu-side .imu-kv').innerText`), /item × 0\.0175|item ÷ 16\.4/);
    assert.equal(await js(`[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='item').disabled`), true);
  }
  await js(`props.imu = {...props.imu, source:'lsm6dsv16x', imu_bus:null}; draw()`);
  await wait('document.querySelector(".sv-metrics").innerText.includes("LSM6DSV16X (I²C)")', 'I2C label');
  // Exercise the actual global Space handler used beside the IMU page. Keep its
  // real component source, with only the link transport supplied by this fixture.
  await js(`(async()=>{
    const source=await (await fetch('/app.js')).text();
    const component=source.slice(source.indexOf('const TEXT_TYPES'),source.indexOf('function TopBar'));
    const {html,h,render,useState,useEffect,useCallback}=await import('/vendor/preact-htm.module.js');
    const EStop=Function('html','useState','useEffect','useCallback','link',component+';return EStop;')(
      html,useState,useEffect,useCallback,props.link);
    const root=document.createElement('aside');document.body.append(root);
    render(h(EStop,{st:{mode:'commissioning'},live:true,open:true}),root);
  })()`);
  await pause(120);
  await js(`window.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',key:' ',bubbles:true}));`);
  await wait(`calls.some(c=>c.name==='relax')`, 'Space emergency relax retained');
  assert.deepEqual(await js(`calls.find(c=>c.name==='relax').args`), {});
  await js(`replies.shift()({ok:true,data:{}})`);
  // Render the real overview band with only unrelated dependencies supplied by
  // the fixture. Missing servos and diagnostic IMU evidence are separate cells.
  await js(`(async()=>{
    const source=await (await fetch('/app.js')).text();
    const component=source.slice(source.indexOf('function Band('),source.indexOf('function Band(')+source.slice(source.indexOf('function Band(')).indexOf('// ----------------', 1));
    const {html,h,render}=await import('/vendor/preact-htm.module.js');
    const {imuDiagnosticCopy}=await import('/startup-status.js');
    const Band=Function('html','useStickyHeight','boardCell','padCell','BatteryCell','Badge','SensorBandCells','api','imuDiagnosticCopy',
      component+';return Band;')(html,()=>null,()=>({big:'—'}),()=>({word:'—'}),()=>null,()=>null,()=>null,{},imuDiagnosticCopy);
    const root=document.createElement('aside');root.id='overview-band';document.body.append(root);
    window.drawBand=(diagnostic,gray=false,source='bus')=>render(h(Band,{st:{posture:'starting',servos:{online:0,total:15},imu:{source,status:'missing',diagnostic}},gray,live:!gray,onSame:()=>{}}),root);
    drawBand({status:'identified',info:{protocol:'feetech',board:'imu_to_dxl',profile:'legacy'}});
  })()`);
  await wait(`document.querySelector('#overview-band a[href="#/imu"]').innerText.includes('item')`, 'overview independent IMU');
  assert.match(await js(`document.querySelector('#overview-band').innerText`), /item\s*0\s*\/ 15/);
  assert.match(await js(`document.querySelector('#overview-band a[href="#/imu"]').innerText`), /IMU-to-DXL.*Feetech/);
  assert.doesNotMatch(await js(`document.querySelector('#overview-band a[href="#/imu"]').innerText`), /item|Normal|item/);
  await js(`drawBand({status:'identified',info:{protocol:'feetech',board:'imu_to_dxl'}},true)`);
  await wait(`document.querySelector('#overview-band a[href="#/imu"]').innerText.includes('itemUnknown')`, 'stale overview invalidation');
  for (const [status, word] of [['data_wait','item · item'], ['no_response','No Reply'], ['stale','item'], ['unknown','itemUnknown']]) {
    await js(`drawBand({status:${JSON.stringify(status)},info:null})`);
    await wait(`document.querySelector('#overview-band a[href="#/imu"]').innerText.includes(${JSON.stringify(word)})`, 'overview '+status);
  }
  await js(`drawBand({status:'identified',info:{protocol:'feetech',board:'imu_to_dxl'}},false,'lsm6dsv16x')`);
  await wait(`document.querySelector('#overview-band a[href="#/imu"]').innerText.includes('item')`, 'I2C retains configured source health');
  assert.doesNotMatch(await js(`document.querySelector('#overview-band a[href="#/imu"]').innerText`), /ID 200|IMU-to-DXL/);
  // Exercise the real Main subscription, so a reconnect and new source/profile
  // state cannot revive its cached IMU message before the next imu event arrives.
  await js(`(async()=>{
    const source=await (await fetch('/app.js')).text();
    const component=source.slice(source.indexOf('function Main('),source.indexOf('function Login('));
    const {html,h,render,useState,useEffect,useRef}=await import('/vendor/preact-htm.module.js');
    const root=document.createElement('aside');root.id='main-fixture';document.body.append(root);
    window.mainListener=null;
    const link={status:'open',on:fn=>{mainListener=fn;return()=>{}},connect:()=>{}};
    const blank=()=>null;
    const ImuPage=({imu})=>html\`<output id="main-imu">\${imu?.diagnostic?.info?.board || imu?.imu_bus?.board || 'unknown'}</output>\`;
    const names=['html','useState','useEffect','useRef','useRoute','useNow','useStickyHeight','link',
      'padActivity','scrollFor','OVERVIEW','Overview','Nav','TopBar','ServoBar','TabBar','ImuPage',
      'STALE_ALARM_MS','STUDIO_TEST'];
    const values=[html,useState,useEffect,useRef,()=>'/imu',()=>performance.now(),()=>null,link,
      {inView:false,subscribe:()=>()=>{}},()=>{},()=>false,blank,blank,blank,blank,blank,ImuPage,3000,true];
    const Main=Function(...names,component+';return Main;')(...values);
    render(h(Main,{duck:{}}),root);
  })()`);
  await wait(`typeof mainListener==='function'`, 'Main subscription');
  const mainState = {t:'state',mode:'motion',build:'test47',posture:'starting',robotd_ok:true,robotd_age_ms:0,
    imu:{source:'bus',configured_imu_profile:'legacy',diagnostic:{status:'identified',info:{board:'imu_to_dxl'}}}};
  await js(`window.mainState=${JSON.stringify(mainState)}; mainListener(mainState);
    mainListener({t:'imu',source:'bus',configured_imu_profile:'legacy',diagnostic:mainState.imu.diagnostic})`);
  await wait(`document.querySelector('#main-imu').textContent==='imu_to_dxl'`, 'Main initial diagnostic');
  await js(`mainListener({t:'_status',status:'closed'});mainListener({t:'_status',status:'open'});mainListener(mainState)`);
  await wait(`document.querySelector('#main-imu').textContent==='unknown'`, 'Main clears previous connection IMU');
  await js(`mainListener({t:'imu',source:'bus',configured_imu_profile:'legacy',diagnostic:mainState.imu.diagnostic})`);
  await wait(`document.querySelector('#main-imu').textContent==='imu_to_dxl'`, 'Main new connection IMU');
  await js(`mainListener({...mainState,imu:{...mainState.imu,configured_imu_profile:'qinmo',diagnostic:{status:'unknown',info:null}}})`);
  await wait(`document.querySelector('#main-imu').textContent==='unknown'`, 'Main profile change invalidates IMU');
  await js(`mainListener({...mainState,posture:'holding',imu:{...mainState.imu,diagnostic:{status:'unknown'}}});
    mainListener({t:'imu',source:'bus',configured_imu_profile:'legacy',imu_bus:{board:'ft6'},diagnostic:{status:'unknown'}})`);
  await wait(`document.querySelector('#main-imu').textContent==='ft6'`, 'Main ordinary control board');
  await js(`mainListener({...mainState,imu:{...mainState.imu,diagnostic:{status:'unknown'}}})`);
  await wait(`document.querySelector('#main-imu').textContent==='unknown'`, 'Main same-build restart clears control board');
  await js(`window.controlState={...mainState,posture:'holding',imu:{...mainState.imu,context_current:true,
      generation:100,diagnostic:{status:'unknown'}}};mainListener(controlState);
    mainListener({t:'imu',source:'bus',configured_imu_profile:'legacy',imu_bus:{board:'ft6'},diagnostic:{status:'unknown'}})`);
  await wait(`document.querySelector('#main-imu').textContent==='ft6'`, 'Main prior daemon control identity');
  await js(`mainListener({...controlState,imu:{...controlState.imu,context_current:false,generation:100.1}})`);
  await wait(`document.querySelector('#main-imu').textContent==='unknown'`, 'Main new hello clears same-phase prior daemon identity');
  // The actual reported entry is motion/HOME with torque already enabled.
  // It must expose collection in place, without sending redo-start or relax.
  await js(`window.calls=[];window.replies=[];
    props.st={mode:'motion',posture:'holding',gravity:[0,0,-1],joints_age_ms:5,imu:{status:'ok'},hold:null};
    props.imu={source:'bus',mode:'motion',mount:{quat:[1,0,0,0],verified:true},
      imu_bus:{protocol:'feetech',board:'imu_to_dxl',identity:[1,1,0,68,77]},
      coarse_sampling:{available:true,reason:null,needs_unload:true},
      redo:{can_start:false,reason:'itemE-Stop, item'}};
    props.live=true;draw()`);
  await wait(`[...document.querySelectorAll('.imu-coarse button')].some(b=>b.textContent.trim()==='item'&&!b.disabled)`, 'motion HOME installation entry enabled');
  await js(`clickText('item')`);
  await pause(750);
  await js(`clickText('item HOME, item')`);
  await wait(`[...document.querySelectorAll('.imu-coarse button')].some(b=>b.textContent.trim()==='item (3 item)'&&!b.disabled)`, 'motion HOME can collect still');
  assert.equal(await js(`calls.length`),0, 'opening collection never changes mode or torque');
  await js(`clickText('item (3 item)')`);
  await wait(`calls.length===1`, 'motion HOME still request');
  assert.deepEqual(await js(`calls[0]`),{name:'imu.op',args:{op:'coarse-capture',phase:'still',seconds:3}});
  await js(`replies.shift()({ok:true,data:{n:30,samples:[{step:'still',g:[0,0,-1]}]}})`);
  await wait(`[...document.querySelectorAll('.imu-coarse button')].some(b=>b.textContent.trim()==='item (3 item)'&&!b.disabled)`, 'motion HOME forward follows still');
  await js(`clickText('item (3 item)')`);
  await wait(`calls.length===2`, 'motion HOME forward request');
  await js(`replies.shift()({ok:true,data:{n:30,samples:[{step:'fwd',g:[.34,0,-.94]}]}})`);
  await wait(`calls.length===3`, 'motion HOME solve request');
  await js(`replies.shift()({ok:true,data:{ok:true,mount:[1,0,0,0],confirm:[],notes:[],
    parts:{pitch_deg:8.6,roll_deg:-2.4,total_deg:24.8}}})`);
  await wait(`[...document.querySelectorAll('.imu-coarse button')].some(b=>b.textContent.trim()==='item'&&b.disabled)`, 'motion HOME final write requires unload');
  assert.equal(await js(`!!document.querySelector('.imu-coarse .imu-parts')`), true, 'coarse confirmation shows correction angles');
  const correction = await js(`document.querySelector('.imu-coarse .imu-parts').innerText`);
  assert.match(correction, /item[\s\S]*\+8\.6°/);
  assert.match(correction, /item[\s\S]*-2\.4°/);
  assert.match(correction, /item[\s\S]*24\.8°/);
  assert.match(await js(`document.querySelector('.imu-coarse').innerText`), /item.*item.*item/);
  await js(`props.imu={...props.imu,coarse_sampling:{...props.imu.coarse_sampling,needs_unload:false}};draw()`);
  await wait(`[...document.querySelectorAll('.imu-coarse button')].some(b=>b.textContent.trim()==='item'&&!b.disabled)`, 'motion HOME unload preserves solved candidate');
  assert.equal(await js(`calls.some(c=>c.args.op==='redo-start'||c.name==='relax'||c.args.op==='verify')`),false);
  // A persisted redo session may hold HOME for sampling, but must unload before
  // final verification/cancellation. Unloading must keep the solved samples.
  await js(`window.calls=[];window.replies=[];window.confirm=()=>true;
    props.st={mode:'commissioning',posture:'commissioning',gravity:[0,0,-1],joints_age_ms:5,imu:{status:'ok'},hold:null};
    props.imu={source:'bus',mode:'commissioning',mount:{quat:[1,0,0,0],verified:true},hold:null,
      imu_bus:{protocol:'feetech',board:'imu_to_dxl',identity:[1,1,0,68,77]},
      redo:{active:true,phase:'commissioning',session_id:'redo-held',source:'bus'}};
    props.live=true;draw()`);
  await wait(`document.querySelector('.imu-stand')&&!document.querySelector('.imu-stand').disabled`, 'redo stance enable available');
  await js(`clickText('item')`);
  await wait(`calls.some(c=>c.args.op==='hold')`, 'redo uses commissioning hold');
  assert.deepEqual(await js(`calls.find(c=>c.args.op==='hold')`), {name:'imu.op',args:{op:'hold'}});
  await js(`replies.shift()({ok:true,data:{accepted:true}});
    props.st={...props.st,hold:{stage:'ramping',max_error_deg:8}};props.imu={...props.imu,hold:props.st.hold};draw()`);
  await wait(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item (3 item)'&&b.disabled)`, 'ramping cannot collect');
  await js(`props.st={...props.st,hold:{stage:'holding',max_error_deg:0.4}};props.imu={...props.imu,hold:props.st.hold};draw()`);
  await wait(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item (3 item)'&&!b.disabled)`, 'settled redo hold can collect');
  assert.match(await js(`document.querySelector('.imu-coarse').innerText`), /item/);
  await js(`clickText('item (3 item)')`);
  await wait(`calls.some(c=>c.args.op==='coarse-capture'&&c.args.phase==='still')`, 'held still capture');
  await js(`replies.shift()({ok:true,data:{n:30,samples:[{step:'still',g:[0,0,-1]}]}})`);
  await wait(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item (3 item)'&&!b.disabled)`, 'held forward capture offered');
  await js(`clickText('item (3 item)')`);
  await wait(`calls.some(c=>c.args.op==='coarse-capture'&&c.args.phase==='fwd')`, 'held forward capture');
  await js(`replies.shift()({ok:true,data:{n:30,samples:[{step:'fwd',g:[0.34,0,-0.94]}]}})`);
  await wait(`calls.some(c=>c.args.op==='solve')`, 'held samples solved');
  await js(`replies.shift()({ok:true,data:{ok:true,mount:[1,0,0,0],confirm:[],notes:[],quality:{grade:'coarse'}}})`);
  await wait(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item'&&b.disabled)`, 'redo verification requires unloading');
  assert.match(await js(`document.querySelector('.imu-coarse').innerText`), /E-Stop/);
  await js(`props.st={...props.st,hold:null};props.imu={...props.imu,hold:null};draw()`);
  await wait(`[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='item'&&!b.disabled)`, 'unloading preserves the solved candidate');
  assert.equal(await js(`calls.filter(c=>c.args.op==='coarse-capture').length`),2);
  assert.equal(await js(`calls.some(c=>c.args.op==='verify')`),false, 'UI never auto-verifies or auto-relaxes');
  assert.deepEqual(errors, []);
  process.stdout.write('IMU bus browser assertions passed\n');
} finally {
  // Windows Edge's launcher may exit before its actual browser child, so the
  // Python parent PID alone cannot reliably release this test's profile files.
  // Close the browser while the target WebSocket is still connected.
  await send('Browser.close').catch(() => {});
  ws.close();
  await pause(500);
}
