import { html, useEffect, useRef, useState } from './vendor/preact-htm.module.js';
import { audioLabel, rhythmLabel } from './audio-ui-state.js';

const CMD_EN = { sit: 'Sit', stand: 'Stand', walk: 'Walk', stop: 'Stop',
  roulade: 'Forward Roll', ground_pick: 'Pick Up', chirp: 'Chirp', greet: 'Greet',
  nod: 'Nod', shake: 'Shake', tilt: 'Tilt' };

export function AudioInteraction({ pad, link, activity }) {
  const status = pad?.audio;
  const [active, setActive] = useState(activity.active);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const alive = useRef(false), busy = useRef(false);
  useEffect(() => {
    alive.current = true;
    const off = activity.subscribe(({active:value}) => setActive(value));
    return () => { alive.current = false; off(); };
  }, [activity]);
  const change = async values => {
    if (busy.current || !activity.active || link.status !== 'open') return;
    busy.current = true; setPending(true); setError(null);
    try {
      const response = await link.cmd('pad.op', {op:'audio', ...values}, 2500);
      if (alive.current && !response?.ok) setError(response?.reason || response?.error || 'Voice toggle failed');
    } catch (err) {
      if (alive.current) setError(err?.message || 'Voice connection lost');
    } finally {
      busy.current = false;
      if (alive.current) setPending(false);
    }
  };
  const ready = active && link.status === 'open' && !pending;
  return html`<section id="audio-interaction" class="pd-motion" aria-label="Voice & Music Interaction">
    <div class="motion-heading"><h4>Voice & Music Interaction</h4>
      <span class="motion-state" role="status" data-audio-listening=${!!status?.listening}>${audioLabel(status)}</span>
    </div>
    <div class="motion-toolbar">
      <a class="btn-light" href="#/settings" data-ai-link>Configure AI Chat (Text Test)</a>
      <button type="button" class=${status?.listening ? 'motion-stop' : 'btn-primary'} data-audio="listening"
        disabled=${!ready || !status?.available} onClick=${() => change({listening:!status?.listening})}>
        ${status?.listening ? 'Stop Listening' : 'Start Listening'}</button>
      <button type="button" class="btn-light" data-audio="music" disabled=${!ready || !status?.available}
        onClick=${() => change({music:!status?.music})}>${status?.music ? 'Exit Music Mode' : 'Music Mode'}</button>
    </div>
    ${error || status?.error ? html`<p class="motion-error" role="alert">${error || status.error}</p>` : null}
    ${!status?.available ? html`<p class="pd-small-p">${status?.why || 'Waiting for voice service'}</p>` : null}
    ${status?.available && status?.voice_ready === false && !status?.error
      ? html`<p class="motion-error" role="alert">Speech recognition is still warming up (model loading); wait a few seconds or check duck-echo logs.</p>`
      : null}
    ${status?.last?.accepted === false
      ? html`<p class="motion-error" role="alert">Last command "${CMD_EN[status.last.command] || status.last.command}" failed${status.last.reason ? `: ${status.last.reason}` : ''}</p>`
      : null}
    ${status?.listening && status?.music ? html`<p class="pd-small-p" data-audio-rhythm>${rhythmLabel(status.rhythm)}</p>
      <p class="pd-small-p">Play music nearby and ask how it feels. Chirps adapt to detected rhythm; low confidence triggers an inquisitive chirp.</p>` : null}
    <p class="pd-small-p motion-note">Press R3 once to enable listening (chirp confirms), press again to disable. Wait briefly after the prompt tone before speaking.
      Supported commands: Sit, Stand, Stop, Nod, Shake, Tilt, Roll, Pick Up, Chirp, Greet.</p>
    <p class="pd-small-p motion-note">To dance to music: disconnect physical gamepad, stand steady, click Start Driving, then click Music Motion below.
      Standalone Music Motion persists across tabs while this page stays open; recording pauses if you leave the Drive view.</p>
  </section>`;
}
