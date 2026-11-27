// Mobile Voice page: board mic (duck-echo + Vosk) and phone mic (AudioWorklet -> /api/asr).
import { html, useState, useEffect, useRef, useCallback } from '../vendor/preact-htm.module.js';
import { link } from './link.js';
import { parseUtterance, runAction, CMD_CN, openPadArea, setAudio } from './actions.js';

const MODE_KEY = 'duck.voice.mode';
const SECURE = typeof window !== 'undefined' ? window.isSecureContext !== false : false;
const CAN_RECORD = typeof navigator !== 'undefined'
  && Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
const RATE = 16000;
const MIN_BYTES = 3200;

const PCM_WORKLET = `
class PcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice());
    return true;
  }
}
registerProcessor('pcm-tap', PcmTap);
`;
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

function makeCtx() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  let c = null;
  try { c = new Ctx({ sampleRate: RATE }); } catch { c = null; }
  if (!c) c = new Ctx();
  return c;
}

function stepTimeout(p, ms, what, onStage) {
  if (onStage) onStage(`${what}…`);
  let t = null;
  const guard = new Promise((_, rej) => {
    t = setTimeout(() => {
      const e = Error(`Timed out at "${what}" after ${Math.round(ms / 1000)}s.`);
      e.friendly = true;
      rej(e);
    }, ms);
  });
  return Promise.race([Promise.resolve(p), guard]).finally(() => clearTimeout(t));
}

let tapLoadedOn = null;
async function loadTap(ctx, onStage) {
  if (!ctx.audioWorklet) return false;
  if (tapLoadedOn === ctx) return 'worklet';
  for (const u of ['/m/pcm-tap.js', './pcm-tap.js']) {
    try {
      await stepTimeout(ctx.audioWorklet.addModule(u), 2500, 'Loading recorder', onStage);
      tapLoadedOn = ctx;
      return 'worklet';
    } catch { /* try next */ }
  }
  let url = null;
  try {
    url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: 'application/javascript' }));
    await stepTimeout(ctx.audioWorklet.addModule(url), 2000, 'Loading recorder', onStage);
    tapLoadedOn = ctx;
    return 'worklet';
  } catch { return false; }
  finally { if (url) URL.revokeObjectURL(url); }
}

function resample(f32, inRate) {
  if (!inRate || inRate === RATE || f32.length === 0) return f32;
  const ratio = inRate / RATE;
  const out = new Float32Array(Math.max(1, Math.floor(f32.length / ratio)));
  for (let i = 0; i < out.length; i++) {
    const p = i * ratio;
    const i0 = Math.floor(p);
    const i1 = Math.min(i0 + 1, f32.length - 1);
    const t = p - i0;
    out[i] = f32[i0] * (1 - t) + f32[i1] * t;
  }
  return out;
}

