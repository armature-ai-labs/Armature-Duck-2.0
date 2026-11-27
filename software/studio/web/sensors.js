import { html, useState, useEffect, useRef } from './vendor/preact-htm.module.js';
import { CameraLink } from './camera-link.js';

if (typeof document !== 'undefined' && !document.getElementById('sensors-css')) {
  const sheet = document.createElement('link');
  sheet.id = 'sensors-css'; sheet.rel = 'stylesheet';
  sheet.href = new URL('./sensors.css', import.meta.url).href;
  document.head.appendChild(sheet);
}

const emptyTof = (state, reason, extra = {}) => ({ ...extra, state, reason, zones: [], valid_count: null, nearest_mm: null, farthest_mm: null });
const finite = n => typeof n === 'number' && Number.isFinite(n);
const TOF_FRESH_MS = 3000;

export function tofSnapshot(data) {
  if (!data || !['ready', 'waiting', 'unavailable', 'error'].includes(data.state)) return emptyTof('error', 'Invalid ToF payload.');
  const info = { sensor: typeof data.sensor === 'string' ? data.sensor : null, simulated: data.simulated === true, connected: data.connected === true,
    hz: finite(data.hz) ? data.hz : null, seq: data.seq, age_ms: data.age_ms };
  if (data.state !== 'ready') return emptyTof(data.state, data.reason || '', info);
  if (data.rows !== 8 || data.cols !== 8 || !Array.isArray(data.distance_mm) || data.distance_mm.length !== 64 ||
    !Array.isArray(data.status) || data.status.length !== 64 || !data.distance_mm.every(finite) ||
    !data.status.every(n => Number.isInteger(n) && n >= 0 && n <= 255) || !finite(data.age_ms) || data.age_ms < 0) {
    return emptyTof('error', 'Invalid ToF frame format; waiting for next 8x8 frame.', info);
  }
  if (data.age_ms >= TOF_FRESH_MS) return emptyTof('stale', 'Readings expired; waiting for fresh ToF frame.', info);
  const zones = data.status.map((status, i) => {
    const raw_mm = data.distance_mm[i];
    const kind = status === 255 ? 'no_target' : (status === 5 || status === 9) && raw_mm > 0 ? 'range' : 'unusable';
    return { kind, mm: kind === 'range' ? raw_mm : null, status, raw_mm };
  });
  const values = zones.filter(z => z.kind === 'range').map(z => z.mm);
  return { ...info, state: 'ready', reason: '', zones, valid_count: values.length,
    nearest_mm: values.length ? Math.min(...values) : null, farthest_mm: values.length ? Math.max(...values) : null };
}

export function cameraSource(config, producer) {
  const configured = config?.source === 'test' ? 'Config: Test Pattern' : config?.source === 'camera' ? 'Config: Camera' : 'Config: Unknown';
  const simulated = producer?.simulated === true || producer?.simulated === 'true';
  return { label: simulated ? `${configured} · Simulated producer` : configured, verified: false,
    note: config?.source === 'test' ? 'Configured for test pattern; playback does not indicate a physical camera.'
      : simulated ? 'Producer explicitly reports simulated device.'
      : config?.auto?.camera_present === true ? 'Kernel detected IMX219 at boot; inspect live preview below.'
        : 'Source inferred from read-only config; inspect live preview to verify hardware.' };
}

