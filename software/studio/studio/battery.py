"""Battery SoC estimation (battery-plan.md v2): warn-only, no automatic actions.

The duck has no fuel gauge or ADC; the only measurement is the supply voltage reported by each of the 15 servos
(0.1 V resolution, powered directly from battery). SoC is estimated via an OCV lookup table: accurate at rest,
and reading lower under load. Design rules:
    1 sample/sec   From fresh 3.0-9.5 V servos, drop highest 3 and lowest 3 and average (<9: median; <5: skip second)
    3 load classes rest (torque off) · light (holding/standing/sitting) · heavy (walking/action/fallen)
    Calm segments  Continuous rest/light >= 30s; record 30s median each second as a calm reading
    Estimate       Highest calm reading in the last 5 min + calibration offset -> divide by 2 cells -> lookup table;
                   if no calm segment in 5 min -> 30s median directly (marked rough load estimate, warn-only)
    Startup        On boot / Studio restart / >10s voltage gap, show 'Estimating' for first 30s with no alerts
    Display        Integer %, 2% deadband hysteresis
    Alerts         Must persist 60s to trigger and 60s above +3% to clear;
                   load floor: 10s median (calibrated) < 6.9 V for 30s -> critical alert, clears >= 7.0 V for 60s

Pure computation: no network, bus, or threads; time is passed in for deterministic testing. Callers log events via log_events().
"""
import json
import math
import os
import tempfile
from collections import deque

LOG_CAT = "Battery"

# 2S Li-ion 18650 (NP-F550, etc.) resting OCV -> SoC%. Conservative LCO-leaning compromise curve (+/-15% accuracy).
OCV_TABLE = ((4.20, 100), (4.15, 95), (4.10, 89), (4.05, 83), (4.00, 77), (3.95, 70), (3.90, 63), (3.85, 55),
             (3.80, 46), (3.75, 37), (3.70, 28), (3.65, 19), (3.60, 12), (3.55, 8), (3.50, 5), (3.40, 2),
             (3.30, 1), (3.00, 0))
CELLS = 2                       # HD-1910 max 8.4 V: 2S only
V_MIN, V_MAX = 3.0, 9.5         # Readings outside this range are treated as invalid
RD_EMPTY_V, RD_FULL_V = 6.6, 8.2    # robotd linear baseline (for comparison only)

ESTIMATING_S = 30       # Show 'Estimating' for first 30s after startup (B-R2)
CALM_S = 30             # Minimum calm segment duration (B-R4)
CALM_WINDOW_S = 300     # 5-minute window for calmest reading
LOAD_ROUGH_S = 30       # 30s median when no calm segment exists
GAP_S = 10.0            # >10s without any valid voltage resets estimator (B-R4)
RUN_GAP_S = 10.0        # Max gap inside a calm segment
KEEP_S = CALM_WINDOW_S + 60
WARN_HOLD_S = 60        # Warn / red hold & clear duration
HYST_PCT = 3
LOW_MED_S = 10          # Load floor uses 10s median
LOW_HOLD_S = 30         # Load floor trigger duration
LOW_CLEAR_HOLD_S = 60   # Load floor clear duration (+0.1 V)
LOW_CLEAR_V = 0.1
DEADBAND_PCT = 2
GLITCH_V = 1.0          # Single-second drop > 1.0 V below 60s median logs a glitch event
GLITCH_EVERY_S = 60     # Log at most once per minute
JUMP_PCT = 10           # >=10% jump after power gap logs battery swap/charge

# robot.state policy labels that represent non-action states
CALM_LABELS = {"held", "walk", "stand", "homing", "sit"}
CLS_CN = {"rest": "Idle (torque off)", "light": "Standing / sitting", "heavy": "Walking / action"}

MAINTENANCE = "Charge promptly when depleted; store at ~50% (7.4-7.6 V) when idle long-term"
WARN_ONLY = "Warn-only: the duck will not stop automatically when empty; power off and unplug battery after use"


# ---------------- Lookup Table ----------------

def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def soc_from_cell(vc):
    """Per-cell voltage -> SoC % (0-100, float; linear interpolation between table entries, clamped)."""
    if not _num(vc):
        return None
    if vc >= OCV_TABLE[0][0]:
        return 100.0
    for (hi_v, hi_p), (lo_v, lo_p) in zip(OCV_TABLE, OCV_TABLE[1:]):
        if lo_v <= vc <= hi_v:
            return lo_p + (vc - lo_v) / (hi_v - lo_v) * (hi_p - lo_p)
    return 0.0


