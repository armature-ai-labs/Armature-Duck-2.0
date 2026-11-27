export function audioLabel(status) {
  if (!status?.available) return 'Voice service offline';
  if (!status.listening) return 'Listening disabled';
  return status.music ? 'Listening to music & prompts' : 'Listening for voice';
}

export function rhythmLabel(rhythm) {
  if (!rhythm || rhythm.confidence < .55 || !Number.isFinite(rhythm.bpm)
      || !Number.isFinite(rhythm.age) || rhythm.age > .5) return 'Waiting for clear beat';
  const feel = { lively: 'Lively', mellow: 'Mellow', sway: 'Sway' }[rhythm.feel];
  return feel ? `${feel} · ${Math.round(rhythm.bpm)} BPM` : 'Waiting for clear beat';
}

export function musicMotionLabel(motion) {
  if (motion?.state !== 'idle_motion' || motion.idle_kind !== 'music') return null;
  return motion.idle_phase === 'moving' ? 'Moving to music' : 'Waiting for steady beat';
}