export function createTofReader({ request, onValue, intervalMs = 500, staleMs = 3000,
  now = () => performance.now(), setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout }) {
  let generation = 0, timer = null, staleTimer = null, controller = null, active = false;
  const stop = () => { active = false; generation++; clearTimeout(timer); clearTimeout(staleTimer); controller?.abort(); controller = null; };
  const read = async current => {
    if (!active || current !== generation) return;
    const requestStartedAt = now();
    controller = new AbortController();
    try {
      const result = await request(controller.signal);
      if (!active || current !== generation) return;
      if (!result?.ok) throw Error(typeof result?.error === 'string' ? result.error : 'ToF read failed.');
      const data = result.data;
      const serverAge = data?.age_ms;
      const received = finite(serverAge) && serverAge >= 0
        ? { ...data, age_ms: serverAge + Math.max(0, now() - requestStartedAt) } : data;
      const value = tofSnapshot(received);
      clearTimeout(staleTimer);
      onValue({ ...value, receivedAt: now(), updatedAt: Date.now() });
      if (value.state === 'ready') staleTimer = setTimeout(() => {
        if (active && current === generation) onValue(emptyTof('stale', 'Readings expired; waiting for fresh ToF frame.'));
      }, Math.max(0, staleMs - value.age_ms));
    } catch (error) {
      if (active && current === generation) {
        clearTimeout(staleTimer);
        onValue(emptyTof('error', error?.name === 'AbortError' ? 'ToF request timed out; check service.' : error.message || 'ToF disconnected.'));
      }
    } finally {
      if (active && current === generation) { controller = null; timer = setTimeout(() => read(current), intervalMs); }
    }
  };
  return { start() { stop(); active = true; onValue(emptyTof('loading', 'Reading ToF...')); read(generation); }, stop };
}

function useVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden');
  useEffect(() => {
    const change = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', change);
    return () => document.removeEventListener('visibilitychange', change);
  }, []);
  return visible;
}

const uncheckedBand = (reason = 'Click for details') => ({ state: 'unknown', word: 'Unchecked', small: reason });
const checkingBand = () => ({ state: 'checking', word: 'Checking', small: 'Read-only check' });

export function cameraBandStatus(data) {
  if (data?.service_state === 'offline') return { state: 'offline', word: 'Offline', small: 'mediad unreachable' };
  if (data?.service_state !== 'online') return uncheckedBand();
  if (data.source === 'test') return { state: 'test', word: 'Test Pattern', small: 'mediad online' };
  return { state: 'online', word: 'Online', small: data.source === 'camera' ? 'Click to preview' : 'Source unconfirmed' };
}

export function tofBandStatus(data) {
  const view = tofSnapshot(data);
  if (view.state === 'ready') {
    if (view.simulated) return { state: 'simulated', word: 'Simulated', small: 'Click for depth map' };
    if (!view.valid_count) return { state: 'waiting', word: 'No Valid Zones', small: 'Frame OK · 0/64 valid' };
    return { state: 'online', word: `${view.nearest_mm} mm`, small: `Nearest · ${view.valid_count}/64 valid` };
  }
  if (view.state === 'unavailable') return view.connected
    ? { state: 'waiting', word: 'Online', small: 'No sensor readings yet' }
    : { state: 'offline', word: 'Offline', small: 'ToF service offline' };
  if (view.state === 'waiting') return { state: 'waiting', word: 'Waiting', small: 'Service responded' };
  if (view.state === 'stale') return uncheckedBand('Last reading stale');
  return { state: 'error', word: 'Read Error', small: 'Click for details' };
}

export function createSensorBandReader({ request, onValue, intervalMs = 10000, staleMs = 20000,
  now = () => performance.now(), setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout }) {
  let generation = 0, active = false, timer = null;
  const controllers = new Set(), expiry = new Map();
  let values = { camera: checkingBand(), tof: checkingBand() };
  const emit = (kind, value) => { values = { ...values, [kind]: value }; onValue(values); };
  const stop = () => {
    active = false; generation++; clearTimeout(timer);
    for (const timeout of expiry.values()) clearTimeout(timeout);
    expiry.clear(); for (const controller of controllers) controller.abort(); controllers.clear();
  };
  const readOne = async (kind, current) => {
    const controller = new AbortController(), started = now(); controllers.add(controller);
    let value, expiresIn = staleMs;
    try {
      const result = await request(kind, controller.signal);
      if (!active || current !== generation) return;
      if (!result?.ok) throw Error('Read failed');
      const elapsed = Math.max(0, now() - started), data = result.data;
      if (kind === 'camera') {
        value = elapsed >= staleMs ? uncheckedBand('Check expired') : cameraBandStatus(data);
      } else {
        const received = finite(data?.age_ms) && data.age_ms >= 0 ? { ...data, age_ms: data.age_ms + elapsed } : data;
        value = tofBandStatus(received);
        if (received?.state === 'ready' && finite(received.age_ms)) {
          expiresIn = Math.max(0, Math.min(staleMs, TOF_FRESH_MS - received.age_ms));
        }
      }
    } catch {
      if (!active || current !== generation) return;
      value = { state: 'error', word: 'Read Error', small: 'Click for details' };
    } finally { controllers.delete(controller); }
    if (!active || current !== generation) return;
    clearTimeout(expiry.get(kind)); emit(kind, value);
    expiry.set(kind, setTimeout(() => {
      if (active && current === generation) emit(kind, uncheckedBand('Status expired; waiting'));
    }, expiresIn));
  };
  const read = async current => {
    await Promise.allSettled(['camera', 'tof'].map(kind => readOne(kind, current)));
    if (active && current === generation) timer = setTimeout(() => read(current), intervalMs);
  };
  return { start() {
    stop(); active = true; values = { camera: checkingBand(), tof: checkingBand() }; onValue(values); read(generation);
  }, stop };
}

