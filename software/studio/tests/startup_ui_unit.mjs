import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const moduleUrl = new URL('../web/startup-status.js', import.meta.url);
const ui = existsSync(moduleUrl) ? await import(moduleUrl) : {};

test('the servo page and overview remain valid browser modules', () => {
  for (const path of ['../web/servo.js', '../web/app.js']) {
    assert.doesNotThrow(() => execFileSync(process.execPath, ['--check', fileURLToPath(new URL(path, import.meta.url))]));
  }
});

test('servo rows distinguish reply, latest miss and unknown evidence', () => {
  assert.equal(typeof ui.servoReplyStatus, 'function');
  assert.equal(ui.servoReplyStatus({ responding: true }).word, 'Responding');
  assert.equal(ui.servoReplyStatus({ responding: false }).word, 'No Reply');
  assert.equal(ui.servoReplyStatus({ responding: null }).word, 'Unknown');
  assert.equal(ui.servoReplyStatus({ responding: null }).kind, '');
  assert.equal(ui.servoReplyStatus({ fresh: false }), null, 'ordinary feedback keeps its existing rendering');
});

test('startup hero uses the backend wait reason and preserves the raw error', () => {
  assert.equal(typeof ui.startupStageCopy, 'function');
  const st = { posture: 'starting', start_wait: { text: 'Waiting for IMU data (source: lsm6dsv16x)' },
    start_error: 'Feetech host IMU has no sample', starting_long: true, starting_s: 40 };
  const copy = ui.startupStageCopy(st);
  assert.equal(copy.h, 'Waiting for IMU data (source: lsm6dsv16x)');
  assert.match(copy.sub, /Feetech host IMU has no sample/);
  assert.match(copy.sub, /40/);
  assert.equal(copy.act.href, '#/logs');
  assert.doesNotMatch(JSON.stringify(copy), /unpowered|might be up/i);
});

test('generic waits stay generic, and old backend messages retain their existing fallback', () => {
  assert.equal(typeof ui.startupStageCopy, 'function');
  const copy = ui.startupStageCopy({ posture: 'starting', start_wait: { text: 'Waiting for bus device response' } });
  assert.equal(copy.h, 'Waiting for bus device response');
  assert.equal(ui.startupStageCopy({ posture: 'starting' }), null);
  assert.equal(ui.startupStageCopy({ posture: 'holding', start_wait: { text: 'old error' } }), null);
});

test('independent IMU diagnostic labels never imply mounting or control readiness', () => {
  assert.equal(typeof ui.imuDiagnosticCopy, 'function');
  const info = {protocol:'feetech',board:'imu_to_dxl',identity:[1],profile:'legacy'};
  for (const [status, word] of [['identified','Identified · Live Data'], ['data_wait','Identified · Waiting for Data'],
    ['no_response','No Response'], ['stale','Diagnostic Stale'], ['unknown','Diagnostic Unknown']]) {
    const copy = ui.imuDiagnosticCopy({status,info: ['identified','data_wait'].includes(status) ? info : null});
    assert.equal(copy.word, word);
    assert.doesNotMatch(JSON.stringify(copy), /Normal|Mount Verified|Ready to Walk|Calibrated/i);
    if (status === 'identified') assert.match(copy.board, /IMU-to-DXL.*Feetech.*200/);
  }
  assert.equal(ui.imuDiagnosticCopy(undefined).word, 'Diagnostic Unknown');
  assert.equal(ui.imuDiagnosticCopy({status:'identified',info}, false).word, 'Diagnostic Unknown');
  assert.equal(ui.imuDiagnosticCopy({status:'unknown',responding:true,info:null}).word, 'Responding · Type Unconfirmed');
  assert.equal(ui.imuDiagnosticCopy({status:'identified',age_ms:2800,info}, true, 200).word, 'Diagnostic Stale');
  assert.equal(ui.imuDiagnosticCopy({status:'identified',age_ms:2800,info}, true, 199).word, 'Identified · Live Data');
});