async function openRecorder(onStage) {
  const stage = (s) => { try { onStage && onStage(s); } catch { /* ignore */ } };
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) throw Error('Web Audio API is not supported in this browser.');
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw Error('Microphone API unavailable — ensure the page is served over HTTPS.');
  }

  const ctx = makeCtx();
  let wakeP = null;
  try { wakeP = ctx.resume(); } catch { wakeP = null; }
  let kick = null;
  try {
    kick = ctx.createBufferSource();
    kick.buffer = ctx.createBuffer(1, 1, ctx.sampleRate);
    kick.connect(ctx.destination);
    kick.start(0);
  } catch { kick = null; }

  let stream = null, node = null, src = null, mute = null, closed = false, lastState = '';
  const chunks = [];
  let total = 0;
  const push = (f) => { if (f && f.length) { chunks.push(f); total += f.length; } };

  const teardown = () => {
    closed = true;
    lastState = ctx.state;
    try { src && src.disconnect(); } catch { /* ignore */ }
    try { node && node.disconnect(); } catch { /* ignore */ }
    try { mute && mute.disconnect(); } catch { /* ignore */ }
    try { kick && kick.disconnect(); } catch { /* ignore */ }
    try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
    try { if (ctx.state !== 'closed') ctx.close(); } catch { /* ignore */ }
  };

  try {
    if (wakeP) {
      try { await stepTimeout(wakeP, 1500, 'Waking audio', stage); } catch { /* continue */ }
    }
    if (ctx.state !== 'running') {
      try { await stepTimeout(ctx.resume(), 1200, 'Waking audio', stage); } catch { /* continue */ }
    }
    if (isIOS) {
      try {
        const el = new Audio(SILENT_WAV);
        el.volume = 0;
        el.play().then(() => el.pause(), () => { /* ignore */ });
      } catch { /* ignore */ }
    }

    const micP = navigator.mediaDevices.getUserMedia({ audio: true });
    micP.then((s) => {
      if (closed) { try { s.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ } }
    }, () => { /* handled below */ });
    stream = await stepTimeout(micP, 10000, 'Opening microphone', stage);

    const kind = await loadTap(ctx, stage);
    if (kind === 'worklet') {
      node = new AudioWorkletNode(ctx, 'pcm-tap');
      node.port.onmessage = (e) => push(e.data);
    } else if (ctx.createScriptProcessor) {
      stage('Preparing recorder (compat)…');
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (e) => push(e.inputBuffer.getChannelData(0).slice());
    } else {
      throw Error('Neither AudioWorklet nor ScriptProcessor is supported.');
    }

    src = ctx.createMediaStreamSource(stream);
    mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);

    const inRate = ctx.sampleRate || RATE;
    const tapKind = kind === 'worklet' ? 'AudioWorklet' : 'ScriptProcessor';
    stage('');
    return {
      cancel: teardown,
      seconds: () => total / (inRate || RATE),
      diag: () => `ctx=${lastState || ctx.state}, tap=${tapKind}, rate=${inRate}`,
      state: () => ctx.state,
      async stop() {
        if (closed) return new ArrayBuffer(0);
        teardown();

        const flat = new Float32Array(total);
        let o = 0;
        for (const c of chunks) { flat.set(c, o); o += c.length; }
        const pcm16 = resample(flat, inRate);
        const out = new Int16Array(pcm16.length);
        for (let i = 0; i < pcm16.length; i++) {
          const v = Math.max(-1, Math.min(1, pcm16[i]));
          out[i] = v < 0 ? v * 0x8000 : v * 0x7FFF;
        }
        return out.buffer;
      },
    };
  } catch (e) {
    teardown();
    throw e;
  }
}

async function asrOnBoard(pcm, signal) {
  const res = await fetch('/api/asr', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: pcm,
    signal,
  });
  if (res.status === 401) throw Error('Session expired — sign in again.');
  if (res.status === 413) throw Error('Recording too long — keep commands short.');
  let body = null;
  try { body = await res.json(); } catch { throw Error(`ASR failed (HTTP ${res.status})`); }
  if (!body || body.ok !== true) throw Error((body && body.error) || 'Could not recognize speech');
  return (body.data && body.data.text) || '';
}

export async function recognizePhoneRecording(pcm, signal, transcribe, onText) {
  if (signal.aborted) return false;
  const text = await transcribe(pcm, signal);
  if (signal.aborted || !text) return false;
  await onText(text);
  return true;
}

const micWhy = (e) => {
  if (e && e.friendly && e.message) return e.message;
  const n = e && e.name;
  const m = (e && e.message) ? ` (${e.name || ''} ${e.message})` : '';
  if (n === 'NotAllowedError' || n === 'SecurityError') {
    return `Microphone permission denied${m}. Allow microphone access in browser site settings (and trust the local HTTPS certificate if prompted).`;
  }
  if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No microphone found on this device.';
  if (n === 'NotReadableError') return `Microphone in use by another app${m}.`;
  return `Microphone error: ${(e && e.message) || e}${m}`;
};

const PERM_CN = { granted: 'Granted', prompt: 'Not requested yet', denied: 'Denied', unknown: 'Unknown' };

const modeOf = () => {
  try { return localStorage.getItem(MODE_KEY) === 'phone' ? 'phone' : 'board'; } catch { return 'board'; }
};