def soc_from_pack(v, cells=CELLS):
    return soc_from_cell(v / cells) if _num(v) else None


def linear_pct(v):
    """robotd linear formula: 6.6 V = 0%, 8.2 V = 100% (under-load scale, reads high at low SoC)."""
    if not _num(v) or v <= 0:
        return None
    return max(0.0, min(1.0, (v - RD_EMPTY_V) / (RD_FULL_V - RD_EMPTY_V))) * 100


# ---------------- 1-Hz Sample Aggregation ----------------

def trimmed(volts, fresh=None):
    """15 servo voltages -> (reading_or_None, count_used, [(idx, volts)] valid).
    Valid = finite, 3.0-9.5 V, and fresh[i] is True if provided.
    >= 9: drop top 3 and bottom 3 and average; 5-8: median; < 5: None."""
    valid = []
    for i, v in enumerate(volts or ()):
        if fresh is not None and (i >= len(fresh) or fresh[i] is not True):
            continue
        if _num(v) and V_MIN <= v <= V_MAX:
            valid.append((i, float(v)))
    n = len(valid)
    if n < 5:
        return None, n, valid
    xs = sorted(v for _, v in valid)
    if n >= 9:
        mid = xs[3:-3]
        return sum(mid) / len(mid), n, valid
    return _median(xs), n, valid


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def classify(torque_on, moving=False, fallen=False, label=None):
    """rest / light / heavy; returns None if torque state is unknown (excluded from calm segments)."""
    if torque_on is False:
        return "rest"
    if torque_on is not True:
        return None
    if moving or fallen or (isinstance(label, str) and label not in CALM_LABELS):
        return "heavy"
    return "light"


def classify_robotd(bus, state):
    """robotd busStatus + robot.state -> load class. Torque uses native.torque_state_confirmed (readback);
    motion checks non-zero move.applied, action policy labels, and safety.fallen."""
    native = (bus or {}).get("native") if isinstance((bus or {}).get("native"), dict) else {}
    torque = native.get("torque_state_confirmed")
    st = state if isinstance(state, dict) else {}
    applied = (st.get("move") or {}).get("applied") if isinstance(st.get("move"), dict) else None
    moving = isinstance(applied, list) and any(_num(x) and abs(x) > 1e-3 for x in applied)
    fallen = (st.get("safety") or {}).get("fallen") is True if isinstance(st.get("safety"), dict) else False
    return classify(torque if isinstance(torque, bool) else None, moving, fallen, st.get("policy"))


def robotd_volts(bus):
    """busStatus -> (15 voltages, 15 fresh flags). Returns (None, None) if unavailable."""
    native = (bus or {}).get("native") if isinstance((bus or {}).get("native"), dict) else {}
    joints = native.get("joints")
    if not isinstance(joints, list) or not joints:
        return None, None
    js = [j if isinstance(j, dict) else {} for j in joints]
    return [j.get("volts") for j in js], [j.get("feedback_fresh") is True for j in js]


def classify_servo_session(rows):
    """During servo tuning, Studio owns the bus: any torque on -> light, all off -> rest, unknown -> None."""
    flags = [r.get("torque") for r in rows or () if isinstance(r, dict)]
    known = [bool(f) for f in flags if f is not None and f in (0, 1)]   # servo.py stores 0 / 1 (also accepts True / False)
    if not known:
        return None
    return "light" if any(known) else "rest"


def servo_session_volts(rows):
    """servo.snapshot() rows -> (volts, fresh)."""
    rs = [r if isinstance(r, dict) else {} for r in rows or ()]
    if not rs:
        return None, None
    return [r.get("volt") for r in rs], [r.get("fresh") is True for r in rs]


# ---------------- Settings ----------------

SETTINGS_FILE = "battery-settings.json"
SETTINGS_VERSION = 1
PRESETS = {"2s_liion": "2S Li-ion (NP-F550, etc.)", "bench": "Bench PSU (no battery estimate)"}
DEFAULTS = {"preset": "2s_liion", "warn_pct": 30, "red_pct": 15, "load_floor_v": 6.9, "cal_offset_v": 0.0}
RANGES = {"warn_pct": (15, 60), "red_pct": (5, 40), "load_floor_v": (6.6, 7.4), "cal_offset_v": (-0.5, 0.5)}
LABELS = {"preset": "Battery Preset", "warn_pct": "Warn Threshold", "red_pct": "Red Threshold", "load_floor_v": "Load Voltage Floor", "cal_offset_v": "Voltage Calibration"}
CAL_CONFIRM_V = 0.3     # Ask for confirmation if calibration offset exceeds +/-0.3 V (likely mismeasured, B-R11)


