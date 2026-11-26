"""Studio structured logging: categorized, leveled, rotated on disk, and streamed over WebSocket."""
import asyncio
import json
import os
import queue
import re
import threading
import time
import traceback
from collections import deque
from pathlib import Path

CATS = ["System", "Connection", "Network", "robotd", "Command", "Safety", "Bus", "Calibration", "IMU", "Gamepad", "UI", "Battery", "Tuning", "Sensors", "Models"]
LEVELS = ["debug", "info", "warn", "error"]
_TAG = {"debug": "[DEBUG]", "info": "", "warn": "[WARN]", "error": "[ERROR]"}

KEEP_DAYS = 14
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024    # Cap daily info logs after 20 MB; still log warn/error
MAX_SECRETS = 64
RING = 500
DEDUP_WINDOW_S = 60
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SECRET_KEYS = frozenset({"psk", "password", "passwd", "secret", "token", "authorization"})


def redact(value):
    """Replace sensitive dictionary values with '[REDACTED]' recursively."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and k.lower() in SECRET_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _stamp(ts):
    """2026-10-18T09:41:07.123+08:00"""
    lt = time.localtime(ts)
    off = time.strftime("%z", lt)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{int(ts * 1000) % 1000:03d}" + (off[:3] + ":" + off[3:] if off else "")


class Log:
    def __init__(self, directory, prefix="studio", echo=True, background=True):
        self.dir = Path(directory)
        self.prefix = prefix
        self.echo = echo
        self.debug_to_file = False
        self._lock = threading.Lock()
        self._ring = deque(maxlen=RING)
        self._n = 0
        self._day = None
        self._day_bytes = 0
        self._capped = False
        self._listeners = set()      # (loop, queue): page WebSocket subscriptions
        self._secrets = set()
        self._seen = {}              # (cat, level, msg) -> [first timestamp, repeat count]
        self.counts = {lv: 0 for lv in LEVELS}
        self.write_errors = 0        # Number of log write errors (visible in diagnostic bundle)
        self._q = queue.SimpleQueue() if background else None
        if background:
            threading.Thread(target=self._writer, name="studio-log", daemon=True).start()

    # ---- Write ----

    def hide(self, value):
        """Register a secret string to be replaced with '[REDACTED]' in future logs."""
        if value and len(value) >= 4:
            with self._lock:
                if len(self._secrets) < MAX_SECRETS:
                    self._secrets.add(str(value))

    def write(self, msg, cat="System", level="info", step=None, raw=None, hint=None, ctx=None,
              exc=False, dedup=True):
        """Log an entry without ever raising (safe on E-Stop path)."""
        try:
            return self._write(msg, cat, level, step, raw, hint, ctx, exc, dedup)
        except Exception:  # noqa: BLE001
            self.write_errors += 1
            return None

    def _write(self, msg, cat, level, step, raw, hint, ctx, exc, dedup):
        if level not in LEVELS:
            level = "info"
        now = time.time()
        msg, raw, hint, step = (self._clean(x) for x in (msg, raw, hint, step))
        tb = self._clean(traceback.format_exc()) if exc else ""
        ctx_text = self._clean(json.dumps(redact(ctx), ensure_ascii=False, default=str)) if ctx else None
        with self._lock:
            if dedup:
                key = (cat, level, msg)
                seen = self._seen.get(key)
                if seen and now - seen[0] < DEDUP_WINDOW_S:
                    seen[1] += 1
                    return None
                if seen and seen[1]:
                    self._emit_locked(now, cat, level, f"(Previous '{msg}' repeated {seen[1]} times within 1m)")
                self._seen[key] = [now, 0]
                if len(self._seen) > 2000:
                    self._seen = {k: v for k, v in self._seen.items() if now - v[0] < DEDUP_WINDOW_S}
            return self._emit_locked(now, cat, level, msg, step, raw, hint, ctx_text, tb)

    def _emit_locked(self, now, cat, level, msg, step=None, raw=None, hint=None, ctx=None, tb=""):
        parts = [f"{_stamp(now)} [{cat}]{_TAG[level]} {msg}"]
        if step:
            parts.append(f"at: {step}" if level in ("warn", "error") else f"step: {step}")
        if raw:
            parts.append(f"raw: {raw}")
        if hint:
            parts.append(f"hint: {hint}")
        if ctx:
            parts.append(f"ctx: {ctx}")
        line = " · ".join(parts)
        self.counts[level] += 1
        if level != "debug" or self.debug_to_file:
            self._to_file(now, line + "\n" + tb, level in ("warn", "error"))
        entry = None
        if level != "debug":
            self._n += 1
            entry = {"n": self._n, "ts": now, "t": time.strftime("%H:%M:%S", time.localtime(now)),
                     "cat": cat, "level": level, "msg": msg, "step": step, "raw": raw, "hint": hint, "ctx": ctx}
            self._ring.append(entry)
            for loop, q in list(self._listeners):
                try:
                    loop.call_soon_threadsafe(_offer, q, entry)
                except RuntimeError:
                    pass
        if self.echo:
            try:
                print(line + ("\n" + tb.rstrip() if tb else ""), flush=True)
            except Exception:  # noqa: BLE001
                self.write_errors += 1
        return entry

    def debug(self, msg, **kw):
        return self.write(msg, level="debug", **kw)

    def info(self, msg, **kw):
        return self.write(msg, level="info", **kw)

    def warn(self, msg, **kw):
        return self.write(msg, level="warn", **kw)

    def error(self, msg, **kw):
        return self.write(msg, level="error", **kw)

    # ---- Read ----

    def recent(self, limit=200, cats=None, min_level="info"):
        floor = LEVELS.index(min_level) if min_level in LEVELS else 1
        with self._lock:
            items = [e for e in self._ring
                     if LEVELS.index(e["level"]) >= floor and (not cats or e["cat"] in cats)]
        return items[-limit:]

    def subscribe(self, loop=None, maxsize=200):
        """Return an asyncio.Queue for live UI log streaming (drops oldest when full)."""
        loop = loop or asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._listeners.add((loop, q))
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._listeners = {(lp, qq) for lp, qq in self._listeners if qq is not q}

    def files(self, days=2):
        """Return log files for the most recent `days` days (oldest to newest)."""
        self.flush()
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob(f"{self.prefix}-*.log"))[-days:]

    def snapshot(self):
        with self._lock:
            return {"counts": dict(self.counts), "last_n": self._n, "dir": str(self.dir),
                    "write_errors": self.write_errors}

    def flush(self, timeout=2.0):
        """Wait for the background writer thread to drain queued log entries."""
        if self._q is None:
            return
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    # ---- Files ----

    def _clean(self, text):
        if text is None:
            return None
        text = _ANSI.sub("", str(text))
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        return text

    def _to_file(self, now, text, important=False):
        if self._q is not None:
            self._q.put((now, text, important))
        else:
            self._append(now, text, important)

    def _writer(self):
        while True:
            item = self._q.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            self._append(*item)

    def _append(self, now, text, important=False):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{self.prefix}-{day}.log"
            if day != self._day:
                self._day = day
                self._prune()
                self._day_bytes = _size(path)
                self._capped = False
            if self._day_bytes > MAX_FILE_BYTES:
                if not important:
                    if not self._capped:
                        self._capped = True
                        text = f"(Daily log exceeded {MAX_FILE_BYTES >> 20} MB; logging warn/error only)\n"
                    else:
                        return
                elif self._day_bytes > MAX_FILE_BYTES * 2:
                    return
            data = text.encode("utf-8")
            with open(path, "ab") as f:
                f.write(data)
            self._day_bytes += len(data)
        except OSError:
            pass

    def _prune(self):
        """Keep at most KEEP_DAYS files and prune oldest files above MAX_TOTAL_BYTES (keeping today)."""
        logs = sorted(self.dir.glob(f"{self.prefix}-*.log"))
        today = self.dir / f"{self.prefix}-{self._day}.log"
        old = [p for p in logs if p != today]
        excess = len(old) - (KEEP_DAYS - 1)
        for p in old[:max(excess, 0)]:
            _unlink(p)
        logs = sorted(self.dir.glob(f"{self.prefix}-*.log"))
        total = sum(_size(p) for p in logs)
        for p in logs:
            if total <= MAX_TOTAL_BYTES:
                break
            if p == today:
                continue
            total -= _size(p)
            _unlink(p)


def _offer(q, entry):
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(entry)


def _size(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _unlink(p):
    try:
        p.unlink()
    except OSError:
        pass


class Timer:
    """Log command dispatch, completion, and duration without deduplication."""

    def __init__(self, log, what, step, who=None, args=None, ctx=None):
        self.log, self.what, self.step, self.ctx = log, what, step, ctx
        self.t0 = time.monotonic()
        self.done = False
        detail = f" ({who})" if who else ""
        self.log.write(f"→ {what}{detail}", cat="Command", step=step,
                       raw=json.dumps(redact(args), ensure_ascii=False) if args else None, dedup=False)

    def ms(self):
        return int((time.monotonic() - self.t0) * 1000)

    def ok(self, reply=None, note=""):
        self.done = True
        self.log.write(f"← {self.what} ok · {self.ms()} ms{(' · ' + note) if note else ''}", cat="Command",
                       step=self.step, raw=_brief(reply), dedup=False)

    def fail(self, why, raw=None, hint=None, ctx=None):
        self.done = True
        self.log.write(f"← {self.what} failed: {why} · {self.ms()} ms", cat="Command", level="error",
                       step=self.step, raw=_brief(raw), hint=hint, ctx=ctx or self.ctx, dedup=False)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is not None and not self.done:
            self.fail(f"{et.__name__}: {ev}")
        elif not self.done:
            self.ok()
        return False


def _brief(x, limit=600):
    if x is None:
        return None
    s = x if isinstance(x, str) else json.dumps(redact(x), ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"... ({len(s)} chars)"


def log_cmd(log, what, step, **kw):
    return Timer(log, what, step, **kw)


def to_json(entry):
    return json.dumps(entry, ensure_ascii=False)


_default = None


def setup(directory, **kw):
    """Initialize process-wide logger instance."""
    global _default
    _default = Log(directory, **kw)
    return _default


def get():
    if _default is None:
        return setup(os.path.join(os.getcwd(), "logs"))
    return _default
