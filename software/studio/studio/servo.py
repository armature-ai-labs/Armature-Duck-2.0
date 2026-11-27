"""Servo page bus session: Studio opens the servo bus directly (like servo-web), reads 15 servos, and releases torque on the bus.

This step is read/release-safe; holding and zeroing are handled in servo_ops.
Why open the bus directly: neither robotd version supports holding a single servo indefinitely.
When robotd is stopped, Studio talks directly to the servos. **Single bus entry point: ServoSession.open().**

Threading: Feetech bus I/O is blocking (pyserial); polling runs at 50 Hz in a background thread and updates snapshots,
while the web UI reads the latest at 10 Hz. Torque-off does not queue; it writes directly on the calling thread (per-packet lock).

Torque-off = latched E-Stop: sync_write 40=0 -> sync_read 40 verify -> retry any unconfirmed individually up to 3 rounds.
Once latched, any torque-on / target / zero write checks `latched` first and requires explicit unlatch.

Handback / leave: servo ops run on worker threads and can race with handback/leave.
- Every op is tracked via op_begin / op_end.
- Handback / leave sets `closing`, rejects new ops, waits for in-flight ops (with timeout),
  reads live torque (not 1s cache), releases torque, and closes the bus.
- Leave refuses to hand back if calibration / motion / lively is running, or if any servo torque is non-zero or unreadable.
"""
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

from .bus import feetech
from .communication import ServoHealth, accepted_snapshot_age_ms, counter, number
from .startup_status import servo_observations

IDS = [20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14]     # Same order as robotd JOINT_NAMES
NAMES = {
    20: ("Left Hip Yaw", "Left Hip Yaw"), 21: ("Left Hip Roll", "Left Hip Roll"), 22: ("Left Hip Pitch", "Left Hip Pitch"),
    23: ("Left Knee", "Left Knee"), 24: ("Left Ankle", "Left Ankle"),
    30: ("Neck Pitch", "Neck Pitch"), 31: ("Head Pitch", "Head Pitch"), 32: ("Head Yaw", "Head Yaw"),
    33: ("Head Roll", "Head Roll"), 34: ("Mouth", "Mouth"),
    10: ("Right Hip Yaw", "Right Hip Yaw"), 11: ("Right Hip Roll", "Right Hip Roll"), 12: ("Right Hip Pitch", "Right Hip Pitch"),
    13: ("Right Knee", "Right Knee"), 14: ("Right Ankle", "Right Ankle"),
}
GROUP = {**{i: "R" for i in (10, 11, 12, 13, 14)}, **{i: "L" for i in (20, 21, 22, 23, 24)},
         **{i: "H" for i in (30, 31, 32, 33, 34)}}
# HD-1910 positive count direction is opposite to the XL330 model; verified on hardware for all 15 servos: -1
DEFAULT_DIRECTIONS = {i: -1 for i in IDS}
POLL_HZ = 50
TORQUE_EVERY_S = 1.0          # Torque switch polled separately once per second
STATIC_REGS = {13: "temp_limit", 21: "p", 22: "d", 33: "mode", 8: "resp_level", 19: "unload",
               50: "p_run", 51: "d_run"}    # 21/22 are EEPROM; HD-1910 active P/D are in RAM 50/51 (written by servo_ops before torque-on)
BATTERY_FULL_V, BATTERY_EMPTY_V = 8.2, 6.6      # Same formula as duck-control/src/model.rs
LEAVE_WAIT_S = 3.0            # Max wait for in-flight ops when leaving Servo page (do not hand back on timeout)
HANDBACK_WAIT_S = 10.0        # Max wait for in-flight ops on manual Hand Back (calibration stops before next servo)
# Ops considered active work: leaving the Servo page will not auto-handback while these are running
BUSY_OPS = {"calibrate": "Calibrating", "undo": "Undoing calibration", "timing": "Measuring bus timing", "scan_all": "Scanning all IDs",
            "find": "Finding servo",
            "scan": "Scanning", "set_id": "Changing ID", "reboot": "Rebooting servo", "write_reg": "Writing register"}


class Refused(Exception):
    """Raised when an operation cannot run right now (latched, bus closed, closing, invalid args). Message is user-facing."""


def angle_deg(raw, direction):
    return None if raw is None else direction * (raw - 2048) * 360.0 / 4096


def battery_percent(volts):
    if not volts or volts <= 0:
        return None
    return max(0.0, min(1.0, (volts - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V))) * 100


def robotd_active():
    """Is robotd running? Returns False on non-systemd hosts, None if systemctl errors/times out."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        r = subprocess.run(["systemctl", "is-active", "robotd"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() in ("active", "activating", "reloading")
    except (OSError, subprocess.TimeoutExpired):
        return None


def boot_id():
    """Boot ID from /proc/sys/kernel/random/boot_id, or None if unavailable."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


BASELINE_PREFIX = "servo-baseline-"
_baseline_mem = {}       # In-memory baseline when boot_id is unavailable or in sim: (state_dir, is_sim) -> {id: {"41","46"}}