class SettingsError(ValueError):
    pass


def _check_item(k, v):
    """Validate a single setting: returns (normalized_value, None) or (None, reason)."""
    if k == "preset":
        return (v, None) if v in PRESETS else (None, f"must be one of {', '.join(PRESETS)}")
    lo, hi = RANGES[k]
    if not _num(v):
        return None, "must be a number"
    if k in ("warn_pct", "red_pct"):
        if float(v) != int(v):
            return None, "must be an integer"
        v = int(v)
    else:
        v = round(float(v), 2)
    if not lo <= v <= hi:
        return None, f"must be between {lo} and {hi}"
    return v, None


def validate_settings(values, base=None):
    """Merge values (partial allowed) onto base (default DEFAULTS): returns (new_settings, [errors...]).
    Unknown keys are rejected; red threshold must be at least 5% below warn threshold."""
    out = dict(DEFAULTS if base is None else base)
    errors = []
    if not isinstance(values, dict):
        return out, ["Settings must be an object"]
    for k, v in values.items():
        if k not in DEFAULTS:
            errors.append(f"Unknown setting '{k}'")
            continue
        good, why = _check_item(k, v)
        if why:
            errors.append(f"{LABELS[k]} {why} (got {v!r})")
        else:
            out[k] = good
    if not errors and out["red_pct"] > out["warn_pct"] - 5:
        errors.append(f"Red threshold ({out['red_pct']}%) must be at least 5% below warn threshold ({out['warn_pct']}%)")
    return out, errors


def settings_path(state_dir):
    return os.path.join(state_dir or ".", SETTINGS_FILE)


def load_settings(state_dir, log=None):
    """Load settings. Missing file = defaults. Corrupt file (invalid JSON / non-object): rename to .bad, use defaults, log warn;
    invalid individual items: use defaults for those items and log warn; unknown keys ignored for forward compatibility."""
    p = settings_path(state_dir)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Top-level JSON is not an object ({type(data).__name__})")
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError) as e:
        bad = p + ".bad"
        try:
            os.replace(p, bad)
        except OSError:
            bad = None
        if log is not None:
            log.warn("Could not read battery settings; using defaults", cat=LOG_CAT, step=p, raw=f"{type(e).__name__}: {e}",
                     hint=(f"Corrupt file moved to {bad}; save again in Settings" if bad else "Failed to rename corrupt file; check state directory permissions"))
        return dict(DEFAULTS)
    out, wrong = dict(DEFAULTS), []
    for k in DEFAULTS:
        if k in data:
            good, why = _check_item(k, data[k])
            if why:
                wrong.append(f"{LABELS[k]}={data[k]!r} ({why})")
            else:
                out[k] = good
    if out["red_pct"] > out["warn_pct"] - 5:
        wrong.append(f"Red threshold {out['red_pct']}% is not at least 5% below warn threshold {out['warn_pct']}%")
        out["warn_pct"], out["red_pct"] = DEFAULTS["warn_pct"], DEFAULTS["red_pct"]
    if wrong and log is not None:
        log.warn("Some battery settings were invalid and reset to defaults", cat=LOG_CAT, step=p, raw="; ".join(wrong),
                 hint="Likely edited manually; save again in Settings")
    return out


def save_settings(state_dir, settings):
    """Write settings atomically (temp file then rename). Validates first and raises SettingsError on invalid values or I/O failure."""
    good, errors = validate_settings(settings, DEFAULTS)
    if errors:
        raise SettingsError("; ".join(errors))
    if not state_dir:
        raise SettingsError("This duck has no state directory configured; cannot save")
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".battery-settings-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(good, v=SETTINGS_VERSION), f, ensure_ascii=False, indent=1)
        os.replace(tmp, settings_path(state_dir))
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SettingsError(f"Failed to save: {type(e).__name__}: {e}") from None
    return good


