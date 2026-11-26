"""Gamepad drive, virtual sticks, posture commands, skills, sounds, and action combos.

PadOps (pad.py) inherits from this mixin, sharing the state lock, robotd readback, and logging.
Safety rules:
- **Lease (steering wheel)**: Pages driving sticks, holding triggers/X, or running actions hold the lease;
  other pages must click 'Take Over'. Inactive lease expires after 6s.
- **Active drive area**: Pages send drive {open, seq} on entering the gamepad area and drive {on:false, seq} on leaving.
- **Deadman**: Pages send sticks at 10 Hz while held and stop on release; robotd has a 500 ms deadman.
- **Latched head & body**: robot.head and robot.pose do not expire in robotd; pose active:true forces standing network.
- **Watchdogs**: Non-zero head/body without updates for 0.6s re-center; mode without heartbeat for 3s exits; held keys timeout at 0.45s (0.3s for X).
- **Physical gamepad mirror**: When physical gamepad is attached, web sticks/keys/mode/SVG buttons are rejected.
- **Post-E-Stop 2s guard**: Rejects stand/walk/sit/skills/sticks/actions for 2s after torque-off.
"""
import itertools
from contextlib import nullcontext
import json
import math
import os
import re
import threading

from . import pad_combo as C
from .estop import STARTING_PHASES, relaxed, torque_off
from .log import log_cmd
from .pad_combo import Refused
from .rpc import RpcError, Timeout, Unreachable
from .updates import neutral_robot_command

MAX_V = 0.3
MAX_W = 1.5
V_CEIL, W_CEIL = 0.6, 3.0
HEAD_RAD = 2.5
HEAD_CEIL = 2.5
HEAD_CHOICES = (0.5, 1.0, 2.5)
BODY_Z_UP, BODY_Z_DOWN, BODY_ANGLE = 0.010, 0.025, 0.2618
BODY_TRAIN_ANGLE = 0.26
DEADZONE = 0.1
LEASE_S = 6.0
RELAX_GUARD_S = 2.0
HOLD_S = 0.45
KEYS_HOLD_S = 0.45
X_HOLD_S = 0.3
STICK_STALE_S = 0.6
MODE_HB_S = 3.0
TICK_S = 0.04
RELAX_CLEAR_DELAY_S = 0.1
MOVE_MIN_GAP_S = 0.04
WALK_STOP_WAIT_S = 1.0
WALK_VERIFY_S = 3.0
WALK_VERIFY_POLL_S = 0.1
CMD_TIMEOUT_S = 3.0
BINDINGS_STALE_S = 10.0
LOW_BATTERY = 10
PADD_UNIT = "/etc/systemd/system/padd.service"
PADD_DROPIN_DIR = "/etc/systemd/system/padd.service.d"
PADD_INPUT_ACK = "/run/padd/input-settings.json"
FAST_OPS = ("move", "stop", "sticks", "keys", "mode", "action_hold", "action_stop", "drive", "watch")
FAST_WORKERS = 4
GONE_KEEP = 500
INF = float("inf")

POSTURE_CN = {"absent": "robotd stopped", "starting": "robotd starting", "commissioning": "Commissioning", "sitting": "Sitting",
              "limp": "Torque Off", "walking": "Walking", "policy": "Standing (Ready to Walk)", "holding": "Standing (Hold)",
              "homing": "Standing up", "unknown": "Posture unknown", "servo_session": "Servo Tuning"}
