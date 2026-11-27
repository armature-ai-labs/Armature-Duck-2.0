// Read-only views of owned snapshots. No serial reads, calibration writes or motion.
import { html, useState, useEffect, useRef } from './vendor/preact-htm.module.js';

const number = v => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, d = 0) => number(v) ? v.toFixed(d) : '—';
const count = v => Number.isSafeInteger(v) && v >= 0 ? v.toLocaleString('en-US', {useGrouping:false}) : '—';
const ageText = age => !number(age) ? 'Age unknown' : age < 1000 ? `${Math.round(age)} ms ago` : `${(age / 1000).toFixed(1)}s ago`;
const groups = [['L','Left Leg','20–24'],['R','Right Leg','10–14'],['H','Head & Neck','30–34']];
const sourceText = source => ({robotd:'robotd telemetry',bus:'Servo session',sim:'Simulated'}[source] || 'Source unconfirmed');

function rowStatus(row, live, elapsed) {
  const age = number(row.age_ms) ? row.age_ms + elapsed : null;
  if (!live || elapsed >= 1500 || number(age) && age >= 1500) return {kind:'stale',text:'Stale',age};
  if (!row.fresh) return row.responding === false ? {kind:'bad',text:'No Reply',age} : {kind:'neutral',text:'Waiting',age};
  if (row.comm?.streak > 0) return {kind:row.comm.streak >= 3?'bad':'warn',text:`${row.comm.streak} missed`,age};
  if (number(row.health?.rate) && row.health.rate > .01) return {kind:'warn',text:'Packet drops',age};
  if (!number(row.comm?.asked)) return {kind:'neutral',text:'Responding · No stats',age};
  return {kind:'ok',text:'Healthy',age};
}

function Rate({ health }) {
  const known = number(health?.rate) && health.rate >= 0 && health.rate <= 1 && health.asked > 0;
  return html`<span class="comm-rate"><b>${known ? `${(health.rate * 100).toFixed(1)}%` : '—'}</b>
    <small>${known ? `${count(health.missed)} / ${count(health.asked)}` : 'No stats yet'}</small></span>`;
}

function Parameter({label, value, suffix = ''}) {
  return html`<div><dt>${label}</dt><dd>${value == null ? 'N/A' : `${value}${suffix}`}</dd></div>`;
}

function ServoDetail({row, status}) {
  const c = row.comm || {}, h = row.health || {}, p = row.parameters || {};
  const pdi = value => Array.isArray(value) && value.length===3 && value.every(number) ? value.join(' / ') : null;
  const source = ({robotd_before_arm_cache:'robotd pre-arm cache',studio_cached_inspection:'Servo session register read'})[p.source] || 'Provided telemetry';
  return html`<div class="sv-monitor-detail" id=${`servo-detail-${row.id}`} data-id=${row.id}>
    <div class="comm-detail-head"><b>${row.id} · ${row.en || row.cn}</b><span>Latest ${ageText(status.age)}</span></div>
    <dl class="comm-values">
      <${Parameter} label="Asked" value=${number(c.asked)?count(c.asked):null} />
      <${Parameter} label="Replied" value=${number(c.ok)?count(c.ok):null} />
      <${Parameter} label="Missed" value=${number(c.missed)?count(c.missed):null} />
      <${Parameter} label="Miss Streak" value=${number(c.streak)?count(c.streak):null} />
      <${Parameter} label="Max Streak" value=${number(c.max_streak)?count(c.max_streak):null} />
      <${Parameter} label="Window" value=${number(h.observed_s)?fmt(h.observed_s,1):null} suffix=" / 60s" />
      <${Parameter} label="Raw Load" value=${row.load} />
      <${Parameter} label="Current" value=${number(row.current_ma)?fmt(row.current_ma):null} suffix=" mA" />
      <${Parameter} label="Raw Speed" value=${row.speed_raw} />
      <${Parameter} label="Angular Vel" value=${number(row.velocity_rad_s)?fmt(row.velocity_rad_s,3):null} suffix=" rad/s" />
    </dl>
    <details class="comm-parameters"><summary>Parameters & Calibration<span>Config, readback & cache</span></summary>
    <dl class="comm-values">
      <${Parameter} label="Active P / D" value=${row.p_run == null || row.d_run == null ? null : `${row.p_run} / ${row.d_run}`} />
      <${Parameter} label="Active Limit" value=${row.limit} />
      <${Parameter} label="Stored P / D" value=${row.p == null || row.d == null ? null : `${row.p} / ${row.d}`} />
      ${pdi(p.runtime?.pdi) ? html`<${Parameter} label=${p.runtime?.observed===true?'Active P / D / I':'Pre-arm RAM P / D / I (cached)'} value=${pdi(p.runtime.pdi)} />`:null}
      ${pdi(p.eeprom?.pdi) ? html`<${Parameter} label="Stored P / D / I (cached)" value=${pdi(p.eeprom.pdi)} />`:null}
      ${number(p.before_arm?.acceleration_raw) ? html`<${Parameter} label="Pre-arm Accel" value=${p.before_arm.acceleration_raw} />`:null}
      ${p.configured ? html`<${Parameter} label="Config P / D" value=${number(p.configured.kp)&&number(p.configured.kd)?`${p.configured.kp} / ${p.configured.kd}`:null} />
        <${Parameter} label="Config Limit" value=${p.configured.torque_limit} />`:null}
      <${Parameter} label="Zero Offset" value=${row.offset} />
      <${Parameter} label="Model Dir" value=${row.dir} />
      <${Parameter} label="Min Angle" value=${row.min_angle ?? p.min_angle} />
      <${Parameter} label="Max Angle" value=${row.max_angle ?? p.max_angle} />
    </dl>
    <p class="comm-caption">Source: ${source} · ${number(p.age_ms) ? ageText(p.age_ms) : 'Timestamp N/A'}.
      Static parameters are cached snapshots; missing fields are not filled with defaults.</p>
    </details>
  </div>`;
}