def settings_changes(old, new):
    """Format a concise diff for logs, e.g. 'Red Threshold 15% -> 20%; Voltage Calibration +0.00 -> +0.12 V'. Empty if unchanged."""
    unit = {"warn_pct": "%", "red_pct": "%", "load_floor_v": " V", "cal_offset_v": " V"}
    parts = []
    for k in DEFAULTS:
        a, b = old.get(k), new.get(k)
        if a != b:
            fa = PRESETS.get(a, a) if k == "preset" else (f"{a:+.2f}" if k == "cal_offset_v" else a)
            fb = PRESETS.get(b, b) if k == "preset" else (f"{b:+.2f}" if k == "cal_offset_v" else b)
            parts.append(f"{LABELS[k]} {fa}{unit.get(k, '')} -> {fb}{unit.get(k, '')}")
    return "; ".join(parts)


# ---------------- Estimator ----------------

class _Latch:
    """Switch that turns on only after on_s seconds and turns off only after off_s seconds."""

    def __init__(self, on_s, off_s):
        self.on_s, self.off_s = on_s, off_s
        self.on = False
        self._since = None

    def step(self, now, cond_on, cond_off):
        """Return 'on' / 'off' (changed on this tick) or None."""
        if not self.on:
            if cond_on:
                self._since = now if self._since is None else self._since
                if now - self._since >= self.on_s:
                    self.on, self._since = True, None
                    return "on"
            else:
                self._since = None
        else:
            if cond_off:
                self._since = now if self._since is None else self._since
                if now - self._since >= self.off_s:
                    self.on, self._since = False, None
                    return "off"
            else:
                self._since = None
        return None

    def reset(self):
        self.on, self._since = False, None


def _ev(level, msg, hint=None, raw=None, ctx=None, step=None):
    return {"level": level, "msg": msg, "hint": hint, "raw": raw, "ctx": ctx, "step": step}


