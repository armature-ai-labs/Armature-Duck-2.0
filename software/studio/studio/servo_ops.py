"""Servo page operations: torque, target motion, official/saved poses, center calibration, bus tools, and lively head motion.

Studio safety rules:
- When `session.latched` (torque-off latch), torque-on, target writes, motion, calibration, undo, register writes, and ID changes are refused until unlatched.
- Successfully calibrated servos automatically hold torque (align target to current position first).
- Calibration target is always official zero (2048).
- Gains (P / D / torque_limit) match robotd before enabling torque (see MainGains, apply_gains).
- Calibration backups and poses live in Studio's state dir (`servo-calib/`, `servo-poses/`); sim mode uses a temp dir.
"""
import json
import math
import os
import random
import struct
import tempfile
import threading
import time

from .bus import feetech
from .servo import IDS, Refused  # noqa: F401  # Re-exported from servo.py

CALIB_TOLERANCE = 2      # Max step error after calibration
DROOP_TOLERANCE = 3      # Gravity droop tolerance in steps while torque is off
OFFSET_LIMIT = 2047      # Safe range for offset register 31
CALIB_BIG_DEG = 30       # Warn threshold in degrees for large calibration delta
IMU_BUS_ID = 200         # Bus ID reserved for the IMU board
FOLD_KNEE_SEND = 88 * math.pi / 180     # Fold pose knee angle capped at 88 deg to avoid ankle-thigh collision
JOINT = {20: "left_hip_yaw", 21: "left_hip_roll", 22: "left_hip_pitch", 23: "left_knee", 24: "left_ankle",
         30: "neck_pitch", 31: "head_pitch", 32: "head_yaw", 33: "head_roll", 34: "mouth",
         10: "right_hip_yaw", 11: "right_hip_roll", 12: "right_hip_pitch", 13: "right_knee", 14: "right_ankle"}
# Official poses from microduck_rl scene.xml keyframes (radians, by servo ID)
OFFICIAL = {
    "zero": ("Zero", {i: 0.0 for i in IDS}),
    "stand": ("Stand", {20: 0, 21: -0.0873, 22: -0.4579, 23: -0.0049, 24: 0.453, 30: 0.3491, 31: 0.3491, 32: 0, 33: 0,
                     10: 0, 11: 0.0873, 12: 0.4579, 13: 0.0049, 14: -0.453}),
    "sit": ("Sit", {20: 0, 21: 0, 22: -0.5236, 23: 1.0472, 24: 0, 30: 0.5, 31: 1.6, 32: 0, 33: 0,
                   10: 0, 11: 0, 12: 0.5236, 13: -1.0472, 14: 0}),
    "fold": ("Fold", {20: 0, 21: 0, 22: 1.57, 23: 1.57, 24: 0, 30: 1, 31: 1, 32: 0, 33: 0,
                      10: 0, 11: 0, 12: -1.57, 13: -1.57, 14: 0}),
}
# High-risk registers highlighted in the UI
DANGER = {5: "ID: may collide; connect only one servo when changing", 6: "Baud rate: changing causes loss of comms", 8: "Return delay/level: 0 disables write acks",
          9: "Min angle limit", 11: "Max angle limit", 19: "Unload condition (protection): disabling stops thermal/overcurrent unload",
          33: "Operating mode: velocity mode spins continuously", 40: "Torque enable: use the page toggle instead"}
DEG = 4096 / 360
# Lively head gestures: [delta_deg_from_center, duration_s]
GESTURES = [
    (3, lambda s: [({33: 14 * s, 32: 8 * s}, 0.5), ({}, random.uniform(0.6, 1.4)), ({33: 0, 32: 0}, 0.6)]),           # Tilt
    (3, lambda s: [({32: 30 * s, 33: -5 * s, 31: 4}, 0.6), ({}, random.uniform(0.8, 1.8)), ({32: 0, 33: 0, 31: 0}, 0.7)]),  # Glance
    (2, lambda s: [({32: -20}, 0.3), ({32: 20}, 0.38), ({32: -12}, 0.32), ({32: 0}, 0.35)]),                            # Shake
    (2, lambda s: [({31: 12}, 0.3), ({31: -7}, 0.34), ({31: 6}, 0.3), ({31: 0}, 0.35)]),                                # Nod
    (2, lambda s: [({34: 16}, 0.22), ({34: 0}, 0.26), ({34: 13}, 0.22), ({34: 0}, 0.28)]),                              # Quack
    (2, lambda s: [({33: 12 * s, 31: -8, 34: 9}, 0.55), ({}, random.uniform(0.5, 1.0)), ({34: 0}, 0.25), ({33: 0, 31: 0}, 0.6)]),  # Curious
    (2, lambda s: [({32: 18 * s, 31: 6}, 0.45), ({32: -18 * s, 31: 6}, 0.7), ({32: 0, 31: 0}, 0.5)]),                   # Scan
    (1, lambda s: [({33: 7, 32: 6}, 0.18), ({33: -7, 32: -6}, 0.2), ({33: 0, 32: 0}, 0.25)]),                           # Twitch
]
CAT = {"Motion": "Bus", "Register": "Bus", "Direction": "Calibration", "Pose": "Calibration"}


def enc_sm(v):
    """Sign-magnitude encoding: registers 31 (offset), 42 (goal pos), 46 (speed) use BIT15 as sign bit."""
    v = int(v)
    return (0x8000 | (-v & 0x7FFF)) if v < 0 else (v & 0x7FFF)


def fmt_off(v):
    if v is None:
        return "?"
    if v < 0:
        return str(v)
    return f"{feetech.sign15(v)} (raw {v})" if v & 0x8000 else str(v)


def _ack(err):
    """Write ack: 0 = OK, None = no reply, else status bit."""
    return "OK" if err == 0 else ("(no reply)" if err is None else f"(status 0x{err:02X})")