export function ServoMonitor({sv, selected, onSelect, live = true, elapsed = 0}) {
  const c = sv?.communication || {};
  const rows = sv?.rows || [];
  const strip=useRef(null), [ends,setEnds]=useState({before:false,after:false});
  const ordered=groups.flatMap(([group,label])=>rows.filter(r=>r.group===group).map(row=>({row,group,label})));
  const picked=rows.find(r=>r.id===selected);
  useEffect(()=>{
    const node=strip.current;
    const measure=()=>{const before=node.scrollLeft>2,after=node.scrollLeft+node.clientWidth<node.scrollWidth-2;
      setEnds(prev=>prev.before===before&&prev.after===after?prev:{before,after});};
    measure();node.addEventListener('scroll',measure,{passive:true});
    const observer=typeof ResizeObserver==='function'?new ResizeObserver(measure):null;
    observer?.observe(node);window.addEventListener('resize',measure);
    return()=>{node.removeEventListener('scroll',measure);observer?.disconnect();window.removeEventListener('resize',measure);};
  },[rows.length]);
  useEffect(()=>{
    const node=strip.current,card=node?.querySelector(`[data-id="${selected}"]`);
    if(!card)return;
    const box=node.getBoundingClientRect(),r=card.getBoundingClientRect();
    if(r.left<box.left || r.right>box.right)node.scrollLeft+=r.left-box.left-(node.clientWidth-r.width)/2;
  },[selected,rows.length]);
  const slide=direction=>strip.current?.scrollBy({left:direction*Math.max(160,strip.current.clientWidth*.8),
    behavior:window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches?'auto':'smooth'});
  const statuses = rows.map(row => ({row,status:rowStatus(row,live,elapsed)}));
  const valid = statuses.filter(({row,status}) => status.kind !== 'stale' && row.fresh).map(({row}) => row);
  const feedbackKnown = valid.length > 0 || statuses.some(({row,status}) => status.kind === 'bad' && row.responding === false);
  const hottest = valid.filter(r => number(r.temp)).sort((a,b)=>b.temp-a.temp)[0];
  const lowest = valid.filter(r => number(r.volt)).sort((a,b)=>a.volt-b.volt)[0];
  const attention = statuses.filter(({status}) => ['bad','warn'].includes(status.kind));
  return html`<section class="board sv-monitor" aria-label="Servo Live Status">
    <div class="comm-monitor-head"><div><h2>Live Status</h2><p>${sv?.sim ? 'Simulated' : sourceText(c.source || sv?.source)} · Read-only</p></div>
      <span class=${'comm-badge '+(!live || elapsed>=1500 ? 'stale':'neutral')}>${!live?'Disconnected':elapsed>=1500?'Waiting for data':ageText(number(c.age_ms)?c.age_ms+elapsed:null)}</span></div>
    <div class="comm-summary">
      <div><span>Online</span><b>${feedbackKnown ? valid.length : '—'}<small> / ${sv?.total ?? 15}</small></b></div>
      <div><span>Peak Temp</span><b>${fmt(hottest?.temp)}<small> °C${hottest ? ` · #${hottest.id}`:''}</small></b></div>
      <div><span>Min Voltage</span><b>${fmt(lowest?.volt,1)}<small> V${lowest ? ` · #${lowest.id}`:''}</small></b></div>
      <div><span>Update Rate</span><b>${live && elapsed<1500 ? fmt(c.sample_hz,1):'—'}<small> Hz</small></b></div>
    </div>
    ${attention.length ? html`<div class="comm-attention" role="status"><b>${attention.length} need attention</b>
      ${attention.map(x=>html`<button type="button" onClick=${()=>onSelect(x.row.id)}>#${x.row.id} ${x.row.en || x.row.cn} · ${x.status.text}</button>`)}</div>`:null}
    <div class="comm-strip-heading"><span>${rows.length} Servos · ${ends.before||ends.after?'Scroll horizontally':'All visible'}</span>${ends.before||ends.after ? html`<span class="comm-strip-arrows">
      <button type="button" aria-label="Scroll servos left" aria-controls="servo-card-strip" disabled=${!ends.before} onClick=${()=>slide(-1)}>‹</button>
      <button type="button" aria-label="Scroll servos right" aria-controls="servo-card-strip" data-servo-next disabled=${!ends.after} onClick=${()=>slide(1)}>›</button>
    </span>`:null}</div>
    <div class="comm-strip-wrap"><div class="comm-servo-strip" id="servo-card-strip" ref=${strip} role="region" aria-label="Servo cards" tabindex="0">
      ${ordered.map(({row,group,label})=>{
        const status=rowStatus(row,live,elapsed);
        return html`<button type="button" key=${row.id} class=${'sv-monitor-row comm-servo-card '+status.kind+(selected===row.id?' selected':'')} data-id=${row.id}
          aria-expanded=${selected===row.id} aria-controls=${`servo-detail-${row.id}`} onClick=${()=>onSelect(selected===row.id?null:row.id)}>
          <span class="comm-card-heading"><span class=${'sv-id '+group}>${row.id}</span><b>${row.en || row.cn}</b><small>${label}</small></span>
          <span class="comm-card-metrics">
            <span><small>Temp</small><b>${fmt(row.temp)}<em> °C</em></b></span>
            <span><small>Volt</small><b>${fmt(row.volt,1)}<em> V</em></b></span>
            <span><small>Angle</small><b>${fmt(row.deg,1)}<em>°</em></b></span>
            <span title="60s drop rate"><small>Drops</small><${Rate} health=${row.health} /></span>
          </span>
          <span class="comm-card-tail"><span class=${'comm-badge '+status.kind}>${status.text}</span><span>${selected===row.id?'Hide ▴':'Details ▾'}</span></span>
        </button>`;
      })}</div></div>
    ${picked ? html`<div class="comm-strip-detail"><${ServoDetail} row=${picked} status=${rowStatus(picked,live,elapsed)} /></div>`:null}
    ${!rows.length ? html`<p class="comm-empty">Waiting for servo telemetry.</p>` : null}
    <div class="comm-footer"><span>Bus Bad Checksum <b>${count(c.counters?.bad_checksum ?? c.counters?.bad_checksums)}</b></span>
      <span>Read Failures <b>${count(c.counters?.failed_reads)}</b></span><span>Timeouts <b>${count(c.counters?.timeout)}</b></span></div>
    <p class="comm-caption">Drop rate = missed replies / requests in 60s window. Bus-wide checksum errors are not attributed to individual servos.</p>
  </section>`;
}

export function ImuHealth({imu, live = true, elapsed = 0}) {
  const h=imu?.health || {}, meta=imu?.metadata || {};
  const age=number(h.age_ms)?h.age_ms+elapsed:null;
  const fresh=live && elapsed<1500 && h.fresh===true && (!number(age)||age<1500);
  const valuesAge=number(imu?.values_age_ms)?imu.values_age_ms+elapsed:null;
  const valuesFresh=fresh && number(valuesAge) && valuesAge<1500;
  const vector=(value,d=4)=>Array.isArray(value)&&value.every(number)?value.map(v=>v.toFixed(d)).join(' / '):null;
  const board={imu_to_dxl:'IMU-to-DXL',ft6:'FT6',qinmo:'Qinmo'}[meta.board] || meta.board;
  const source={bus:'Bus ID 200',lsm6dsv16x:'I2C · LSM6DSV16X',bno08x:'I2C · BNO08x'}[h.source || imu?.source] || h.source || 'Source unconfirmed';
  return html`<section class=${'board imu-card imu-health'+(fresh?'':' stale')} aria-label="IMU Live Status">
    <div class="comm-monitor-head"><div><h3>IMU Live Status</h3><p>${source}</p></div>
      <span class=${'comm-badge '+(fresh?'ok':'stale')}>${!live?'Disconnected':fresh?'Healthy':h.reason || 'Waiting / Stale'}</span></div>
    <dl class="comm-values">
      <${Parameter} label="Chip" value=${meta.chip} /><${Parameter} label="Board" value=${board} />
      <${Parameter} label="Protocol" value=${meta.protocol} /><${Parameter} label="Firmware" value=${meta.firmware != null && typeof meta.firmware==='object'?JSON.stringify(meta.firmware):meta.firmware} />
      <${Parameter} label=${h.rate_basis==='accepted_snapshot_timestamp'?'Snapshot Rate':h.rate_basis==='accepted_host_read'?'Host Read Rate':'Update Rate'} value=${fresh && number(h.update_hz)?fmt(h.update_hz,1):null} suffix=" Hz" />
      <${Parameter} label="Data Age" value=${number(age)?ageText(age):null} />
      ${number(h.sensor_age_upper_bound_ms)?html`<${Parameter} label="Sensor Age Upper Bound" value=${fmt(h.sensor_age_upper_bound_ms+elapsed)} suffix=" ms" />`:null}
    </dl>
    ${h.last_error ? html`<p class="comm-last-error"><b>Last Error</b><span>${h.last_error}</span><small>Historical record; see live status above.</small></p>`:null}
    <div class=${'comm-sensor-values'+(valuesFresh?'':' stale')}><p class="comm-values-note">${valuesFresh?`Attitude · ${ageText(valuesAge)}`:`Attitude ${number(valuesAge)?'stale':'age unknown'} · ${ageText(valuesAge)}`}</p>
    <dl class="comm-values">
      <${Parameter} label="Gyro X / Y / Z" value=${vector(imu?.gyro,3)} suffix=" rad/s" />
      <${Parameter} label="Quat W / X / Y / Z" value=${vector(imu?.quaternion_wxyz)} />
      <${Parameter} label="Accel X / Y / Z" value=${Array.isArray(imu?.accel_m_s2)?vector(imu.accel_m_s2,3):null} suffix=" m/s²" />
      <${Parameter} label="Sensor Temp" value=${number(imu?.temperature_c)?fmt(imu.temperature_c,1):null} suffix=" °C" />
    </dl>
    </div>
    <div class="comm-footer">${Object.entries(h.counters || {}).filter(([,v])=>number(v)).map(([key,value])=>html`<span>${({samples:'Samples',dropped:'Dropped',invalid:'Invalid',errors:'Errors',stale:'Stale',rejected:'Rejected',timeouts:'Timeouts',bad_checksums:'Bad Checksums'})[key] || key} <b>${count(value)}</b></span>`)}</div>
    <p class="comm-caption">Update rate reflects valid data accepted by robotd. Missing acceleration or temperature fields are not extrapolated.</p>
  </section>`;
}

export const _test={rowStatus,ageText};