class Estimator:
    """One instance per duck. Fed by a dedicated 1 Hz loop (never inside summarize), deduplicated by sample key."""

    def __init__(self, settings=None):
        self.settings = dict(DEFAULTS)
        if settings:
            self.set_settings(settings)
        self._ref_pct = None        # Last shown percentage before reset (in-memory only), for swap/charge detection
        self._reset(None)

    # ---- State ----

    def _reset(self, now):
        self.samples = deque()      # (t, raw_V, cls); calibration offset applied on read so setting changes take effect immediately
        self.calm = deque()         # (t, 30s_median, dominant_cls)
        self.started_at = None
        self.last_valid_t = None
        self.last_feed_t = None
        self.last_key = None
        self.run_start = None
        self.run_last_t = None
        self.shown = None
        self.mode = "no_data"
        self.est = None             # (pct_float, calibrated_V, kind: calm/load, cls, timestamp)
        self.spread = None
        self.started_logged = False
        self.warn = _Latch(WARN_HOLD_S, WARN_HOLD_S)
        self.red = _Latch(WARN_HOLD_S, WARN_HOLD_S)
        self.low = _Latch(LOW_HOLD_S, LOW_CLEAR_HOLD_S)
        self.low_min = None
        self.glitch_at = None
        self.n_used = 0
        self.source = None

    def set_settings(self, settings):
        good, errors = validate_settings(settings, DEFAULTS)
        if errors:
            raise SettingsError("; ".join(errors))
        self.settings = good

    @property
    def off(self):
        return self.settings["cal_offset_v"]

    # ---- Feed ----

    def feed(self, now, volts=None, fresh=None, cls=None, key=None, source="robotd"):
        """Call once per second. volts=None or duplicate key means no new sample (still advances gap timer).
        Returns log events to be recorded via log_events()."""
        events = []
        new = volts is not None and (key is None or key != self.last_key)
        if new and key is None and self.last_feed_t is not None and now - self.last_feed_t < 0.5:
            new = False             # Deduplicate by time when no key is provided
        if new:
            self.last_key, self.last_feed_t = key, now
            v, n, valid = trimmed(volts, fresh)
            self.n_used = n
            if v is not None:
                events += self._add(now, v, cls, valid, volts, source)
        if self.last_valid_t is not None and now - self.last_valid_t > GAP_S:
            if self.started_at is not None:
                events.append(_ev("warn", f"No servo voltage readings for over {GAP_S:.0f}s; resetting battery estimate",
                                  hint="Servos lost power (battery unplugged/swapped) or bus disconnected; will show 'Estimating' for 30s on recovery",
                                  ctx={"last_pct": self.shown}))
                if self.shown is not None:
                    self._ref_pct = self.shown
            self._reset(now)
        events += self._evaluate(now)
        return events

    def _add(self, now, v, cls, valid, volts, source):
        events = []
        if self.started_at is None:
            self.started_at = now
        # Glitch detection: log if current reading drops >1.0 V below 60s median (median itself filters it out)
        recent = [x for t, x, _ in self.samples if now - t <= 60]
        if len(recent) >= 10:
            med = _median(recent)
            if v < med - GLITCH_V and (self.glitch_at is None or now - self.glitch_at >= GLITCH_EVERY_S):
                self.glitch_at = now
                raw = [round(x, 2) if _num(x) else x for x in (volts or [])]
                events.append(_ev("warn", "Voltage reading dropped by more than 1 V",
                                  hint="A 1-2s dip does not affect the estimate; if frequent, check battery connector and servo bus",
                                  raw=f"now {v:.2f} V, 1m median {med:.2f} V; 15 servos: {raw}",
                                  ctx={"cls": cls, "source": source}))
        self.samples.append((now, v, cls))
        self.last_valid_t = now
        self.source = source
        lo = min(valid, key=lambda x: x[1])
        hi = max(valid, key=lambda x: x[1])
        self.spread = {"min": lo[1], "min_i": lo[0], "max": hi[1], "max_i": hi[0], "n": len(valid)}
        # Calm segments: continuous rest / light without large gaps
        if cls in ("rest", "light"):
            if self.run_start is None or self.run_last_t is None or now - self.run_last_t > RUN_GAP_S:
                self.run_start = now
            self.run_last_t = now
            if now - self.run_start >= CALM_S:
                win = [(x, c) for t, x, c in self.samples if t >= now - CALM_S and t >= self.run_start]
                if len(win) >= CALM_S // 2:
                    nrest = sum(1 for _, c in win if c == "rest")
                    self.calm.append((now, _median([x for x, _ in win]), "rest" if nrest * 2 >= len(win) else "light"))
        else:
            self.run_start = self.run_last_t = None
        while self.samples and now - self.samples[0][0] > KEEP_S:
            self.samples.popleft()
        while self.calm and now - self.calm[0][0] > CALM_WINDOW_S:
            self.calm.popleft()
        return events

    # ---- Computation ----

    def _evaluate(self, now):
        events = []
        s = self.settings
        if self.started_at is None:
            self.mode, self.shown, self.est = "no_data", None, None
            return events
        bench = s["preset"] == "bench"
        warming = now - self.started_at < ESTIMATING_S
        while self.calm and now - self.calm[0][0] > CALM_WINDOW_S:
            self.calm.popleft()
        prev_mode = self.mode
        if bench:
            self.mode, self.shown, self.est = "bench", None, None
        elif warming:
            self.mode, self.shown, self.est = "estimating", None, None
        else:
            if self.calm:
                t, v, c = max(self.calm, key=lambda x: x[1])
                vv = v + self.off
                self.mode, self.est = "calm", (soc_from_pack(vv), vv, "calm", c, t)
            else:
                win = [x for t, x, _ in self.samples if now - t <= LOAD_ROUGH_S]
                if win:
                    vv = _median(win) + self.off
                    self.mode, self.est = "load_rough", (soc_from_pack(vv), vv, "load", None, now)
            if self.est is not None:
                pct = self.est[0]
                if self.shown is None or prev_mode != self.mode or abs(pct - self.shown) >= DEADBAND_PCT:
                    self.shown = int(round(pct))
                events += self._log_estimate(now)

        # Warnings: suppressed for the first 30s after startup (B-R2); bench PSU skips percentage alerts.
        if warming:
            return events
        pct = self.shown
        w, r = s["warn_pct"], s["red_pct"]
        have = pct is not None and self.mode in ("calm", "load_rough")
        ch = self.warn.step(now, have and pct <= w, (not have) or pct >= w + HYST_PCT)
        if ch == "on":
            events.append(_ev("warn", f"Battery reached warn threshold {w}%: ~{pct}%",
                              hint="Prepare to charge; " + self._basis_short(), ctx=self._ctx()))
        elif ch == "off":
            events.append(_ev("info", f"Battery recovered to ~{pct}%, warning cleared" if pct is not None else "Battery warning cleared", ctx=self._ctx()))
        # Rough load estimates only trigger warn, never red (to avoid false alarms while walking).
        ch = self.red.step(now, have and self.mode == "calm" and pct <= r, (not have) or pct >= r + HYST_PCT)
        if ch == "on":
            events.append(_ev("error", f"Battery reached red threshold {r}%: ~{pct}%",
                              hint="Charge now; " + WARN_ONLY, ctx=self._ctx()))
        elif ch == "off":
            events.append(_ev("info", f"Battery recovered to ~{pct}%, red alert cleared" if pct is not None else "Battery red alert cleared", ctx=self._ctx()))
        # Load floor: 10s median (calibrated) below threshold for 30s continuously.
        m10 = self.volts_now(now)
        floor = s["load_floor_v"]
        if m10 is not None and m10 < floor:
            self.low_min = m10 if self.low_min is None else min(self.low_min, m10)
        ch = self.low.step(now, m10 is not None and m10 < floor, m10 is not None and m10 >= floor + LOW_CLEAR_V)
        if ch == "on":
            events.append(_ev("error", f"Voltage very low: below {floor:.1f} V for {LOW_HOLD_S}s (min {self.low_min:.2f} V)",
                              hint="Stop, sit down, and prepare to charge; " + WARN_ONLY, ctx=self._ctx()))
        elif ch == "off":
            events.append(_ev("info", f"Voltage recovered to {m10:.2f} V, low-voltage alert cleared", ctx=self._ctx()))
            self.low_min = None
        if not self.low.on and not (m10 is not None and m10 < floor):
            self.low_min = None
        return events

    def _log_estimate(self, now):
        """Log initial estimate once; after a >10s voltage gap, if first calm estimate jumps >=10%, log battery swap/charge.
        Do not trigger during continuous readings because switching from holding to limp can raise voltage by 20% on the same pack."""
        events = []
        pct, vv, kind, c, _ = self.est
        if not self.started_logged:
            self.started_logged = True
            events.append(_ev("info", f"Battery estimate ready: {self._basis_short()} -> ~{int(round(pct))}%",
                              hint=f"Lookup estimate (+/-15% accuracy); calibration {self.off:+.2f} V", ctx=self._ctx()))
        ref = self._ref_pct
        if ref is not None and kind == "calm":
            if pct - ref >= JUMP_PCT:
                events.append(_ev("info", f"Battery estimate jumped +{pct - ref:.0f}% after power gap: battery swapped or charged",
                                  hint=f"~{ref:.0f}% -> {pct:.0f}% ({self._basis_short()})", ctx=self._ctx()))
            self._ref_pct = None
        return events

    # ---- Outputs ----

    def volts_now(self, now):
        """10s median (with calibration) for subtext and load floor. Returns None with fewer than 5 samples."""
        win = [x for t, x, _ in self.samples if now - t <= LOW_MED_S]
        return _median(win) + self.off if len(win) >= 5 else None

    def level(self):
        if self.red.on or self.low.on:
            return "error"
        return "warn" if self.warn.on else None

    def current_cls(self):
        return self.samples[-1][2] if self.samples else None

    def _basis_short(self):
        if self.est is None:
            return "No readings yet"
        _, vv, kind, c, _ = self.est
        if kind == "calm":
            return f"{vv:.2f} V while {'idle' if c == 'rest' else 'standing / sitting'}"
        return f"{vv:.2f} V while walking (under load, reads low)"

    def _ctx(self):
        return {"mode": self.mode, "pct": self.shown, "cls": self.current_cls(), "source": self.source,
                "basis_v": round(self.est[1], 3) if self.est else None, "cal": self.off,
                "calm_points": len(self.calm), "samples": len(self.samples)}

    def view(self, now, rd=None, names=None, shutdown=None):
        """UI payload for battery widget, detail popover, and todo list."""
        s = self.settings
        v10 = self.volts_now(now)
        cls = self.current_cls()
        rd = rd if isinstance(rd, dict) else {}
        rd_v = rd.get("volts") if _num(rd.get("volts")) else None
        rd_pct = rd.get("percent") if _num(rd.get("percent")) else (linear_pct(v10 - self.off) if v10 is not None else None)
        basis = None
        if self.est is not None:
            pct, vv, kind, c, t = self.est
            basis = {"kind": kind, "cls": c, "volts": round(vv, 2), "cell": round(vv / CELLS, 3),
                     "age_s": round(max(0.0, now - t)), "pct": round(pct, 1)}
        why = {
            "no_data": "No voltage reading",
            "estimating": "Estimating (just started)",
            "bench": "Bench PSU (battery % disabled)",
            "load_rough": "Walking: rough load-voltage estimate (reads low)",
        }.get(self.mode)
        if self.mode == "calm":
            why = ("Idle" if cls == "rest" else "Standing / sitting" if cls == "light"
                   else "Walking (using last idle/stand voltage)" if cls == "heavy" else "Using last calm voltage")
        detail = self._detail(now, v10, cls, rd_v, rd_pct, names, shutdown)
        return {
            "pct": self.shown,
            "mode": self.mode,
            "level": self.level(),
            "warn": self.warn.on,
            "red": self.red.on,
            "low_volt": self.low.on,
            "volts": round(v10, 2) if v10 is not None else None,
            "cls": cls,
            "why": why,
            "basis": basis,
            "rd_volts": round(rd_v, 2) if rd_v is not None else None,
            "rd_pct": round(rd_pct) if rd_pct is not None else None,
            "spread": self._spread(names),
            "warn_pct": s["warn_pct"], "red_pct": s["red_pct"], "load_floor_v": s["load_floor_v"],
            "cal_offset_v": self.off, "preset": s["preset"], "source": self.source, "shutdown": shutdown,
            "estimating_left_s": (max(0, int(math.ceil(ESTIMATING_S - (now - self.started_at))))
                                  if self.mode == "estimating" else None),
            "detail": detail,
        }

    def _spread(self, names):
        sp = self.spread
        if not sp:
            return None
        nm = lambda i: names[i] if names and i < len(names) else f"cell {i + 1}"
        return {"min": round(sp["min"], 2), "min_name": nm(sp["min_i"]), "max": round(sp["max"], 2),
                "max_name": nm(sp["max_i"]), "n": sp["n"]}

    def _detail(self, now, v10, cls, rd_v, rd_pct, names, shutdown):
        """Detail popover lines explaining how the estimate was computed."""
        s = self.settings
        lines = []
        cal = f"cal {self.off:+.2f} V"
        if self.mode == "no_data":
            lines.append("No servo voltage readings: servos unpowered (battery disconnected?) or robotd not running")
        elif self.mode == "estimating":
            left = max(0, int(math.ceil(ESTIMATING_S - (now - self.started_at))))
            lines.append(f"Sampling voltage; estimate ready in {left}s (first {ESTIMATING_S}s excluded to avoid step-load dips)")
        elif self.mode == "bench":
            lines.append("Bench PSU mode selected in Settings: showing voltage only; percentage alerts disabled")
        elif self.est is not None:
            pct, vv, kind, c, t = self.est
            if kind == "calm":
                ago = int(now - t)
                when = "now" if ago < 5 else f"{ago // 60}m ago" if ago >= 60 else f"{ago}s ago"
                lines.append(f"Calmest 5m window ({CLS_CN.get(c, c)}, {when}): {vv:.2f} V "
                             f"(trimmed mean of 15 servos, {cal}) -> {vv / CELLS:.2f} V/cell -> ~{pct:.0f}%")
                if c == "light":
                    lines.append("Servos draw current while standing/sitting, so voltage reads slightly lower; idle limp for 30s+ is most accurate")
            else:
                lines.append(f"Continuous motion for 5m: rough estimate from walking voltage {vv:.2f} V ({cal}) -> ~{pct:.0f}%. "
                             "Walking sags voltage; pause for 30s to re-estimate under calm load")
            lines.append("2S Li-ion lookup table (+/-15% accuracy; conservative without calibration)")
        if v10 is not None and self.mode != "no_data":
            lines.append(f"Now: {v10:.2f} V ({CLS_CN.get(cls, 'unknown state')})")
        if self.mode in ("calm", "load_rough"):
            lines.append(f"Warn {s['warn_pct']}% · Red {s['red_pct']}% · Low-voltage alert below {s['load_floor_v']:.1f} V for {LOW_HOLD_S}s "
                         "(warn-only; 30% on the lookup table is higher voltage than robotd's linear 30%)")
        elif self.mode == "bench":
            lines.append(f"Low-voltage alert below {s['load_floor_v']:.1f} V for {LOW_HOLD_S}s (check PSU current limit or thin wiring)")
        if rd_pct is not None and self.mode != "bench":
            src = f"{rd_v:.2f} V, " if rd_v is not None else ""
            lines.append(f"robotd linear estimate: {rd_pct:.0f}% ({src}6.6-8.2 V linear, reads high at low SoC)")
        sp = self._spread(names)
        if sp and self.mode != "no_data":
            lines.append(f"{sp['n']} servos: min {sp['min']:.1f} V ({sp['min_name']}) · max {sp['max']:.1f} V ({sp['max_name']})")
        if self.mode == "bench":
            return lines                # Bench PSU: no battery, skip charging/storage notes
        lines.append(stop_words({"source": self.source, "shutdown": shutdown}))
        lines.append(MAINTENANCE)
        return lines

    # ---- Calibration ----

    def calibrate(self, now, measured_v):
        """Record multimeter-measured battery voltage; only accepted after 30s idle rest (B-R11).
        Returns {ok, offset, confirm, reason}. Requires re-confirmation if offset exceeds +/-0.3 V."""
        if not _num(measured_v) or not 6.0 <= measured_v <= 9.0:
            return {"ok": False, "offset": None, "confirm": False, "reason": "Measured voltage must be 6.0-9.0 V; check multimeter range and polarity"}
        win = [(x, c) for t, x, c in self.samples if now - t <= CALM_S]
        rest_long = (self.run_start is not None and now - self.run_start >= CALM_S
                     and len(win) >= CALM_S // 2 and all(c == "rest" for _, c in win))
        if not rest_long:
            return {"ok": False, "offset": None, "confirm": False,
                    "reason": f"Release torque and rest idle for at least {CALM_S}s before calibrating"}
        raw = _median([x for x, _ in win])
        off = round(measured_v - raw, 2)
        lo, hi = RANGES["cal_offset_v"]
        if not lo <= off <= hi:
            return {"ok": False, "offset": off, "confirm": False,
                    "reason": f"Measured {measured_v:.2f} V vs servo reading {raw:.2f} V differs by {off:+.2f} V (exceeds +/-{hi} V); measure at the HAT battery input"}
        confirm = abs(off) > CAL_CONFIRM_V
        why = (f"Servo reading {raw:.2f} V, measured {measured_v:.2f} V (offset {off:+.2f} V)"
               + ("; large offset — confirm you measured at the HAT battery input?" if confirm else ""))
        return {"ok": True, "offset": off, "confirm": confirm, "reason": why, "servo_v": round(raw, 3)}


# ---------------- Todo & Logging ----------------

def stop_words(view):
    """Whether auto-shutdown will trigger on empty battery: warn-only. During servo tuning, robotd is paused so auto-shutdown is inactive."""
    if view.get("source") == "servo":
        return "robotd is paused during servo tuning, so it will not sit down automatically; power off and unplug battery when done"
    if view.get("shutdown") is True:
        return "robotd auto-shutdown is enabled: it will sit down and shut off when 10s average drops to 6.6 V"
    return WARN_ONLY


def todo_items(view):
    """Convert estimate view into actionable todo items (most urgent first). Warn-only."""
    if not isinstance(view, dict):
        return []
    items = []
    pct, floor = view.get("pct"), view.get("load_floor_v")
    if view.get("low_volt"):
        v = view.get("volts")
        vtxt = f": {v:.2f} V" if _num(v) else ""
        if view.get("preset") == "bench":
            items.append({"key": "battery_low_volt", "level": "error", "msg": f"PSU voltage low{vtxt}",
                          "hint": f"Below {floor:.1f} V for {LOW_HOLD_S}s; check PSU current limit or cable gauge/length"})
        else:
            items.append({"key": "battery_low_volt", "level": "error", "msg": f"Voltage very low{vtxt}",
                          "hint": f"Below {floor:.1f} V for {LOW_HOLD_S}s; stop, sit down, and prepare to charge. {stop_words(view)}"})
    rough = view.get("mode") == "load_rough"
    tail = " (rough load-voltage estimate, reads low)" if rough else ""
    if view.get("red") and _num(pct):
        items.append({"key": "battery", "level": "error", "msg": f"Battery low: ~{pct}%",
                      "hint": f"Charge soon. {stop_words(view)}"})
    elif view.get("warn") and _num(pct):
        items.append({"key": "battery", "level": "warn", "msg": f"Battery ~{pct}%{tail}",
                      "hint": "Prepare to charge; lookup estimate (+/-15% accuracy; conservative without calibration)"})
    return items


def log_events(log, events):
    """Write events returned by feed() to Studio log (category Battery). No-op if log is None."""
    if log is None:
        return
    for e in events or ():
        log.write(e["msg"], cat=LOG_CAT, level=e["level"], step=e.get("step"), raw=e.get("raw"),
                  hint=e.get("hint"), ctx=e.get("ctx"))