class ServoSession:
    def __init__(self, log, port=None, sim=False, directions=None, baud=1_000_000, state_dir="."):
        self.log, self.port, self.sim, self.baud, self.state_dir = log, port, sim, baud, state_dir
        self.ops = None                  # servo_ops.ServoOps when bus is open
        self.directions = {**DEFAULT_DIRECTIONS, **(directions or {})}    # Keys are ints; dict(..., **d) fails on non-str keys
        self.bus = None
        self.state = "closed"            # closed / opening / open / error
        self.error = None
        self.latched = False             # Torque-off latch
        self.relaxing = False
        self.last_relax = None           # {"ok", "ms", "bad", "at"}
        self.static = {}                 # id -> {temp_limit, p, d, mode, ...}
        self.rows = {}                   # id -> latest servo row
        self.torque = {}                 # id -> 0/1/None
        self.torque_at = None
        self.hz = 0.0
        self.opened_at = None
        self.by = None
        self._stop = threading.Event()
        self._thread = None
        self._health = ServoHealth()
        self._sample_sequence = 0
        self._last_poll_at = None
        self._window = []                # Recent (t, asked, missed) tuples for drop-rate calc
        self.sim_serial = None
        self.handed_back_at = None       # Last handback monotonic timestamp
        self.closing = False             # Handing back / leaving: reject new ops and disallow unlatch
        self._cv = threading.Condition()
        self._inflight = {}              # In-flight servo ops: token -> op name
        self._tok = 0
        self.restored = False            # Whether this session was resumed after Studio restart
        self.orphan = None               # Unrecovered prior session info: {"why", "at"}
        self.helper_available = None     # Whether root helper for starting/stopping robotd is available
        self.conflict_at = None          # Monotonic timestamp when robotd was detected running mid-session
        self.probe_robotd = None         # Whether to poll robotd status mid-session (None = only on real serial)
        self.boot_id = None              # Current boot_id for 41/46 baseline persistence
        self.baseline = {}               # id -> {"41": raw_int, "46": raw_int} restored before handback
        self.handback_note = None        # Warning note if handback succeeded with unconfirmed torque-off servos
        self.giveback_error = None       # Error message if robotd failed to restart after handback
        self.giveback_hint = None        # Recovery hint if robotd failed to restart
        self.marker_error = None         # Error writing session marker

    # ---- Open / Close ----

    def open(self, by=None, port=None, restored=False):
        """Open the servo bus (blocking; run in worker thread). Returns (ok, reason).
        port: selected serial port or "sim" for simulated servos.
        Cannot switch ports while already open; must hand back first.
        restored: whether resuming a previous session after Studio restart."""
        if port:
            want_sim = port == "sim"
            if self.state == "open" and (want_sim != self.sim or (not want_sim and port != self.port)):
                # Switching ports while open would close the live bus while servos may hold torque
                return False, "Cannot switch serial port while active: click Hand Back to Main first"
            self.sim = want_sim
            if not want_sim:
                self.port = port
        if self.state in ("open", "opening"):
            return True, None
        if not self.sim and not self.port:
            self.state, self.error = "closed", None
            return False, "Select a serial port or Sim (No Hardware) first"
        self.state, self.error, self.by = "opening", None, by
        self._health = ServoHealth()
        self._sample_sequence, self._last_poll_at = 0, None
        try:
            if self.sim:
                from .bus.sim_bus import SimSerial
                self.sim_serial = SimSerial(IDS, phys=_sim_home())
                self.bus = feetech.FeetechBus(None, ser=self.sim_serial)
                where = "Sim (15 virtual servos)"
            else:
                active = robotd_active()
                if active is None:
                    raise RuntimeError("Cannot check robotd status (systemctl failed); refusing to open bus to avoid conflict")
                if active:
                    raise RuntimeError("Main firmware robotd is still using the servo bus; stop it first")
                self.bus = feetech.FeetechBus(self.port, self.baud)
                where = f"{self.port} @ {self.baud}"
            self.log.info(f"Opened servo bus: {where}", cat="Bus", raw=f"by={by}")
            found = [i for i in IDS if self.bus.ping(i) is not None]
            missing = [i for i in IDS if i not in found]
            self._read_static(found)
            self._record_baseline(found)
            self._read_torque()
            self.restored = bool(restored)
            self.state, self.opened_at, self.latched = "open", time.monotonic(), False
            from .servo_ops import ServoOps
            self.ops = ServoOps(self, self.state_dir)
            self.ops.read_phase()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="servo-poll", daemon=True)
            self._thread.start()
            msg = f"Servo bus opened: {len(found)}/15 responded"
            if missing:
                self.log.warn(msg, cat="Bus", raw=f"no reply: {missing}", hint="Check wiring and power for missing servos")
            else:
                self.log.info(msg, cat="Bus")
            return True, None
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self.log.error("Failed to open servo bus", cat="Bus", raw=self.error,
                           hint="Stop robotd on the duck, or check USB-TTL adapter and port name")
            self._close_bus()
            return False, self.error

    def holding(self):
        """Count of servos currently holding torque (1s cached readback for UI display). Returns 0 if bus closed."""
        if self.state != "open":
            return 0
        return sum(1 for i in IDS if self.torque.get(i) == 1)

    def unknown(self):
        """Count of servos whose torque could not be read (no reply). Returns 0 if bus closed."""
        if self.state != "open":
            return 0
        return sum(1 for i in IDS if self.torque.get(i) not in (0, 1))

    @property
    def halted(self):
        """True if torque-off is latched or session is closing; background motion/calibration must stop."""
        return self.latched or self.closing

    # ---- In-flight servo ops tracking ----

    def op_begin(self, name):
        """Register an in-flight servo op. Raises Refused if closing or bus not open. Returns token for op_end."""
        with self._cv:
            if self.closing:
                raise Refused("Handing back to main: rejecting new servo ops")
            if self.state != "open":
                raise Refused("Servo bus is not open: click Take Over Bus first")
            self._tok += 1
            tok = self._tok
            self._inflight[tok] = str(name or "?")
            return tok

    def op_end(self, tok):
        """Mark an in-flight op as finished (idempotent)."""
        with self._cv:
            if self._inflight.pop(tok, None) is not None:
                self._cv.notify_all()

    @contextlib.contextmanager
    def op_slot(self, name):
        tok = self.op_begin(name)
        try:
            yield
        finally:
            self.op_end(tok)

    def wait_ops(self, timeout):
        """Wait for all in-flight servo ops to finish; returns False on timeout."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._inflight, timeout)

    def inflight_names(self):
        with self._cv:
            return sorted(set(self._inflight.values()))

    def _busy_locked(self):
        """Return reason string if calibration / motion / lively is running, else None (call under _cv)."""
        for name in self._inflight.values():
            if name in BUSY_OPS:
                return BUSY_OPS[name]
        ops = self.ops
        if ops is not None:
            if ops.calib_lock.locked():
                return "Calibrating"
            if ops._motion is not None and ops._motion.is_alive() and not ops._motion_stop.is_set():
                return "Motion running"
            if ops.idle_running:
                return "Lively running"
        return None

    def read_torque_now(self):
        """Live read of torque enable for all 15 servos (bypasses cache): {id: 0 / 1 / None}."""
        try:
            got = self._read_torque()
        except Exception as e:  # noqa: BLE001
            self.log.warn("Live torque read failed", cat="Bus", raw=f"{type(e).__name__}: {e}")
            return {i: None for i in IDS}
        return {i: (v[1][0] if v else None) for i, v in ((i, got.get(i)) for i in IDS)}

    def leave(self, why, wait_s=LEAVE_WAIT_S, before_close=None):
        """Auto-handback when leaving the Servo page (blocking; run in worker thread).
        Returns ("closed", None), ("kept", {...}), or ("handed_back", None)."""
        with self._cv:
            if self.state != "open":
                return "closed", None
            busy = self._busy_locked()
            if busy:
                return "kept", {"why": busy, "holding": self.holding(), "unknown": self.unknown()}
            self.closing = True
        try:
            if not self.wait_ops(wait_s):
                return "kept", {"why": f"Servo ops still running ({', '.join(self.inflight_names())})", "holding": self.holding(),
                                "unknown": self.unknown()}
            tq = self.read_torque_now()
            on = [i for i in IDS if tq.get(i) == 1]
            unk = [i for i in IDS if tq.get(i) not in (0, 1)]
            if on or unk:
                parts = ([f"{len(on)} holding torque"] if on else []) + ([f"{len(unk)} torque unreadable"] if unk else [])
                return "kept", {"why": ", ".join(parts), "holding": len(on), "unknown": len(unk)}
            ok, reason = self.relax_all(why)
            if not ok:
                self.log.warn("Pre-handback torque-off not fully confirmed", cat="Safety", raw=reason, hint="robotd will also release torque on startup; power off if unsure")
            bad, _unknown = self._restore_baseline()
            if bad:
                ids = ", ".join(f"#{i}" for i in bad)
                msg = f"{len(bad)} servos failed 41/46 baseline restore ({ids}): not handed back. Servos are relaxed; retry Hand Back"
                self.log.error("41/46 baseline restore failed: not handed back", cat="Safety", raw=ids, dedup=False, hint=msg)
                return "kept", {"why": msg, "holding": self.holding(), "unknown": self.unknown()}
            if before_close is not None:
                before_close()          # Clear pause state before closing bus so external state is consistent
            self.close()
            return "handed_back", None
        finally:
            with self._cv:
                self.closing = False

    def hand_back_bus(self, why, wait_s=HANDBACK_WAIT_S, before_close=None):
        """Manual Hand Back to Main (blocking; run in worker thread): set closing -> stop motion/lively -> wait in-flight -> torque off -> close bus."""
        with self._cv:
            if self.state != "open":
                return True, None
            self.closing = True
        try:
            if self.ops is not None:
                self.ops.idle_running = False
                self.ops._motion_stop.set()
            if not self.wait_ops(wait_s):
                names = ", ".join(self.inflight_names()) or "?"
                return False, f"Servo ops still running ({names}) after {wait_s:.0f}s: not handed back. Wait and retry Hand Back"
            ok, reason = self.relax_all(why)
            if not ok:
                self.log.warn("Pre-handback torque-off not fully confirmed", cat="Safety", raw=reason, hint="robotd will also release torque on startup; power off if unsure")
            bad, unknown = self._restore_baseline()
            if bad:
                ids = ", ".join(f"#{i}" for i in bad)
                msg = f"{len(bad)} servos failed 41/46 baseline restore ({ids}): not handed back. Servos are relaxed; retry Hand Back"
                self.log.error("41/46 baseline restore failed: not handed back", cat="Safety", raw=ids, dedup=False, hint=msg)
                return False, msg
            relax_bad = list((self.last_relax or {}).get("bad") or [])
            note_ids = sorted(set(relax_bad) | set(unknown), key=IDS.index)
            note = (f"{', '.join(f'#{i}' for i in note_ids)} torque-off unconfirmed: robotd will release on startup; power off if unsure"
                    if note_ids else None)
            if before_close is not None:
                before_close()          # Clear pause state before closing bus
            self.close()
            return True, note
        finally:
            with self._cv:
                self.closing = False

    def close(self):
        """Close serial port without altering servo state (used on exit or external robotd conflict)."""
        if self.ops is not None:
            self.ops.idle_running = False
            self.ops._motion_stop.set()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._close_bus()
        self.ops = None
        if self.state != "closed":
            self.log.info("Closed servo bus", cat="Bus", dedup=False)
        self.state = "closed"
        self.rows, self.torque = {}, {}
        self.restored = False

    def _close_bus(self):
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:  # noqa: BLE001
                pass
        self.bus = None

    # ---- Polling ----

    def _read_static(self, ids):
        for sid in ids:
            d = {}
            for addr, key in STATIC_REGS.items():
                try:
                    d[key] = self.bus.read_u8(sid, addr)
                except feetech.BusError:
                    d[key] = None
            try:
                d["offset"] = feetech.sign15(self.bus.read_u16(sid, 31))
                d["torque_limit"] = self.bus.read_u16(sid, 48)
            except feetech.BusError:
                d.setdefault("offset", None)
            d["_at"] = time.monotonic()
            self.static[sid] = d

    def _baseline_path(self, bid):
        return os.path.join(self.state_dir, f"{BASELINE_PREFIX}{bid}.json")

    def _baseline_load(self, bid):
        """Read 41/46 baseline for this boot_id (or process memory for sim / missing boot_id)."""
        if bid is None:
            return _baseline_mem.setdefault((os.path.abspath(self.state_dir), bool(self.sim)), {})
        try:
            with open(self._baseline_path(bid), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        regs = data.get("regs") if isinstance(data, dict) else None
        out = {}
        if isinstance(regs, dict):
            for k, v in regs.items():
                try:
                    sid = int(k)
                except (TypeError, ValueError):
                    continue
                if sid in IDS and isinstance(v, dict) and isinstance(v.get("41"), int) and isinstance(v.get("46"), int):
                    out[sid] = {"41": v["41"] & 0xFF, "46": v["46"] & 0xFFFF}
        return out

    def _baseline_save(self, bid, regs):
        """Atomically write servo-baseline-<boot_id>.json via temp file + os.replace."""
        path = self._baseline_path(bid)
        data = {"v": 1, "boot_id": bid, "at": time.time(),
                "regs": {str(i): dict(regs[i]) for i in IDS if i in regs}}
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            self.log.warn("Failed to save 41/46 baseline file (kept in memory)", cat="Bus", step=path,
                          raw=f"{type(e).__name__}: {e}", hint="Check state directory disk space and permissions")

    def _record_baseline(self, found):
        """Capture raw register 41/46 baseline on first takeover of this boot."""
        bid = boot_id()
        self.boot_id = bid
        if self.sim:
            # Sim servos use in-memory baseline only
            regs = self._baseline_load(None)
        else:
            if bid is not None:
                current = os.path.basename(self._baseline_path(bid))
                try:
                    names = os.listdir(self.state_dir)
                except OSError:
                    names = []
                for name in names:                  # Stale baseline from previous boots
                    if name.startswith(BASELINE_PREFIX) and name.endswith(".json") and name != current:
                        try:
                            os.remove(os.path.join(self.state_dir, name))
                        except OSError:
                            pass
            regs = self._baseline_load(bid)
        added = False
        for sid in found:
            if sid in regs:
                continue
            try:
                regs[sid] = {"41": self.bus.read_u8(sid, 41), "46": self.bus.read_u16(sid, 46)}
            except feetech.BusError as e:
                self.log.warn(f"41/46 baseline: #{sid} unreadable, skipped", cat="Bus", step="Read 41/46 baseline",
                              raw=f"{type(e).__name__}: {e}", hint="Servo may be offline on the bus")
                continue
            added = True
        self.baseline = regs
        if not self.sim and bid is not None and added:
            self._baseline_save(bid, regs)

    def _restore_baseline(self):
        """Restore 41/46 baseline before handback and verify readback (up to 2 retries).
        Returns (mismatch_ids, no_reply_ids)."""
        if self.state != "open" or self.bus is None:
            return [], []
        base = getattr(self, "baseline", None) or {}
        missing = [i for i in IDS if i not in base]
        if missing:
            self.log.info(f"Handback: {len(missing)} servos have no 41/46 baseline, skipped", cat="Bus",
                          raw=f"missing baseline: {missing}", dedup=False)
        bad, unknown = [], []
        for sid in IDS:
            want = base.get(sid)
            if not want:
                continue
            outcome, last = "unknown", None
            for _ in range(3):                      # Initial + 2 retries
                try:
                    self.bus.write_u8(sid, 41, int(want["41"]) & 0xFF)
                    self.bus.write_u16(sid, 46, int(want["46"]) & 0xFFFF)
                    got41 = self.bus.read_u8(sid, 41)
                    got46 = self.bus.read_u16(sid, 46)
                except feetech.BusError as e:
                    outcome, last = "unknown", e
                    continue
                if got41 == (int(want["41"]) & 0xFF) and got46 == (int(want["46"]) & 0xFFFF):
                    outcome = "ok"
                    break
                outcome, last = "bad", f"readback 41={got41}, 46={got46}; expected {want['41']}, {want['46']}"
            if outcome == "ok":
                continue
            if outcome == "bad":
                bad.append(sid)
                self.log.error(f"41/46 baseline readback mismatch: #{sid}", cat="Safety", step="Verify 41/46",
                               raw=str(last), hint="Not handing back on mismatch; click Hand Back again")
            else:
                unknown.append(sid)
                self.log.warn(f"41/46 baseline restore: #{sid} no reply", cat="Safety", step="Write/Verify 41/46",
                              raw=f"{type(last).__name__}: {last}", hint="Servo did not respond")
        return bad, unknown

    def refresh_static(self, ids):
        """Refresh static/EEPROM registers (offset, P/D) for given servos after calibration or register write."""
        if self.bus is not None:
            self._read_static([i for i in ids if i in IDS])

    def _read_torque(self):
        got = self.bus.sync_read(IDS, 40, 1)
        self.torque = {sid: (v[1][0] if v else None) for sid, v in got.items()}
        self.torque_at = time.monotonic()
        return got

    def _loop(self):
        period = 1.0 / POLL_HZ
        n, t_rate = 0, time.monotonic()
        next_torque = time.monotonic() + TORQUE_EVERY_S
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self.ops is not None and self.ops.calib_lock.locked():
                # Avoid bus contention during calibration or timing tests
                self._stop.wait(0.02)
                continue
            try:
                states = self.bus.states(IDS)
                now = time.monotonic()
                if any(state is not None for state in states.values()):
                    self._sample_sequence += 1
                    self._last_poll_at = now
                for sid, s in states.items():
                    if s is not None:
                        s["at"] = now
                        self.rows[sid] = s
                if now >= next_torque:
                    self._read_torque()
                    next_torque = now + TORQUE_EVERY_S
            except Exception as e:  # noqa: BLE001  # Serial error: log once and retry
                self.log.error("Servo read error", cat="Bus", raw=f"{type(e).__name__}: {e}", dedup=True)
                time.sleep(0.5)
            n += 1
            if time.monotonic() - t_rate >= 1.0:
                self.hz = n / (time.monotonic() - t_rate)
                n, t_rate = 0, time.monotonic()
                self._note_window()
            left = period - (time.monotonic() - t0)
            if left > 0:
                self._stop.wait(left)

    def _note_window(self):
        tot = self._totals()
        self._window.append((time.monotonic(), tot[0], tot[1]))
        self._window = self._window[-6:]        # Last ~5 seconds

    def _totals(self):
        per = self.bus.per_id if self.bus else {}
        return (sum(c["asked"] for c in per.values()), sum(c["missed"] for c in per.values()))

    def drop_rate(self):
        if len(self._window) < 2:
            return None
        (_, a0, m0), (_, a1, m1) = self._window[0], self._window[-1]
        return None if a1 == a0 else (m1 - m0) / (a1 - a0)

    # ---- Torque Off ----

    def relax_all(self, by=None):
        """Release torque on all 15 servos and verify via readback (blocking). Returns (all_confirmed, reason)."""
        if self.state != "open" or self.bus is None:
            return False, "Servo bus is not open"
        self.latched, self.relaxing = True, True
        if self.ops is not None:                 # Stop active motion/lively transitions first
            self.ops.idle_running = False
            self.ops._motion_stop.set()
        t0 = time.monotonic()
        bad = list(IDS)
        try:
            self.bus.sync_write(40, 1, [(sid, b"\x00") for sid in IDS])
            for _ in range(3):
                got = self._read_torque()
                bad = [sid for sid, v in got.items() if v is None or v[1][0] != 0]
                if not bad:
                    break
                for sid in bad:                      # Retry unconfirmed servos individually
                    try:
                        self.bus.write_u8(sid, 40, 0)
                    except feetech.BusError:
                        pass
        except Exception as e:  # noqa: BLE001
            self.log.error("Bus torque-off error", cat="Safety", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="Support the duck by hand and power off")
        finally:
            self.relaxing = False
        ms = round((time.monotonic() - t0) * 1000)
        self.last_relax = {"ok": not bad, "ms": ms, "bad": bad, "at": time.monotonic()}
        if bad:
            self.log.error(f"Bus torque-off: {len(bad)} unconfirmed", cat="Safety", step="sync_read 40 verify",
                           raw=f"unconfirmed: {bad}", ctx={"by": by, "ms": ms}, hint="Possible loose wire; power off to be safe", dedup=False)
            return False, f"{len(bad)} unconfirmed: {bad}"
        # Always log state-change torque-off events without dedup
        self.log.info(f"Bus torque-off: all 15 confirmed ({ms} ms)", cat="Safety", step="sync_read 40 verify", raw=f"by={by}", dedup=False)
        return True, None

    # ---- Web Snapshot ----

    def snapshot(self, *, diagnostics=False):
        now = time.monotonic()
        rows, volts = [], []
        per = self.bus.per_id if self.bus else {}
        for sid in IDS:
            s = self.rows.get(sid)
            st = self.static.get(sid, {})
            c = per.get(sid, {})
            age = None if not s else now - s["at"]
            fresh = age is not None and age < 0.5
            if fresh and s.get("volt"):
                volts.append(s["volt"])
            cn, en = NAMES[sid]
            rows.append({
                "id": sid, "cn": cn, "en": en, "group": GROUP[sid], "dir": self.directions[sid],
                "raw": s["pos"] if s else None,
                "deg": round(angle_deg(s["pos"], self.directions[sid]), 1) if s else None,
                "volt": s["volt"] if s else None, "temp": s["temp"] if s else None,
                "temp_limit": st.get("temp_limit"), "load": s["load"] if s else None,
                "current_ma": round(s["current_ma"]) if s else None,
                "status": s["status_text"] if s else None, "moving": s["moving"] if s else None,
                "torque": self.torque.get(sid), "fresh": fresh, "age_ms": None if age is None else round(age * 1000),
                "p": st.get("p"), "d": st.get("d"), "mode": st.get("mode"), "offset": st.get("offset"),
                "comm": {k: c.get(k) for k in ("asked", "ok", "missed", "alarm", "streak", "max_streak")},
            })
        avg_v = round(sum(volts) / len(volts), 2) if volts else None
        torque_fresh = self.torque_at is not None and now - self.torque_at < 2.5
        all_off = torque_fresh and all(self.torque.get(i) == 0 for i in IDS)
        dr = self.drop_rate()
        result = {
            "t": "servo", "state": self.state, "error": self.error, "sim": self.sim, "source": "bus",
            "port": "Sim (No Hardware)" if self.sim else self.port,
            "marker_error": self.marker_error,
            "online": sum(1 for r in rows if r["fresh"]), "total": len(IDS),
            "hz": round(self.hz, 1), "avg_volts": avg_v, "battery_pct": battery_percent(avg_v),
            "drop_rate": None if dr is None else round(dr, 4),
            "bus": dict(self.bus.stats) if self.bus else None,
            "holding": sum(1 for i in IDS if self.torque.get(i) == 1),
            "all_off": all_off, "latched": self.latched, "relaxing": self.relaxing, "closing": self.closing,
            "last_relax": None if not self.last_relax else {k: v for k, v in self.last_relax.items() if k != "at"},
            "rows": rows,
            # Header gains summary & per-row running P/D/torque_limit
            "gains": ops.gains_view(rows) if (ops := self.ops) is not None else None,
        }

        if not diagnostics:
            return result
        for row in rows:
            inspected_at = self.static.get(row["id"], {}).get("_at")
            row["parameters"] = {"source": "studio_cached_inspection",
                                 "age_ms": round(max(0., now-inspected_at)*1000) if inspected_at is not None else None,
                                 "runtime": {"p": self.static.get(row["id"], {}).get("p_run"),
                                             "d": self.static.get(row["id"], {}).get("d_run"),
                                             "torque_limit": self.static.get(row["id"], {}).get("torque_limit")},
                                 "eeprom": {"p": row["p"], "d": row["d"], "mode": row["mode"], "offset": row["offset"]}}
        return self._health.apply(result, identity=("bus", self.opened_at),
            receipt=now, token=self._sample_sequence, row_tokens={i: self.rows.get(i, {}).get("at") for i in IDS},
            counters=result["bus"] or {},
            source_age_ms=None if self._last_poll_at is None else max(0., now-self._last_poll_at)*1000,
            available=self.state == "open", sequence=self._sample_sequence, now=now)

    def estop_status(self):
        """Status for topbar E-Stop button during active servo session."""
        if self.state != "open":
            return {"state": "unavailable", "why": "Servo bus not open; power off if needed"}
        if self.relaxing:
            return {"state": "pending", "why": "Releasing torque..."}
        s = self.snapshot()
        if s["all_off"]:
            return {"state": "confirmed", "why": "All 15 servos relaxed (bus verified)"}
        return {"state": "ready", "why": "Servo tuning active: E-Stop writes directly to bus"}


def view_from_robotd(bus, directions, state="closed", error=None, sim=False):
    """Read-only servo table snapshot from robotd busStatus when bus is not taken over."""
    # Robotd carries IPC receipt/error evidence. The legacy dict form stays valid
    # for ordinary feedback, but cannot establish fresh startup observations.
    rd = bus if hasattr(bus, "bus") else None
    if rd is not None:
        bus = rd.bus.value
    observations = servo_observations(bus, present=getattr(rd, "present", None),
                                      bus_age_s=rd.bus.age() if rd is not None else None,
                                      bus_error=getattr(rd.bus, "error", None) if rd is not None else None)
    if observations is not None:
        view = _view_startup_observations(observations, directions, state, error, sim)
        native = (bus or {}).get("native") or {}
        observed = (native.get("servo_observations") or {}).get("rows") or []
        by_id = {r.get("id"): r for r in observed if isinstance(r, dict)}
        for row in view["rows"]:
            comm = by_id.get(row["id"], {}).get("communication")
            if isinstance(comm, dict):
                row["comm"] = dict(comm)
        return _robotd_health(view, rd, bus)
    native = (bus or {}).get("native") if isinstance(bus, dict) and isinstance(bus.get("native"), dict) else {}
    joints = {j.get("id"): j for j in (native.get("joints") or []) if isinstance(j, dict)}
    if (bus or {}).get("mode") == "commissioning":
        last_sample = bus.get("last_sample") if isinstance(bus.get("last_sample"), dict) else {}
        sampled = {r.get("id"): r for r in (last_sample.get("servos") or []) if isinstance(r, dict)}
        observed = native.get("servo_observations") or {}
        observed_rows = {r.get("id"): r for r in observed.get("rows", []) if isinstance(r, dict)}
        sample_age = accepted_snapshot_age_ms(bus)
        for sid in IDS:
            feedback = sampled.get(sid, {})
            check = observed_rows.get(sid, {})
            age = sample_age
            if number(observed.get("age_ms")) and number(check.get("feedback_age_ms")):
                age = observed["age_ms"]+check["feedback_age_ms"]
            joints[sid] = {"position_raw": check.get("position_raw", feedback.get("position_raw")),
                "volts": check.get("volts", feedback.get("voltage_v")),
                "temperature_c": check.get("temperature_c", feedback.get("temperature_c")),
                "load_raw": check.get("load_raw", feedback.get("load_raw")),
                "speed_raw": check.get("speed_raw", feedback.get("speed_raw")),
                "current_raw": check.get("current_raw", feedback.get("current_raw")),
                "sample_age_ms": age, "feedback_fresh": age is not None and age < 500.
                    and check.get("responding", bool(feedback)) is True,
                "communication": check.get("communication")}
    torque_on = native.get("torque_enabled")
    rows, volts = [], []
    for sid in IDS:
        j = joints.get(sid) or {}
        raw = j.get("position_raw")
        d = DEFAULT_DIRECTIONS[sid] if directions is None else directions.get(sid, -1)
        fresh = j.get("feedback_fresh") is True
        if fresh and isinstance(j.get("volts"), (int, float)):
            volts.append(j["volts"])
        cn, en = NAMES[sid]
        rows.append({
            "id": sid, "cn": cn, "en": en, "group": GROUP[sid], "dir": d,
            "raw": raw, "deg": round(angle_deg(raw, d), 1) if isinstance(raw, int) else None,
            "volt": j.get("volts"), "temp": j.get("temperature_c"), "temp_limit": None,
            "load": j.get("load_raw"), "current_ma": j.get("current_ma"), "status": None, "moving": None,
            "speed_raw": j.get("speed_raw"), "current_raw": j.get("current_raw"),
            "velocity_rad_s": j.get("velocity_rad_s"),
            "torque": None if torque_on is None else int(bool(torque_on)), "fresh": fresh,
            "age_ms": j.get("sample_age_ms"), "p": None, "d": None, "mode": None, "offset": None,
            "comm": dict(j["communication"]) if isinstance(j.get("communication"), dict) else
                    {"asked": None, "ok": None, "missed": j.get("missing_total"), "alarm": None,
                     "streak": j.get("consecutive_missing"), "max_streak": None},
        })
    avg_v = round(sum(volts) / len(volts), 2) if volts else None
    return _robotd_health({
        "t": "servo", "state": state, "error": error, "sim": sim, "source": "robotd",
        "port": "Main Firmware", "online": sum(1 for r in rows if r["fresh"]), "total": len(IDS),
        "hz": None, "avg_volts": avg_v, "battery_pct": battery_percent(avg_v), "drop_rate": None, "bus": None,
        "holding": sum(1 for r in rows if r["torque"] == 1), "all_off": False, "latched": False,
        "relaxing": False, "last_relax": None, "rows": rows,
    }, rd, bus)


def _robotd_health(view, rd, bus):
    bus = bus if isinstance(bus, dict) else {}
    native = bus.get("native") or {}
    telemetry = native.get("telemetry") or {}
    order = native.get("read_order") if isinstance(native.get("read_order"), dict) else None
    hd = native.get("hd1910") or {}
    inspection = {item.get("id"): item for item in hd.get("registers_before_arm", []) if isinstance(item, dict)}
    for row in view["rows"]:
        cached = inspection.get(row["id"], {})
        row["parameters"] = {"source": "robotd_before_arm_cache", "age_ms": None,
            "runtime": {"pdi": cached.get("temporary_pdi"), "observed": False,
                        "temporary_gains_applied": hd.get("temporary_gains_applied"),
                        "restore_pending": hd.get("restore_pending")},
            "eeprom": {"pdi": cached.get("eeprom_pdi"), "mode": cached.get("mode")},
            "before_arm": {key: cached.get(key) for key in ("acceleration_raw", "goal_current_raw", "goal_speed_raw")},
            "configured": hd.get("servo_parameters")}
    session = telemetry.get("session", (order or {}).get("session", bus.get("session")))
    hello_at = getattr(getattr(rd, "hello", None), "at", None)
    identity = ("robotd", session, hello_at,
                (order or {}).get("generation"), tuple((order or {}).get("ids", (order or {}).get("servo_ids", []))))
    tracker = getattr(rd, "_servo_health", None) if rd is not None else None
    if tracker is None:
        tracker = ServoHealth()
        if rd is not None:
            rd._servo_health = tracker
    receipt = getattr(getattr(rd, "bus", None), "at", None)
    if rd is None:
        receipt = time.monotonic()
    token = (bus.get("cycle", bus.get("total_cycles")), native.get("sample_sequence"),
             tuple((r["id"], r["comm"].get("asked"), r.get("raw")) for r in view["rows"]))
    if view.get("diagnostic") and receipt is not None:
        receipt_age = max(0., time.monotonic()-receipt)*1000
        for row in view["rows"]:
            if row["age_ms"] is not None:
                row["age_ms"] = max(0., row["age_ms"]-receipt_age)
    counters = {"bad_checksum": telemetry.get("bad_checksums"), "timeout": telemetry.get("timeouts"),
                "failed_reads": telemetry.get("failed_reads")}
    available = rd is None or (getattr(rd, "present", None) is True and not rd.bus.error
                               and (hello_at is None or receipt is not None and receipt >= hello_at))
    total, failed = telemetry.get("total_reads"), telemetry.get("failed_reads")
    sequence = bus.get("total_success") if bus.get("mode") == "commissioning" else None
    if counter(total) and counter(failed) and total >= failed:
        sequence = total - failed
    age = native.get("sample_age_ms")
    if not number(age):
        age = (native.get("servo_observations") or {}).get("age_ms")
    if not number(age):
        age = min((r["age_ms"] for r in view["rows"] if number(r["age_ms"])), default=None)
    return tracker.apply(view, identity=identity, receipt=receipt, token=token, counters=counters,
                         source_age_ms=age, available=available, sequence=sequence, require_age=rd is not None)


def _view_startup_observations(observations, directions, state, error, sim):
    by_id = {row["id"]: row for row in observations["rows"]}
    rows, volts = [], []
    for sid in IDS:
        row = by_id.get(sid, {})
        cn, en = NAMES[sid]
        volt = row.get("volts")
        if volt is not None:
            volts.append(volt)
        rows.append({
            "id": sid, "cn": cn, "en": en, "group": GROUP[sid],
            "dir": DEFAULT_DIRECTIONS[sid] if directions is None else directions.get(sid, -1),
            "raw": row.get("position_raw"), "deg": None, "volt": volt,
            "temp": row.get("temperature_c"), "temp_limit": None, "load": row.get("load_raw"),
            "speed_raw": row.get("speed_raw"), "current_raw": row.get("current_raw"), "current_ma": None,
            "responding": row.get("responding"), "fresh": row.get("responding") is True,
            "feedback_fresh": row.get("feedback_fresh", False), "age_ms": row.get("check_age_ms"),
            "torque": None, "status": None, "moving": None, "p": None, "d": None, "mode": None, "offset": None,
            "comm": {key: None for key in ("asked", "ok", "missed", "alarm", "streak", "max_streak")},
        })
    avg_v = round(sum(volts) / len(volts), 2) if volts else None
    return {"t": "servo", "state": state, "error": error, "sim": sim, "source": "robotd", "diagnostic": True,
            "port": "Main Firmware", "online": observations["online"], "total": len(IDS),
            "hz": None, "avg_volts": avg_v, "battery_pct": battery_percent(avg_v), "drop_rate": None, "bus": None,
            "holding": None, "all_off": False, "latched": False, "relaxing": False, "last_relax": None, "rows": rows}


def _sim_home():
    """Initialize sim servos to stand pose (raw = 2048 + dir * angle)."""
    home = {20: 0.0, 21: -0.0873, 22: -0.4579, 23: -0.0049, 24: 0.4530, 30: 0.3491, 31: 0.3491,
            32: 0.0, 33: 0.0, 34: 0.0, 10: 0.0, 11: 0.0873, 12: 0.4579, 13: 0.0049, 14: -0.4530}
    return {sid: round(2048 + DEFAULT_DIRECTIONS[sid] * rad * 4096 / (2 * 3.141592653589793)) for sid, rad in home.items()}


def list_ports():
    """List currently visible serial ports."""
    try:
        from serial.tools import list_ports as lp
        return [{"device": p.device, "desc": (p.description or "").strip()} for p in lp.comports()]
    except Exception:  # noqa: BLE001
        return []


def load_directions(state_dir):
    """Load servo direction table from servo-directions.json, defaulting to -1."""
    p = os.path.join(state_dir, "servo-directions.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return {int(k): int(v) for k, v in d.items() if k.isdigit() and int(v) in (1, -1)}
    except (OSError, ValueError):
        return {}
