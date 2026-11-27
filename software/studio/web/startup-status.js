// Startup replies are diagnostic evidence; missing/expired evidence stays unknown.
export function servoReplyStatus(row) {
  if (!Object.hasOwn(row, 'responding')) return null;
  if (row.responding === true) return { word: 'Responding', kind: 'ok' };
  if (row.responding === false) return { word: 'No Reply', kind: 'bad' };
  return { word: 'Unknown', kind: '' };
}

export function startupStageCopy(st) {
  if (st?.posture !== 'starting' || !st.start_wait?.text) return null;
  return { h: st.start_wait.text,
    sub: `${st.starting_long ? `${st.starting_s}s elapsed. ` : ''}${st.start_error ? `Raw: ${st.start_error}. ` : ''}Check Logs for robotd's latest output.`,
    act: { href: '#/logs', label: 'Open Logs' } };
}

// Diagnostic evidence is separate from mounting and control readiness.
export function imuDiagnosticCopy(diagnostic, live = true, elapsedMs = 0) {
  const d = live !== false ? diagnostic : null;
  const expired = d && Number.isFinite(d.age_ms) && d.age_ms + Math.max(0, elapsedMs) >= 3000;
  const status = expired ? 'stale' : d?.status || 'unknown';
  const words = { identified: 'Identified · Live Data', data_wait: 'Identified · Waiting for Data',
    no_response: 'No Response', stale: 'Diagnostic Stale', unknown: 'Diagnostic Unknown' };
  const boards = { ft6: 'FT6', imu_to_dxl: 'IMU-to-DXL', sflp_124_12: 'SFLP 124/12', qinmo: 'Qinmo (Configured)' };
  const protocols = { feetech: 'Feetech', dxl2: 'Dynamixel 2.0' };
  const info = !expired && ['identified', 'data_wait', 'stale'].includes(status) ? d?.info : null;
  const board = info && boards[info.board] && protocols[info.protocol]
    ? `${boards[info.board]} · ${protocols[info.protocol]} (ID 200)` : null;
  return { word: status === 'unknown' && d?.responding === true ? 'Responding · Type Unconfirmed' : words[status] || words.unknown, board,
    kind: status === 'no_response' ? 'bad' : ['data_wait', 'stale'].includes(status) ? 'warn' : '',
    hint: { identified: 'Decodable IMU readings received; control readiness still depends on robotd verification.',
      data_wait: 'Board responded, but sensor is not ready or readings are invalid.',
      no_response: 'No response from ID 200 on read-only query; check wiring and selected IMU profile.',
      stale: 'Diagnostic or sample evidence expired; waiting for fresh read-only query.',
      unknown: d?.responding === true ? 'Board responded, but identity or selected profile is unconfirmed.' : 'No active diagnostic evidence; older robotd builds may not report this field.' }[status] || 'Waiting for diagnostic evidence.' };
}