export function SensorBandCells({ api, active }) {
  const visible = useVisible();
  const [values, setValues] = useState({ camera: checkingBand(), tof: checkingBand() });
  useEffect(() => {
    if (!active || !visible) { setValues({ camera: uncheckedBand(), tof: uncheckedBand() }); return; }
    const reader = createSensorBandReader({ request: (kind, signal) => api(`/api/${kind}`, undefined, 3500, signal), onValue: setValues });
    reader.start(); return () => reader.stop();
  }, [api, active, visible]);
  return ['camera', 'tof'].map(kind => html`<a key=${kind} href=${`#/${kind}`} class=${`band-sensor band-${kind}`} data-state=${values[kind].state}>
    <span class="label">${kind === 'camera' ? 'Camera' : 'ToF'}</span>
    <span class="word">${values[kind].word}</span><span class="small">${values[kind].small}</span>
  </a>`);
}

const CAMERA_STATES = { loading: 'Reading config...', connecting: 'Connecting camera...', signaling: 'Signaling connected...',
  negotiating: 'Negotiating WebRTC...', receiving: 'Video track received...', playing: 'Playing', stopped: 'Stopped', hidden: 'Paused', error: 'Preview failed',
  test: 'Test pattern inactive', offline: 'Offline', unknown: 'Source unconfirmed' };

export function inlineCameraPolicy(info) {
  if (info?.service_state === 'offline') return { play: false, state: 'offline', message: 'Camera stream unavailable; preview will appear when connected.' };
  if (info?.source === 'test') return { play: false, state: 'test', message: 'Configured for test pattern; connect a physical camera to preview.' };
  if (info?.source !== 'camera' || info?.service_state !== 'online') return { play: false, state: 'unknown', message: 'Camera source unconfirmed; open Camera page to inspect.' };
  return { play: true };
}

function useCameraSession({ api, video, active = true, inline = false }) {
  const camera = useRef(null);
  const [config, setConfig] = useState(null), [connection, setConnection] = useState({ state: 'loading' });
  const [wanted, setWanted] = useState(true), [attempt, setAttempt] = useState(0);
  const visible = useVisible();
  useEffect(() => {
    if (!active || !visible || !wanted) {
      camera.current?.stop(!active || !visible ? 'hidden' : 'stopped');
      setConnection({ state: !active || !visible ? 'hidden' : 'stopped' });
      return;
    }
    const abort = new AbortController(); let disposed = false, retry = null;
    const transport = new CameraLink({ video: video.current, onState: setConnection });
    camera.current = transport;
    setConnection({ state: 'loading' });
    setConfig(null);
    const read = async () => {
      let info;
      try {
        const result = await api('/api/camera', undefined, 3500, abort.signal);
        info = result?.ok ? result.data : { source: 'unknown', error: result?.error || 'Failed to read camera config' };
      } catch (error) { info = { source: 'unknown', error: error.name === 'AbortError' ? 'Camera config timed out' : 'Failed to read camera config' }; }
      if (disposed) return;
      setConfig(info);
      if (inline) {
        const policy = inlineCameraPolicy(info);
        if (!policy.play) {
          setConnection({ state: policy.state, message: policy.message });
          retry = setTimeout(read, 10000);
          return;
        }
      }
      const proxyPath = typeof location !== 'undefined' && location.protocol === 'https:' ? '/media-ws' : null;
      transport.start(info?.signaling_port ?? 8443, proxyPath);
    };
    read();
    return () => { disposed = true; clearTimeout(retry); abort.abort(); transport.stop(); if (camera.current === transport) camera.current = null; };
  }, [api, active, visible, wanted, attempt, inline]);
  return { config, connection, wanted, reconnect: () => { setWanted(true); setAttempt(n => n + 1); },
    stop: () => { camera.current?.stop(); setWanted(false); } };
}

function CameraScreen({ connection, video, compact = false }) {
  const screen = useRef(null), [ratio, setRatio] = useState(16 / 9);
  useEffect(() => {
    const measure = () => {
      const rect = screen.current?.getBoundingClientRect();
      if (rect?.width > 0 && rect?.height > 0) setRatio(old => Math.abs(old - rect.width / rect.height) < 0.001 ? old : rect.width / rect.height);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(screen.current); measure();
    return () => observer.disconnect();
  }, []);
  const rotate = connection.metadata?.rotate || 0;
  const width = connection.width || connection.metadata?.width || 640;
  const height = connection.height || connection.metadata?.height || 480;
  const sideways = rotate === 90 || rotate === 270;
  return html`<div ref=${screen} class="camera-screen" data-playing=${connection.state === 'playing'}
    style=${compact ? { '--camera-display-aspect': String(sideways ? height / width : width / height) } : { aspectRatio: sideways ? `${height} / ${width}` : `${width} / ${height}`,
      maxWidth: `min(100%, ${72 * (sideways ? height / width : width / height)}vh)` }}>
    <video class=${compact ? 'inline-camera-video' : 'camera-video'} ref=${video} muted playsInline autoPlay aria-label="Duck camera feed"
      style=${{ width: sideways ? `${100 / ratio}%` : '100%', height: sideways ? `${100 * ratio}%` : '100%', transform: `translate(-50%, -50%) rotate(${rotate}deg)` }}></video>
    ${connection.state !== 'playing' ? html`<div class="camera-placeholder">${connection.error || connection.message || CAMERA_STATES[connection.state] || 'Waiting for video'}</div>` : null}
  </div>`;
}

export function InlineCamera({ api, active = false }) {
  const card = useRef(null), video = useRef(null);
  const [inView, setInView] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => setInView(entries.some(entry => entry.isIntersecting)), { threshold: 0 });
    observer.observe(card.current);
    return () => observer.disconnect();
  }, []);
  const { config, connection, reconnect } = useCameraSession({ api, video, active: active && inView, inline: true });
  const source = cameraSource(config, connection.producer);
  return html`<section ref=${card} class="board pd-card inline-camera" data-expanded=${expanded} aria-labelledby="inline-camera-title">
    <div class="inline-camera-head"><h3 id="inline-camera-title">Camera</h3>
      <span class="sensor-pill inline-camera-status" role="status" data-state=${connection.state}>${CAMERA_STATES[connection.state] || 'Waiting'}</span>
      <div class="inline-camera-actions"><button class="inline-camera-expand" type="button" aria-pressed=${expanded}
        disabled=${!expanded && connection.state !== 'playing'} onClick=${() => setExpanded(value => !value)}>${expanded ? 'Restore' : 'Expand'}</button>
        <button class="btn-light inline-camera-retry" type="button" onClick=${reconnect} disabled=${['loading', 'connecting', 'signaling', 'negotiating', 'receiving'].includes(connection.state)}>Retry</button></div>
    </div>
    <${CameraScreen} connection=${connection} video=${video} compact />
    <div class="inline-camera-meta"><span class="inline-camera-source">${config ? source.label : 'Front camera feed'}</span></div>
  </section>`;
}

export function CameraPage({ api }) {
  const video = useRef(null);
  const { config, connection, wanted, reconnect, stop } = useCameraSession({ api, video });
  const source = cameraSource(config, connection.producer);
  return html`<section class="sensor-page camera-page" aria-labelledby="camera-title">
    <header class="sensor-heading"><div><h1 id="camera-title">Camera</h1><p>Live WebRTC video feed from the duck.</p></div>
      <span class="sensor-pill camera-status" role="status" data-state=${connection.state}>${CAMERA_STATES[connection.state] || connection.state}</span></header>
    <div class="board sensor-board camera-source"><strong>${source.label}</strong><p>${source.note}</p>
      ${config?.error ? html`<p class="sensor-note">${config.error}; still attempting connection.</p>` : null}
      ${config?.auto?.note ? html`<p class="sensor-note camera-auto">${config.auto.note}</p>` : null}</div>
    <div class="sensor-actions">
      <button type="button" class="camera-start sensor-primary" onClick=${reconnect} disabled=${wanted && connection.state !== 'error' && connection.state !== 'stopped'}>Start Preview</button>
      <button type="button" class="camera-stop" onClick=${stop} disabled=${!wanted}>Stop Preview</button>
      <button type="button" class="camera-reconnect" onClick=${reconnect}>Reconnect</button>
    </div>
    <${CameraScreen} connection=${connection} video=${video} />
    <div class="board sensor-board sensor-metrics"><div><span>Resolution</span><b class="camera-size">${connection.state === 'playing' ? `${connection.width} × ${connection.height}` : '—'}</b></div>
      <div><span>Stream</span><b class="camera-received">${connection.state === 'playing' ? `Streaming${connection.frames != null ? ` · ${connection.frames} frames` : ''}` : 'Waiting for frames'}</b></div></div>
    ${connection.error ? html`<p class="sensor-error" role="alert">${connection.error}</p>` : null}
  </section>`;
}

const TOF_STATES = { loading: 'Connecting', ready: 'Live', waiting: 'Waiting for frame', unavailable: 'Unavailable', error: 'Read Error', stale: 'Stale', hidden: 'Paused' };
const zoneLabel = (zone, index) => `Row ${Math.floor(index / 8) + 1}, Col ${index % 8 + 1}: ${zone.kind === 'range' ? `${zone.mm} mm` : zone.kind === 'no_target' ? 'No target' : 'Invalid'} (raw ${zone.raw_mm} mm, status ${zone.status})`;

export function TofPage({ api }) {
  const [value, setValue] = useState(emptyTof('loading', 'Reading ToF...'));
  const [attempt, setAttempt] = useState(0), [selected, setSelected] = useState(null);
  const [view3d, setView3d] = useState(false);
  const tofCanvas = useRef(null), tof3dRef = useRef(null);
  const visible = useVisible();
  useEffect(() => {
    if (!visible) { setValue(emptyTof('hidden', 'Auto-resumes when tab is visible.')); return; }
    const reader = createTofReader({ request: signal => api('/api/tof', undefined, 3500, signal), onValue: setValue });
    reader.start(); return () => reader.stop();
  }, [api, visible, attempt]);
  useEffect(() => {
    if (!view3d || !tofCanvas.current) return undefined;
    let alive = true;
    import('./tof3d.js').then(({ Tof3D }) => {
      if (!alive || !tofCanvas.current) return;
      const viz = new Tof3D(tofCanvas.current);
      tof3dRef.current = viz;
    }).catch(() => {});
    return () => { alive = false; tof3dRef.current?.dispose(); tof3dRef.current = null; };
  }, [view3d]);
  useEffect(() => {
    if (tof3dRef.current && value.state === 'ready') {
      tof3dRef.current.setGrid(value.zones.map(z => (z.kind === 'range' ? z.mm : 0)));
    }
  }, [value]);
  const ready = value.state === 'ready';
  const zones = ready ? value.zones : Array(64).fill(null);
  const selection = ready && selected != null ? value.zones[selected] : null;
  const range = Math.max(1, (value.farthest_mm || 0) - (value.nearest_mm || 0));
  return html`<section class="sensor-page tof-page" aria-labelledby="tof-title">
    <header class="sensor-heading"><div><h1 id="tof-title">ToF Depth</h1><p>8 × 8 multizone range in millimeters (sensor native orientation).</p></div>
      <span class="sensor-pill tof-status" role="status" data-state=${value.state}>${ready && !value.valid_count ? 'Frame OK, no valid target' : TOF_STATES[value.state]}</span></header>
    <div class="board sensor-board tof-summary"><div><strong class="tof-sensor">${value.sensor || 'Sensor unconfirmed'}</strong>
      ${value.simulated ? html`<span class="sensor-pill tof-simulated">Simulated</span>` : null}</div>
      <p class="tof-reason" role=${value.state === 'error' ? 'alert' : 'status'}>${value.connected && !ready ? 'ToF service connected. ' : ''}${value.reason || (ready && !value.valid_count ? 'All 64 zones reported no valid target.' : ready ? 'Color-mapped by valid range (near = darker/hotter, far = lighter).' : 'Waiting for sensor readings.')}</p></div>
    <div class="sensor-actions">
      <button type="button" class="tof-refresh" onClick=${() => setAttempt(n => n + 1)}>Refresh</button>
      <button type="button" onClick=${() => setView3d(v => !v)}>${view3d ? '2D Matrix' : '3D Frustum'}</button>
    </div>
    <div class="tof-content"><div class="board sensor-board tof-visual">
      ${view3d
        ? html`<canvas ref=${tofCanvas} style="width:100%;height:320px;display:block;background:#090d13;border-radius:var(--radius)" aria-label="3D ToF depth frustum"></canvas>`
        : html`<div class="tof-grid" aria-label="ToF 8x8 depth zones" aria-busy=${!ready}>${zones.map((zone, i) => html`<button type="button" key=${i}
          class=${`tof-zone ${zone?.kind || 'empty'}`} data-zone=${i} data-kind=${zone?.kind || 'empty'} disabled=${!zone}
          title=${zone ? zoneLabel(zone, i) : 'No reading'} aria-label=${zone ? zoneLabel(zone, i) : `Row ${Math.floor(i / 8) + 1}, Col ${i % 8 + 1}: no reading`}
          aria-pressed=${selected === i && !!zone} onClick=${() => setSelected(i)}
          style=${zone?.kind === 'range' ? { backgroundColor: `hsl(28 72% ${32 + (zone.mm - value.nearest_mm) / range * 42}%)`, color: (zone.mm - value.nearest_mm) / range < .45 ? '#f6f6f2' : '#111110' } : null}>${zone?.kind === 'range' ? Math.round(zone.mm) : zone?.kind === 'no_target' ? '—' : zone ? '×' : '·'}</button>`)}</div>`}
      <div class="tof-legend"><span><i class="range"></i>Valid Range</span><span><i class="no_target"></i>No Target —</span><span><i class="unusable"></i>Invalid ×</span></div>
      <p class="tof-zone-detail" aria-live="polite">${selection ? zoneLabel(selection, selected) : 'Click any zone to inspect millimeter range and status.'}</p>
    </div><div class="board sensor-board sensor-metrics tof-metrics">
      <div><span>Valid Zones</span><b class="tof-valid">${ready ? `${value.valid_count} / 64` : '—'}</b></div>
      <div><span>Nearest</span><b class="tof-nearest">${ready && value.nearest_mm != null ? `${value.nearest_mm} mm` : '—'}</b></div>
      <div><span>Farthest</span><b class="tof-farthest">${ready && value.farthest_mm != null ? `${value.farthest_mm} mm` : '—'}</b></div>
      <div><span>Updated</span><b class="tof-updated">${ready ? new Date(value.updatedAt).toLocaleTimeString() : '—'}</b></div>
      <div><span>Rate</span><b>${ready && value.hz != null ? `${value.hz} Hz` : '—'}</b></div>
    </div></div>
  </section>`;
}
