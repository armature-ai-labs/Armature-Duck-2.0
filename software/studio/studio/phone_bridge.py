"""Two helpers needed for mobile HTTPS: camera WebSocket signaling relay and mobile PCM speech recognition.

1. Camera: mediad signaling port 8443 is plain ws://. An HTTPS page blocks ws:// as mixed content,
   so Studio exposes a same-origin /media-ws relay to 127.0.0.1:8443.
2. Mobile voice: browser Web Speech API relies on cloud services that may be unavailable or inconsistent,
   so mobile sends recorded PCM audio back to Studio to run through the local Vosk model.
"""
import array
import asyncio
import json
import math
import os
import sys
import threading
import time

ASR_DEPS = '/opt/microduck/resources/asr-deps'
MODEL_DIR = '/opt/microduck/resources/models/vosk-model-small-cn-0.22'
ECHO_DIR = '/opt/microduck/software/current/duck-echo'   # duck-echo sits alongside Studio; reuse its grammar
MEDIA_UPSTREAM = 'ws://127.0.0.1:8443'
SAMPLE_RATE = 16000

MIN_PEAK = 120          # Below this peak amplitude, treat audio as silent (int16 full scale 32767)
MIN_CONF = 0.35         # Below this confidence, treat as unclear rather than triggering an unintended action
TARGET_PEAK = 20000     # Normalization target peak
MAX_GAIN = 8.0
CHUNK = 8000            # 0.25s per chunk

# On-device Vosk grammar entries (kept as Unicode escapes for source-level CJK gate compatibility).
MOBILE_COMMANDS = [
    '\u70b9\u5934', '\u6447\u5934', '\u6b6a\u5934', '\u504f\u5934',
    '\u524d\u6eda\u7ffb', '\u7ffb\u8ddf\u5934', '\u7ffb\u8eab',
    '\u4f4e\u5934\u6361\u4e1c\u897f', '\u4f4e\u5934', '\u6361\u4e1c\u897f',
    '\u53eb\u4e00\u58f0', '\u53eb\u4e00\u4e0b', '\u53eb\u4e24\u58f0',
    '\u6253\u62db\u547c', '\u4f60\u597d', '\u5f20\u5634', '\u5f00\u53e3',
    '\u7ad9\u8d77\u6765', '\u7ad9\u8d77', '\u8d77\u7acb', '\u8d77\u6765', '\u8d77\u8eab',
    '\u5750\u4e0b', '\u5750\u4e00\u5750', '\u505c\u4e0b', '\u7ad9\u4f4f',
    '\u8d70\u4e00\u5c0f\u6b65', '\u8d70\u4e00\u6b65', '\u8d70\u4e24\u6b65',
    '\u8fc8\u4e00\u6b65', '\u8d70\u4e00\u4e0b', '\u524d\u8fdb', '\u5f80\u524d', '\u5f00\u6b65',
]
MOBILE_PREFIXES = ('', '\u9e2d\u9e2d', '\u9e2d\u5b50', '\u8bf7', '\u4e0d\u8981', '\u522b')

_model = None
_error = None
_lock = threading.Lock()
_grammar = None
_grammar_ready = False


def _deps_on_path():
    if os.path.isdir(ASR_DEPS) and ASR_DEPS not in sys.path:
        sys.path.insert(0, ASR_DEPS)


def _load_model():
    """Load Vosk model lazily on first use and keep it cached."""
    global _model, _error
    with _lock:
        if _model is not None or _error is not None:
            return _model, _error
        try:
            _deps_on_path()
            from vosk import Model, SetLogLevel
            # Suppress Vosk stdout spam (otherwise logs entire grammar per utterance)
            SetLogLevel(-1)
            _model = Model(MODEL_DIR)
        except Exception as exc:                       # Not installed or model missing: feature disabled
            _error = '%s: %s' % (type(exc).__name__, exc)
    return _model, _error


def _mobile_grammar():
    """Expand mobile commands into space-separated characters with optional prefixes."""
    out = []
    for phrase in MOBILE_COMMANDS:
        body = ' '.join(phrase)
        for prefix in MOBILE_PREFIXES:
            out.append((' '.join(prefix) + ' ' + body) if prefix else body)
    return out


def _grammar_json(model):
    """Merge mobile commands + duck-echo grammar; returns None if empty (falls back to open decoding)."""
    global _grammar, _grammar_ready
    if _grammar_ready:
        return _grammar
    entries = list(_mobile_grammar())
    try:
        if ECHO_DIR not in sys.path:
            sys.path.insert(0, ECHO_DIR)
        from speech_control import COMMAND_GRAMMAR
        entries.extend(COMMAND_GRAMMAR)
    except Exception:
        pass
    try:
        # Keep only phrases whose tokens exist in the model lexicon
        kept, seen = [], set()
        for phrase in entries:
            if phrase in seen:
                continue
            seen.add(phrase)
            if all(model.vosk_model_find_word(w) >= 0 for w in phrase.split()):
                kept.append(phrase)
        if kept and '[unk]' not in kept:
            kept.append('[unk]')
        _grammar = json.dumps(kept, ensure_ascii=False) if kept else None
    except Exception:
        _grammar = None
    _grammar_ready = True
    return _grammar