def infer_offset_sign(before, after, d_off, slack=8):
    """Infer offset sign from a single calibration step: after - before ~= s * d_off."""
    if before is None or after is None or d_off is None or abs(d_off) < 4:
        return None
    moved = after - before
    miss = {s: abs(moved - s * d_off) for s in (1, -1)}
    s = min(miss, key=miss.get)
    if miss[s] > max(slack, abs(d_off) // 2) or miss[-s] - miss[s] < abs(d_off):
        return None
    return s


def parse_ids(text):
    """Parse "20-24,30-34,10-14" into [20..24, 30..34, 10..14]."""
    out = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    if any(not 0 <= i <= 253 for i in out):
        raise Refused("ID must be between 0 and 253")
    return out


# ---------------- Gains (P / D / torque_limit) matching robotd ----------------
# Studio writes the same RAM gains as robotd (default P5, D0, torque_limit 1000) before enabling torque.
ROBOT_ETC = "/etc/robot/feetech-ft5"
SERVO_PARAMS = ROBOT_ETC + "/servo-test-parameters.json"       # Same dir as calibration file
ROBOTD_TOMLS = (ROBOT_ETC + "/robotd.toml", "/etc/robot/robotd.toml")   # Same as onboard.ROBOTD_CONFIGS
POLICY_DIR = "/opt/robot/policies/current"                     # Default policy dir when [policy] walk is unset
DEFAULT_GAINS = (5, 0, 1000)      # Default P=5, D=0, torque_limit=1000
GAIN_REGS = {21: "EEPROM P", 22: "EEPROM D", 48: "Torque Limit", 49: "Torque Limit High", 50: "P", 51: "D"}
GAINS_RECHECK_S = 3.0             # Stat params file at most every 3s


def _stat(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_servo_params(path):
    """Read servo-test-parameters.json: exact keys kp (1-32), kd (0-64), torque_limit (200-1000) as ints."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate keys")
        return dict(items)
    with open(path, "rb") as f:
        obj = json.loads(f.read().decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(obj, dict) or set(obj) != {"kp", "kd", "torque_limit"}:
        raise ValueError("Must have exact keys: kp, kd, torque_limit")
    for k in ("kp", "kd", "torque_limit"):
        if type(obj[k]) is not int:
            raise ValueError(f"{k} must be an integer, got {obj[k]!r}")
    if not 1 <= obj["kp"] <= 32:
        raise ValueError("P (kp) must be 1-32")
    if not 0 <= obj["kd"] <= 64:
        raise ValueError("D (kd) must be 0-64")
    if not 200 <= obj["torque_limit"] <= 1000:
        raise ValueError("torque_limit must be 200-1000")
    return obj["kp"], obj["kd"], obj["torque_limit"]


def _walk_contract(toml_path):
    """Resolve deployment-contract.json from [policy] walk in robotd.toml."""
    import tomllib     # noqa: PLC0415  # Python 3.11+
    with open(toml_path, "rb") as f:
        policy = tomllib.load(f).get("policy") or {}
    walk = policy.get("walk")
    if not isinstance(walk, str) or not walk.strip() or walk.strip().lower() == "none":
        walk = POLICY_DIR + "/" + ("roller.onnx" if policy.get("mode") == "roller" else "velstand.onnx")
    elif not (walk.startswith("/") or os.path.isabs(walk)):
        walk = "/" + walk                 # Relative paths resolve against / under systemd
    return walk.rsplit("/", 1)[0] + "/deployment-contract.json"


def contract_kp(path):
    """Read model.actuators[0].kp_fw (1-32 int) from walk deployment-contract.json."""
    with open(path, encoding="utf-8") as f:
        v = json.load(f)
    acts = ((v.get("model") or {}).get("actuators")) if isinstance(v, dict) else None
    if not isinstance(acts, list) or len(acts) != 1 or not isinstance(acts[0], dict):
        raise ValueError("model.actuators must have length 1")
    p = acts[0].get("kp_fw")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p != int(p) or not 1 <= p <= 32:
        raise ValueError(f"kp_fw must be an int in 1-32, got {p!r}")
    return int(p)


class MainGains:
    """Resolve P / D / torque_limit matching robotd (servo-test-parameters.json or contract kp_fw fallback)."""

    def __init__(self, params_path=SERVO_PARAMS, robotd_tomls=ROBOTD_TOMLS, log=None):
        self.params_path, self.robotd_tomls, self.log = params_path, tuple(robotd_tomls), log
        self._lock = threading.Lock()
        self._key, self._value, self._checked = None, None, 0.0
        self._walk = (None, None)          # (robotd.toml key, contract path)

    def get(self, max_age=0.0):
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now - self._checked < max_age:
                return self._value
            self._checked = now
            toml = next((p for p in self.robotd_tomls if _stat(p)), None)
            tkey = (toml, _stat(toml) if toml else None)
            if self._walk[0] != tkey:
                try:
                    contract = _walk_contract(toml) if toml else None
                except Exception as e:  # noqa: BLE001
                    contract = ("bad", f"{type(e).__name__}: {e}")
                self._walk = (tkey, contract)
            contract = self._walk[1]
            key = (_stat(self.params_path), tkey, contract,
                   _stat(contract) if isinstance(contract, str) else None)
            if key != self._key or self._value is None:
                self._key, self._value = key, self._compute(toml, contract)
                if self._value["warn"] and self.log:
                    self.log(self._value["warn"], "Bus", "warn")
            return self._value

    def _compute(self, toml, contract):
        kp, kd, limit = DEFAULT_GAINS
        warn = None
        if _stat(self.params_path):
            try:
                kp, kd, limit = load_servo_params(self.params_path)
                return {"kp": kp, "kd": kd, "limit": limit, "source": "Params File",
                        "detail": f"{self.params_path} (used by robotd when present)", "warn": None}
            except Exception as e:  # noqa: BLE001
                warn = (f"Invalid servo params file {self.params_path} ({e}): robotd will stall waiting for bus; "
                        f"Studio is using robotd defaults. Fix or remove this file")
        etc = os.path.dirname(self.params_path)
        if toml is None and not os.path.isdir(etc):
            detail = f"No robotd config on this host ({etc}); using V167 defaults: P5, D0, limit 1000"
        elif isinstance(contract, str):
            try:
                kp = contract_kp(contract)
                detail = f"robotd V167: P from walk contract kp_fw ({contract}), D0, limit 1000"
            except Exception as e:  # noqa: BLE001
                detail = f"Walk contract {contract} unreadable ({type(e).__name__}: {e}); using P5, D0, limit 1000"
        else:
            why = contract[1] if isinstance(contract, tuple) else "robotd.toml not found"
            detail = f"Walk policy unreadable ({why}); using P5, D0, limit 1000"
        return {"kp": kp, "kd": 0, "limit": 1000, "source": "Main Default", "detail": detail, "warn": warn}


def gains_bytes(g):
    """Pack RAM 48..51 payload: [limit_lo, limit_hi, P, D]."""
    return struct.pack("<H", g["limit"]) + bytes([g["kp"], g["kd"]])


def gains_text(b):
    return "unreadable" if b is None else f"P{b[2]} · D{b[3]} · Limit {b[0] | b[1] << 8}"


class ServoOps:
    def __init__(self, session, state_dir):
        self.s = session
        self.state_dir = state_dir
        self.offset_sign = None          # +1 or -1; probed before calibration
        self.speed_unit = 1.0            # Steps/s per unit in speed reg 46
        self.max_speed_reg = 3000        # Uncapped speed register value
        self.stream_last = {}
        self.calib_lock = threading.Lock()   # Pause polling during calibration
        self._motion = None
        self._motion_stop = threading.Event()
        self.idle_running = False
        self._idle_thread = None
        self._sim_calib_dir = None
        # MainGains for torque-on; manual tracks register-panel overrides {id: {addr: val}}
        self.gains = MainGains(log=self.log)
        self.manual = {}
        self._gains_logged = None

    # ---- Helpers ----

    @property
    def bus(self):
        if self.s.bus is None or self.s.state != "open":
            raise Refused("Servo bus is not open: click Take Over Bus first")
        return self.s.bus

    def log(self, msg, cat="Bus", level="info", exc=False):
        self.s.log.write(msg, cat=CAT.get(cat, cat), level=level, exc=exc)

    def guard(self):
        if self.s.latched:
            raise Refused("Torque-off is latched: click Torque All On or Hold first")

    def dirs(self):
        return self.s.directions

    def present(self):
        return [i for i in IDS if (self.s.rows.get(i) or {}).get("at")] or list(IDS)

    def calib_dir(self):
        if self.s.sim:
            if not self._sim_calib_dir or not os.path.isdir(self._sim_calib_dir):
                self._sim_calib_dir = tempfile.mkdtemp(prefix="studio-sim-calib-")
            return self._sim_calib_dir
        return os.path.join(self.state_dir, "servo-calib")

    def poses_dir(self):
        return os.path.join(self.state_dir, "servo-poses")

    def read_off(self, i):
        return feetech.sign15(self.bus.read_u16(i, 31))

    def read_pos(self, i):
        return feetech.sign15(self.bus.read_u16(i, 56))

    def write_off(self, i, v):
        return self.bus.write_u16(i, 31, enc_sm(v))

    def wait_off_change(self, i, was, ms=20):
        """Poll position after offset write until settled (max ms)."""
        end = time.monotonic() + ms / 1000.0
        off = was
        while True:
            off = self.read_off(i)
            if off != was or time.monotonic() >= end:
                return off, off != was

    def read_phase(self):
        """Read phase reg 18 on bus open to determine speed unit."""
        if self.s.sim:
            return
        try:
            ph = self.bus.read_u8(self.present()[0], 18)
            self.speed_unit = 1.0 if ph & 0x04 else 50.0
            self.max_speed_reg = 0 if ph & 0x08 else int(3000 / self.speed_unit)
            self.log(f"Phase 18 = {ph} -> speed unit {self.speed_unit:g} steps/s, max reg {self.max_speed_reg}")
        except Exception as e:  # noqa: BLE001
            self.log(f"Failed to read phase reg, using 1 step/s: {type(e).__name__}: {e}", "System", "warn")

    def release_speed(self, ids):
        """Set max accel (41=0) and uncapped speed (46) without touching goal position 42."""
        ids = list(ids)
        if ids:
            self.bus.sync_write(41, 1, [(i, bytes([0])) for i in ids])
            self.bus.sync_write(46, 2, [(i, struct.pack("<H", enc_sm(self.max_speed_reg))) for i in ids])

    # ---- Gains enforcement before torque-on ----

    def _read_gains(self, ids):
        """Verify RAM 48..51 via sync_read with per-servo fallback; caches into session.static."""
        got = self.bus.sync_read(ids, 48, 4)
        out = {}
        for i in ids:
            v = got.get(i)
            data = bytes(v[1]) if v else None
            if data is None:
                try:
                    data = bytes(self.bus.read(i, 48, 4)[1])
                except feetech.BusError:
                    data = None
            out[i] = data
            if data is not None:
                st = self.s.static.setdefault(i, {})
                st["torque_limit"], st["p_run"], st["d_run"] = data[0] | data[1] << 8, data[2], data[3]
        return out

    def _log_gains(self, g):
        if self._gains_logged != g:
            self._gains_logged = g
            self.log(f"Torque-on gains: P{g['kp']} · D{g['kd']} · Limit {g['limit']} ({g['source']}: {g['detail']})")

    def _clear_manual(self, i):
        """Clear manual RAM gain overrides (48-51) for servo i after applying MainGains."""
        m = self.manual.get(i)
        if m:
            for a in (47, 48, 49, 50, 51):            # 47: 2-byte write from 47 covers 48
                m.pop(a, None)
            if not m:
                self.manual.pop(i, None)

    def apply_gains(self, ids):
        """Write MainGains to RAM 48..51 before torque-on and verify readback. Returns {id: reason} for failures."""
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        g = self.gains.get()
        self._log_gains(g)
        want = gains_bytes(g)
        before, wrote, cur = {}, [], {}
        try:
            cur = self._read_gains(ids)
            before = dict(cur)
            todo = [i for i in ids if cur[i] != want]
            for _ in range(2):
                if not todo:
                    break
                self.bus.sync_write(48, 4, [(i, want) for i in todo])
                wrote += [i for i in todo if i not in wrote]
                cur.update(self._read_gains(todo))
                todo = [i for i in todo if cur[i] != want]
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            bad = {i: f"Bus error writing gains ({err})" for i in ids if cur.get(i) != want}
            self.log(f"Error writing pre-torque gains: {err}; skipping torque-on for {sorted(bad)}", "Bus", "error", exc=True)
            return bad
        bad = {}
        for i in ids:
            if cur[i] == want:
                self._clear_manual(i)
                continue
            if cur[i] is None:
                bad[i] = "Failed to read gains (regs 48-51 no reply)"
            else:
                c = cur[i]
                diff = [f"{a} ({name}) wrote {w} read {r}" for a, name, w, r in (
                    (48, "Limit", g["limit"], c[0] | c[1] << 8), (50, "P", g["kp"], c[2]), (51, "D", g["kd"], c[3])) if w != r]
                bad[i] = "Gains write mismatch: reg " + ", ".join(diff)
            self.log(f"#{i} pre-torque gains (P{g['kp']} · D{g['kd']} · Limit {g['limit']}): {bad[i]}; torque stays off", "Bus", "error")
        ok = [i for i in wrote if i not in bad]
        if ok:
            self.log(f"Pre-torque gains set to P{g['kp']} · D{g['kd']} · Limit {g['limit']} ({g['source']}):"
                     + " ".join(f"#{i} (was {gains_text(before.get(i))})" for i in ok))
        return bad

    def check_gains(self, ids):
        """Verify gains on already-torqued servos without writing (changing P under load causes droop)."""
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        want = gains_bytes(self.gains.get())
        try:
            cur = self._read_gains(ids)
        except Exception as e:  # noqa: BLE001
            self.log(f"Error verifying gains on active servos: {type(e).__name__}: {e}", "Bus", "warn")
            return {}
        differ = {}
        for i in ids:
            if cur[i] == want:
                self._clear_manual(i)
            elif cur[i] is not None:
                differ[i] = (f"Already torqued with {gains_text(cur[i])}, differs from main ({gains_text(want)}); left unchanged"
                             " (release torque and re-hold to apply)")
        if differ:
            self.log("Active servo gains differ from main (left unchanged): " + " ".join(f"#{i} {gains_text(cur[i])}" for i in differ)
                     + f" (main: {gains_text(want)}); release and re-hold to update", "Bus", "warn")
        return differ

    def _gains_then_torque(self, i, trail, label="Torque On"):
        """Restore torque after calibration/undo: apply MainGains first."""
        bad = self.apply_gains([i])
        if bad:
            trail.append(f"FAIL {bad[i]}, torque not restored")
            return False
        trail.append(label + _ack(self.bus.write_u8(i, 40, 1)))
        return True

    def gains_view(self, rows=None):
        """Return header gains summary and annotate each row with running P/D/limit and gains_ok."""
        g = self.gains.get(max_age=GAINS_RECHECK_S)
        want = (g["kp"], g["kd"], g["limit"])
        differ_on = []
        for r in rows or []:
            st = self.s.static.get(r["id"]) or {}
            run = (st.get("p_run"), st.get("d_run"), st.get("torque_limit"))
            r["p_run"], r["d_run"], r["limit"] = run
            r["gains_ok"] = None if None in run else run == want
            if r["gains_ok"] is False and r.get("torque") == 1:
                differ_on.append(r["id"])
        manual = [{"id": i, "addr": a, "value": v, "what": GAIN_REGS.get(a, "")}
                  for i, m in sorted(list(self.manual.items())) for a, v in sorted(list(m.items()))]
        return {"kp": g["kp"], "kd": g["kd"], "limit": g["limit"], "source": g["source"], "detail": g["detail"],
                "warn": g["warn"], "manual": manual, "differ_on": differ_on}

    # ---- Torque ----

    def torque_on_aligned(self, ids):
        """Enable torque after applying MainGains and aligning goal position (42) to present position."""
        self.guard()
        bus = self.bus
        tq = bus.sync_read(ids, 40, 1)
        already = [i for i in ids if tq.get(i) and tq[i][1][0] == 1]
        need = [i for i in ids if i not in already]
        aligned, bad = {}, {}
        # Apply MainGains before enabling torque
        bad.update({i: w + ", torque kept off" for i, w in self.apply_gains(need).items()})
        differ = self.check_gains(already)
        need = [i for i in need if i not in bad]
        st = bus.states(need) if need else {}
        for i in need:
            if not st.get(i):
                bad[i] = "Position unreadable, torque kept off (avoid jumping to stale goal)"
                continue
            p = st[i]["pos"]
            got = None
            try:
                for _ in range(2):
                    bus.write_u16(i, 42, enc_sm(p))
                    got = feetech.sign15(bus.read_u16(i, 42))
                    if got == p:
                        aligned[i] = p
                        break
                else:
                    bad[i] = f"Goal write {p} read back {got}, torque kept off"
            except Exception as e:  # noqa: BLE001
                bad[i] = f"Goal write error ({type(e).__name__}: {e}), torque kept off"
        on = already + [i for i in need if i in aligned]
        if on:
            self.guard()                                   # Abort if E-Stop latched mid-align
            bus.sync_write(40, 1, [(i, bytes([1])) for i in on])
        self.s._read_torque()
        self.log(f"Torque ON -> {on}" + (f"; aligned goal: " + " ".join(f"#{i}->{p}" for i, p in aligned.items()) if aligned else "")
                 + (f"; skipped " + " ".join(f"#{i} ({w})" for i, w in bad.items()) if bad else ""), "Motion",
                 "warn" if bad else "info")
        g = self.gains.get(max_age=GAINS_RECHECK_S)
        return {"on": on, "bad": {str(i): w for i, w in bad.items()}, "differ": {str(i): w for i, w in differ.items()},
                "gains": {k: g[k] for k in ("kp", "kd", "limit", "source")}}

    def torque_off(self, ids):
        self.bus.sync_write(40, 1, [(i, bytes([0])) for i in ids])
        self.s._read_torque()
        self.log(f"Torque OFF -> {ids}", "Motion")
        return {"off": ids}

    # ---- Motion ----

    def goal(self, sid, pos):
        self.guard()
        pos = max(0, min(4095, int(pos)))
        if self._motion and self._motion.is_alive():
            self.stop(quiet=True)              # Manual goal overrides active transition
        self.bus.write_u16(int(sid), 42, enc_sm(pos))
        return {"id": sid, "pos": pos}

    def goals_verify(self, want):
        """Write goals and verify readback, retrying up to 4 rounds."""
        want = {int(i): max(0, min(4095, int(p))) for i, p in want.items()}
        attempt = 0
        for attempt in range(4):
            self.bus.sync_write(42, 2, [(i, struct.pack("<H", p)) for i, p in want.items()])
            time.sleep(0.02)
            st = self.bus.states(list(want))
            want = {i: p for i, p in want.items() if not st.get(i) or (st[i]["goal"] & 0xFFFF) != p}
            if not want:
                break
        if want:
            self.log(f"Goal readback: {len(want)} servos unconfirmed after 4 rounds {sorted(want)}", "Motion", "warn")
        return {"missing": sorted(want), "attempts": attempt + 1}

    def move(self, goals, seconds=2.0, mode="profile", acc=30):
        """Move from current positions to goals ({id: raw}). mode='profile' or 'stream' (20 Hz)."""
        self.guard()
        goals = {int(i): max(0, min(4095, int(p))) for i, p in goals.items() if int(i) in IDS}
        if not goals:
            raise Refused("No servos selected to move")
        self.stop(quiet=True)
        self._motion_stop.clear()
        t = threading.Thread(target=self._run_move, args=(goals, float(seconds), mode, int(acc)), name="servo-move", daemon=True)
        self._motion = t
        t.start()
        return {"started": len(goals), "seconds": seconds, "mode": mode}

    def _run_move(self, goals, seconds, mode, acc):
        try:
            if mode == "profile" and seconds > 0:
                self._profile(goals, seconds, acc)
                if self._motion_stop.wait(seconds + 0.4):
                    return
            else:
                st = self.bus.states(list(goals))
                start = {i: (st[i]["pos"] if st.get(i) else p) for i, p in goals.items()}
                t0 = time.monotonic()
                while True:
                    if self.s.halted:
                        return
                    tt = 1.0 if seconds <= 0 else min(1.0, (time.monotonic() - t0) / seconds)
                    g = {i: round(start[i] + (p - start[i]) * tt) for i, p in goals.items()}
                    if tt >= 1:
                        break
                    self._stream(g, 0.05)
                    if self._motion_stop.wait(0.05):
                        return
            if not self.s.halted:
                self.goals_verify(goals)
                self.release_speed(list(goals))
                for i in goals:
                    self.stream_last.pop(i, None)
        except Refused:
            pass
        except Exception as e:  # noqa: BLE001
            self.log(f"Motion error: {type(e).__name__}: {e}", "Motion", "error", exc=True)

    def _profile(self, want, seconds, acc):
        """Write regs 41..47 (accel, goal, time=0, speed) for onboard trapezoidal profile."""
        self.guard()
        acc = max(1, min(254, acc))
        st = self.bus.states(list(want))
        items = []
        for i, p in want.items():
            cur = st[i]["pos"] if st.get(i) else p
            sps = abs(p - cur) / max(0.2, seconds)
            reg = max(1, min(32767, int(round(sps / self.speed_unit)))) if sps > 0 else 1
            items.append((i, bytes([acc]) + struct.pack("<H", enc_sm(p)) + struct.pack("<H", 0) + struct.pack("<H", reg)))
        self.bus.sync_write(41, 7, items)
        for i in want:
            self.stream_last.pop(i, None)

    def _stream(self, want, dt):
        """Streaming interpolation: send per-frame goals with matched speed limit."""
        self.guard()
        fresh = [i for i in want if i not in self.stream_last]
        if fresh:
            st = self.bus.states(fresh)
            for i in fresh:
                self.stream_last[i] = st[i]["pos"] if st.get(i) else want[i]
        items = []
        for i, p in want.items():
            sps = abs(p - self.stream_last[i]) / dt * 1.2
            reg = max(3, min(32767, int(round(sps / self.speed_unit))))
            items.append((i, bytes([0]) + struct.pack("<H", enc_sm(p)) + struct.pack("<H", 0) + struct.pack("<H", reg)))
            self.stream_last[i] = p
        self.bus.sync_write(41, 7, items)

    def stop(self, quiet=False):
        """Stop active transitions and lively motion; set goal = present position."""
        self._motion_stop.set()
        self.idle_running = False
        if self._motion and self._motion.is_alive():
            self._motion.join(timeout=0.5)
        if quiet:
            return {}
        if self.s.state == "open":
            st = self.bus.states(self.present())
            g = {i: s["pos"] for i, s in st.items() if s}
            if g and not self.s.halted:
                self.goals_verify(g)
            self.log("Stopped: goal set to present position", "Motion")
        return {}

    def official_goals(self, key, for_send=True):
        if key not in OFFICIAL:
            raise Refused(f"Unknown official pose: {key}")
        label, rad = OFFICIAL[key]
        rad = dict(rad)
        if key == "fold" and for_send:
            rad[23], rad[13] = FOLD_KNEE_SEND, -FOLD_KNEE_SEND
        d = self.dirs()
        return label, {i: int(round(2048 + a * 4096 / (2 * math.pi) * d.get(i, -1))) for i, a in rad.items()}

    def go_official(self, key, seconds, mode, acc):
        label, goals = self.official_goals(key)
        self.log(f"Moving to official pose '{label}' ({seconds:g}s, {mode})", "Motion")
        return self.move(goals, seconds, mode, acc)

    # ---- Lively ----

    def idle(self, on):
        if not on:
            self.idle_running = False
            return {"idle": False}
        self.guard()
        head = [i for i in (30, 31, 32, 33, 34) if i in self.present()]
        if len(head) < 3:
            raise Refused("Head servos are offline")
        self.torque_on_aligned(head)
        st = self.bus.states(head)
        center = {i: st[i]["pos"] for i in head if st.get(i)}
        self.stop(quiet=True)
        self.idle_running = True
        self._idle_thread = threading.Thread(target=self._run_idle, args=(center,), name="servo-idle", daemon=True)
        self._idle_thread.start()
        self.log(f"Lively mode started, center {center}", "Motion")
        return {"idle": True}

    def _run_idle(self, center):
        d = self.dirs()

        def head(delta, sec):
            goals = {i: max(0, min(4095, round(center[i] + deg * DEG * d.get(i, -1)))) for i, deg in delta.items() if i in center}
            if goals:
                self._profile(goals, sec, 35)
            time.sleep(sec + 0.06)

        bag = [g for w, g in GESTURES for _ in range(w)]
        last = None
        try:
            while self.idle_running and not self.s.halted:
                g = random.choice(bag)
                if g is last:
                    g = random.choice(bag)
                last = g
                for delta, sec in g(random.choice([-1, 1])):
                    if not self.idle_running or self.s.halted:
                        break
                    if delta:
                        head(delta, sec)
                    else:
                        time.sleep(sec)
                if self.idle_running:
                    time.sleep(random.uniform(0.3, 1.5))
            if not self.s.halted and self.s.state == "open":
                head({i: 0 for i in center}, 0.7)
                self.release_speed(list(center))
            self.log("Lively mode stopped, head returned to center", "Motion")
        except Refused:
            pass
        except Exception as e:  # noqa: BLE001
            self.log(f"Lively error: {type(e).__name__}: {e}", "Motion", "error", exc=True)
        finally:
            self.idle_running = False

    # ---- Center Calibration ----

    def backup_calibration(self, ids):
        rows = {}
        for i in ids:
            try:
                ver = self.bus.read(i, 0, 5)[1]
                rows[str(i)] = {"offset": self.bus.read_u16(i, 31), "pos": self.read_pos(i),
                                "torque": self.bus.read_u8(i, 40), "lock": self.bus.read_u8(i, 55),
                                "firmware": f"{ver[0]}.{ver[1]}"}
            except Exception as e:  # noqa: BLE001
                rows[str(i)] = {"error": f"{type(e).__name__}: {e}"}
                self.log(f"#{i} unreadable during backup: {type(e).__name__}: {e}", "Calibration", "warn")
        d = self.calib_dir()
        os.makedirs(d, exist_ok=True)
        now = time.time()
        fn = f"eeprom-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}{int(now * 1000) % 1000:03d}.json"
        _save_json(os.path.join(d, fn), {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "servos": rows})
        return fn, rows

    def probe_offset_sign(self, i):
        """Probe offset sign (+1 step) to see whether raw = base + offset or base - offset."""
        if self.offset_sign is not None:
            return self.offset_sign
        try:
            o0, r0 = self.read_off(i), self.read_pos(i)
            if abs(o0 + 1) > OFFSET_LIMIT:
                return None
            self.bus.unlock(i)
            self.write_off(i, o0 + 1)
            self.wait_off_change(i, o0)
            r1 = self.read_pos(i)
            self.write_off(i, o0)
            self.wait_off_change(i, o0 + 1)
            self.bus.lock(i)
            if r1 == r0:
                self.log(f"Offset sign probe: #{i} +1 did not change position ({r0}); fallback will test both", "Calibration", "warn")
                return None
            self.offset_sign = 1 if r1 > r0 else -1
            self.log(f"Offset sign probe: #{i} +1 -> {r0}->{r1} (raw = base {'+' if self.offset_sign > 0 else '-'} offset)", "Calibration")
            return self.offset_sign
        except Exception as e:  # noqa: BLE001
            self.log(f"Offset sign probe failed: {type(e).__name__}: {e}", "Calibration", "warn")
            return None

    def calibrate_by_offset(self, i, target, trail):
        """Fallback calibration by writing register 31 directly when 0x0B is unsupported."""
        o0, r0 = self.read_off(i), self.read_pos(i)
        signs = [self.offset_sign] if self.offset_sign else [1, -1]
        ok, pos, off = False, r0, o0
        try:
            for sign in signs:
                cand = o0 + sign * (target - r0)
                if abs(cand) > OFFSET_LIMIT:
                    trail.append(f"offset {cand:+d} out of range, skipped")
                    continue
                self.write_off(i, cand)
                off, _ = self.wait_off_change(i, off)
                pos = self.read_pos(i)
                trail.append(f"write offset {cand}->pos {pos}")
                if abs(pos - target) <= CALIB_TOLERANCE:
                    ok = True
                    return True, pos, off
            return False, pos, off
        finally:
            if not ok and off != o0:
                try:
                    self.write_off(i, o0)
                    off, _ = self.wait_off_change(i, off)
                    trail.append(f"failed, restored offset {o0}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"#{i} failed to restore offset after fallback error: {type(e).__name__}: {e}", "Calibration", "error")

    def calibrate_one(self, i, before, target=2048):
        """Calibrate one servo so current physical position becomes target (2048 = official zero), compensating gravity droop."""
        bus = self.bus
        was_on = before.get("torque") == 1
        old_off = before.get("offset")
        trail, step, ok, pre, pos, off, why = [], "limit_speed", False, None, None, None, ""
        method, droop = "0x0B", 0
        gains_bad = False             # Skip torque restore if MainGains write fails
        try:
            bus.write_u8(i, 41, 10)                                    # Slow accel during calibration safety
            bus.write_u16(i, 46, enc_sm(200))
            step = "unlock"
            trail.append("unlock " + _ack(bus.unlock(i)))
            step = "read_pos"
            pre = self.read_pos(i)
            o_before = self.read_off(i)
            if was_on:
                step = "torque_off"
                bus.write_u8(i, 40, 0)
                if bus.read_u8(i, 40) != 0:                           # Ensure torque is off before 0x0B
                    bus.write_u8(i, 40, 0)
                    if bus.read_u8(i, 40) != 0:
                        raise feetech.BusError("Torque-off not confirmed (readback != 0)")
                trail.append("torque_off OK")
            # Re-read before 0x0B to infer offset sign if still unknown
            # Compare pre-0x0B and post-0x0B position delta against offset delta
            post = self.read_pos(i) if was_on else pre
            step = "0x0B_calib"
            trail.append(f"0x0B→{target}" + _ack(bus.calibrate_to(i, target)))
            step = "verify_offset"
            off, changed = self.wait_off_change(i, o_before)
            pos = self.read_pos(i)
            ok = changed or abs(pre - target) <= CALIB_TOLERANCE
            if not ok:
                step = "fallback_write_31"
                trail.append("0x0B did not change offset, using reg 31 fallback")
                ok, pos, off = self.calibrate_by_offset(i, target, trail)
                method = "reg31"
            if ok:
                step = "droop_comp"
                sign = self.offset_sign
                if sign is None:
                    sign = infer_offset_sign(post, pos, off - o_before)
                    if sign is not None:
                        self.offset_sign = sign
                        trail.append(f"offset sign inferred: raw = base {'+' if sign > 0 else '-'} offset")
                if sign is None:
                    # Do not compensate droop if sign is unknown
                    droop = 0
                    if abs(pre - pos) > DROOP_TOLERANCE:
                        trail.append("offset sign unknown, skipped droop comp")
                else:
                    droop = pre - target + sign * (off - o_before)
                if abs(droop) > DROOP_TOLERANCE and abs(off - sign * droop) <= OFFSET_LIMIT:
                    self.write_off(i, off - sign * droop)
                    off, _ = self.wait_off_change(i, off)
                    pos = self.read_pos(i)
                    trail.append(f"droop {droop:+d} steps, comp {-sign * droop:+d}")
                elif abs(droop) > DROOP_TOLERANCE:
                    trail.append(f"droop {droop:+d} steps out of range, skipped")
            step = "write_goal"
            trail.append(f"goal {pos} " + _ack(bus.write_u16(i, 42, enc_sm(pos))))
            if was_on and not self.s.halted:
                step = "apply_gains_and_torque"                          # Write MainGains before enabling torque
                gains_bad = not self._gains_then_torque(i, trail)
            if not ok:
                why = f"Neither 0x0B nor reg 31 changed offset (still {fmt_off(o_before)}), pos {pos} != {target}"
        except Exception as e:  # noqa: BLE001
            ok = False
            why = f"Failed at {step}: {type(e).__name__}: {e}"
            trail.append("✗" + why)
            self.log(f"#{i} {why}", "Calibration", "error")
        finally:
            try:
                p = self.read_pos(i)
                bus.write_u16(i, 42, enc_sm(p))
                # Do not restore torque if E-Stop latched mid-calibration
                if was_on and not self.s.halted and not gains_bad and bus.read_u8(i, 40) != 1:
                    self._gains_then_torque(i, trail, "torque restored")      # Skip if MainGains fail
            except Exception as e:  # noqa: BLE001
                trail.append(f"FAIL finalize (goal/torque): {type(e).__name__}: {e}")
                self.log(f"#{i} finalize failed; torque may be off: {type(e).__name__}: {e}", "Calibration", "error")
            try:
                bus.lock(i)
                locked = bus.read_u8(i, 55)
                trail.append("lock OK" if locked == 1 else f"lock readback {locked}")
            except Exception as e:  # noqa: BLE001
                trail.append(f"FAIL lock: {type(e).__name__}: {e}")
                self.log(f"#{i} lock failed; EEPROM may remain unlocked: {type(e).__name__}: {e}", "Calibration", "error")
            try:
                self.release_speed([i])
            except Exception:  # noqa: BLE001
                pass
        gap = "" if pre is None else f", delta {(target - pre) * 360 / 4096:+.1f} deg"
        head = (f"#{i} pos {pre if pre is not None else '?'}->{pos if pos is not None else '?'} (target {target}{gap}, {method})"
                f" offset {fmt_off(old_off)}->{fmt_off(off)}" + (f" droop {droop:+d}" if droop else "")
                + f" prev_torque={'ON' if was_on else 'OFF'}")
        self.log(head + " · " + " ".join(trail) + ("" if ok else f" · FAIL: {why}"), "Calibration", "info" if ok else "warn")
        return {"ok": ok, "pre": pre, "pos": pos, "target": target, "droop": droop, "method": method, "why": why}

    def calib_plan(self, ids):
        """Read-only calibration preview: current raw vs official zero (2048) and delta degrees."""
        st = self.bus.states(ids)
        rows = []
        for i in ids:
            s = st.get(i)
            pos = s["pos"] if s else None
            rows.append({"id": i, "joint": JOINT.get(i), "pos": pos, "target": 2048,
                         "diff_deg": None if pos is None else round((2048 - pos) * 360 / 4096, 1)})
        return {"name": "Official Zero 2048", "rows": rows, "big_deg": CALIB_BIG_DEG, "sim": self.s.sim}

    def calibrate(self, ids):
        """Calibrate selected servos so their current position becomes 2048 (official zero)."""
        self.guard()
        ids = [i for i in ids if i in IDS]
        if not ids:
            raise Refused("No servos selected for calibration")
        self.stop(quiet=True)
        with self.calib_lock:
            res = self._calibrate(ids)
        if res["ok"] and not self.s.halted:
            res["hold"] = self.torque_on_aligned(res["ok"])
        self.s.refresh_static(ids)
        return res

    def _calibrate(self, ids):
        sim = self.s.sim
        self.log(f"Calibrating {len(ids)} servos to zero 2048: {ids}" + (" (sim: temp backup, poses unchanged)" if sim else ""), "Calibration")
        fn, before = self.backup_calibration(ids)
        self.log(f"Backup saved to {self.calib_dir()}{os.sep}{fn}", "Calibration")
        self.probe_offset_sign(next((i for i in ids if "offset" in before.get(str(i), {})), ids[0]))
        ok, bad, deltas, big = [], {}, {}, {}
        for i in ids:
            if self.s.latched:
                bad[i] = "Torque-off latched, remaining skipped"
                continue
            if self.s.closing:
                bad[i] = "Handing back to main, remaining skipped"
                continue
            b = before.get(str(i), {})
            if "offset" not in b:
                bad[i] = "Unreadable during backup, skipped"
                continue
            r = self.calibrate_one(i, b, 2048)
            if r["ok"]:
                ok.append(i)
                if not sim:
                    try:
                        from .commit import record_centered
                        record_centered(self.state_dir, [i], self.s.by)
                    except OSError as e:
                        self.log(f"#{i} calibrated, but failed to record in Studio state: {e}", "Calibration", "warn")
                deltas[str(i)] = r["target"] - r["pre"]
                if abs(deltas[str(i)]) * 360 / 4096 > CALIB_BIG_DEG:
                    big[i] = deltas[str(i)] * 360 / 4096
            else:
                bad[i] = r["why"]
        # Persist pose migration plan before mutating pose files
        bp = os.path.join(self.calib_dir(), fn)
        rec = _load_json(bp)
        plan = [p["file"] for p in self.list_poses() if p.get("goals")] if not sim else []
        rec.update({"ref": None, "targets": {str(i): 2048 for i in ids}, "deltas": deltas, "shifted": plan,
                    "bad": {str(k): v for k, v in bad.items()}, "sim": sim})
        _save_json(bp, rec)
        moved = self.shift_saved_poses(deltas, only=set(plan)) if not sim else []
        if moved != plan:
            rec["shifted"] = moved
            _save_json(bp, rec)
        if moved:
            self.log(f"Migrated saved poses to new zero: {moved}", "Pose")
        if big:
            self.log(f"Large calibration delta (>= {CALIB_BIG_DEG} deg): " + " ".join(f"#{i} {d:+.1f} deg" for i, d in big.items())
                     + ". If servo direction +/- is inverted, the new zero will be wrong: nudge with slider and verify 3D matches hardware."
                     "", "Calibration", "warn")
        self.log(f"Calibration complete: {len(ok)} OK {ok}" + (f"; {len(bad)} failed {sorted(bad)}" if bad else "")
                 + f"; backup {fn}", "Calibration", "warn" if bad or big else "info")
        return {"ok": ok, "bad": {str(k): v for k, v in bad.items()}, "backup": fn,
                "big": {str(k): round(v, 1) for k, v in big.items()}}

    def undo(self):
        """Restore offsets from latest calibration backup (torque off -> unlock -> write 31 -> lock)."""
        self.guard()
        self.stop(quiet=True)
        with self.calib_lock:
            res = self._undo()
        self.s.refresh_static([int(k) for k in res.get("ok", [])])
        return res

    def _undo(self):
        bus = self.bus
        d = self.calib_dir()
        files = sorted(f for f in os.listdir(d) if f.startswith("eeprom-")) if os.path.isdir(d) else []
        if not files:
            raise Refused("No calibration backup to undo" + (" (sim mode only undoes sim calibrations)" if self.s.sim else ""))
        fn = files[-1]
        rec = _load_json(os.path.join(d, fn))
        self.log(f"Undoing calibration: restoring {fn} ({rec.get('time', '?')})", "Calibration")
        ok, bad = [], {}
        for k, row in rec["servos"].items():
            i = int(k)
            if "offset" not in row:
                self.log(f"#{i} had no offset in backup ({row.get('error', '?')}), skipped", "Calibration", "warn")
                continue
            trail, step, on = [], "read_state", False
            gains_bad = False         # Skip torque restore if MainGains fail
            try:
                on = bus.read_u8(i, 40) == 1
                off0, p0 = bus.read_u16(i, 31), self.read_pos(i)
                if on:
                    step = "torque_off"
                    bus.write_u8(i, 40, 0)
                    if bus.read_u8(i, 40) != 0:
                        bus.write_u8(i, 40, 0)
                        if bus.read_u8(i, 40) != 0:
                            raise feetech.BusError("Torque-off not confirmed (readback != 0)")
                    trail.append("torque_off OK")
                step = "unlock"
                trail.append("unlock " + _ack(bus.unlock(i)))
                step = "write_offset"
                trail.append(f"write offset {fmt_off(row['offset'])} " + _ack(bus.write_u16(i, 31, row["offset"])))
                step = "readback"
                self.wait_off_change(i, feetech.sign15(off0))
                off1, p1 = bus.read_u16(i, 31), self.read_pos(i)
                step = "write_goal"
                trail.append(f"goal {p1} " + _ack(bus.write_u16(i, 42, enc_sm(p1))))
                if on and not self.s.halted:
                    step = "apply_gains_and_torque"                      # Write MainGains before enabling torque
                    gains_bad = not self._gains_then_torque(i, trail)
                good = off1 == row["offset"]
                if good:
                    ok.append(i)
                else:
                    bad[i] = f"offset wrote {row['offset']} read back {off1}"
                self.log(f"#{i} offset {fmt_off(off0)}->{fmt_off(off1)} (backup {fmt_off(row['offset'])}) pos {p0}->{p1} · "
                         + " ".join(trail), "Calibration", "info" if good else "warn")
            except Exception as e:  # noqa: BLE001
                bad[i] = f"Failed at {step}: {type(e).__name__}: {e}"
                self.log(f"#{i} undo {bad[i]}", "Calibration", "error")
            finally:
                try:
                    p = self.read_pos(i)
                    bus.write_u16(i, 42, enc_sm(p))
                    if on and not self.s.halted and not gains_bad and bus.read_u8(i, 40) != 1:
                        self._gains_then_torque(i, trail, "torque restored")  # Skip if MainGains fail
                except Exception as e:  # noqa: BLE001
                    self.log(f"#{i} undo finalize failed; torque may be off: {type(e).__name__}: {e}", "Calibration", "error")
                try:
                    bus.lock(i)
                except Exception as e:  # noqa: BLE001
                    self.log(f"#{i} lock failed: {type(e).__name__}: {e}", "Calibration", "error")
        if not rec.get("sim") and "shifted" in rec:
            later, t0 = [], rec.get("time", "")
            for p in self.list_poses():
                if p["file"] not in rec["shifted"] and p.get("goals") and (p.get("time") or "") >= t0[:16]:
                    later.append(p["file"])
            moved = self.shift_saved_poses(rec.get("deltas", {}), sign=-1, only=set(rec["shifted"]) | set(later))
            if moved:
                self.log(f"Reverted saved poses to prior zero: {moved}", "Pose")
        if ok and not rec.get("sim"):
            try:
                from .commit import forget_centered
                gone = forget_centered(self.state_dir, ok)
                if gone:
                    self.log(f"Removed {len(gone)} undone servos from calibrated list: {gone}", "Calibration")
            except OSError as e:
                self.log(f"Failed to remove undone servos from Studio calibrated list: {type(e).__name__}: {e}", "Calibration", "warn")
        os.rename(os.path.join(d, fn), os.path.join(d, "undone-" + fn))
        self.log(f"Undo complete ({fn} -> undone-{fn}): {len(ok)} restored" + (f"; failed {sorted(bad)}" if bad else ""),
                 "Calibration", "warn" if bad else "info")
        return {"ok": ok, "bad": {str(k): v for k, v in bad.items()}, "backup": fn}

    # ---- Poses ----

    def list_poses(self):
        out = []
        d = self.poses_dir()
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    try:
                        p = _load_json(os.path.join(d, fn))
                        out.append({"file": fn, "name": p.get("name", fn[:-5]), "time": p.get("time", ""),
                                    "goals": {r["id"]: r["pos"] for r in p.get("rows", [])}})
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Pose file {fn} unreadable: {type(e).__name__}: {e}", "Pose", "warn")
        return out

    def save_pose(self, name):
        st = self.bus.states(self.present())
        rows = [{"id": i, "joint": JOINT.get(i), "pos": s["pos"], "deg": round((s["pos"] - 2048) * 360 / 4096, 1)}
                for i, s in st.items() if s]
        os.makedirs(self.poses_dir(), exist_ok=True)
        safe = "".join(c for c in str(name) if c not in '\\/:*?"<>|').strip()[:40] or "pose"
        fn = f"{safe}-{time.strftime('%Y-%m-%d')}.json"
        _save_json(os.path.join(self.poses_dir(), fn), {"name": safe, "time": time.strftime("%Y-%m-%d %H:%M"), "rows": rows})
        missing = [i for i, v in st.items() if not v]
        self.log(f"Pose saved: {fn} ({len(rows)} servos)" + (f"; unreadable {missing}" if missing else ""), "Pose",
                 "warn" if missing else "info")
        return {"file": fn}

    def delete_pose(self, fn):
        """Move pose file to trash with timestamp prefix instead of hard deleting."""
        fn = str(fn or "")
        if not fn or os.path.basename(fn) != fn or not fn.endswith(".json"):
            raise Refused(f"Invalid pose file {fn!r}")
        src = os.path.join(self.poses_dir(), fn)
        if not os.path.isfile(src):
            raise Refused(f"Pose not found: {fn}")
        trash = os.path.join(self.poses_dir(), ".trash")
        os.makedirs(trash, exist_ok=True)
        os.replace(src, os.path.join(trash, f"{time.strftime('%Y%m%d-%H%M%S')}-{fn}"))
        self.log(f"Pose deleted: {fn} (moved to trash)", "Pose")
        return {}

    def go_pose(self, fn, seconds, mode, acc):
        p = next((x for x in self.list_poses() if x["file"] == fn), None)
        if not p:
            raise Refused(f"Pose not found: {fn}")
        self.log(f"Moving to pose '{p['name']}' ({seconds:g}s)", "Motion")
        return self.move(p["goals"], seconds, mode, acc)

    def shift_saved_poses(self, deltas, sign=1, only=None):
        """Shift saved pose raw positions by delta after re-calibration."""
        changed = []
        d = self.poses_dir()
        if not os.path.isdir(d) or not any(deltas.values()):
            return changed
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or (only is not None and fn not in only):
                continue
            path = os.path.join(d, fn)
            pose = _load_json(path)
            hit = False
            for r in pose.get("rows") or []:
                dl = deltas.get(str(r.get("id")))
                if dl:
                    r["pos"] = int(r["pos"]) + sign * int(dl)
                    r["deg"] = round((r["pos"] - 2048) * 360 / 4096, 1)
                    hit = True
            if hit:
                _save_json(path, pose)
                changed.append(fn)
        return changed

    # ---- Bus Tools ----

    def scan(self, ids):
        found = [i for i in ids if self.bus.ping(i) is not None]
        lost = [i for i in ids if i not in found] if len(ids) <= 32 else []
        self.log(f"Scan {ids[0]}..{ids[-1]}: {len(found)} online {found}" + (f"; offline {lost}" if lost else ""),
                 "Bus", "warn" if lost else "info")
        return {"found": found, "lost": lost}

    def find(self):
        """Scan all bus IDs 0-253 and report firmware versions (useful for newly installed ID-1 servos)."""
        found = []
        for i in range(254):
            if self.bus.ping(i) is None:
                continue
            row = {"id": i, "known": i in IDS, "joint": JOINT.get(i), "kind": "imu" if i == IMU_BUS_ID else "servo"}
            try:
                r = self.bus.read(i, 0, 5)[1]
                row["firmware"], row["model"] = f"{r[0]}.{r[1]}", f"{r[3]}.{r[4]}"
            except feetech.BusError:
                pass                                     # List servo even if version read fails
            found.append(row)
        have = {f["id"] for f in found}
        missing = [i for i in IDS if i not in have]
        new = [f["id"] for f in found if f["kind"] == "servo" and not f["known"]]
        self.log(f"Find servos 0-253: {len(found)} online {sorted(have)}" + (f"; unassigned {new}" if new else "")
                 + (f"; missing from 15 slots: {missing}" if missing else ""), "Bus", "warn" if new or missing else "info")
        return {"found": found, "missing": missing}

    def dump(self, sid):
        d = self.bus.dump(int(sid))
        r = d["raw"]
        self.log(f"Dump #{sid}: fw {r[0]}.{r[1]} model {r[3]}.{r[4]} mode={r[33]} phase={r[18]} P/D/I={r[21]}/{r[22]}/{r[23]}"
                 f" (active {r[50]}/{r[51]}/{r[52]}, limit {r[48] | r[49] << 8}) "
                 f"offset={fmt_off(r[31] | r[32] << 8)} torque={r[40]} lock={r[55]} ack={r[8]}", "Register")
        return {"id": int(sid), **d}

    def write_reg(self, sid, addr, size, value, unlock):
        self.guard()
        sid, addr, size, value = int(sid), int(addr), int(size), int(value)
        if addr == 40:
            raise Refused("Use the torque toggle on the page instead of writing reg 40")
        if size not in (1, 2) or not 0 <= addr <= 86:
            raise Refused("Address must be 0-86, size 1 or 2 bytes")
        bus = self.bus
        name = feetech.REGISTERS.get(addr, ("?",))[0]
        rd = bus.read_u8 if size == 1 else bus.read_u16
        old = rd(sid, addr)
        if unlock:
            bus.unlock(sid)
        try:
            err = (bus.write_u8 if size == 1 else bus.write_u16)(sid, addr, value)
        finally:
            if unlock:
                bus.lock(sid)
        new = rd(sid, addr)
        same = new == value & (0xFF if size == 1 else 0xFFFF)
        self.log(f"#{sid} write {addr} '{name}' {old} -> {value}{_ack(err)}, readback {new}" + (" (unlocked, re-locked)" if unlock else "")
                 + ("" if same else " (readback mismatch: read-only or out of range?)"), "Register", "info" if same else "warn")
        touched = sorted(set(range(addr, addr + size)) & set(GAIN_REGS))
        if touched:
            # Track manual P/D/limit overrides for UI warning
            self.manual.setdefault(sid, {})[addr] = new
            g = self.gains.get(max_age=GAINS_RECHECK_S)
            eeprom = all(a in (21, 22) for a in touched)
            self.log(f"#{sid} manually modified {', '.join(GAIN_REGS[a] for a in touched)} (reg {addr} = {new}), differs from main"
                     f" (P{g['kp']} · D{g['kd']} · Limit {g['limit']})"
                     + (": EEPROM value takes effect on power cycle; Studio writes RAM (50/51/48) on torque-on" if eeprom else
                        ": active while torqued; Studio will restore main gains on next torque-on"), "Register", "warn")
        self.s.refresh_static([sid])
        return {"old": old, "new": new, "same": same}

    def reboot(self, sid):
        sid = int(sid)
        self.bus.write_u8(sid, 40, 0)
        self.bus.reboot(sid)
        self.log(f"#{sid} reboot 0x08 (torque off, returns in ~0.8s)", "Register")
        return {}

    def set_id(self, old, new):
        self.guard()
        old, new = int(old), int(new)
        if not 0 <= new <= 253:
            raise Refused("New ID must be 0-253")
        if IMU_BUS_ID in (old, new):
            raise Refused(f"ID {IMU_BUS_ID} is reserved for the IMU board on the bus")
        if self.bus.ping(old) is None:
            raise Refused(f"Servo #{old} not found on bus: run Find Servos first")
        if new != old and self.bus.ping(new) is not None:
            raise Refused(f"ID {new} is already in use on the bus")
        ok = self.bus.set_id(old, new)
        self.log(f"Set ID {old} -> {new}: " + ("new ID responds to ping" if ok else "new ID did not respond"), "Register", "info" if ok else "error")
        self.s.refresh_static([new])
        return {"ok": ok, "old": old, "new": new}

    def timing(self, n=200):
        """Run sync_read across all online servos n times to measure latency and failures."""
        ids = self.present()
        times, fails = [], 0
        with self.calib_lock:                      # Pause polling to measure raw bus timing
            for _ in range(int(n)):
                t = time.perf_counter()
                r = self.bus.sync_read(ids, 56, 15)
                times.append((time.perf_counter() - t) * 1000)
                fails += sum(1 for v in r.values() if v is None)
        times.sort()
        res = {"n": n, "ids": len(ids), "min_ms": round(times[0], 2), "avg_ms": round(sum(times) / n, 2),
               "p99_ms": round(times[max(0, int(n * 0.99) - 1)], 2), "max_ms": round(times[-1], 2), "fails": fails}
        self.log(f"Timing test: {res}", "Bus", "warn" if fails else "info")
        return res

    def set_dir(self, sid, d):
        sid, d = int(sid), (-1 if int(d) < 0 else 1)
        self.s.directions[sid] = d
        path = os.path.join(self.state_dir, "servo-directions.json")
        os.makedirs(self.state_dir, exist_ok=True)
        _save_json(path, {str(k): v for k, v in self.s.directions.items()})
        self.log(f"Direction #{sid} -> {d:+d} (saved to servo-directions.json)", "Direction")
        return {"dirs": {str(k): v for k, v in self.s.directions.items()}}

    # ---- UI Commands ----

    def handle(self, args, tok=None):
        """Handle UI servo command (runs in worker thread). Raises Refused on invalid state/args."""
        if tok is not None:
            return self._handle(args)
        with self.s.op_slot(args.get("op")):
            return self._handle(args)

    def _handle(self, args):
        op = args.get("op")
        sec = float(args.get("seconds", 2))
        mode = "stream" if args.get("mode") == "stream" else "profile"
        acc = int(args.get("acc", 30))
        if op == "torque":
            ids = [int(i) for i in (args.get("ids") or [])] or self.present()
            if int(args.get("on", 0)) and self.s.latched and self.s.closing:
                raise Refused("Handing back to main: torque-off is locked")       # Reject torque-on while closing
            if int(args.get("on", 0)) and self.s.latched:
                # Manual torque-on/hold unlatches E-Stop
                self.s.latched = False
                self.log("Manual torque-on: E-Stop latch cleared", "Safety")
            return self.torque_on_aligned(ids) if int(args.get("on", 0)) else self.torque_off(ids)
        if op == "goal":
            return self.goal(args["id"], args["pos"])
        if op == "move":
            return self.move(args.get("goals") or {}, sec, mode, acc)
        if op == "stop":
            return self.stop()
        if op == "official":
            return self.go_official(args.get("key"), sec, mode, acc)
        if op == "idle":
            return self.idle(bool(args.get("on")))
        if op == "calib_plan":
            return self.calib_plan(parse_ids(args.get("ids") or "20-24,30-34,10-14"))
        if op == "calibrate":
            return self.calibrate(parse_ids(args.get("ids") or "20-24,30-34,10-14"))
        if op == "undo":
            return self.undo()
        if op == "poses":
            return {"poses": self.list_poses()}
        if op == "save_pose":
            self.save_pose(args.get("name") or "pose")
            return {"poses": self.list_poses()}
        if op == "delete_pose":
            self.delete_pose(args.get("file"))
            return {"poses": self.list_poses()}
        if op == "go_pose":
            return self.go_pose(args.get("file"), sec, mode, acc)
        if op == "scan":
            return self.scan(parse_ids(args.get("ids") or "20-24,30-34,10-14"))
        if op == "scan_all":
            return self.scan(list(range(0, 254)))
        if op == "find":
            return self.find()
        if op == "dump":
            return self.dump(args["id"])
        if op == "write_reg":
            return self.write_reg(args["id"], args["addr"], args.get("size", 1), args["value"], bool(args.get("unlock")))
        if op == "reboot":
            return self.reboot(args["id"])
        if op == "set_id":
            return self.set_id(args["old"], args["new"])
        if op == "timing":
            return self.timing(int(args.get("n", 200)))
        if op == "set_dir":
            return self.set_dir(args["id"], args["dir"])
        if op == "unlatch":
            self.s.latched = False
            self.log("E-Stop latch cleared: torque-on enabled", "Safety")
            return {}
        if op == "info":
            return {"danger": {str(k): v for k, v in DANGER.items()},
                    "registers": {str(k): v for k, v in feetech.REGISTERS.items()},
                    "official": {k: v[0] for k, v in OFFICIAL.items()}}
        raise Refused(f"Unknown command: {op}")


def _save_json(path, obj):
    """Atomic write via .tmp + rename."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
