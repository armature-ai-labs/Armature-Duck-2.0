import test from 'node:test';
import assert from 'node:assert/strict';
import { audioLabel, rhythmLabel } from '../web/audio-ui-state.js';
import * as audioUI from '../web/audio-ui-state.js';

test('listening state is explicit so R3 clicks are observable', () => {
  assert.equal(audioLabel({available:true,listening:false}), 'Listening disabled');
  assert.equal(audioLabel({available:true,listening:true}), 'Listening for voice');
  assert.equal(audioLabel({available:true,listening:true,music:true}), 'Listening to music & prompts');
  assert.equal(audioLabel({available:false}), 'Voice service offline');
});
test('uncertain or stale music does not display an invented beat or genre', () => {
  assert.equal(rhythmLabel({confidence:0,bpm:120,feel:'lively',age:0}), 'Waiting for clear beat');
  assert.equal(rhythmLabel({confidence:.9,bpm:120,feel:'lively',age:.8}), 'Waiting for clear beat');
  assert.equal(rhythmLabel({confidence:.9,bpm:120,feel:'lively',age:.02}), 'Lively · 120 BPM');
  assert.equal(rhythmLabel({confidence:.9,bpm:100,feel:'sway',age:.02}), 'Sway · 100 BPM');
});
test('standalone music distinguishes waiting for stable beats from actual motion', () => {
  const motion = {state:'idle_motion', idle_kind:'music', idle_phase:'waiting'};
  assert.equal(audioUI.musicMotionLabel?.(motion), 'Waiting for steady beat');
  assert.equal(audioUI.musicMotionLabel?.({...motion, idle_phase:'moving'}), 'Moving to music');
  assert.equal(audioUI.musicMotionLabel?.({...motion, idle_phase:'waiting'}), 'Waiting for steady beat');
  assert.equal(audioUI.musicMotionLabel?.({...motion, idle_phase:null}), 'Waiting for steady beat');
});
test('music phase labels leave ordinary recording and lively status labels alone', () => {
  assert.equal(audioUI.musicMotionLabel?.({state:'recording', idle_kind:'music', idle_phase:'moving'}), null);
  assert.equal(audioUI.musicMotionLabel?.({state:'idle_motion', idle_kind:'lively'}), null);
  assert.equal(audioUI.musicMotionLabel?.({state:'idle'}), null);
});