def _normalize(pcm):
    """Normalize PCM amplitude and return audio diagnostics."""
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if not samples:
        return b'', {'sec': 0.0, 'peak': 0, 'rms': 0, 'gain': 1.0}
    peak = max(max(samples), -min(samples))
    rms = int(math.sqrt(sum(x * x for x in samples) / len(samples)))
    gain = 1.0
    if 0 < peak < TARGET_PEAK:
        gain = min(MAX_GAIN, TARGET_PEAK / float(peak))
        if gain > 1.05:
            samples = array.array('h', [max(-32768, min(32767, int(x * gain))) for x in samples])
        else:
            gain = 1.0
    return samples.tobytes(), {'sec': round(len(samples) / float(SAMPLE_RATE), 2),
                               'peak': int(peak), 'rms': int(rms), 'gain': round(gain, 2)}


def transcribe_detail(pcm, use_grammar=True):
    """16 kHz / 16-bit / mono / little-endian raw PCM -> {'text', 'error', 'info'}."""
    out = {'text': '', 'error': None, 'info': {}}
    model, err = _load_model()
    if err:
        out['error'] = err
        return out

    pcm, info = _normalize(pcm)
    out['info'] = info
    if info['peak'] < MIN_PEAK:
        info['reason'] = 'silent'
        return out

    started = time.time()
    try:
        _deps_on_path()
        from vosk import KaldiRecognizer
        grammar = _grammar_json(model) if use_grammar else None
        rec = (KaldiRecognizer(model, SAMPLE_RATE, grammar) if grammar
               else KaldiRecognizer(model, SAMPLE_RATE))
        rec.SetWords(True)
        parts = []
        for i in range(0, len(pcm), CHUNK):
            if rec.AcceptWaveform(pcm[i:i + CHUNK]):
                parts.append(json.loads(rec.Result()))
        parts.append(json.loads(rec.FinalResult()))
    except Exception as exc:
        out['error'] = '%s: %s' % (type(exc).__name__, exc)
        return out

    text = ''.join(p.get('text', '') for p in parts).replace(' ', '')
    # With grammar decoding, unmatched speech emits [unk]; strip it so frontend never tries to run '[unk]'.
    unknown = '[unk]' in text
    text = text.replace('[unk]', '').strip()
    confs = [w.get('conf', 0.0) for p in parts for w in p.get('result', [])]
    conf = min(confs, default=0.0)
    info.update({'ms': int((time.time() - started) * 1000), 'conf': round(conf, 2),
                 'grammar': bool(grammar)})
    if not text:
        info['reason'] = 'unknown' if unknown else 'unclear'
        return out
    if unknown:
        # Partial match with [unk]: safer to treat as unrecognized.
        info['reason'] = 'unknown'
        info['heard'] = text
        return out
    if conf < MIN_CONF:
        # Low confidence: preserve heard text in info for diagnostics, report unclear to UI.
        info['reason'] = 'unclear'
        info['heard'] = text
        return out
    out['text'] = text
    return out


def transcribe(pcm):
    """Legacy wrapper returning (text, error)."""
    detail = transcribe_detail(pcm)
    return detail['text'], detail['error']


def warmup():
    """Preload Vosk model and compile grammar in the background on startup."""
    model, err = _load_model()
    if model is not None:
        _grammar_json(model)
    return (model is not None), err


async def pump_media(ws, upstream=MEDIA_UPSTREAM):
    """Bidirectional relay between Starlette WebSocket and local mediad."""
    _deps_on_path()
    import websockets

    async with websockets.connect(upstream, max_size=None, ping_interval=None) as up:

        async def down_to_up():
            """Browser -> mediad"""
            try:
                while True:
                    message = await ws.receive()
                    kind = message.get('type')
                    if kind == 'websocket.disconnect':
                        return
                    if kind == 'websocket.receive':
                        data = message.get('bytes')
                        await up.send(data if data is not None else (message.get('text') or ''))
            except Exception:
                return

        async def up_to_down():
            """mediad -> Browser"""
            try:
                async for data in up:
                    if isinstance(data, bytes):
                        await ws.send_bytes(data)
                    else:
                        await ws.send_text(data)
            except Exception:
                return

        tasks = [asyncio.create_task(down_to_up()), asyncio.create_task(up_to_down())]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
