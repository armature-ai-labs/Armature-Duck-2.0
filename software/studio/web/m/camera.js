// Mobile Camera page: low-latency WebRTC feed from mediad.
import { html, useState, useEffect, useRef, useCallback } from '../vendor/preact-htm.module.js';
import { CameraLink } from '../camera-link.js';
import { api } from './link.js';

const WORD = {
  stopped: 'Stopped', connecting: 'Connecting', signaling: 'Signaling',
  negotiating: 'Negotiating', receiving: 'Receiving', playing: 'Live', error: 'Error',
};

export function CameraPage() {
  const videoRef = useRef(null);
  const camRef = useRef(null);
  const [info, setInfo] = useState(null);
  const [state, setState] = useState({ state: 'stopped', error: null, width: null, height: null, frames: 0 });
  const [starting, setStarting] = useState(false);

  const start = useCallback(async () => {
    if (starting) return;
    setStarting(true);
    let cfg = null;
    try { cfg = await api('/api/camera', undefined, 6000); }
    catch (e) { setState({ state: 'error', error: `Failed to load camera config: ${e.message}` }); setStarting(false); return; }
    if (cfg.status === 401) { setState({ state: 'error', error: 'Session expired — sign in again.' }); setStarting(false); return; }
    const d = cfg.data || cfg;
    setInfo(d);
    const port = d.signaling_port;
    if (!Number.isInteger(port)) { setState({ state: 'error', error: 'Signaling port unavailable.' }); setStarting(false); return; }

    camRef.current?.stop();
    const cam = new CameraLink({ video: videoRef.current, onState: (s) => setState(s) });
    camRef.current = cam;
    cam.start(port, location.protocol === 'https:' ? '/media-ws' : null);
    setStarting(false);
  }, [starting]);

  useEffect(() => {
    start();
    return () => { camRef.current?.stop(); camRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const playing = state.state === 'playing';
  const over = !playing;
  const reason = state.error
    || (info && info.service_state && info.service_state !== 'ok' ? info.service_reason : null);

  return html`<div>
    <div class="m-video-wrap">
      <video ref=${videoRef} playsinline muted autoplay></video>
      <div class=${'m-video-tag' + (playing ? '' : ' bad')}><i></i>${playing ? 'Live' : WORD[state.state] || state.state}</div>
      ${over ? html`<div class="m-video-over">
        ${state.state !== 'error' ? html`<span class="spin"></span>` : null}
        <span>${state.error || (state.state === 'stopped' ? 'Stopped' : 'Connecting to robot camera…')}</span>
        ${reason ? html`<span style="font-size:12px;opacity:.8">${reason}</span>` : null}
        <button class="m-btn" style="height:38px;font-size:14px;background:rgba(255,255,255,.14);color:#fff;box-shadow:none"
          onClick=${start} disabled=${starting}>${starting ? 'Connecting…' : 'Reconnect'}</button>
      </div>` : null}
    </div>

    <h2 class="m-h2">Stream Info</h2>
    <div class="m-card">
      <div class="m-kv"><span class="k">Status</span><span class="v">${WORD[state.state] || state.state}</span></div>
      <div class="m-kv"><span class="k">Resolution</span><span class="v">${state.width && state.height ? `${state.width} × ${state.height}` : '—'}</span></div>
      <div class="m-kv"><span class="k">Frames</span><span class="v">${typeof state.frames === 'number' ? state.frames : '—'}</span></div>
      <div class="m-kv"><span class="k">Source</span><span class="v">${info?.source_label || '—'}</span></div>
      <div class="m-kv"><span class="k">Signaling Port</span><span class="v">${info?.signaling_port ?? '—'}</span></div>
    </div>

    <p class="m-tip">
      Video streams directly from mediad on the robot. Ensure your phone is on the same Wi-Fi and accessing <b>${location.hostname}:${location.port || '8000'}</b> directly.
    </p>
    ${location.protocol === 'https:' ? html`<div class="m-banner ok">
      <span>HTTPS active: signaling is proxied over /media-ws.</span>
    </div>` : null}
  </div>`;
}