DRIVING = ("policy", "walking")
MODES = ("drive", "head", "body")
MODE_CN = {"drive": "Walk Mode", "head": "Head Mode", "body": "Body Mode"}
DRIVE_MODE_CN = {"walk": "Walk", "roller": "Roller"}
HELD_KEYS = ("lt", "rt", "x")
KEY_CN = {"a": "A", "x": "X", "lb": "LB", "rb": "RB", "dpad_down": "D-Pad Down", "lt": "LT", "rt": "RT"}
BIND_KEYS = ("a", "x", "lb", "rb", "dpad_down")
DEFAULT_BIND = {"a": "ground_pick", "x": "roulade", "lb": "kick_left", "rb": "kick_right", "dpad_down": "sit_toggle"}
HEAD_ZERO = {"neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.0, "head_roll": 0.0}
POSE_OFF = {"active": False}
POSE_CENTER = {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": True}
_FLAG = re.compile(r"--(max-linear-backward|max-linear|max-angular)(?:=|\s+)(\S+)")
_FLAG_HEAD = re.compile(r"--max-head(?:=|\s+)(\S+)")
_FLAG_PATH = re.compile(r"--(input-settings|config)(?:=|\s+)(\S+)")
NO_SIT_INFO = "Cannot read sitting state (robot.policies missing or stale): wait a few seconds and retry"
MIRROR_SVG = "Physical gamepad is connected: on-screen controls mirror it and do not accept input"
MODE_IDLE = "Policy is not running: sticks will not move head/body (Y/B still switch modes; click 'Start Walking' to activate)"


def plain_refusal(reason):
    """Translate robotd refusal message to clear user-facing text."""
    r = str(reason or "")
    low = r.lower()
    if "policy is not driving" in low:
        return "Policy is not running: click 'Start Walking' first (or press Start twice on gamepad)"
    if "home pose" in low and "still" in low:
        return "Still returning to standing posture; wait a second and retry"
    if "no skill named" in low:
        return f"This duck does not have this skill ({r})"
    if "no skills configured" in low:
        return "No skills configured on this duck"
    if "no voice" in low:
        return "No voice available: audio is off or sound bank is empty (run sounds ensure-bank on the duck)"
    if "read-only" in low:
        return "robotd is currently read-only (commissioning mode); motion commands rejected"
    if "roller onnx models are not installed" in low:
        return "Roller ONNX models are not installed on this duck: staying in Walk mode"
    if "nothing to switch between" in low:
        return "No walking policies installed to switch between"
    if "requires stationary home" in low:
        return "Must be standing stationary at home (no active skill) to switch modes"
    return f"robotd refused: {r}" if r else "robotd refused (no reason given)"


def _exec_line(texts):
    """Find the last effective ExecStart in padd unit + drop-ins [(path, text)]."""
    exec_line, exec_src = None, None
    for path, text in texts:
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if line.startswith("ExecStart="):
                val = line[len("ExecStart="):].strip()
                exec_line, exec_src = (val or None), (path if val else None)
    return exec_line, exec_src


def parse_speed_limits(texts):
    """Parse padd unit/drop-ins [(path, text)] -> {"v", "w", "src"}."""
    exec_line, exec_src = _exec_line(texts)
    found, bad = {}, []
    for k, v in _FLAG.findall(exec_line or ""):
        try:
            x = float(v)
        except ValueError:
            bad.append(f"--{k} {v}")
            continue
        if math.isfinite(x) and x > 0:
            found[k] = x
        else:
            bad.append(f"--{k} {v}")
    v = min([found[k] for k in ("max-linear", "max-linear-backward") if k in found] or [MAX_V])
    w = found.get("max-angular", MAX_W)
    if found:
        src = f"padd config ({exec_src})"
    elif texts:
        src = "Default (unmodified in padd.service)"
    else:
        src = "Default (padd config unreadable)"
    if bad:
        src += f"; ignored invalid flags: {', '.join(bad)}"
    capped = v > V_CEIL or w > W_CEIL
    return {"v": min(v, V_CEIL), "w": min(w, W_CEIL), "src": src + (" (clamped to safety ceiling)" if capped else "")}


def parse_padd_inputs(texts):
    """Parse --max-head, --input-settings, --config from ExecStart."""
    exec_line, exec_src = _exec_line(texts)
    out = {"max_head": None, "input_settings": None, "config": None, "bad": [], "src": exec_src}
    for v in _FLAG_HEAD.findall(exec_line or ""):
        try:
            x = float(v)
        except ValueError:
            out["bad"].append(f"--max-head {v}")
            continue
        if math.isfinite(x) and x > 0:
            out["max_head"] = x
        else:
            out["bad"].append(f"--max-head {v}")
    for k, v in _FLAG_PATH.findall(exec_line or ""):
        out["input_settings" if k == "input-settings" else "config"] = v
    return out


def _padd_settings(d):
    """Validate padd input settings JSON: exact keys mouth_percent (0-100) and head_rad (0.5/1.0/2.5)."""
    if not isinstance(d, dict) or set(d) != {"mouth_percent", "head_rad"}:
        return None
    m, h = d["mouth_percent"], d["head_rad"]
    if not (isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m) and 0 <= m <= 100):
        return None
    if not (isinstance(h, (int, float)) and not isinstance(h, bool) and float(h) in HEAD_CHOICES):
        return None
    return float(h), float(m)


def input_limits(inputs, settings_text=None, settings_error=None, ack=None):
    """Compute head/mouth limits matching padd's rules -> {"head", "mouth", "src"}."""
    head = inputs.get("max_head") or HEAD_RAD
    src = f"--max-head {head:g}" if inputs.get("max_head") else "Default (head 2.5 rad, mouth 100%)"
    mouth = 100.0
    path = inputs.get("input_settings")
    got = _padd_settings(ack) if path and ack is not None else None
    if got is not None:
        head, mouth = got
        src = f"Active in padd ({PADD_INPUT_ACK})"
    elif path:
        if settings_error is not None:
            src += f"; {path} unreadable ({settings_error}), using fallback"
        else:
            try:
                got = _padd_settings(json.loads(settings_text or ""))
            except ValueError:
                got = None
            if got is not None:
                head, mouth = got
                src = f"padd input settings ({path})"
            else:
                src += f"; {path} rejected by padd (requires exact keys mouth_percent 0-100, head_rad in 0.5/1.0/2.5), using fallback"
    if inputs.get("bad"):
        src += f"; ignored invalid flags: {', '.join(inputs['bad'])}"
    if head > HEAD_CEIL:
        head, src = HEAD_CEIL, src + f", head clamped to {HEAD_CEIL:g} rad"
    return {"head": head, "mouth": mouth, "src": src}


def _dz(v):
    return 0.0 if abs(v) < DEADZONE else v


def sticks_to_intent(mode, lx, ly, rx, ry, lim, head_rad):
    """Map normalized sticks (-1..1, +x right, +y down) to robotd intent (method, params), matching padd signs."""
    LX, LY, RX, RY = _dz(lx), -_dz(ly), _dz(rx), -_dz(ry)
    r = lambda x: round(x, 4) + 0.0    # noqa: E731 -- +0.0 strips -0.0
    if mode == "head":
        return "robot.head", {"neck_pitch": r(RY * head_rad), "head_pitch": r(-LY * head_rad),
                              "head_yaw": r(-LX * head_rad), "head_roll": r(RX * head_rad)}
    if mode == "body":
        z = LY * (BODY_Z_UP if LY >= 0 else BODY_Z_DOWN)
        clampa = lambda a: max(-BODY_TRAIN_ANGLE, min(BODY_TRAIN_ANGLE, a))   # noqa: E731
        return "robot.pose", {"z": r(max(-BODY_Z_DOWN, min(BODY_Z_UP, z))), "roll": r(clampa(RX * BODY_ANGLE)),
                              "pitch": r(clampa(RY * BODY_ANGLE)), "active": True}
    v, w = lim["v"], lim["w"]
    return "robot.move", {"vx": r(LY * v), "vy": r(-LX * v), "vyaw": r(-RX * w)}


class _Abort(Exception):
    pass


class DriveMixin:
    # ================= Initialization =================

    def _drive_init(self, state_dir):
        self.state_dir = state_dir
        self._send_lk = threading.Lock()      # Serializes check+send to robotd (acquire before _lk)
        self._seqs = {}                       # conn -> {"hi", "stop", "life", "mode", "latest":{chan}, "sent":{chan}}
        self._gone = {}                       # Disconnected conns -> True (never regain lease)
        self._closed = {}                     # Conns outside gamepad area -> True
        self._halt_after_gone = {}
        self._conn_ids = itertools.count(1)
        self._last_sent_at = {}               # chan -> last sent monotonic time
        self._web_driving = False
        self.virtual = {"owner": None, "last": [0.0, 0.0, 0.0], "at": None}
        self._driver = None                   # {"conn", "by", "since", "seen"}
        self._cmd_busy = False
        self._last_cmd = None
        self._relax_presses = None
        self._relax_noted = 0
        self._relax_at = -1e9
        self._torque_was = None
        self._action = None
        self._last_action = None
        self._pad_said = False
        self._limits = parse_speed_limits([])
        self._inputs = input_limits({})
        self._imu_head, self._imu_head_why = None, None
        self.list_dir = os.listdir
        self._user_combos = C.load_user_combos(state_dir, self.log)
        self._latch = {"head": None, "body": None}
        self._modes = {}
        self._mode_exit = {}
        self._clears = []
        self._keys = {}
        self._x_armed = {}
        self._was_driving = False
        self._pose_reset = "pending"          # Send robot.pose {"active": false} once on first robotd connect
        self._pose_reset_tries = 0
        self._bindings_due = 0.0
        self._tick_evt = threading.Event()
        self._tick_stop = threading.Event()
        self._tick_said = set()
        self._tick_thread = None

    def _drive_start(self):
        """Start 25 Hz background tick thread (called at end of PadOps.__init__)."""
        self._tick_thread = threading.Thread(target=self._tick_loop, name="pad-tick", daemon=True)
        self._tick_thread.start()

    def new_conn(self):
        """Assign monotonic connection ID; new connections start outside gamepad drive area (closed)."""
        conn = f"ws{next(self._conn_ids)}"
        with self._lk:
            self._remember(self._closed, conn)
        return conn

    def executor_for(self, args):
        """Route fast drive/motion heartbeats to dedicated 4-worker pool."""
        fast_motion = (args or {}).get("op") == "motion" and (args or {}).get("action") in ("heartbeat", "stop", "pause", "record_pause", "idle_stop")
        return self._fast_pool if (args or {}).get("op") in FAST_OPS or fast_motion else None

    # ================= Lease & Ownership =================

    def _pad_attached(self, now):
        """Check if physical gamepad is connected: (True/False/None, detail)."""
        if self.tap is not None and self._tap_on and self.tap_state == "attached":
            alive = getattr(self.tap, "alive", None)
            if alive is None or alive():
                return True, "Physical gamepad detected via padd tap"
        st = self._status if isinstance(self._status, dict) else None
        fresh = st is not None and self._status_at is not None and now - self._status_at < 15
        if fresh:
            if any(isinstance(p, dict) and p.get("connected") for p in st.get("pads") or []) and st.get("driver") == "active":
                return True, "configd reports gamepad connected and padd active"
            return False, None
        if self._worker_error:
            return None, f"Gamepad status worker error ({self._worker_error}); refresh page to retry"
        if self.tap is not None and self._tap_on and self.tap_state in ("denied", "error"):
            return None, f"configd and button tap unreadable ({self.tap_why or self.tap_state})"
        return False, None

    def _owner(self, now):
        if self._pad_attached(now)[0]:
            return "pad"
        if self.virtual["at"] is not None and now - self.virtual["at"] < 0.6 and self._web_driving:
            return "web"
        return None

    def _lease_holder(self, now):
        d = self._driver
        return d if d is not None and now - d["seen"] < LEASE_S else None

    def _seq_of(self, conn):
        q = self._seqs.get(conn)
        if q is None:
            q = self._seqs[conn] = {"hi": -1, "stop": -1, "life": -1, "mode": -1, "keys": -1, "latest": {}, "sent": {}}
            if len(self._seqs) > GONE_KEEP * 2:
                for k in list(self._seqs)[:GONE_KEEP]:
                    if k != conn and (self._driver or {}).get("conn") != k:
                        self._seqs.pop(k, None)
        return q

    def _remember(self, table, conn):
        table[conn] = True
        while len(table) > GONE_KEEP:
            table.pop(next(iter(table)))

    def _take_lease(self, conn, by, now, force=False, reopen=False):
        """Acquire steering lease (call under _lk)."""
        if conn in self._gone:
            raise Refused("This page has disconnected")
        if conn in self._closed and not reopen:
            raise Refused("Page is not in the gamepad control section: scroll to Remote Control to drive")
        d = self._driver
        if d is not None and d["conn"] == conn:
            d["seen"] = now
            if by:
                d["by"] = by
            if reopen:
                self._closed.pop(conn, None)
            return None
        held = self._lease_holder(now)
        if held is not None and not force:
            raise Refused(f"{held['by'] or 'Another page'} is driving ({now - held['since']:.0f}s). Click 'Take Over' to control")
        if reopen:
            self._closed.pop(conn, None)
        self._driver = {"conn": conn, "by": by, "since": now, "seen": now}
        if d is not None and d["conn"] != conn and held is None:
            self.log.info(f"Web control transferred to {by or 'page'} (previous {d['by'] or 'page'} inactive for {now - d['seen']:.0f}s)",
                          cat="Safety", dedup=False)
            self._request_clear(d["conn"], "Lease expired after 6s inactivity")
        return held or d

    def _touch_lease(self, conn, now):
        d = self._driver
        if d is not None and d["conn"] == conn:
            d["seen"] = now

    def _drive(self, args, conn, by):
        """Handle drive open, leave (on:false), and takeover (on:true, force)."""
        now = self.clock()
        seq = self._seq(args)
        if args.get("open"):
            with self._lk:
                if conn in self._gone:
                    raise Refused("This page has disconnected")
                q = self._seq_of(conn)
                if seq is not None and seq <= q["life"]:
                    return {"dropped": "Stale open (page left/returned later)"}
                if seq is not None:
                    q["life"] = seq
                self._closed.pop(conn, None)
            return {"open": True}
        if args.get("on"):
            with self._lk:
                attached, _ = self._pad_attached(now)
                if attached:
                    raise Refused("Physical gamepad is connected: web sticks mirror it only")
                q = self._seq_of(conn)
                if seq is not None and seq <= q["life"]:
                    return {"dropped": "Stale drive on (page left gamepad section)"}
                if seq is not None:
                    q["life"] = seq
                prev = self._take_lease(conn, by, now, force=bool(args.get("force")), reopen=True)
            if prev is not None and prev.get("conn") != conn:
                self.log.warn(f"{by or 'A page'} took over remote control (was {prev.get('by') or 'another page'})", cat="Safety",
                              hint="Sticks, buttons, and actions on the previous page are now disabled", dedup=False)
                self._halt(f"Taken over by {by or 'another page'}", prev.get("conn"))
            return {"driver": by}
        with self._lk:
            q = self._seq_of(conn)
            if seq is not None and seq <= q["life"]:
                return {"dropped": "Stale drive off (page returned later)"}
            if seq is not None:
                q["life"] = seq
            if conn not in self._gone:
                self._remember(self._closed, conn)
            top = max(q["hi"], seq if seq is not None else -1)
            q["stop"] = max(q["stop"], top)
            q["hi"] = max(q["hi"], top)
            mine = self._driver is not None and self._driver["conn"] == conn
            background_music = self.motion_studio.background_music(conn)
            if mine and not background_music:
                self._driver = None
        if background_music:
            pass
        elif mine:
            self._halt("Left gamepad section", conn)
        else:
            self._request_clear(conn, "Left gamepad section")
            self._flush_clears(force=True)
        why = str(args.get("why") or "")[:40]
        self.log.info(f"{by or 'Page'} left gamepad section: {why or 'no reason given'}", cat="Gamepad")
        return {}

    def mark_gone(self, conn):
        """Called synchronously on WebSocket disconnect (in-memory only)."""
        if conn is None:
            return False
        with self._lk:
            if conn in self._gone:
                return False
            self._remember(self._gone, conn)
            self.motion_studio.invalidate("Page disconnected", conn)
            self._closed.pop(conn, None)
            self._seq_of(conn)["stop"] = INF
            mine = self._driver is not None and self._driver["conn"] == conn
            if mine:
                self._driver = None
            a = self._action
            act = a is not None and a["conn"] == conn
            if act:
                a["why"] = a["why"] or "Page disconnected"
                a["stop"].set()
            self._request_clear(conn, "Page disconnected")
            self._mode_exit.pop(conn, None)
            need = (mine and self._web_driving) or act
            self._halt_after_gone[conn] = need
        return need

    def drop_client(self, conn, by=None):
        """Called in worker thread after mark_gone to send stop and clear latched head/body."""
        with self._lk:
            need = self._halt_after_gone.pop(conn, None)
        if need is None:
            self.mark_gone(conn)
            with self._lk:
                need = self._halt_after_gone.pop(conn, False)
        if need:
            self.log.info(f"Page disconnected ({by or conn}): releasing web control and sending stop", cat="Safety", dedup=False)
            self._halt("Page disconnected", conn)
        else:
            self._flush_clears(force=True)

    def _halt(self, why, conn=None, clear_all=False):
        """Stop web driving, abort actions, clear head/body latches, and send zero velocity."""
        self.motion_studio.invalidate(why, None if clear_all else conn)
        self.motion_studio.tick()
        self._action_stop(conn, why)
        self._request_clear(None if clear_all else conn, why)
        if self.robot is None:
            return
        with self._send_lk:
            with self._lk:
                now = self.clock()
                if conn is not None:
                    q = self._seq_of(conn)
                    q["stop"] = max(q["stop"], q["hi"])
                was = self._web_driving
                self._web_driving = False
                attached, _ = self._pad_attached(now)
                send = was or not attached
                if send:
                    self.virtual.update(last=[0.0, 0.0, 0.0], at=now)
            if send:
                try:
                    self._notify_output("robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
                except (Unreachable, Timeout) as e:
                    self.log.warn("Failed to send 'stop'", cat="Safety", step="robot.move", raw=str(e),
                                  hint="robotd deadman will stop automatically after 0.5s")
        self._flush_clears(force=True)
        if was:
            self.log.info(f"Web control released ({why})", cat="Gamepad")

    # ================= Head / Body Cleanup & Modes =================

    def _mode_of(self, conn):
        m = self._modes.get(conn)
        return m["mode"] if m else "drive"

    def _request_clear(self, conn, why, delay=0.0, say=True):
        """Schedule head/body cleanup, mode exit, and key release (in-memory only, never throws)."""
        try:
            with self._lk:
                now = self.clock()
                self._clears.append({"conn": conn, "why": why, "due": now + delay, "tries": 0})
                for c in [c for c, x in self._mode_exit.items() if now - x["at"] > 10]:
                    self._mode_exit.pop(c, None)
                for c in list(self._modes):
                    if conn is None or c == conn:
                        m = self._modes.pop(c)
                        self._mode_exit[c] = {"why": why, "at": now}
                        if say:
                            self.log.info(f"Exited {MODE_CN[m['mode']]} ({why})", cat="Gamepad", dedup=False)
                for c, st in self._keys.items():
                    if conn is None or c == conn:
                        st["end"] = st.get("end") or why
                if conn is None:
                    self._x_armed.clear()
                else:
                    self._x_armed.pop(conn, None)
            self._tick_evt.set()
        except Exception:  # noqa: BLE001
            pass

    def _flush_clears(self, force=False):
        """Send due head=0 / pose={"active": false} cleanups under _send_lk."""
        if self.robot is None:
            with self._lk:
                self._clears.clear()
            return
        with self._send_lk:
            with self._lk:
                now = self.clock()
                due = [c for c in self._clears if force or c["due"] <= now]
                if not due:
                    return
                ids = {id(c) for c in due}
                self._clears = [c for c in self._clears if id(c) not in ids]
                sends = []
                for kind, method, params in (("head", "robot.head", HEAD_ZERO), ("body", "robot.pose", POSE_OFF)):
                    lat = self._latch[kind]
                    if lat is None:
                        continue
                    hit = next((c for c in due if c["conn"] is None or c["conn"] == lat["conn"]), None)
                    if hit is not None:
                        self._latch[kind] = None
                        sends.append((kind, method, params, hit, lat))
            for kind, method, params, req, lat in sends:
                try:
                    self._notify_output(method, dict(params))
                    self.log.info(("Head centered" if kind == "head" else "Body reset to neutral, exited Body Mode") + f" ({req['why']})",
                                  cat="Gamepad", raw=f"{method} {json.dumps(params)}", dedup=False)
                except (Unreachable, Timeout) as e:
                    with self._lk:
                        if self._latch[kind] is None:
                            self._latch[kind] = lat                 # Still latched: retry in 1s, up to 3 times
                        if req["tries"] < 2:
                            self._clears.append(dict(req, due=self.clock() + 1.0, tries=req["tries"] + 1))
                    self.log.warn("Failed to center head / body", cat="Safety", step=method, raw=str(e),
                                  hint=("Retrying in 1s" if req["tries"] < 2 else "Failed after 3 attempts: ") +
                                  "If Body Mode stays active, the duck won't walk; press B twice on gamepad or torque off and re-stand")

    def _mode_op(self, args, conn, by):
        """Switch between Walk / Head / Body modes (Y/B buttons) or send mode heartbeat."""
        mode = args.get("mode")
        if mode not in MODES:
            raise Refused(f"Unknown mode: {mode}")
        seq = self._seq(args)
        now = self.clock()
        stop_first = False
        with self._lk:
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale mode request dropped", "mode": self._mode_of(conn)}
            if conn in self._gone:
                raise Refused("This page has disconnected")
            cur = self._mode_of(conn)
            if mode != "drive":
                if conn in self._closed:
                    return {"dropped": "Page is not in the gamepad section", "mode": cur}
                if self._pad_attached(now)[0]:
                    raise Refused(MIRROR_SVG)
            if args.get("hb"):
                if mode == cur:
                    if mode != "drive":
                        self._modes[conn]["hb"] = now
                    return {"mode": cur}
                why = (self._mode_exit.get(conn) or {}).get("why")
                return {"mode": cur, "dropped": f"Backend already exited {MODE_CN.get(mode, mode)}" + (f" ({why})" if why else "")}
            q = self._seq_of(conn)
            if seq is not None:
                if seq <= q["mode"]:
                    return {"dropped": "Stale request", "mode": cur}
                q["mode"] = seq
            ex = self._mode_exit.get(conn)
            if mode == "drive":
                self._mode_exit.pop(conn, None)
            elif cur == "drive" and ex is not None:
                return {"dropped": f"Backend already exited {MODE_CN[mode]} ({ex['why']}): return to Walk Mode and press again", "mode": cur}
            note = MODE_IDLE if mode != "drive" and self._posture() not in DRIVING else None
            if mode == cur:
                if mode != "drive":
                    self._modes[conn]["hb"] = now
                return {"mode": cur, "note": note}
            if cur != "drive":
                self._modes.pop(conn, None)
                self._clears.append({"conn": conn, "why": f"Exited {MODE_CN[cur]}", "due": now, "tries": 0})
            if mode != "drive":
                self._modes[conn] = {"mode": mode, "hb": now}
                d = self._driver
                stop_first = self._web_driving and d is not None and d["conn"] == conn
            q["stop"] = max(q["stop"], q["hi"])
        self.log.info(f"Web gamepad {'Y' if 'head' in (mode, cur) else 'B'} -> {MODE_CN[mode]} ({by or 'page'})"
                      + (f": {note}" if note else ""), cat="Gamepad", dedup=False)
        if stop_first:
            self._stop_move({}, conn, by)
        self._flush_clears(force=True)
        return {"mode": mode, "note": note}

    # ================= Torque Off Guard =================

    def note_relax(self, by=None, why="Torque disabled"):
        """Called synchronously by app.run_relax on event loop when E-Stop / Space / Select is pressed."""
        try:
            with self._lk:
                self._relax_at = self.clock()
                self._relax_noted += 1
                self.motion_studio.invalidate(why)
                a = self._action
                if a is not None:
                    a["why"] = a["why"] or why
                    a["stop"].set()
            self._request_clear(None, why, RELAX_CLEAR_DELAY_S)
            self._test_evt.set()
        except Exception:  # noqa: BLE001
            pass

    def _relax_seen(self, view, now):
        """Detect torque-off events from robotd status (press count or torque on->off transition)."""
        new = False
        presses = view.get("estop_presses")
        if isinstance(presses, int) and not isinstance(presses, bool):
            if self._relax_presses is not None and presses > self._relax_presses:
                new = True
            self._relax_presses = presses
        bus = view.get("bus")
        if isinstance(bus, dict) and bus.get("mode") in ("motion", "commissioning"):
            on = bool((bus.get("native") or {}).get("torque_state_confirmed") is True) if bus.get("mode") == "motion" \
                else bus.get("torque_state") == "on"
            off = torque_off(bus) or relaxed(bus) is True
            if self._torque_was is True and off:
                new = True
            self._torque_was = True if on else (False if off else self._torque_was)
        if new:
            self._relax_at = now
        return new

    def _drive_feed(self, view):
        """Called on event loop from feed_robot (in-memory only)."""
        self.motion_studio.policy_feed(view)
        if self._pose_reset == "pending" and view.get("present") is True:
            self._pose_reset = "due"
            self._tick_evt.set()
        drv = self._posture() in DRIVING
        if not drv and not self.motion_studio.posture_allowed():
            self.motion_studio.invalidate("Policy left Ready-to-Walk state")
        left = self._was_driving and not drv
        self._was_driving = drv
        if left and (self._modes or self._latch["head"] is not None or self._latch["body"] is not None):
            self._request_clear(None, f"Policy stopped (now {POSTURE_CN.get(self._posture(), self._posture())})")

    def _relax_block(self, now):
        r = self._robot or {}
        if r.get("estop_active"):
            return "Disabling torque; wait for confirmation"
        guard = self.w.get("relax_guard", RELAX_GUARD_S)
        if now - self._relax_at < guard:
            return f"Torque recently disabled: motion commands blocked for {guard:g}s"
        return None

    # ================= Speed Limits & Head / Mouth Range =================

    def _speed_limits(self, now=None):
        """Return cached padd full-stick speed limits (refreshed in background thread)."""
        return self._limits

    def _read_padd_texts(self):
        texts = []
        try:
            texts.append((PADD_UNIT, self.read_text(PADD_UNIT)))
        except Exception:  # noqa: BLE001 -- missing/unreadable: use default
            pass
        try:
            for name in sorted(self.list_dir(PADD_DROPIN_DIR)):
                if name.endswith(".conf"):
                    p = os.path.join(PADD_DROPIN_DIR, name)
                    texts.append((p, self.read_text(p)))
        except Exception:  # noqa: BLE001
            pass
        return texts

    def _read_inputs(self, texts):
        """Read head/mouth input limits and padd --config path (preferring live /run/padd/input-settings.json ack)."""
        inputs = parse_padd_inputs(texts)
        text, err, ack = None, None, None
        if inputs["input_settings"]:
            try:
                text = self.read_text(inputs["input_settings"])
            except FileNotFoundError:
                err = "file not found"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            try:
                a = json.loads(self.read_text(PADD_INPUT_ACK))
                pid = a.get("pid") if isinstance(a, dict) else None
                alive = isinstance(pid, int) and not isinstance(pid, bool) and self.exists(f"/proc/{pid}")
                if alive and a.get("path") == inputs["input_settings"]:
                    ack = a.get("settings")
            except Exception:  # noqa: BLE001
                pass
        return input_limits(inputs, text, err, ack), inputs["config"]

    # ================= Web Sticks =================

    def _seq(self, args):
        seq = args.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            return None
        return seq

    def _gate(self, conn, now):
        """Common gate for continuous drive/stick/key commands (call under _lk)."""
        if conn in self._gone:
            return {"dropped": "Page disconnected"}
        if conn in self._closed:
            return {"dropped": "Page is not in the gamepad section"}
        attached, _ = self._pad_attached(now)
        if attached:
            if not self._pad_said:
                self._pad_said = True
                self.log.info("Physical gamepad is connected; web sticks are mirroring only", cat="Gamepad")
            raise Refused("Physical gamepad is connected: web sticks mirror it only (padd sends zero velocity every 0.1s). Turn off the physical gamepad to use web control")
        self._pad_said = False
        ok, why = self._robot_ok(now)
        if ok is not True:
            raise Refused(f"Cannot control right now: {why or 'robotd status unreadable'}")
        block = self._relax_block(now)
        if block:
            raise Refused(block)
        return None

    def _send_intent(self, chan, method, params, conn, by, seq, mode=None):
        """Send one continuous intent frame (move/head/pose) with sequencing, lease, and 40ms coalescing."""
        now = self.clock()
        motion_epoch = self.motion_studio.epoch
        with self._lk:
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale command dropped"}, False
            g = self._gate(conn, now)
            if g:
                return g, False
            if mode is not None and self._mode_of(conn) != mode:
                return {"dropped": f"Mode mismatch: backend is in {MODE_CN[self._mode_of(conn)]}", "mode": self._mode_of(conn)}, False
            q = self._seq_of(conn)
            if seq is None:
                seq = q["hi"] + 1
            if seq <= q["stop"] or seq <= q["sent"].get(chan, -1):
                return {"dropped": "Stale command (already released)"}, False
            self._take_lease(conn, by, now)
            q["hi"] = max(q["hi"], seq)
            q["latest"][chan] = max(q["latest"].get(chan, -1), seq)
            gap = MOVE_MIN_GAP_S - (now - self._last_sent_at.get(chan, -1e9))
        if self.robot is None:
            raise Refused("robotd address is not configured")
        if self._clears:
            self._flush_clears()
        if gap > 0:
            self._cancel_wait(gap)
        with self._send_lk:
            with self._lk:
                d, q = self._driver, self._seq_of(conn)
                if (not self.motion_studio.request_current() or motion_epoch != self.motion_studio.epoch or conn in self._gone or conn in self._closed or seq != q["latest"].get(chan) or seq <= q["stop"]
                        or d is None or d["conn"] != conn or (mode is not None and self._mode_of(conn) != mode)):
                    return {"dropped": "Superseded by newer command or released"}, False
                q["sent"][chan] = seq
                self._last_sent_at[chan] = self.clock()
            try:
                self._notify_output(method, params)
            except (Unreachable, Timeout) as e:
                raise Refused(f"Send failed: {e}") from None
            with self._lk:
                if chan in ("head", "body"):
                    nonzero = any(abs(v) > 1e-9 for k, v in params.items() if k != "active")
                    self._latch[chan] = {"conn": conn, "nonzero": nonzero, "at": self.clock()}
                    self._tick_evt.set()
        return {"merged": gap > 0}, True

    def _move(self, args, conn=None, by=None, mode="drive"):
        """Send one walking velocity frame (legacy move or sticks in drive mode)."""
        try:
            vals = [float(args.get(k, 0) or 0) for k in ("vx", "vy", "vyaw")]
        except (TypeError, ValueError):
            raise Refused("Velocity values must be numbers") from None
        if not all(math.isfinite(v) for v in vals):
            raise Refused("Velocity values must be finite")
        lim = self._speed_limits()
        vx = max(-lim["v"], min(lim["v"], vals[0]))
        vy = max(-lim["v"], min(lim["v"], vals[1]))
        vyaw = max(-lim["w"], min(lim["w"], vals[2]))
        with self._lk:
            start = not self._web_driving and any((vx, vy, vyaw))
        r, sent = self._send_intent("move", "robot.move", {"vx": vx, "vy": vy, "vyaw": vyaw}, conn, by, self._seq(args),
                                    mode=mode)
        if not sent:
            return r
        with self._lk:
            self._web_driving = any((vx, vy, vyaw))
            self.virtual.update(last=[vx, vy, vyaw], at=self.clock())
        if start:
            self.log.info(f"Web control driving started ({by or 'page'}, robot.move)", cat="Gamepad")
        return {"sent": [vx, vy, vyaw], "merged": r["merged"]}

    def _sticks(self, args, conn=None, by=None):
        """Handle web gamepad sticks: {mode, lx, ly, rx, ry, seq}."""
        mode = args.get("mode", "drive")
        if mode not in MODES:
            raise Refused(f"Unknown mode: {mode}")
        try:
            vals = [float(args.get(k, 0) or 0) for k in ("lx", "ly", "rx", "ry")]
        except (TypeError, ValueError):
            raise Refused("Stick values must be numbers") from None
        if not all(math.isfinite(v) for v in vals):
            raise Refused("Stick values must be finite")
        lx, ly, rx, ry = (max(-1.0, min(1.0, v)) for v in vals)
        now = self.clock()
        with self._lk:
            cur = self._mode_of(conn)
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale stick request dropped", "mode": cur}
            if conn in self._gone or conn in self._closed:
                return {"dropped": "Page left gamepad section", "mode": cur}
            if mode != cur:
                why = (self._mode_exit.get(conn) or {}).get("why")
                return {"dropped": f"Mode mismatch: backend is in {MODE_CN[cur]}" + (f" ({why})" if why else ""), "mode": cur}
            if cur != "drive":
                self._modes[conn]["hb"] = now
                if self._posture() not in DRIVING:
                    self._gate(conn, now)
                    return {"dropped": MODE_IDLE, "mode": cur}
                a = self._action
                if cur == "head" and a is not None and any(C.head_at(s, 0.0) is not None for s in a["steps"]):
                    raise Refused(f"Running '{a['name']}' (also moves head): wait for it to finish or release")
        method, params = sticks_to_intent(cur, lx, ly, rx, ry, self._speed_limits(), self._inputs["head"])
        if cur == "drive":
            r = self._move(dict(params, seq=args.get("seq")), conn, by, mode="drive")
            return dict(r, mode=self._mode_of(conn))
        r, sent = self._send_intent(cur, method, params, conn, by, self._seq(args), mode=cur)
        if sent:
            return {"sent": params, "mode": cur, "merged": r["merged"]}
        return dict(r, mode=cur)

    def _cancel_wait(self, s):
        threading.Event().wait(s)

    def _stop_move(self, args=None, conn=None, by=None):
        """Release sticks: send move 0 in Walk Mode; re-center head/body in Head/Body Mode."""
        if self.robot is None:
            return {}
        args = args or {}
        with self._send_lk:
            with self._lk:
                if not self.motion_studio.request_current():
                    return {"dropped": "Motion session changed; stale stop request dropped"}
                now = self.clock()
                q = self._seq_of(conn)
                seq = self._seq(args)
                if seq is None:
                    seq = q["hi"] + 1
                q["stop"] = max(q["stop"], seq)
                q["hi"] = max(q["hi"], seq)
                d = self._driver
                mine = d is None or d["conn"] == conn
                attached, _ = self._pad_attached(now)
                if args.get("owned_only") is True and (d is None or not mine or attached or self._mode_of(conn) != "drive"):
                    return {"dropped": "Page does not hold walking control; stop frame not sent"}
                if d is not None and d["conn"] == conn:
                    d["seen"] = now
                send = self._web_driving or not attached
                center = None
                mode = self._mode_of(conn)
                lat = self._latch.get(mode) if mode != "drive" else None
                if lat is not None and lat["conn"] == conn and lat["nonzero"]:
                    center = ("robot.head", HEAD_ZERO) if mode == "head" else ("robot.pose", POSE_CENTER)
                    lat.update(nonzero=False, at=now)
            if send:
                try:
                    self._notify_output("robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
                except (Unreachable, Timeout) as e:
                    raise Refused(f"Failed to send stop (robotd deadman stops in 0.5s): {e}") from None
            if center is not None:
                try:
                    self._notify_output(center[0], dict(center[1]))
                except (Unreachable, Timeout) as e:
                    with self._lk:
                        lat.update(nonzero=True)
                    raise Refused(f"Failed to re-center (watchdog retries in 0.6s): {e}") from None
            with self._lk:
                was = self._web_driving and mine
                if mine and send:
                    self._web_driving = False
                    self.virtual.update(last=[0.0, 0.0, 0.0], at=self.clock())
        if was:
            self.log.info("Web control released", cat="Gamepad")
        return {"sent": send, **({"centered": True} if center is not None else {})}

    # ================= Held Keys: LT / RT / X =================

    def _keys_op(self, args, conn, by):
        """Handle held keys {held, down, seq}."""
        held, down = args.get("held"), args.get("down") or []
        if not isinstance(held, list) or not isinstance(down, list) or any(k not in HELD_KEYS for k in held + down):
            raise Refused("held / down must only contain lt, rt, x")
        seq = self._seq(args)
        now = self.clock()
        with self._lk:
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale key request dropped", "held": []}
            if conn in self._gone or conn in self._closed:
                return {"dropped": "Page left gamepad section", "held": []}
            st = self._keys.get(conn)
            q = self._seq_of(conn)
            if seq is not None:
                if seq <= q["keys"]:
                    return {"dropped": "Stale request", "held": sorted(st["held"]) if st is not None and not st.get("end") else []}
                q["keys"] = seq
            had = set(st["held"]) if st is not None and not st.get("end") else set()
            want = set(held)
            pressed = (set(down) & want) - had
            keep = (want & had) | pressed
            ended = sorted(want - keep)
            if pressed:
                attached, _ = self._pad_attached(now)
                if attached:
                    raise Refused(MIRROR_SVG)
                base = self._base_block(now)
                if base:
                    raise Refused(base)
                a = self._action
                if pressed & {"lt", "rt"} and a is not None and any(C.mouth_at(s, 0.0) is not None for s in a["steps"]):
                    raise Refused(f"Running '{a['name']}' (also moves mouth): wait for it to finish or release")
                self._take_lease(conn, by, now)
            if st is None or st.get("end"):
                if not keep:
                    return {"held": [], "ended": ended}
                st = self._keys[conn] = {"held": {}, "started": now, "by": by, "mouth": False, "rt": False,
                                         "wheee": False, "x_down": None, "end": None,
                                         **({k: st[k] for k in ("mouth", "wheee")} if st is not None else {})}
            released = set(st["held"]) - keep
            for k in released:
                st["held"].pop(k, None)
                if k == "x":
                    self._x_armed.pop(conn, None)
            for k in keep:
                st["held"][k] = now
            if "x" in pressed:
                st["x_down"] = now
                self._x_armed.pop(conn, None)     # New press: invalidate previous X arm
            if keep:
                self._touch_lease(conn, now)
        for k in sorted(pressed):
            self.log.info(f"Web gamepad {KEY_CN[k]} pressed ({by or 'page'})", cat="Gamepad", dedup=False)
        for k in sorted(released):
            self.log.info(f"Web gamepad {KEY_CN[k]} released", cat="Gamepad", dedup=False)
        self._tick_evt.set()
        return {"held": sorted(keep), "ended": ended}

    def _keys_abort(self, conn, st, now):
        if conn in self._gone or conn in self._closed:
            return "Page left gamepad section"
        if self._pad_attached(now)[0]:
            return "Physical gamepad connected"
        d = self._driver
        if d is None or d["conn"] != conn:
            return "Taken over by another page"
        if self._relax_at >= st["started"] or (self._robot or {}).get("estop_active"):
            return "Torque disabled"
        return self._base_block(now)

    def _keys_plan(self, now, sends, logs):
        for conn, st in list(self._keys.items()):
            why = st.get("end") or self._keys_abort(conn, st, now)
            if why:
                fresh = set()
            else:
                fresh = {k for k, at in st["held"].items() if now - at < (X_HOLD_S if k == "x" else KEYS_HOLD_S)}
            lt, rt, x = "lt" in fresh, "rt" in fresh, "x" in fresh
            mouth = (lt or rt) and self._relax_block(now) is None
            if mouth or st["mouth"]:
                sends.append(("robot.mouth", {"open": round(self._inputs["mouth"] / 100.0, 3) if mouth else 0.0}, None))
                st["mouth"] = mouth
            if rt and not st["rt"]:
                sends.append(("robot.sound", {"tag": "chirp"}, None))
            st["rt"] = rt
            if lt:
                sends.append(("robot.sound", {"tag": "wheee", "hold": True}, None))
                st["wheee"] = True
            elif st["wheee"]:
                sends.append(("robot.sound", {"tag": "wheee", "hold": False}, None))
                st["wheee"] = False
            arm = self._x_armed.get(conn)
            if x and arm is not None and arm["x_down"] == st.get("x_down") \
                    and self._posture() in DRIVING and self._relax_block(now) is None:
                sends.append(("robot.do", {"skill": arm["skill"]}, None))
            gone = set(st["held"]) - fresh
            if gone:
                reason = why or "Page missed 0.45s hold heartbeat (released, navigated away, or disconnected)"
                logs.append(("Safety" if why in (None, "Torque disabled") else "Gamepad",
                             f"Web gamepad {', '.join(KEY_CN[k] for k in sorted(gone))} stopped: {reason}"))
                for k in gone:
                    st["held"].pop(k, None)
                if "x" in gone:
                    self._x_armed.pop(conn, None)
            if why:
                st["end"] = why
            if not st["held"] and not st["mouth"] and not st["wheee"]:
                del self._keys[conn]

    def _key_press(self, args, conn, by):
        key = args.get("key")
        if key not in BIND_KEYS:
            raise Refused(f"Unknown key: {key}")
        now = self.clock()
        if args.get("src") == "svg" and self._pad_attached(now)[0]:
            raise Refused(MIRROR_SVG)
        if self.robot is not None and now - self._bindings_at > BINDINGS_STALE_S:
            self._fetch_bindings()
        with self._lk:
            mv = self._motion_view(self.clock())
        row = mv["keys"][key]
        name = KEY_CN[key]
        if not row["ok"]:
            self.log.info(f"Web gamepad {name}: disabled ({row['why']})", cat="Gamepad")
            raise Refused(row["why"])
        self.log.info(f"Web gamepad {name} -> {row['does']} ({by or 'page'})", cat="Gamepad", dedup=False)
        label = f"Web gamepad {name} -> {row['does']}"
        if row["op"] == "skill":
            with self._lk:
                st = self._keys.get(conn)
                x_down0 = st.get("x_down") if st is not None else None
            r = self._motion_cmd("skill", {"name": row["skill"]}, conn, by, label=label)
            if key == "x":
                with self._lk:
                    st = self._keys.get(conn)
                    if x_down0 is None and st is not None and not st.get("end") and "x" in st["held"]:
                        x_down0 = st.get("x_down")
                    if x_down0 is not None:
                        self._x_armed[conn] = {"skill": row["skill"], "x_down": x_down0}
                self._tick_evt.set()
            return r
        return self._motion_cmd(row["op"], {}, conn, by, label=label)

    def _start(self, args, conn, by):
        now = self.clock()
        if args.get("src") == "svg" and self._pad_attached(now)[0]:
            raise Refused(MIRROR_SVG)
        with self._lk:
            st = self._motion_view(now)["start"]
        expect = args.get("expect")
        if expect is not None and expect != st["op"]:
            self.log.info(f"Web gamepad Start: page expected '{expect}', backend chose '{st['label']}', skipped", cat="Gamepad")
            raise Refused(f"State just changed, press again (Start will now '{st['label']}')")
        if not st["ok"]:
            self.log.info(f"Web gamepad Start: disabled ({st['why']})", cat="Gamepad")
            raise Refused(st["why"])
        self.log.info(f"Web gamepad Start -> {st['label']} ({by or 'page'})", cat="Gamepad", dedup=False)
        return self._motion_cmd(st["op"], {}, conn, by, label=f"Web gamepad Start -> {st['label']}")

    def _reboot(self, args, conn, by):
        now = self.clock()
        if args.get("src") == "svg" and self._pad_attached(now)[0]:
            raise Refused(MIRROR_SVG)
        with self._lk:
            mv = self._motion_view(now)
        row = mv["reboot"]
        what = "Web gamepad D-Pad Right -> Reboot all servos"
        if not row["ok"]:
            self.log.info(f"Web gamepad D-Pad Right: disabled ({row['why']})", cat="Gamepad")
            with self._lk:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": row["why"], "at": now}
            raise Refused(row["why"])
        self.log.info(f"{what} ({by or 'page'})", cat="Gamepad", dedup=False)
        try:
            r = self._call(what, "robot.rebootMotors", {"ids": []}, by, {"posture": mv["posture"]})
        except Refused as e:
            with self._lk:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": str(e), "at": self.clock()}
            raise
        self.note_relax(by, "Reboot servos (robotd disables torque first)")
        note = r.get("reason") if isinstance(r, dict) else None
        with self._lk:
            self._last_cmd = {"what": what, "by": by, "ok": True, "reason": note, "at": self.clock()}
        return {"accepted": True, "reason": note, "did": "reboot"}

    def _drive_mode(self, args, conn, by):
        now = self.clock()
        if args.get("src") == "svg" and self._pad_attached(now)[0]:
            raise Refused(MIRROR_SVG)
        with self._lk:
            mv = self._motion_view(now)
            row = mv["drive_mode"]
            target = row["target"]
            what = f"Web gamepad D-Pad Up (hold 3s) -> Switch to {DRIVE_MODE_CN.get(target, 'other mode')}"
            why = row["why"]
            expect = args.get("expect")
            if why is None and expect is not None and expect != target:
                why = f"State just changed, hold again ({row['does'].replace('Hold 3s: ', '')})"
            if why is None and self._cmd_busy:
                why = "Previous command still waiting for reply; wait a moment"
            if why is None:
                held = self._lease_holder(now)
                if held is not None and held["conn"] != conn:
                    why = f"{held['by'] or 'Another page'} is driving sticks: wait for release or click 'Take Over'"
            if why is not None:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": why, "at": now}
            else:
                self._cmd_busy = True
        if why is not None:
            self.log.info(f"Web gamepad D-Pad Up: cannot switch ({why})", cat="Gamepad")
            raise Refused(why)
        self.log.info(f"{what} ({by or 'page'})", cat="Gamepad", dedup=False)
        try:
            r = self._call(what, "robot.setMode", {"mode": target}, by, {"posture": mv["posture"], "mode": row["current"]})
        except Refused as e:
            with self._lk:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": str(e), "at": self.clock()}
            raise
        finally:
            with self._lk:
                self._cmd_busy = False
        note = r.get("reason") if isinstance(r, dict) else None
        if note and "already" in note.lower():
            note = f"Already in {DRIVE_MODE_CN[target]} mode"
        with self._lk:
            self._last_cmd = {"what": what, "by": by, "ok": True, "reason": note, "at": self.clock()}
        return {"accepted": True, "reason": note, "did": "drive_mode", "target": target}

    # ================= Background Tick: Held Keys, Watchdogs, Cleanup =================

    def _tick_work(self):
        return bool(self.motion_studio.session or self.motion_studio._pending_cleanup or self._keys or self._clears or self._modes or self._pose_reset == "due"
                    or (self._latch["head"] or {}).get("nonzero") or (self._latch["body"] or {}).get("nonzero"))

    def _tick_loop(self):
        while not self._tick_stop.is_set():
            try:
                self._tick_once()
            except Exception as e:  # noqa: BLE001
                key = f"{type(e).__name__}: {e}"[:200]
                if key not in self._tick_said:
                    self._tick_said.add(key)
                    self.log.error("Gamepad background tick error (held keys / watchdogs)", cat="Gamepad", raw=key, exc=True,
                                   hint="Studio internal error; export diagnostics. Thread will keep running")
            with self._lk:
                busy = self._tick_work()
            if busy:
                self._tick_stop.wait(TICK_S)
            else:
                self._tick_evt.wait(1.0)
                self._tick_evt.clear()

    def _tick_once(self):
        if self._update_blocked():
            with self._lk:
                held = bool(self._modes or self._keys or self._latch['head'] or self._latch['body'])
            if held:
                self._request_clear(None, 'Software / robotd update protection still active', say=False)
        self.motion_studio.tick()
        self._flush_clears()
        sends, logs = [], []
        with self._send_lk:
            with self._lk:
                now = self.clock()
                if self._pose_reset == "due":
                    self._pose_reset = "sending"
                    sends.append(("robot.pose", dict(POSE_OFF), "pose_reset"))
                if self._pad_attached(now)[0] and (self._modes or self._keys or self._latch["head"] or self._latch["body"]):
                    self._request_clear(None, "Physical gamepad connected")
                for kind, center in (("head", HEAD_ZERO), ("body", POSE_CENTER)):
                    lat = self._latch[kind]
                    if lat is not None and lat["nonzero"] and now - lat["at"] > STICK_STALE_S:
                        lat.update(nonzero=False, at=now)
                        sends.append(("robot.head" if kind == "head" else "robot.pose", dict(center), "watchdog"))
                        logs.append(("Safety", f"{'Head' if kind == 'head' else 'Body'} stick had no new frame for {STICK_STALE_S:g}s (page stalled?): "
                                               f"{'head centered' if kind == 'head' else 'body reset to neutral'}"))
                for c, m in list(self._modes.items()):
                    if now - m["hb"] > MODE_HB_S:
                        why = f"Page missed {MODE_HB_S:g}s heartbeat in {MODE_CN[m['mode']]}"
                        logs.append(("Safety", f"{why}: exited"))
                        self._request_clear(c, why, say=False)
                self._keys_plan(now, sends, logs)
            for method, params, tag in (sends if self.robot is not None else []):
                try:
                    self._notify_output(method, params)
                except (Unreachable, Timeout) as e:
                    if tag == "pose_reset":
                        with self._lk:
                            self._pose_reset_tries += 1
                            self._pose_reset = "pending" if self._pose_reset_tries < 3 else "failed"
                    if method not in self._tick_said:
                        self._tick_said.add(method)
                        self.log.warn(f"Web gamepad {method} failed to send", cat="Gamepad", step=method, raw=str(e),
                                      hint="Is robotd offline? Missing mouth/head frames are harmless")
                    continue
                self._tick_said.discard(method)
                if tag == "pose_reset":
                    with self._lk:
                        self._pose_reset = "done"
                    self.log.info("Studio connected to robotd: sent robot.pose {\"active\": false} to clear any prior Body Mode",
                                  cat="Gamepad")
        for cat, msg in logs:
            self.log.write(msg, cat=cat, level="info", dedup=False)

    def _drive_close(self):
        """Called on Studio shutdown: stop tick thread and clear latched head/body/keys."""
        self._tick_stop.set()
        self._tick_evt.set()
        t = self._tick_thread
        if t is not None and t is not threading.current_thread():
            t.join(0.5)
        self.motion_studio.close()
        try:
            self._request_clear(None, "Studio closed")
            self._flush_clears(force=True)
            sends = []
            with self._lk:
                for st in self._keys.values():
                    if st["mouth"]:
                        sends.append(("robot.mouth", {"open": 0.0}))
                    if st["wheee"]:
                        sends.append(("robot.sound", {"tag": "wheee", "hold": False}))
                self._keys.clear()
            if self.robot is not None:
                with self._send_lk:
                    for method, params in sends:
                        try:
                            self._notify_output(method, params)
                        except (Unreachable, Timeout):
                            pass
        except Exception:  # noqa: BLE001
            pass

    # ================= Stand / Walk / Sit / Skills / Sounds =================

    def _update_blocked(self):
        try:
            return getattr(self, 'update_guard', lambda: False)() is True
        except Exception:
            return True

    def _base_block(self, now, *, ignore_mapping=False):
        if self._update_blocked():
            return 'Software / robotd update protection still active'
        mapping = getattr(self, 'mapping', None)
        if not ignore_mapping and mapping is not None and mapping.blocked:
            return 'Gamepad mapping / center confirmation in progress; motion paused (web E-Stop still available)'
        r = self._robot or {}
        if self._robot_at is None or now - self._robot_at > 3:
            return "Studio has not received robotd status yet"
        if r.get("paused"):
            return "Servo tuning active: robotd is paused. Click 'Hand Back to robotd' in the top bar first"
        if r.get("present") is False:
            return "robotd is not running"
        bus = r.get("bus") if isinstance(r.get("bus"), dict) else None
        if bus is None or r.get("bus_at") is None or now - r["bus_at"] > 3:
            return "Cannot read robotd status"
        if bus.get("phase") in STARTING_PHASES:
            return "robotd is still starting up"
        if bus.get("mode") == "commissioning":
            return "Commissioning mode: robotd is read-only and rejects motion commands"
        if bus.get("mode") != "motion":
            return f"robotd is in {bus.get('mode')} mode"
        return None

    def _policies(self):
        p = (self._robot or {}).get("policies")
        return p if isinstance(p, dict) else None

    def _known_skills(self):
        """Recognized robot.do names: ground_pick + sit_toggle + skills. Returns None if robot.policies unreadable."""
        p = self._policies()
        if p is None:
            return None
        slots = {s.get("slot"): s for s in p.get("slots") or [] if isinstance(s, dict)}
        names = []
        if (slots.get("ground_pick") or {}).get("path"):
            names.append("ground_pick")
        if (slots.get("sitstand") or {}).get("path"):
            names.append("sit_toggle")
        names += [s for s in p.get("skills") or [] if isinstance(s, str)]
        return names

    def _posture(self):
        r = self._robot or {}
        return "servo_session" if r.get("paused") else (r.get("posture") or "unknown")

    def _skill_why(self, name, need_drive, known, pol):
        if known is None:
            return "Cannot read duck skills (robot.policies is only queried in motion mode)"
        if name not in known:
            return f"This duck does not have '{C.SKILL_CN.get(name, name)}'"
        if need_drive:
            return need_drive
        if name == "roller_stand" and pol.get("mode") != "roller":
            return "Requires roller hardware and Roller mode (hold D-Pad Up 3s)"
        return None

    def _bind_map(self):
        rows = {b.get("button"): b for b in self.bindings or [] if isinstance(b, dict)}
        out = {}
        for k in BIND_KEYS:
            b = rows.get(k)
            if b is not None and isinstance(b.get("skill"), str):
                out[k] = {"skill": b["skill"], "error": b.get("error") or None, "overridden": bool(b.get("overridden")),
                          "src": "pad.bindings"}
            else:
                out[k] = {"skill": DEFAULT_BIND[k], "error": None, "overridden": False,
                          "src": "Default" + (" (pad.bindings unreadable)" if self.bindings is None else "")}
        return out

    def _key_rows(self, buttons, sitting, skills, skills_why, known, need_drive, pol):
        out = {}
        for k, b in self._bind_map().items():
            skill = b["skill"]
            row = {"key": k, "skill": skill, "src": b["src"], "overridden": b["overridden"], "op": None,
                   "does": C.SKILL_CN.get(skill, skill) if skill else "(Disabled)"}
            if b["error"]:
                row.update(ok=False, why=f"Bound '{skill}' is unavailable: {b['error']}")
            elif not skill:
                row.update(ok=False, why="Key is disabled (empty in robotd.toml [pad])")
            elif skill == "sit_toggle":
                if sitting is None:
                    row.update(ok=False, why=NO_SIT_INFO, op="sit")
                else:
                    btn = buttons["stand" if sitting else "sit"]
                    row.update(ok=btn["ok"], why=btn["why"], op=btn["op"], does=btn["label"])
            else:
                s = next((x for x in skills if x["name"] == skill), None)
                why = s["why"] if s is not None else (skills_why or self._skill_why(skill, need_drive, known, pol))
                row.update(ok=why is None, why=why, op="skill")
            out[k] = row
        return out

    def _motion_view(self, now):  # noqa: C901
        """Compute button availability and reasons for UI and server-side validation."""
        base = self._base_block(now)
        relax = self._relax_block(now)
        block = base or relax
        pose = self._posture()
        pol = self._policies() or {}
        sit_raw = pol.get("sitting")
        sitting = sit_raw if isinstance(sit_raw, bool) else None
        homed = pol.get("homed") if isinstance(pol.get("homed"), bool) else None
        r = self._robot or {}
        bus = r.get("bus") if isinstance(r.get("bus"), dict) else {}
        policy_on = bus.get("policy_enabled") is True
        torque_on = (bus.get("native") or {}).get("torque_state_confirmed") is True
        batt = r.get("battery_pct")
        job = self._job_alive()
        guard = bus.get("home_start_guard") if isinstance(bus.get("home_start_guard"), dict) else {}
        unsteady = "Still standing up; wait until steady" + (f" ({guard['reason']})" if guard.get("ready") is False
                                                           and isinstance(guard.get("reason"), str) and guard["reason"] else "")

        def b(op, label, call, why):
            return {"op": op, "label": label, "call": call, "ok": why is None, "why": why}

        stuck = None
        if block:
            stand = b("stand", "Stand Up", None, block)
        elif sitting is None:
            stand = b("stand", "Stand Up", None, NO_SIT_INFO)
        elif sitting:
            if policy_on and torque_on and (homed is True or bus.get("homed") is True):
                stand = b("stand", "Stand Up", "robot.do sit_toggle",
                          None if "sit_toggle" in (self._known_skills() or []) else "Sit/stand model is not installed on this duck")
            else:
                stuck = ("Sitting with policy stopped (likely torque-off while sitting): cannot stand from web — "
                         "support the duck upright and click 'Stand Up', or press Start on physical gamepad")
                stand = b("stand", "Stand Up", None, stuck)
        elif pose == "limp":
            stand = b("stand", "Stand Up", "robot.init", None)
            if isinstance(batt, (int, float)) and not isinstance(batt, bool) and batt < 15:
                if self.battery_shutdown is True:
                    stand["note"] = f"Low battery ({batt:.0f}%): charge soon; auto-shutdown at 6.6 V"
                else:
                    stand["note"] = f"Low battery ({batt:.0f}%): charge soon"
        elif pose == "homing":
            stand = b("stand", "Stand Up", None, unsteady)
        elif pose in ("holding", "policy", "walking"):
            stand = b("stand", "Stand Up", None, "Already standing")
        else:
            stand = b("stand", "Stand Up", None, f"Cannot read current posture ({POSTURE_CN.get(pose, pose)})")

        if pose in DRIVING and not base:
            walk = b("walk_stop", "Stop (Stand)", "robot.stop -> robot.enable {on:false}", None)
        elif block:
            walk = b("walk", "Start Walking", None, block)
        elif job:
            walk = b("walk", "Start Walking", None, "Gamepad scan/pairing in progress (uses Bluetooth); wait for completion")
        elif stuck:
            walk = b("walk", "Start Walking", None, "Sitting without policy running: see 'Stand Up' hint")
        elif sitting is None:
            walk = b("walk", "Start Walking", None, NO_SIT_INFO)
        elif sitting:
            walk = b("walk", "Start Walking", None, "Sitting: click 'Stand Up' first")
        elif pose == "holding":
            walk = b("walk", "Start Walking", "robot.enable {on:true}", None)
        elif pose == "limp":
            walk = b("walk", "Start Walking", None, "Torque off: click 'Stand Up' first")
        elif pose == "homing":
            walk = b("walk", "Start Walking", None, unsteady)
        else:
            walk = b("walk", "Start Walking", None, f"Cannot read current posture ({POSTURE_CN.get(pose, pose)})")

        known = self._known_skills()
        if block:
            sit = b("sit", "Sit Down", None, block)
        elif sitting is None:
            sit = b("sit", "Sit Down", None, NO_SIT_INFO)
        elif sitting:
            sit = b("sit", "Sit Down", None, "Already sitting")
        elif known is not None and "sit_toggle" not in known:
            sit = b("sit", "Sit Down", None, "Sit/stand model (sitstand) is not installed on this duck")
        elif pose == "policy" and policy_on:
            sit = b("sit", "Sit Down", "robot.do sit_toggle", None if homed is not False else "Returning to home posture; wait a second")
        elif pose == "walking":
            sit = b("sit", "Sit Down", None, "Walking: release sticks and wait for stop")
        elif pose == "holding":
            sit = b("sit", "Sit Down", None, "Sitting down requires policy running: click 'Start Walking' first")
        elif pose == "limp":
            sit = b("sit", "Sit Down", None, "Torque off: click 'Stand Up' first")
        else:
            sit = b("sit", "Sit Down", None, f"Cannot sit while {POSTURE_CN.get(pose, pose)}")

        if walk["op"] == "walk_stop":
            pick = walk
        elif pose == "holding":
            pick = walk
        else:
            pick = stand
        start = {"op": pick["op"], "label": pick["label"], "ok": pick["ok"], "why": pick["why"], "call": pick["call"]}
        reboot = {"ok": base is None, "why": base, "does": "Reboot all servos: duck goes limp (support it), then press Start to stand"}
        cur = pol.get("mode") if pol.get("mode") in DRIVE_MODE_CN else None
        target = {"walk": "roller", "roller": "walk"}.get(cur)
        mode_why = block or (None if target else "Cannot read Walk/Roller mode (robot.policies unreadable)")
        drive_mode = {"ok": mode_why is None, "why": mode_why, "current": cur, "target": target,
                      "does": f"Hold 3s: Switch to {DRIVE_MODE_CN[target]}" if target else "Hold 3s: Walk / Roller"}

        need_drive = None
        if block:
            need_drive = block
        elif pose not in DRIVING:
            need_drive = "Requires 'Ready to Walk' (policy running): click 'Start Walking' first" if pose == "holding" else \
                f"Currently {POSTURE_CN.get(pose, pose)}: stand up and click 'Start Walking' first"
        elif sitting is None:
            need_drive = NO_SIT_INFO
        elif sitting:
            need_drive = "Sitting: stand up first"
        elif homed is False:
            need_drive = "Returning to home posture; wait a second"
        skills = []
        for name in [n for n in (known or []) if n != "sit_toggle"]:
            why = self._skill_why(name, need_drive, known, pol)
            skills.append({"name": name, "label": C.SKILL_CN.get(name, name), "ok": why is None, "why": why})
        skills_why = None if known is not None else "Cannot read duck skills (robot.policies is only queried in motion mode)"
        sounds = [{"tag": t, "label": lab, "ok": base is None, "why": base} for t, lab in C.SOUNDS.items()]

        attached, _ = self._pad_attached(now)
        held_mouth = any({"lt", "rt"} & set(st["held"]) for st in self._keys.values() if not st.get("end"))
        head_mode = any(m["mode"] == "head" for m in self._modes.values()) or bool((self._latch["head"] or {}).get("nonzero"))
        limp = pose == "limp" or relaxed(bus) is True

        def combo_row(c, builtin):
            why = None
            if C.needs_policy(c["steps"]):
                why = need_drive
            if why is None and attached:
                why = "Physical gamepad is connected and overrides mouth/velocity: turn it off first"
            if why is None:
                why = base or relax
            if why is None:
                for s in c["steps"]:
                    if s["do"] == "skill":
                        why = self._skill_why(s["name"], need_drive, known, pol)
                        if why:
                            break
            if why is None and held_mouth and any(C.mouth_at(s, 0.0) is not None for s in c["steps"]):
                why = "LT / RT is holding mouth open: release first"
            if why is None and head_mode and any(C.head_at(s, 0.0) is not None for s in c["steps"]):
                why = "Head Mode is active: press Y to return to Walk Mode first"
            text = C.describe(c["steps"])
            if why is None and limp and not C.needs_policy(c["steps"]):
                text += " (Torque off: mouth won't move until standing)"
            return {"id": c["id"], "name": c["name"], "builtin": builtin, "text": text,
                    "seconds": round(C.duration(c["steps"]), 1), "ok": why is None, "why": why}

        actions = [combo_row(c, True) for c in C.ACTIONS]
        combos = [combo_row(c, True) for c in C.BUILTIN_COMBOS] + [combo_row(c, False) for c in self._user_combos]
        a = self._action
        running = None if a is None else {"id": a["id"], "name": a["name"], "by": a["by"], "step": a["step"],
                                          "steps": len(a["steps"]), "elapsed_s": round(now - a["started"], 1)}
        trig_why = base
        if trig_why is None and a is not None and any(C.mouth_at(s, 0.0) is not None for s in a["steps"]):
            trig_why = f"Running '{a['name']}' (also moves mouth): wait for it to finish"
        triggers = {"ok": trig_why is None, "why": trig_why, "mouth_pct": self._inputs["mouth"],
                    "note": "Torque off: sound only, mouth won't move until standing" if trig_why is None and (limp or relax) else None}
        last = None
        if self._last_cmd is not None and now - self._last_cmd["at"] < 15:
            last = {k: v for k, v in self._last_cmd.items() if k != "at"}
            last["age_s"] = round(now - self._last_cmd["at"], 1)
        last_action = None
        if self._last_action is not None and now - self._last_action["at"] < 15:
            last_action = {k: v for k, v in self._last_action.items() if k != "at"}
        buttons = {"stand": stand, "walk": walk, "sit": sit}
        return {"posture": pose, "posture_cn": POSTURE_CN.get(pose, pose), "block": block, "sitting": sitting,
                "buttons": buttons, "start": start, "reboot": reboot, "drive_mode": drive_mode,
                "keys": self._key_rows(buttons, sitting, skills, skills_why, known, need_drive, pol),
                "triggers": triggers, "driving": pose in DRIVING,
                "skills": skills, "skills_why": skills_why,
                "sounds": sounds, "actions": actions, "combos": combos, "running": running, "last": last,
                "last_action": last_action, "busy": self._cmd_busy}

    # ================= Command Execution =================

    def _call(self, what, method, params, by, ctx=None, posture_target=None, ordered=False, verify=None):
        if posture_target is None:
            return self._call_rpc(what, method, params, by, ctx, verify=verify)
        motion = self.motion_studio
        with (nullcontext() if ordered else self._send_lk):
            with self._lk:
                if not motion.request_current():
                    raise Refused("Motion session changed; stale command dropped")
                recording = motion.session and motion.state == "recording"
                legacy = (not recording and not ordered and not motion.posture.pending
                          and "state" not in (self._robot or {}))
                if recording:
                    motion._boundary(record=True)
                    motion._refresh_request()
            if legacy:
                return self._call_rpc(what, method, params, by, ctx)
            if recording:
                motion._flush_outputs()
            with self._lk:
                if recording and (not motion.session or motion.session.get("invalid")):
                    raise Refused("Motion session ended; sit/stand request cancelled")
                token = motion.begin_posture(posture_target, manual=not recording and not ordered)
                origin = motion.recording_origin()
            if not token["command"]:
                with self._lk:
                    motion.posture.accepted(token, self.clock())
                return {"accepted": True, "reason": "Already in target posture"}
            return self._call_rpc(what, method, params, by, ctx, posture_token=token, record_origin=origin)

    def _call_rpc(self, what, method, params, by, ctx=None, posture_token=None, record_origin=None,
                  verify=None):
        """Execute an RPC call, log timing, and translate errors to Refused."""
        if self.robot is None:
            raise Refused("robotd address is not configured")
        if self._update_blocked() and not neutral_robot_command(method, params):
            raise Refused('Software / robotd update protection still active; motion request cancelled')
        with self._lk:
            if not self.motion_studio.request_current():
                raise Refused("Motion session changed; stale command dropped")
        if method == "robot.sound":
            record_origin = self.motion_studio.recording_origin()
        skill_fence = self.motion_studio.note_skill() if method == "robot.do" and posture_token is None else None
        with log_cmd(self.log, what, method, who=by, args=params or None, ctx=ctx) as t:
            try:
                r = self.robot.call(method, params, CMD_TIMEOUT_S)
            except Unreachable as e:
                if posture_token:
                    self.motion_studio.posture_reply(posture_token, None, reason="Connection lost")
                t.fail(f"Send failed: {e}", hint="Is robotd offline? Check Overview")
                raise Refused(f"Send failed: {e}") from None
            except Timeout:
                if posture_token:
                    self.motion_studio.posture_reply(posture_token, None, reason="Reply timeout")
                t.fail(f"robotd did not reply within {CMD_TIMEOUT_S:g}s", hint="It may be stalled: check robotd logs and E-Stop if needed")
                raise Refused(f"robotd did not reply within {CMD_TIMEOUT_S:g}s") from None
            except RpcError as e:
                if posture_token:
                    self.motion_studio.posture_reply(posture_token, False)
                why = plain_refusal(e.message)
                t.fail(f"robotd error: {e.message}", raw={"code": e.code, "message": e.message}, hint=why)
                raise Refused(why) from None
            if isinstance(r, dict) and r.get("accepted") is False:
                if posture_token:
                    self.motion_studio.posture_reply(posture_token, False)
                self.motion_studio.rejected_skill(skill_fence)
                why = plain_refusal(r.get("reason"))
                t.fail(f"robotd rejected: {r.get('reason')}", raw=r, hint=why)
                raise Refused(why)
            if verify is not None:
                why = verify()
                if why:
                    t.fail(f"robotd accepted, but execution unconfirmed: {why}", raw=r, hint=why)
                    raise Refused(why)
            if posture_token:
                if not isinstance(r, dict) or r.get("accepted") is not True:
                    self.motion_studio.posture_reply(posture_token, None, reason="Ambiguous reply format")
                    raise Refused(self.motion_studio.posture.error)
                self.motion_studio.posture_reply(posture_token, True, origin=record_origin)
            t.ok(r, note=(r.get("reason") or "") if isinstance(r, dict) else "")
        if method == "robot.sound":
            self.motion_studio.accepted_from(record_origin, method, params)
        return r

    @staticmethod
    def _policy_refusal(bus):
        """Extract policy refusal reason from busStatus (last_enable_refusal or home_start_guard.reason)."""
        if not isinstance(bus, dict):
            return None
        why = bus.get("last_enable_refusal")
        if isinstance(why, str) and why:
            return why
        guard = bus.get("home_start_guard")
        if isinstance(guard, dict):
            why = guard.get("reason")
            if isinstance(why, str) and why:
                return why
        return None

    def _verify_policy_started(self):
        """Poll busStatus after robot.enable until policy_enabled is True or timeout (using iteration count)."""
        step = max(0.01, float(self.w.get("walk_verify_poll", WALK_VERIFY_POLL_S)))
        span = max(step, float(self.w.get("walk_verify", WALK_VERIFY_S)))
        bus = None
        for _ in range(int(span / step) + 1):
            try:
                r = self.robot.call("robot.busStatus", {}, CMD_TIMEOUT_S)
            except (Unreachable, Timeout):
                r = None
            if isinstance(r, dict):
                bus = r
                if r.get("policy_enabled") is True:
                    return None
            self._cancel_wait(step)
        return self._policy_refusal(bus) or "robotd did not enable walking policy (may still be homing or tilted)"

    def _motion_cmd(self, op, args, conn, by, label=None):  # noqa: C901
        now = self.clock()
        what = {"stand": "Stand Up", "walk": "Start Walking", "walk_stop": "Stop (Stand)", "sit": "Sit Down"}.get(op)
        urgent = op == "walk_stop"
        exclusive = op not in ("walk_stop", "sound")
        with self._lk:
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale command dropped"}
            if self._cmd_busy and exclusive:
                raise Refused("Previous command still waiting for reply; wait a moment")
            mv = self._motion_view(now)
            if op == "skill":
                name = args.get("name")
                row = next((s for s in mv["skills"] if s["name"] == name), None)
                if row is None:
                    raise Refused(mv["skills_why"] or f"This duck does not have '{C.SKILL_CN.get(name, name)}'")
                what = f"Skill: {row['label']}"
                ok, why = row["ok"], row["why"]
            elif op == "sound":
                tag = args.get("tag")
                row = next((s for s in mv["sounds"] if s["tag"] == tag), None)
                if row is None:
                    raise Refused(f"Unknown sound: {tag}")
                what = f"Sound: {row['label']}"
                ok, why = row["ok"], row["why"]
            else:
                btn = next((x for x in mv["buttons"].values() if x["op"] == op), None)
                if btn is None:
                    pose = mv["posture_cn"]
                    msg = {"walk": "Already 'Ready to Walk': push sticks to walk" if mv["posture"] in DRIVING else f"Currently {pose}; 'Start Walking' unavailable",
                           "walk_stop": f"Currently {pose}; not walking"}.get(op, "Cannot run this command right now")
                    self._last_cmd = {"what": label or what, "by": by, "ok": False, "reason": msg, "at": now}
                    raise Refused(msg)
                ok, why = btn["ok"], btn["why"]
            what = label or what
            if ok and op not in ("sound", "walk_stop"):
                held = self._lease_holder(now)
                if held is not None and held["conn"] != conn:
                    ok, why = False, f"{held['by'] or 'Another page'} is driving sticks: wait for release or click 'Take Over'"
            if not ok:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": why, "at": now}
                raise Refused(why)
            if exclusive:
                self._cmd_busy = True
            pose = mv["posture"]
            stand_call = mv["buttons"]["stand"]["call"]
        try:
            ctx = {"posture": pose}
            if op == "stand":
                if stand_call == "robot.init":
                    r = self._call(what, "robot.init", {}, by, ctx)
                else:
                    r = self._call(what + " (sitting: sit_toggle)", "robot.do", {"skill": "sit_toggle"}, by, ctx, posture_target="stand")
            elif op == "walk":
                r = self._call(what, "robot.enable", {"on": True, "toggle": False}, by, ctx,
                               verify=self._verify_policy_started)
            elif op == "walk_stop":
                with self._lk:
                    d = self._driver
                self._halt("Clicked Stop", d["conn"] if d else None, clear_all=True)
                self._call("Stop: zero velocity", "robot.stop", {}, by, ctx)
                for _ in range(int(WALK_STOP_WAIT_S / 0.05)):
                    applied = (self._robot or {}).get("applied")
                    if not (isinstance(applied, list) and any(abs(v) > 0.01 for v in applied if isinstance(v, (int, float)))):
                        break
                    self._cancel_wait(0.05)
                r = self._call(what, "robot.enable", {"on": False, "toggle": False}, by, ctx)
            elif op == "sit":
                r = self._call(what, "robot.do", {"skill": "sit_toggle"}, by, ctx, posture_target="sit")
            elif op == "skill":
                r = self._call(what, "robot.do", {"skill": args.get("name")}, by, ctx)
            else:
                r = self._call(what, "robot.sound", {"tag": args.get("tag")}, by, ctx)
        except Refused as e:
            with self._lk:
                self._last_cmd = {"what": what, "by": by, "ok": False, "reason": str(e), "at": self.clock()}
            raise
        finally:
            if exclusive:
                with self._lk:
                    self._cmd_busy = False
        note = r.get("reason") if isinstance(r, dict) else None
        with self._lk:
            self._last_cmd = {"what": what, "by": by, "ok": True, "reason": note, "at": self.clock()}
        return {"accepted": True, "reason": note, "did": op}

    # ================= Actions & Combos =================

    def _find_combo(self, cid):
        for c in C.ACTIONS + C.BUILTIN_COMBOS + self._user_combos:
            if c["id"] == cid:
                return c
        return None

    def _action_start(self, args, conn, by):
        cid = args.get("id")
        combo = self._find_combo(cid)
        if combo is None:
            raise Refused(f"Unknown action: {cid}")
        press = self._seq({"seq": args.get("press")})
        now = self.clock()
        with self._lk:
            if not self.motion_studio.request_current():
                return {"dropped": "Motion session changed; stale action request dropped"}
            a = self._action
            if a is not None:
                if a["conn"] == conn and a["id"] == cid:
                    a["hold"] = now
                    a["press"] = press
                    return {"running": cid}
                raise Refused(f"Running '{a['name']}' ({a['by'] or 'page'}): wait for it to finish or release")
            mv = self._motion_view(now)
            row = next((x for x in mv["actions"] + mv["combos"] if x["id"] == cid), None)
            if row is None or not row["ok"]:
                raise Refused((row or {}).get("why") or "Cannot run right now")
            self._take_lease(conn, by, now)
            a = {"id": cid, "name": combo["name"], "conn": conn, "by": by, "started": now, "hold": now, "press": press,
                 "stop": threading.Event(), "why": None, "step": 0, "steps": list(combo["steps"]),
                 "motion_epoch": self.motion_studio.epoch}
            self._action = a
        self.log.info(f"Started action '{combo['name']}' ({by or 'page'})", cat="Gamepad", raw=C.describe(combo["steps"]), dedup=False)
        threading.Thread(target=self._run_action, args=(a,), name="pad-action", daemon=True).start()
        return {"started": cid, "seconds": round(C.duration(combo["steps"]), 1)}

    def _action_hold(self, conn, press=None):
        press = self._seq({"seq": press})
        with self._lk:
            a = self._action
            if (a is not None and a["conn"] == conn and conn not in self._gone
                    and (press is None or a.get("press") is None or press == a["press"])):
                a["hold"] = self.clock()
                self._touch_lease(conn, a["hold"])
                return {"running": a["id"]}
        return {"running": None}

    def _action_stop(self, conn, why="Released"):
        with self._lk:
            a = self._action
            if a is None or (conn is not None and a["conn"] != conn):
                return {}
            a["why"] = a["why"] or why
            a["stop"].set()
        return {}

    def _abort_reason(self, a, needs_policy):
        now = self.clock()
        with self._lk:
            if a["stop"].is_set():
                return a["why"] or "Stopped"
            if now - a["hold"] > HOLD_S:
                return f"Page missed {HOLD_S:g}s hold heartbeat (released, navigated away, or disconnected)"
            if self._relax_at >= a["started"]:
                return "Torque disabled"
            r = self._robot or {}
            if r.get("estop_active"):
                return "Torque disabled"
            if needs_policy and relaxed(r.get("bus")) is True:
                return "Torque off (limp)"
            if self._pad_attached(now)[0]:
                return "Physical gamepad connected"
            d = self._driver
            if d is None or d["conn"] != a["conn"]:
                return "Taken over by another page"
            base = self._base_block(now)
            if base:
                return base
            if needs_policy and self._posture() not in DRIVING:
                return "Policy stopped"
        return None

    def _notify_output(self, method, params):
        if self._update_blocked() and not neutral_robot_command(method, params):
            return False
        if method == "robot.do":
            self.motion_studio.note_skill()
        self.robot.notify(method, params)
        self.motion_studio.accepted(method, params)
        return True

    def _notify_quiet(self, method, params, action=None, cleanup=False):
        with self._send_lk:
            with self._lk:
                if action is not None and (self._action is not action
                        or action.get("motion_epoch") != self.motion_studio.epoch
                        or (action["stop"].is_set() and not cleanup)):
                    return False
            return self._notify_output(method, params)

    def _run_action(self, a):  # noqa: C901
        steps, needs = a["steps"], C.needs_policy(a["steps"])
        moves_head = any(C.head_at(s, 0.0) is not None for s in steps)
        moves_mouth = any(C.mouth_at(s, 0.0) is not None for s in steps)
        result, level = "Completed", "info"
        try:
            if self.robot is None:
                raise _Abort("robotd address is not configured")
            for i, step in enumerate(steps):
                with self._lk:
                    a["step"] = i
                why = self._abort_reason(a, needs)
                if why:
                    raise _Abort(why)
                if step["do"] == "sound":
                    self._call(f"Action '{a['name']}': sound", "robot.sound", {"tag": step["tag"]}, a["by"])
                elif step["do"] == "skill":
                    with self._lk:
                        mv = self._motion_view(self.clock())
                    row = next((s for s in mv["skills"] if s["name"] == step["name"]), None)
                    why = (row or {}).get("why") if row else f"This duck does not have '{C.SKILL_CN.get(step['name'], step['name'])}'"
                    if why:
                        raise _Abort(why)
                    if self._abort_reason(a, needs):
                        raise _Abort(self._abort_reason(a, needs))
                    self._call(f"Action '{a['name']}': {C.SKILL_CN.get(step['name'], step['name'])}", "robot.do",
                               {"skill": step["name"]}, a["by"])
                t0 = self.clock()
                dur = C.step_seconds(step)
                while True:
                    why = self._abort_reason(a, needs)
                    if why:
                        raise _Abort(why)
                    t = self.clock() - t0
                    if t >= dur:
                        break
                    h, m = C.head_at(step, t), C.mouth_at(step, t)
                    if h is not None:
                        self._notify_quiet("robot.head", dict(zip(("neck_pitch", "head_pitch", "head_yaw", "head_roll"), h)), action=a)
                    if m is not None:
                        self._notify_quiet("robot.mouth", {"open": m}, action=a)
                    a["stop"].wait(C.TICK_S)
        except _Abort as e:
            result, level = f"Stopped: {e}", "info"
        except Refused as e:
            result, level = f"Incomplete: {e}", "warn"
        except (Unreachable, Timeout) as e:
            result, level = f"Incomplete: send failed ({e})", "warn"
        except Exception as e:  # noqa: BLE001
            result, level = f"Studio error: {type(e).__name__}: {e}", "error"
            self.log.error(f"Action '{a['name']}' error", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="Studio internal error; export diagnostics")
        finally:
            for used, method, params in ((moves_head, "robot.head", {"neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.0, "head_roll": 0.0}),
                                         (moves_mouth, "robot.mouth", {"open": 0.0})):
                if used and self.robot is not None:
                    try:
                        self._notify_quiet(method, params, action=a, cleanup=True)
                    except (Unreachable, Timeout) as e:
                        self.log.warn("Failed to re-center head / close mouth after action", cat="Gamepad", step=method, raw=str(e),
                                      hint="Head or mouth may remain offset; trigger the action again or sit & stand")
            with self._lk:
                if self._action is a:
                    self._action = None
                self._last_action = {"id": a["id"], "name": a["name"], "by": a["by"], "result": result,
                                     "ok": result == "Completed", "at": self.clock()}
            cat = "Safety" if ("Torque" in result or "missed" in result or "limp" in result) else "Gamepad"
            self.log.write(f"Action '{a['name']}' {result}", cat=cat, level=level, step=f"Step {a['step'] + 1}/{len(steps)}",
                           dedup=False)

    # ================= User Combos: Save & Delete =================

    def _combo_save(self, args):
        builtin = {c["id"] for c in C.ACTIONS + C.BUILTIN_COMBOS}
        combo = C.clean_combo(args.get("combo"), builtin)
        with self._lk:
            others = [c for c in self._user_combos if c["id"] != combo["id"]]
            if len(others) >= C.MAX_USER_COMBOS:
                raise Refused(f"Maximum {C.MAX_USER_COMBOS} custom combos allowed; delete some first")
            new = others + [combo]
        path = C.save_user_combos(self.state_dir, new)
        with self._lk:
            self._user_combos = new
        self.log.info(f"Saved custom combo '{combo['name']}'", cat="Gamepad", step=path, raw=C.describe(combo["steps"]))
        return {"saved": combo["id"]}

    def _combo_delete(self, args):
        cid = args.get("id")
        with self._lk:
            if not any(c["id"] == cid for c in self._user_combos):
                raise Refused("Custom combo not found (built-in combos cannot be deleted)")
            new = [c for c in self._user_combos if c["id"] != cid]
        C.save_user_combos(self.state_dir, new)
        with self._lk:
            self._user_combos = new
        self.log.info(f"Deleted custom combo {cid}", cat="Gamepad")
        return {"deleted": cid}