function MicIcon() {
  return html`<svg viewBox="0 0 24 24" aria-hidden="true">
    <rect x="9" y="2.6" width="6" height="10.4" rx="3"/><path d="M5.6 11a6.4 6.4 0 0 0 12.8 0M12 17.4V21M8.6 21h6.8"/>
  </svg>`;
}

const BOARD_WORDS = 'stand · sit · stop · roll · pick · chirp · greet · nod · shake · tilt';
const KNOWN_WORDS = `step · ${BOARD_WORDS}`;

export function VoicePage({ st, pad, live, name, say }) {
  const [mode, setMode] = useState(modeOf());
  const [listening, setListening] = useState(false);
  const [boardBusy, setBoardBusy] = useState(false);
  const [heard, setHeard] = useState('');
  const [isInterim, setIsInterim] = useState(false);
  const [sent, setSent] = useState(null);
  const [history, setHistory] = useState([]);
  const [typed, setTyped] = useState('');
  const [err, setErr] = useState(null);
  const [ask, setAsk] = useState(null);
  const [perm, setPerm] = useState('unknown');
  const [asrSec, setAsrSec] = useState(0);
  const [stage, setStage] = useState('');
  const pointerRef = useRef(null);
  const postureRef = useRef(null);
  postureRef.current = live ? st?.posture : null;
  useEffect(() => () => { postureRef.current = null; }, []);

  const audio = pad?.audio || {};
  const boardOk = audio.available === true;
  const boardOn = audio.listening === true;
  const phoneOk = SECURE && CAN_RECORD;
  const phoneWhy = phoneOk ? null
    : !SECURE ? 'Page is served over HTTP; mobile browsers require HTTPS for microphone access.'
      : 'Microphone recording is not supported in this browser.';

  useEffect(() => { try { localStorage.setItem(MODE_KEY, mode); } catch { /* private mode */ } }, [mode]);

  useEffect(() => {
    link.setPadVisible(true);
    openPadArea(link);
  }, []);

  useEffect(() => {
    const q = navigator.permissions && navigator.permissions.query;
    if (!q) return undefined;
    let p = null;
    navigator.permissions.query({ name: 'microphone' })
      .then((r) => { p = r; setPerm(r.state); r.onchange = () => setPerm(r.state); })
      .catch(() => { /* unsupported */ });
    return () => { if (p) p.onchange = null; };
  }, []);

  const askMic = useCallback(async () => {
    if (!CAN_RECORD) { setErr('Microphone recording is unavailable in this browser.'); return; }
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach((t) => t.stop());
      setPerm('granted');
      setErr(null);
    } catch (e) {
      setPerm('denied');
      setErr(micWhy(e));
    }
  }, []);

  const remember = useCallback((cn, ok) => {
    setHistory((h) => [{ cn, ok, at: Date.now() }, ...h].slice(0, 6));
  }, []);

  const dispatch = useCallback(async (cmd, said) => {
    setHeard(said);
    setIsInterim(false);
    setAsk(null);
    const out = await runAction(link, cmd, { getPosture: () => postureRef.current });
    const cn = CMD_CN[cmd] || out.cn;
    setSent({ cn, ok: out.ok, why: out.ok ? null : (out.error || null) });
    remember(cn, out.ok);
    if (!out.ok) { setErr(out.error || 'Command failed'); return; }
    setErr(null);
    if (out.note) say(out.note);
  }, [remember, say]);

  const handleText = useCallback(async (text) => {
    const { cmd, said } = parseUtterance(text, { requireWake: false });
    if (cmd) { await dispatch(cmd, said); return; }
    setHeard(said || text);
    setIsInterim(false);
    setSent(null);
    setAsk(null);
    setErr(said ? `Unrecognized command "${said}". Try: ${KNOWN_WORDS}` : 'Did not catch that — try again.');
  }, [dispatch]);

  const recRef = useRef(null);
  const turnRef = useRef(null);
  const discardPhone = useCallback(() => {
    pointerRef.current = null;
    turnRef.current?.abort();
    turnRef.current = null;
    recRef.current?.cancel();
    recRef.current = null;
  }, []);
  useEffect(() => discardPhone, [discardPhone]);

  const cancelPhone = useCallback(() => {
    discardPhone();
    setListening(false);
    setStage('');
    setIsInterim(false);
  }, [discardPhone]);

  const startPhone = useCallback(async (turn) => {
    setErr(null); setHeard(''); setIsInterim(false); setSent(null); setAsk(null);
    setListening(true);
    setStage('Opening microphone…');
    let timeout;
    const opening = openRecorder((message) => {
      if (!turn.signal.aborted) setStage(message);
    }).then((rec) => {
      if (turn.signal.aborted) { rec.cancel(); return; }
      recRef.current = rec;
      setStage('');
    });
    try {
      await Promise.race([
        opening,
        new Promise((_, rej) => {
          const e = Error('Microphone did not start within 18s. Tap "Allow Mic" above or run /diag.');
          e.friendly = true;
          timeout = setTimeout(() => rej(e), 18000);
        }),
      ]);
    } catch (e) {
      if (turn.signal.aborted) return;
      cancelPhone();
      setErr(micWhy(e));
    } finally {
      clearTimeout(timeout);
    }
  }, [cancelPhone]);

  const stopPhone = useCallback(async (turn) => {
    const rec = recRef.current;
    recRef.current = null;
    setListening(false);
    setStage('');
    if (!rec) {
      turn.abort();
      return;
    }
    let pcm = null;
    try { pcm = await rec.stop(); }
    catch (e) {
      if (!turn.signal.aborted) setErr(`Recording stop failed: ${(e && e.message) || e}`);
      return;
    }
    if (turn.signal.aborted) return;
    if (!pcm || pcm.byteLength < MIN_BYTES) {
      const s = typeof rec.seconds === 'function' ? rec.seconds() : 0;
      const why = typeof rec.diag === 'function' ? ` (${rec.diag()})` : '';
      if (s >= 0.05) {
        setErr(`Only recorded ${s.toFixed(2)}s — hold while speaking, then release.${why}`);
      } else if (pcm && pcm.byteLength > 0) {
        setErr(`Audio too short${why} — hold longer and speak closer to the mic.`);
      } else {
        setErr(`No audio samples captured${why}. Hold for at least 1s or use Chrome.`);
      }
      return;
    }
    setHeard('Recognizing…'); setIsInterim(true);
    setAsrSec(0);
    const tick = setInterval(() => {
      if (!turn.signal.aborted) setAsrSec((n) => n + 1);
    }, 1000);
    try {
      const recognized = await recognizePhoneRecording(pcm, turn.signal, asrOnBoard, handleText);
      if (!recognized && !turn.signal.aborted) {
        setIsInterim(false);
        setErr(`Sent ${(pcm.byteLength / 32000).toFixed(1)}s of audio, but no command matched.`);
        return;
      }
    } catch (e) {
      if (turn.signal.aborted) return;
      setHeard(''); setIsInterim(false);
      setErr((e && e.message) || 'Recognition failed');
    } finally {
      clearInterval(tick);
    }
  }, [handleText]);

  const onDown = useCallback((event) => {
    if (!event.isPrimary || event.button !== 0 || pointerRef.current !== null) return;
    event.preventDefault();
    if (!phoneOk) { setErr(`${phoneWhy} Use keyboard dictation or Robot Mic instead.`); return; }
    discardPhone();
    pointerRef.current = event.pointerId;
    const turn = new AbortController();
    turnRef.current = turn;
    startPhone(turn);
  }, [discardPhone, phoneOk, phoneWhy, startPhone]);

  const onUp = useCallback((event) => {
    if (pointerRef.current !== event.pointerId) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom) {
      cancelPhone();
      return;
    }
    pointerRef.current = null;
    stopPhone(turnRef.current);
  }, [cancelPhone, stopPhone]);

  const onCancel = useCallback((event) => {
    if (pointerRef.current === event.pointerId) cancelPhone();
  }, [cancelPhone]);

  const toggleBoard = useCallback(async (on) => {
    if (boardBusy) return;
    setErr(null);
    setBoardBusy(true);
    const r = await setAudio(link, { listening: Boolean(on) });
    setBoardBusy(false);
    if (r.ok === false) setErr(r.reason || r.error || 'Failed to toggle robot mic');
    else say(on ? 'Robot mic listening' : 'Robot mic stopped');
  }, [boardBusy, say]);

  const last = audio.last;
  const lastSeq = useRef(-1);
  useEffect(() => {
    if (!boardOn || !last || !last.command) return;
    if (typeof last.seq === 'number' && last.seq === lastSeq.current) return;
    lastSeq.current = typeof last.seq === 'number' ? last.seq : lastSeq.current;
    const cn = CMD_CN[last.command] || last.command;
    setHeard(cn);
    setIsInterim(false);
    const failed = last.accepted === false;
    setSent({ cn, ok: !failed, why: failed ? (last.reason || null) : null });
    remember(cn, !failed);
  }, [boardOn, last, remember]);

  const submitTyped = (e) => {
    e.preventDefault();
    const v = typed.trim();
    if (!v) return;
    setTyped('');
    cancelPhone();
    handleText(v);
  };

  return html`<div class="m-voice-hero">
    <h1 class="m-voice-title">Voice Control</h1>
    <p class="m-voice-lede">${mode === 'board'
      ? html`Speak directly to ${name} using its onboard microphone.`
      : html`Hold to record a command on your phone and release to send.`}</p>

    <div class="m-seg" role="group" aria-label="Voice channel" style="margin:14px 0 0">
      <button aria-pressed=${mode === 'board' ? 'true' : 'false'} onClick=${() => { if (mode !== 'board') cancelPhone(); setMode('board'); setErr(null); setAsk(null); }}>
        Robot Mic${boardOn ? ' · Live' : ''}
      </button>
      <button aria-pressed=${mode === 'phone' ? 'true' : 'false'} onClick=${() => { setMode('phone'); setErr(null); setAsk(null); }}>
        Phone Mic
      </button>
    </div>

    ${mode === 'board' ? html`<div>
      <div class=${'m-mic-wrap' + (boardOn ? ' live' : '')}>
        <span class="m-halo" aria-hidden="true"></span>
        <button class=${'m-mic' + (boardOn ? ' live' : '')}
          onClick=${() => toggleBoard(!boardOn)} disabled=${boardBusy}
          aria-label=${boardOn ? 'Stop robot listening' : 'Start robot listening'}>
          <${MicIcon} />
          <span class="t1">${boardBusy ? 'Switching…' : boardOn ? 'Listening' : 'Start Listening'}</span>
          <span class="t2">${boardOn ? 'Tap to stop' : 'Tap and speak'}</span>
        </button>
      </div>
      <p class="m-tip">
        ${boardOn
          ? html`Listening: say <b>stand</b>, <b>nod</b>, or <b>stop</b>. Tap again to disable when done.`
          : html`Uses the microphone configured in Settings. Best in quiet rooms.`}
      </p>
      ${!boardOk ? html`<div class="m-banner bad" style="text-align:left">
        Robot voice service offline: ${audio.why || audio.error || 'duck-echo not running'}. Use Phone Mic or text input.
      </div>` : audio.voice_ready === false ? html`<div class="m-banner bad" style="text-align:left">
        Onboard ASR warming up${audio.error ? ` (${audio.error})` : ' (loading model…)'}.
      </div>` : audio.error ? html`<div class="m-banner bad" style="text-align:left">
        Voice service error (${audio.error})
      </div>` : null}
    </div>` : html`<div>
      <div class=${'m-mic-wrap' + (listening ? ' live' : '')}>
        <span class="m-halo" aria-hidden="true"></span>
        <button class=${'m-mic m-mic-talk' + (listening ? ' live' : '')}
          onPointerDown=${onDown} onPointerUp=${onUp} onPointerCancel=${onCancel} onPointerLeave=${onCancel} onLostPointerCapture=${onCancel}
          onContextMenu=${(e) => e.preventDefault()}
          aria-label="Hold to speak, release to send">
          <${MicIcon} />
          <span class="t1">${listening ? (stage || 'Speak Now') : 'Hold to Speak'}</span>
          ${listening ? html`<span class="t2">${stage ? 'Wait…' : 'Release to send'}</span>` : null}
        </button>
      </div>
      ${perm === 'denied' ? html`<div class="m-banner bad" style="text-align:left">
        <span>Microphone permission denied. Enable it in browser site settings, then tap Retry.</span>
        <button class="act" onClick=${askMic}>Retry</button>
      </div>` : null}
      ${perm !== 'granted' && perm !== 'denied' ? html`<div class="m-banner" style="text-align:left">
        <span>Mic permission: ${PERM_CN[perm]}. Grant access before holding to speak.</span>
        <button class="act" onClick=${askMic}>Allow Mic</button>
      </div>` : null}
      <p class="m-tip">
        ${phoneOk
          ? html`Hold until <b>Speak Now</b> appears, say one command, then release (processed locally on the robot).`
          : html`${phoneWhy} Use keyboard dictation below or switch to Robot Mic.`}
      </p>
      <p class="m-tip"><a href="/diag">Mic issues? Run audio self-check →</a></p>
      ${boardOn ? html`<div class="m-banner" style="text-align:left">
        <span>Robot mic is still listening; disable it to avoid duplicate triggers.</span>
        <button class="act" onClick=${() => toggleBoard(false)}>Stop Robot Mic</button>
      </div>` : null}
    </div>`}

    <div class="m-sent">
      ${sent ? html`Sent: <b>${sent.cn}</b>${sent.ok ? '' : (sent.why ? ` (failed: ${sent.why})` : ' (failed)')}` : html`<span style="color:var(--text-3)">No command sent yet</span>`}
    </div>

    <div class="m-heard">${mode === 'board' ? 'Robot Heard' : 'Recognized'}</div>
    <div class=${'m-heard-text' + (isInterim ? ' interim' : '')}>${heard ? `"${heard}"` : '—'}</div>
    ${isInterim ? html`<div class="m-tip" style="margin-top:2px">
      Transcribing on robot… ${asrSec}s${asrSec >= 4 ? ' (initial model load takes a few seconds)' : ''}
    </div>` : null}

    ${err ? html`<div class="m-banner bad" style="text-align:left">${err}</div>` : null}

    <form class="m-type" onSubmit=${submitTyped}>
      <input id="m-type" placeholder="Or type command: stand" value=${typed} enterkeyhint="send"
        onInput=${(e) => setTyped(e.currentTarget.value)} />
      <button class="m-btn primary" type="submit" disabled=${!typed.trim()}>Send</button>
    </form>

    <div class="m-policy-row">
      <button class="m-key pol-start" onClick=${() => dispatch('walk', '')}>Start Walk<small>enable policy</small></button>
      <button class="m-key pol-stop" onClick=${() => dispatch('stop', '')}>Stop Policy<small>hold standing</small></button>
    </div>

    <h2 class="m-h2" style="text-align:left">Supported Commands</h2>
    <div class="m-card m-pad" style="text-align:left">
      <div class="m-sub">${mode === 'board' ? BOARD_WORDS : KNOWN_WORDS}</div>
      <div class="m-tip" style="margin-bottom:0">
        ${mode === 'board'
          ? html`Speak clearly near the robot while it is standing still.`
          : html`Hold the button until you finish speaking.`}
      </div>
    </div>

    ${history.length ? html`<h2 class="m-h2" style="text-align:left">Recent</h2>
      <div class="m-card m-pad" style="text-align:left">
        <ul class="m-log">
          ${history.map((h) => html`<li class=${h.ok ? '' : 'bad'}>
            <span class="who">${h.ok ? '✓' : '×'}</span><span class="what">${h.cn}</span>
            <span class="when">${new Date(h.at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
          </li>`)}
        </ul>
      </div>` : null}

    <p class="m-tip">
      Commands execute once per utterance; unrecognized phrases are ignored. <b>E-Stop</b> is always at the top-right.
    </p>
  </div>`;
}
