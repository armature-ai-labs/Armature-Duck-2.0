"""Gamepad page backend: status, pairing wizard, live input, E-Stop test; driving logic lives in pad_drive.py.

All 4 system ports are injected for testability (tests/test_pad.py runs with fakes without Bluetooth):
- configd   : ConfigdPort (unix:/run/configd.sock)  - pad.status, pad.pair, pad.forget
- bluetooth : BluetoothCtl (bluetoothctl session) - device discovery & fallback pairing
- tap       : PaddTap (unix:/run/padd/pad.sock)     - raw evdev input stream while page is viewed
- robot     : RobotPort (unix:/run/robotd.sock)     - robot.move/head/mouth, init/enable/do/sound/stop/busStatus, pad.bindings
"""
import codecs
import concurrent.futures
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
from collections import deque

from . import helper as helper_api
from .estop import STARTING_PHASES, relaxed, torque_off
from .pad_combo import Refused
from .motion_studio import MotionStudio, _digest
from .pad_mapping import MappingClient, MappingError, PadMapping
from .pad_drive import (DRIVING, FAST_WORKERS, MAX_V, MAX_W, RELAX_CLEAR_DELAY_S, RELAX_GUARD_S,  # noqa: F401  # MAX_V / MAX_W re-exported
                        DriveMixin, parse_speed_limits)
from .rpc import LINE_LIMIT, RpcError, Timeout, Unreachable, parse_addr
from .state import MAX_AGE
from .state import posture as _posture

SCAN_S, SCAN_MAX_S = 12, 30
PAIR_WINDOW_S = 20        # configd timeout_seconds
PAIR_CALL_MARGIN_S = 110  # configd worst-case call margin
CONNECT_WAIT_S = 10
PADD_WAIT_S = 30          # Wait for kernel input device & padd attach
PAD_PAIR_TIMEOUT_S = 60   # Helper robotctl pad pair timeout (10-120s)
WATCH_LEASE_S = 6.0       # Page heartbeat lease
KEYS_NEED = 4             # Key test requires 4 distinct keys + both sticks
TEST_TORQUE_WAIT_S = 60
TEST_PRESS_WAIT_S = 90
TEST_READBACK_S = 6.0
SELECT_LONG_S = 1.5       # Warn if Select held >= 1.5s
SHUTDOWN_HOLD_S = 2.0     # padd triggers sit-and-shutdown if Select held >= 2s
TEST_PROBE_S = 0.25       # busStatus poll interval during E-Stop test
BUS_FRESH_S = 3.0
STICK_ACTIVE = 0.15
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
WEIRD_BLE = "/var/lib/robot/weird-ble"     # Marker file for boards needing btd restart before pairing
ROBOTD_TOML = "/etc/robot/robotd.toml"      # padd [netpad] config path
FINAL = ("idle", "passed", "failed")
_HELPER_OLD = object()      # Sentinel for legacy helper without pad-pair


class BtError(Exception):
    """Bluetooth error with FAILS key and raw output."""

    def __init__(self, code, raw=""):
        super().__init__(raw or code)
        self.code, self.raw = code, raw


class NoPermission(Unreachable):
    """Socket exists but Studio user lacks permission."""


# ---------------- Human-readable failure messages ----------------

NO_RESTART_BT = "Do not run systemctl restart bluetooth: this board loses BT until reboot"
ROBOT_GROUP = "Add the Studio user to the robot group: sudo usermod -aG robot <user>, then restart Studio"
FAILS = {
    "no_adapter": ("Duck Bluetooth is not ready (hci0 not found)",
                   f"Normal during the first ~75s after boot; wait a minute and retry, or reboot the duck. {NO_RESTART_BT}"),
    "rfkill": ("Bluetooth is soft-blocked (rfkill)", "Run sudo rfkill unblock bluetooth on the duck, then retry"),
    "powered_off": ("Duck Bluetooth is powered off", "Click Retry to power on; reboot the duck if it persists"),
    "bluez_down": ("bluetoothd service is not running", f"Reboot the duck. {NO_RESTART_BT}"),
    "configd_down": ("configd service is not running", "Run sudo systemctl start configd on the duck, then retry"),
    "socket_perm": ("Studio lacks permission for /run/configd.sock", ROBOT_GROUP),
    "configd_busy": ("configd is still finishing a previous pairing", "Wait 1-2 minutes for it to finish, then try again"),
    "driving": ("Duck is walking / policy active", "Bluetooth scan/pair can freeze an active gamepad. Click Stop (Stand) or press Start first"),
    "no_btctl": ("bluetoothctl not found on duck", "Use Auto-Pair to let configd discover the controller"),
    "permission": ("Studio lacks pairing permission", "Install the root helper or run sudo robotctl pad pair over SSH"),
    "bt_permission": ("Studio lacks Bluetooth permission", "Add Studio user to bluetooth group, or run sudo robotctl pad pair over SSH"),
    "scan_failed": ("Bluetooth scan failed to start", "Another process may be scanning/pairing; wait a few seconds and retry"),
    "not_found": ("No controller found in pairing mode", "Ensure the controller LED is flashing rapidly (pairing mode) and try again"),
    "not_found_pad": ("Nearby Bluetooth devices found, but none look like a gamepad", "Check pairing mode LED or pick your device manually from the list"),
    "ambiguous": ("Multiple controllers in pairing mode", "Keep only one in pairing mode, or run Scan and pick the right one"),
    "timeout": ("Controller found, but pairing timed out", "Press the pairing button again and retry while LED flashes rapidly"),
    "rejected": ("Bluetooth pairing rejected",
             "Controller may have exited pairing mode. If persistent, set Privacy = device in /etc/bluetooth/main.conf and reboot"),
    "auth_failed": ("Controller rejected pairing (auth failed)",
                "Clear existing host pairings on the controller, re-enter pairing mode, and retry"),
    "pin": ("Controller requested a PIN", "Switch controller to Switch Pro or X-input mode and retry"),
    "key_missing": ("Bonding key mismatch (PIN or Key Missing)",
                "Click Forget on the paired controller, re-enter pairing mode, and pair again"),
    "le_abort": ("Connection aborted locally (le-connection-abort-by-local)", "Set Privacy = device in /etc/bluetooth/main.conf and reboot the duck"),
    "dhkey": ("DHKey check failed", "Try setting Privacy = off in /etc/bluetooth/main.conf and reboot the duck"),
    "not_bondable": ("Bluetooth adapter is not bondable", "Run sudo btmgmt bondable on on the duck and retry"),
    "agent": ("Failed to register Bluetooth pairing agent", "Another tool may hold the agent; wait a moment or reboot the duck"),
    "busy": ("Another pairing operation is in progress", "Wait 1-2 minutes and retry"),
    "not_trusted": ("Paired, but not marked Trusted", "Click Fix on the controller so it auto-reconnects after reboot"),
    "not_connected": ("Paired, but not connected", "Press the Home / Xbox button on the controller to wake it"),
    "padd_down": ("Controller connected, but padd is not running", "padd runs alongside robotd; start robotd and check padd logs"),
    "no_input": ("Connected, but padd sees no input device",
             "Wake the controller with Home/Xbox, check its mode, or Forget and re-pair"),
    "wrong_input": ("padd attached a different controller", "Disconnect the other controller first, then wake this one"),
    "no_robotctl": ("/usr/local/bin/robotctl not found",
                "Reinstall official firmware/robotctl or run sudo robotctl pad pair on the duck"),
    "weird_ble": ("This board requires btd pause/restart before pairing", "Install the updated helper or run sudo robotctl pad pair on the duck"),
    "cancelled": ("Stopped", "Click Start Over to pair again"),
    "other": ("Pairing failed", "Check raw details below or export diagnostics bundle"),
}

_CLASSIFY = [
    (r"org\.freedesktop\.DBus\.Error\.AccessDenied|Access denied", "bt_permission"),
    (r"Device [0-9A-Fa-f:]{17} not available", "not_found"),
    (r"ServiceUnknown|cannot reach bluetoothd|Waiting to connect to bluetoothd|org\.bluez was not provided", "bluez_down"),
    (r"No default controller|no Bluetooth adapter|Controller .* not available", "no_adapter"),
    (r"NotReady|not powered|Resource Not Ready", "powered_off"),
    (r"InProgress|In Progress|Operation already in progress|org\.bluez\.Error\.Busy", "busy"),
    (r"PIN or Key Missing|Key Missing", "key_missing"),
    (r"DHKey", "dhkey"),
    (r"le-connection-abort-by-local", "le_abort"),
    (r"cannot enter a PIN|cannot enter a passkey|Enter PIN code|Enter passkey|Confirm passkey|RequestPinCode", "pin"),
    (r"Authentication ?Failed|Authentication ?Rejected|Authentication ?Canceled", "auth_failed"),
    (r"AuthenticationTimeout|Page Timeout|page-timeout|ConnectionAttemptFailed|Host is down|did not finish pairing|timed out|Timeout", "timeout"),
    (r"No Bonding|not bondable", "not_bondable"),
    (r"Permission denied", "not_bondable"),     # BlueZ returns Permission denied (13) if unbonded
    (r"Failed to register agent|Agent is already registered|already registered", "agent"),
    (r"Failed to start discovery", "scan_failed"),
]


def classify(raw):
    """Map BlueZ / bluetoothctl / configd error text to a FAILS key."""
    text = str(raw or "")
    for pat, code in _CLASSIFY:
        if re.search(pat, text, re.I):
            return code
    return "other"


def explain(code, raw=None):
    msg, fix = FAILS.get(code, FAILS["other"])
    return {"code": code, "msg": msg, "fix": fix, "raw": (str(raw)[-600:] if raw else None)}


# ---------------- Gamepad detection (matches configd/src/pad.rs) ----------------

def gamepad_evidence(name, icon=None, cls=None, appearance=None):
    """Returns "classified", "name", or None."""
    if icon == "input-gaming":
        return "classified"
    if isinstance(cls, int):
        if (cls >> 8) & 0x1F == 0x05 and ((cls >> 2) & 0x3F) & 0x0F in (0x01, 0x02):
            return "classified"
    if appearance == 0x03C4:
        return "classified"
    lower = (name or "").lower()
    if any(k in lower for k in ("controller", "gamepad", "joystick", "dualsense", "dualshock")):
        return "name"
    return None


def same_pad(a, b):
    """Same last 3 MAC bytes indicate dual-mode (LE + Classic) of the same controller."""
    ta, tb = (a or "").upper().split(":")[-3:], (b or "").upper().split(":")[-3:]
    return len(ta) == 3 and ta == tb


def pad_kind(name, vendor=None):
    """Return controller family: xbox / ps / switch / generic."""
    n = (name or "").lower()
    if vendor == 0x045E or "xbox" in n:
        return "xbox"
    if vendor == 0x054C or any(k in n for k in ("dualsense", "dualshock", "playstation")) or n.strip() == "wireless controller":
        return "ps"
    if vendor == 0x057E or any(k in n for k in ("pro controller", "joy-con", "nintendo", "switch")):
        return "switch"
    return "generic"


KIND_LABEL = {"xbox": "Xbox", "ps": "PlayStation", "switch": "Switch Pro", "generic": ""}
ICON_CN = {"audio-headset": "Headset", "audio-headphones": "Headphones", "audio-card": "Speaker", "phone": "Phone", "computer": "Computer",
           "input-keyboard": "Keyboard", "input-mouse": "Mouse", "input-tablet": "Tablet", "video-display": "Display"}


# ---------------- Live evdev input state ----------------

BTN_ROLE = {0x130: "south", 0x131: "east", 0x133: "north", 0x134: "west", 0x136: "lb", 0x137: "rb",
            0x138: "lt", 0x139: "rt", 0x13A: "select", 0x13B: "start", 0x13C: "home", 0x13D: "l3", 0x13E: "r3",
            0x220: "du", 0x221: "dd", 0x222: "dl", 0x223: "dr", 158: "select", 172: "home"}
# Xbox reports printed X/Y labels; PS/Switch report positional North/West
XBOX_SWAP = {0x133: "west", 0x134: "north"}
XBOX_SWAP_VERIFIED = False
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y = 0, 1, 2, 3, 4, 5, 9, 10, 16, 17
EV_KEY, EV_ABS = 1, 3


class LiveInput:
    """Accumulate evdev events from padd tap into current button/axis state."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.device = None
        self.kind = None
        self.buttons = {}            # role -> bool
        self.other = {}              # Unmapped keys: name -> bool
        self.axes = {}               # role -> normalized value (-1..1 or 0..1)
        self.amap = {}               # evdev axis code -> (role, min, max)
        self.last_at = None
        self.activity_at = None      # Last real input activity timestamp
        self.seen = set()

    def _role(self, code):
        if self.kind == "xbox" and code in XBOX_SWAP:
            return XBOX_SWAP[code]
        return BTN_ROLE.get(code)

    def attach(self, device, now):
        self.reset()
        self.device = device if isinstance(device, dict) else {}
        self.kind = pad_kind(self.device.get("name"), self.device.get("vendor"))
        axes = {a.get("code"): a for a in self.device.get("axes") or [] if isinstance(a, dict)}
        amap = {ABS_X: "lx", ABS_Y: "ly", ABS_HAT0X: "hatx", ABS_HAT0Y: "haty"}
        if ABS_RX in axes:                     # xpad / hid-playstation / hid-nintendo: right stick is RX/RY
            amap.update({ABS_RX: "rx", ABS_RY: "ry"})
            if ABS_BRAKE in axes or ABS_GAS in axes:
                amap.update({ABS_BRAKE: "lt", ABS_GAS: "rt"})
            else:
                amap.update({ABS_Z: "lt", ABS_RZ: "rt"})
        else:                                  # BLE hid-generic Xbox: right stick Z/RZ, triggers BRAKE/GAS
            amap.update({ABS_Z: "rx", ABS_RZ: "ry", ABS_BRAKE: "lt", ABS_GAS: "rt"})
        for code, role in amap.items():
            a = axes.get(code)
            if a is not None:
                self.amap[code] = (role, a.get("min", -32768), a.get("max", 32767))
                self._set_axis(code, a.get("value", 0))
        for b in self.device.get("buttons") or []:
            if isinstance(b, dict):
                self._set_key(b.get("code"), b.get("name"), bool(b.get("pressed")), track=False)
        self.last_at = now

    def _set_axis(self, code, raw):
        role, lo, hi = self.amap[code]
        if not isinstance(raw, (int, float)):
            return
        if role in ("hatx", "haty"):
            v = 0 if raw == 0 else (1 if raw > 0 else -1)
            neg, pos = ("dl", "dr") if role == "hatx" else ("du", "dd")
            self.buttons[neg], self.buttons[pos] = v < 0, v > 0
            return
        if hi == lo:
            return
        if role in ("lt", "rt"):
            v = (raw - lo) / (hi - lo)
            v = max(0.0, min(1.0, v))
        else:
            mid, half = (hi + lo) / 2, (hi - lo) / 2
            v = max(-1.0, min(1.0, (raw - mid) / half))
        self.axes[role] = v

    def _set_key(self, code, name, down, track=True):
        role = self._role(code)
        if role:
            self.buttons[role] = down
            if down and track:
                self.seen.add(role)
        else:
            self.other[name or f"Key 0x{code or 0:03x}"] = down

    def frame(self, frame, now):
        """Return Select edge in this frame: True pressed, False released, None unchanged."""
        select = None
        for ev in (frame or {}).get("events") or []:
            if not isinstance(ev, dict):
                continue
            kind, code, value = ev.get("kind"), ev.get("code"), ev.get("value", 0)
            if kind == EV_KEY:
                if value == 2:               # Key repeat: not released
                    continue
                was = self.buttons.get(self._role(code)) if self._role(code) else None
                self._set_key(code, ev.get("name"), value != 0)
                if self._role(code) == "select" and was != (value != 0):
                    select = value != 0
                self.activity_at = now
            elif kind == EV_ABS and code in self.amap:
                self._set_axis(code, value)
                role = self.amap[code][0]
                if role in ("hatx", "haty"):
                    if value:
                        self.seen.add({("hatx", True): "dr", ("hatx", False): "dl", ("haty", True): "dd",
                                       ("haty", False): "du"}[(role, value > 0)])
                    self.activity_at = now
        for stick, (x, y) in (("ls", ("lx", "ly")), ("rs", ("rx", "ry"))):
            mag = math.hypot(self.axes.get(x, 0.0), self.axes.get(y, 0.0))
            if mag > STICK_ACTIVE:
                self.activity_at = now
            if mag > 0.5:
                self.seen.add(stick)
        for t in ("lt", "rt"):
            if self.axes.get(t, 0.0) > 0.5:
                self.seen.add(t)
        self.last_at = now
        return select

    def view(self, now):
        d = self.device or {}
        return {"device": {"name": d.get("name"), "node": d.get("node"), "unique": d.get("unique"), "kind": self.kind,
                           "bus": "Bluetooth" if d.get("bus") == 5 else "USB" if d.get("bus") == 3 else None,
                           "keys_unverified": self.kind == "xbox" and not XBOX_SWAP_VERIFIED} if self.device else None,
                "buttons": {k: True for k, v in self.buttons.items() if v},
                "axes": {k: round(v, 2) for k, v in self.axes.items()},
                "other": sorted(k for k, v in self.other.items() if v),
                "last_ms": None if self.last_at is None else int((now - self.last_at) * 1000)}


# ---------------- Main PadOps ----------------

PAIR_STEPS = [("pair", "Pair"), ("trust", "Trust (auto-reconnect)"), ("connect", "Connect"),
              ("padd", "padd attach"), ("keys", "Key test: press buttons & move sticks")]
SCAN_STEPS = [("ready", "Bluetooth ready"), ("scan", "Scan nearby gamepads (pairing LED)"), ("pick", "Select gamepad")]


def _idle_wizard():
    return {"state": "idle", "phase": None, "steps": [], "devices": [], "picked": None, "error": None, "via": None,
            "started": None, "elapsed_s": None, "result_name": None, "seen": [], "retry": None, "mac": None,
            "helper_available": None}


def _torque_on(bus):
    """Check whether busStatus confirms torque is ON."""
    if not isinstance(bus, dict):
        return False
    if bus.get("mode") == "commissioning":
        return bus.get("torque_state") == "on"
    native = bus.get("native")
    return isinstance(native, dict) and native.get("torque_state_confirmed") is True


class PadOps(DriveMixin):
    def __init__(self, log, configd=None, bt=None, tap=None, robot=None, *, helper=None, clock=time.monotonic,
                 exists=os.path.exists, read_text=None, robotd_toml=ROBOTD_TOML, weird_ble=WEIRD_BLE, waits=None,
                 state_dir=None, audio=None, mapping_client=None):
        self.log = log
        self.configd, self.bt, self.tap, self.robot = configd, bt, tap, robot
        self.audio = audio
        self.helper = helper_api if helper is None else helper
        self._helper_pad_ok = None       # Helper pad-pair support cache
        self.clock = clock
        self.exists = exists
        self.read_text = read_text or _read_text
        self.robotd_toml, self.weird_ble_path = robotd_toml, weird_ble
        w = {"poll": 0.1, "status_watch": 2.0, "status_job": 0.5, "padd": PADD_WAIT_S, "connect": CONNECT_WAIT_S,
             "readback": TEST_READBACK_S, "torque": TEST_TORQUE_WAIT_S, "press": TEST_PRESS_WAIT_S,
             "lease": WATCH_LEASE_S, "scan_tick": 0.5, "pair_window": PAIR_WINDOW_S, "scan_min": 3,
             "pad_timeout": PAD_PAIR_TIMEOUT_S,
             "reconnect_after": 2.0, "reconnect_interval": 4.0, "reconnect_timeout": 5.0,
             "probe": TEST_PROBE_S, "shutdown_hold": SHUTDOWN_HOLD_S, "select_long": SELECT_LONG_S,
             "relax_guard": RELAX_GUARD_S, "files": 10.0, "bindings": 10.0}
        w.update(waits or {})
        self.w = w
        self._lk = threading.RLock()
        self._wake = threading.Event()
        self._closing = False
        self._worker = None
        self._watch_until = 0.0
        self._status = None              # Latest configd pad.status result
        self._status_at = None
        self._status_error = None
        self._status_due = 0.0
        self._robot = {}                 # Latest robotd view fed from app.py
        self._robot_at = None
        self._netpad = None
        self._netpad_why = None
        self._worker_error = None        # Watchdog thread error
        self._snap_errors = set()
        self.live = LiveInput()
        self.tap_state, self.tap_why = "off", None
        self._tap_on = False
        self._attach_wait_since = None
        self._tap_err_said = False
        self.bindings = None
        self._bindings_at = -1e9
        self._bindings_err = None
        self.wiz = _idle_wizard()
        self._job = None                 # (id, thread)
        self._job_id = 0
        self._cancel = threading.Event()
        self._configd_thread = None      # Active configd pad.pair thread
        self._configd_until = 0.0
        self.test = {"state": "idle"}
        self._test_evt = threading.Event()
        self.on_estop_passed = None          # Callback when E-Stop test passes
        self.battery_shutdown = None         # robotd [safety] battery_empty_shutdown
        self._attach_said = None             # Deduplicate tap attach log per device
        self._pad_config = robotd_toml       # Active config file used by padd.service
        self._drive_init(state_dir)
        self.motion_studio = MotionStudio(self)
        self.mapping = PadMapping(mapping_client, guard=self._mapping_guard, clock=clock)
        # Dedicated fast thread pool for real-time drive/head/stop commands
        # 4 workers so Stop and recenter never queue behind sticks/keys
        self._fast_pool = concurrent.futures.ThreadPoolExecutor(max_workers=FAST_WORKERS, thread_name_prefix="pad-fast")
        # Files read in background thread; snapshot() only reads cached values
        self._files_stop = threading.Event()
        self._refresh_files()
        threading.Thread(target=self._files_loop, name="pad-files", daemon=True).start()
        self._drive_start()

    # ================= Public API =================

    def _mapping_guard(self):
        with self._lk:
            reason = self._base_block(self.clock(), ignore_mapping=True)
            if reason:
                raise MappingError(reason)
            # robot.policies.enabled is the configured model-loading switch,
            # not the live controller state. BusDynamic is flattened on wire.
            bus=self._robot.get('bus') or {}
            pose=self._robot.get('posture')
            native=bus.get('native') or {}
            guard=bus.get('home_start_guard')
            stable=(pose=='limp' and native.get('torque_state_confirmed') is False) or (
                pose=='holding' and native.get('torque_state_confirmed') is True
                and bus.get('homed') is True and (guard is None or isinstance(guard,dict) and guard.get('ready') is True))
            if (bus.get('phase')!='control' or bus.get('policy_enabled') is not False
                    or bus.get('home_recovery') is not False or bus.get('torque_off_pending') is not False or not stable):
                raise MappingError('Click Stop (Stand) and wait until standing before mapping')
            if self._cmd_busy or self._job_alive() or self.motion_studio.session is not None or self.test.get('state') not in FINAL:
                raise MappingError('Finish pairing, motion, recording/playback, and E-Stop test before mapping')
            if (self._lease_holder(self.clock()) is not None or self._action is not None
                    or self._modes or any(not item.get('end') and item.get('held') for item in self._keys.values())
                    or any(self._latch.values())):
                raise MappingError('Release web drive controls and exit drive session before mapping')
            extra=getattr(self,'mapping_guard',None)
            if extra:extra()

    def mark_gone(self, conn):
        self.mapping.leave(conn)
        return super().mark_gone(conn)

    def handle(self, args, by=None, conn=None):
        from .updates import safe_during_update
        if self._update_blocked() and not safe_during_update('pad.op',args):
            raise Refused('Update guard active; only E-Stop and Audio Off are allowed')
        with self.motion_studio.request():
            return self._handle_request(args, by=by, conn=conn)

    def _handle_request(self, args, by=None, conn=None):
        """Handle UI command (runs in thread). Raises Refused on invalid state/args."""
        op = args.get("op")
        if conn is not None and conn in self._gone and op not in ("watch", "stop", "action_stop", "drive"):
            raise Refused("Client disconnected")      # Late queued command
        if op == 'mapping':
            try:
                result = self.mapping.handle(args, conn)
            except MappingError as error:
                raise Refused(str(error)) from None
            self._watch_until = self.clock() + self.w['lease']
            self._ensure_worker()
            return result
        if self.mapping.blocked:
            safe = op in ('watch','refresh','stop','action_stop') or (op=='drive' and args.get('open') is False)
            safe = safe or (op=='keys' and not args.get('held')) or (op=='audio' and args.get('listening') is False)
            if not safe:
                raise Refused('Gamepad mapping/recentering active; save or cancel first (E-Stop still works)')
        if op == "motion":
            return self.motion_studio.handle(args, conn, by)
        if op == "audio":
            values = {key:value for key,value in args.items() if key != "op"}
            if not values or set(values)-{"listening","music"} or any(type(value) is not bool for value in values.values()):
                raise Refused("Audio only supports listen and music toggles")
            if conn is None or conn in self._gone or conn in self._closed:
                raise Refused("Return to the Gamepad Drive tab to control audio")
            if self.audio is None:
                raise Refused("Audio service not configured; check duck-echo")
            if values.get("listening") is False or values.get("music") is False:
                with self._lk:
                    if (self.motion_studio.session or {}).get("idle_kind") == "music":
                        self.motion_studio.invalidate("Listen or music mode disabled")
            try:
                return {"audio":self.audio.set(**values)}
            except (OSError,ValueError,TypeError):
                raise Refused("Cannot reach audio service; check duck-echo") from None
        if self.motion_studio.before(args, conn, by) is False:
            return {"dropped": "Motion session changed; stale request dropped"}
        if op == "watch":
            self._watch()
            return {}
        if op == "refresh":
            self._poll_status()
            with self._lk:
                return {"status": self._status_view(self.clock())}
        if op == "scan":
            secs = args.get("seconds", SCAN_S)
            try:
                secs = max(self.w["scan_min"], min(SCAN_MAX_S, float(secs)))
            except (TypeError, ValueError):
                raise Refused("Scan duration must be a number (seconds)") from None
            if not math.isfinite(secs):
                raise Refused("Scan duration must be a number (seconds)")
            return self._start_job("scan", self._job_scan, secs)
        if op == "pair":
            mac = args.get("mac")
            if mac is not None and not MAC_RE.match(str(mac)):
                raise Refused("Invalid MAC format: expected AA:BB:CC:DD:EE:FF")
            return self._start_job("pair", self._job_pair, (mac.upper() if mac else None, by))
        if op == "cancel":
            return self._cancel_job()
        if op == "forget":
            mac = str(args.get("mac") or "")
            if not MAC_RE.match(mac):
                raise Refused("Invalid MAC format: expected AA:BB:CC:DD:EE:FF")
            return self._forget(mac.upper(), by)
        if op == "keys_done":
            return self._keys_done(manual=True)
        if op == "wizard_reset":
            with self._lk:
                if self._job_alive():
                    raise Refused("Scan/pairing in progress: click Stop first")
                self.wiz = _idle_wizard()
            return {}
        if op == "estop_test":
            return self._estop_test(args.get("action"))
        # ---- Drive commands (pad_drive.py) ----
        if op == "drive":
            return self._drive(args, conn, by)
        if op == "move":
            return self._move(args, conn, by)
        if op == "sticks":
            return self._sticks(args, conn, by)
        if op == "stop":
            return self._stop_move(args, conn, by)
        if op == "mode":
            return self._mode_op(args, conn, by)
        if op == "keys":
            return self._keys_op(args, conn, by)
        if op == "start":
            return self._start(args, conn, by)
        if op == "key":
            return self._key_press(args, conn, by)
        if op == "reboot":
            return self._reboot(args, conn, by)
        if op == "drive_mode":
            return self._drive_mode(args, conn, by)
        if op in ("stand", "walk", "walk_stop", "sit", "skill", "sound"):
            return self._motion_cmd(op, args, conn, by)
        if op == "action_start":
            return self._action_start(args, conn, by)
        if op == "action_hold":
            return self._action_hold(conn, args.get("press"))
        if op == "action_stop":
            return self._action_stop(conn, "UI stop / release")
        if op == "combo_save":
            return self._combo_save(args)
        if op == "combo_delete":
            return self._combo_delete(args)
        raise Refused(f"Unknown command: {op}")

    def watched(self):
        """True when Gamepad page is viewed, pairing, or testing E-Stop (10 Hz push)."""
        with self._lk:
            return self._busy_reason(self.clock())

    def feed_robot(self, view):
        """Ingest latest robotd view from app.py event loop."""
        if not isinstance(view, dict):
            return
        relax_new = False
        with self._lk:
            self._robot = view
            self._robot_at = self.clock()
            st = view.get("pad_status")
            at = view.get("pad_status_at")
            # Reuse robotd 5s pad.status poll when nobody is viewing Gamepad page
            if isinstance(st, dict) and at is not None and (self._status_at is None or at > self._status_at):
                self._status, self._status_at, self._status_error = st, at, None
            elif view.get("pad_status_error") and self._status_at is None:
                self._status_error = view.get("pad_status_error")
            relax_new = self._relax_seen(view, self.clock())
            self._drive_feed(view)          # Track posture changes
        if relax_new:
            self._action_stop(None, "Torque released")
            self._request_clear(None, "Torque released (readback)", RELAX_CLEAR_DELAY_S)   # Flag only
            self._test_evt.set()

    def snapshot(self, robot=None, conn=None):
        if robot is not None:
            self.feed_robot(robot)
        now = self.clock()
        with self._lk:
            status = self._status_view(now)
            live = self.live.view(now)
            live.update(state=self.tap_state if self._tap_on else "off", why=self.tap_why)
            mapping = self.mapping.snapshot(conn)
            mapped = mapping.get('mapped')
            if (mapping.get('available') and mapping.get('profile') and mapped
                    and (mapping.get('device') or {}).get('node') == (live.get('device') or {}).get('node')):
                live.update(buttons=mapped['buttons'], axes=mapped['axes'], mapping_custom=True)
                if live.get('device'):
                    live['device']['keys_unverified'] = False
            wiz = dict(self.wiz)
            if wiz.get("started") is not None and wiz["state"] == "running":
                wiz["elapsed_s"] = now - wiz["started"]
            wiz.pop("started", None)
            wiz["seen"] = sorted(self.live.seen) if wiz.get("phase") == "keys" else []
            wiz["configd_busy_s"] = self._configd_busy_left(now)
            wiz["helper_available"] = self._helper_pad_ok
            test = {k: v for k, v in self.test.items() if not k.startswith("_")}
            if test.get("state") == "pressed" and self.test.get("_pressed_at") is not None:
                test["held_s"] = round(now - self.test["_pressed_at"], 1)
            attached, pad_why = self._pad_attached(now)
            d = self._lease_holder(now)
            ex = self._mode_exit.get(conn)
            ks = self._keys.get(conn)
            virtual = {"owner": self._owner(now), "last": self.virtual["last"],
                       "age_ms": None if self.virtual["at"] is None else int((now - self.virtual["at"]) * 1000),
                       "mirror": attached is True, "mirror_why": pad_why if attached else None,
                       "pad_unknown": pad_why if attached is None else None, "limits": self._speed_limits(),
                       "driver": None if d is None else {"by": d["by"], "mine": d["conn"] == conn,
                                                         "for_s": int(now - d["since"])},
                       # Per-connection mode, exit reason, and held keys
                       "mode": self._mode_of(conn), "mode_exit": None if ex is None or now - ex["at"] > 10 else
                       {"why": ex["why"], "age_s": round(now - ex["at"], 1)},
                       "keys": [] if ks is None or ks.get("end") else sorted(ks["held"]),
                       "inputs": self._inputs, "pad_imu_head": self._imu_head, "pad_imu_head_why": self._imu_head_why,
                       "latch": {k: v is not None for k, v in self._latch.items()}}
            return {"t": "pad", "status": status, "estop": self._estop_checks(now, status), "live": live,
                    "wizard": wiz, "estop_test": test, "virtual": virtual, "motion": self._motion_view(now),
                    "motion_studio": self.motion_studio.snapshot(conn, now),
                    "audio": self.audio.snapshot() if self.audio is not None else {"available":False,"why":"Audio service not configured"},
                    "bindings": self.bindings, "mapping": mapping}

    def safe_snapshot(self, conn=None):
        """Safe snapshot for app push loop: logs unique errors once and returns None on failure."""
        try:
            return self.snapshot(conn=conn)
        except Exception as e:  # noqa: BLE001
            key = f"{type(e).__name__}: {e}"[:200]
            if key not in self._snap_errors:
                self._snap_errors.add(key)
                self.log.error("Gamepad snapshot error, skipping frame", cat="Gamepad", raw=key, exc=True,
                               hint="Internal Studio error; export diagnostics bundle")
            return None

    def diag(self):
        """Redacted snapshot for diagnostics bundle (masks nearby BT names/MACs)."""
        snap = self.safe_snapshot()
        if snap is None:
            return {"error": "snapshot error, see logs"}
        wiz = snap.get("wizard") or {}
        devs = wiz.get("devices") or []
        wiz["devices"] = {"count": len(devs), "pads": sum(1 for d in devs if d.get("pad")),
                          "items": [{"name": _mask_name(d.get("name")), "mac": _mask_mac(d.get("mac")), "pad": d.get("pad"),
                                     "rssi": d.get("rssi")} for d in devs[:20]]}
        for k in ("mac", "picked"):
            if wiz.get(k):
                wiz[k] = _mask_mac(wiz[k])
        dev = (snap.get("live") or {}).get("device") or {}
        if dev.get("unique"):
            dev["unique"] = _mask_mac(dev["unique"])
        mapping_dev=(snap.get('mapping') or {}).get('device') or {}
        if mapping_dev.get('unique'):
            mapping_dev['unique']=_mask_mac(mapping_dev['unique'])
        return snap

    def estop_ok(self):
        """Return only the E-Stop readiness summary."""
        now = self.clock()
        with self._lk:
            return self._estop_checks(now, self._status_view(now))["ok"]

    def onboard_view(self):
        """Fast in-memory summary for onboarding steps 3-4."""
        now = self.clock()
        with self._lk:
            status = self._status_view(now)
            attached = self._tap_on and self.tap_state == "attached"
            dev = dict(self.live.device or {}) if attached else None
            es = self._estop_checks(now, status)
        return {"known": status["known"], "error": status["error"], "pads": status["pads"], "active": status["active"],
                "attached": attached, "device": {"name": (dev or {}).get("name"), "unique": (dev or {}).get("unique")} if dev else None,
                "estop": {"state": es["state"], "checks": es["checks"]}}

    def _estop_passed_record(self):
        """Notify onboarding hook when E-Stop test passes."""
        hook = getattr(self, "on_estop_passed", None)
        if hook is None:
            return
        now = self.clock()
        with self._lk:
            status = self._status_view(now)
            a = status["active"] or {}
            dev = self.live.device or {}
            mac = a.get("mac") or (dev.get("unique") if self.tap_state == "attached" else None)
            name = a.get("name") or dev.get("name")
            bus = (self._robot or {}).get("bus") if isinstance((self._robot or {}).get("bus"), dict) else {}
            rec = {"ms": self.test.get("ms"), "mac": mac, "name": name, "build": bus.get("build")}
        try:
            hook(rec)
        except Exception as e:  # noqa: BLE001
            self.log.error("Failed to record E-Stop pass", cat="Safety", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="E-Stop passed, but onboarding state failed to save")

    def close(self):
        self._closing = True
        self.mapping.close()
        self._cancel.set()
        self._wake.set()
        self._test_evt.set()
        self._action_stop(None, "Studio closing")
        self._drive_close()                 # Best-effort recenter and key release
        if self.audio is not None:
            self.audio.close()
        self._files_stop.set()
        try:
            self._fast_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        if self.tap is not None and self._tap_on:
            try:
                self.tap.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.bt is not None:
            try:
                self.bt.close()
            except Exception:  # noqa: BLE001
                pass

    # ================= Status =================

    def _status_view(self, now):
        st = self._status if isinstance(self._status, dict) else None
        pads = []
        for p in (st or {}).get("pads") or []:
            if isinstance(p, dict):
                pads.append({"mac": p.get("mac"), "name": p.get("name"), "paired": p.get("paired"),
                             "trusted": p.get("trusted"), "connected": p.get("connected"),
                             "kind": pad_kind(p.get("name"))})
        active = next((p for p in pads if p["connected"]), None)
        age = None if self._status_at is None else now - self._status_at
        known = st is not None and age is not None and age < 20
        return {"known": known, "error": None if known else self._status_error,
                "driver": (st or {}).get("driver") if known else None, "pads": pads if known else [],
                "active": active if known else None, "age_ms": None if age is None else int(age * 1000),
                "netpad": self._netpad_value(now), "netpad_why": self._netpad_why}

    def _netpad_value(self, now=None):
        """Cached [netpad] enabled from robotd.toml (None if unreadable)."""
        return self._netpad

    def _refresh_files(self):
        """Read padd.service unit and active robotd.toml ([netpad], [pad_imu_head_control]) into cache.
         (fork padd main.rs:692, 776)."""
        try:
            texts = self._read_padd_texts()
            limits = parse_speed_limits(texts)
            inputs, cfg = self._read_inputs(texts)
        except Exception as e:  # noqa: BLE001  # Fallback to defaults
            limits = dict(self._limits, src=f"Default (padd config error: {type(e).__name__})")
            inputs, cfg = self._inputs, None
        path = cfg or self.robotd_toml
        imu_head, imu_why = None, None
        config_text = None
        try:
            config_text = self.read_text(path)
            data = tomllib.loads(config_text)
            netpad, why = bool((data.get("netpad") or {}).get("enabled", False)), None
            ih = data.get("pad_imu_head_control") or {}
            imu_head = bool(ih.get("enabled", False)) if isinstance(ih, dict) else None
        except FileNotFoundError:
            # File missing: [netpad] and [pad_imu_head_control] default to false
            # Defaults when config file is absent
            netpad, why = False, f"{path} missing; using default (off)"
            imu_head, imu_why = False, why
        except PermissionError as e:
            netpad, why = None, f"Permission denied reading {path} ({e.strerror or e})"
            imu_why = why
        except Exception as e:  # noqa: BLE001  # Parse/read error
            netpad, why = None, f"Cannot read {path} ({type(e).__name__}: {e})"
            imu_why = why
        with self._lk:
            self._netpad, self._netpad_why, self._limits = netpad, why, limits
            self._inputs, self._pad_config = inputs, path
            self._imu_head, self._imu_head_why = imu_head, imu_why
            self._motion_config_digest = _digest({"padd": texts, "path": path, "config": config_text})

    def _files_loop(self):
        while not self._files_stop.wait(self.w["files"]):
            try:
                self._refresh_files()
            except Exception as e:  # noqa: BLE001
                self.log.error("Background config refresh error", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True)

    def _robot_ok(self, now):
        r = self._robot or {}
        bus = r.get("bus") if isinstance(r.get("bus"), dict) else None
        if r.get("paused"):
            return False, "Servo tuning active: robotd paused"
        if r.get("present") is False:
            return False, "robotd is not running"
        if self._robot_at is None or now - self._robot_at > BUS_FRESH_S:
            return None, "No telemetry from robotd yet"
        if bus is None or r.get("bus_at") is None or now - r["bus_at"] > BUS_FRESH_S:
            return None, "robotd status unavailable"
        if bus.get("phase") in STARTING_PHASES:
            return False, "robotd is still starting"
        if bus.get("mode") == "commissioning":
            return False, "Debug mode: Select bypasses robotd"
        if bus.get("mode") != "motion":
            return None, f"robotd in {bus.get('mode')} mode"
        return True, None

    def _estop_checks(self, now, status):
        """Evaluate E-Stop readiness checks: controller connected, padd active, robotd running, netpad off."""
        checks = []
        tap_ok = self._tap_on and self.tap_state == "attached"
        dev = (self.live.device or {}).get("name") if tap_ok else None
        if not status["known"]:
            if tap_ok:
                checks.append({"key": "pad", "label": "Controller connected", "ok": True,
                               "why": f"{dev or 'Controller'} (seen via padd tap; configd unavailable)"})
            else:
                checks.append({"key": "pad", "label": "Controller connected", "ok": None, "why": status["error"] or "configd status unavailable"})
        else:
            a = status["active"]
            checks.append({"key": "pad", "label": "Controller connected", "ok": bool(a),
                           "why": a["name"] if a else ("Paired controller offline" if status["pads"] else "No paired controller")})
        drv = status["driver"]
        if drv is None and tap_ok:
            checks.append({"key": "padd", "label": "padd running (forwards gamepad to robotd)", "ok": True,
                           "why": "padd tap active (configd unavailable)"})
        else:
            checks.append({"key": "padd", "label": "padd running (forwards gamepad to robotd)",
                           "ok": None if drv is None else drv == "active", "why": None if drv in (None, "active") else f"padd state: {drv}"})
        ok, why = self._robot_ok(now)
        checks.append({"key": "robotd", "label": "robotd running (motion mode)", "ok": ok, "why": why})
        np = status["netpad"]
        checks.append({"key": "netpad", "label": "UDP netpad disabled", "ok": None if np is None else not np,
                       "why": "[netpad] enabled: padd ignores Bluetooth gamepads" if np else
                       (status.get("netpad_why") if np is False else
                        f"{status.get('netpad_why') or 'robotd.toml unreadable'}: netpad unverified")})
        if self._tap_on and (status["active"] or tap_ok):
            if self.tap_state == "attached":
                checks.append({"key": "input", "label": "padd opened input device", "ok": True, "why": None})
            elif self.tap_state == "waiting" and self._attach_wait_since is not None and now - self._attach_wait_since > 3:
                checks.append({"key": "input", "label": "padd opened input device", "ok": False,
                               "why": "Connected, but padd has no input device; Forget and re-pair"})
        vals = [c["ok"] for c in checks]
        good = all(v is True for v in vals)
        state = "ok" if good else ("bad" if any(v is False for v in vals) else "unknown")
        return {"ok": good, "state": state, "checks": checks}

    def _poll_status(self):
        if self.configd is None:
            with self._lk:
                self._status_error = "configd not configured on this duck"
            return None
        try:
            r = self.configd.call("pad.status", {}, 2.0)
            if not isinstance(r, dict):
                raise RpcError("pad.status", None, f"Expected object, got {type(r).__name__}")
        except NoPermission as e:
            with self._lk:
                if self._status_error is None:
                    self.log.warn("Studio lacks permission for configd pad.status", cat="Gamepad", step="pad.status", raw=str(e), hint=ROBOT_GROUP)
                self._status_error = str(e)
            return None
        except (Unreachable, Timeout, RpcError) as e:
            with self._lk:
                if self._status_error is None:
                    self.log.warn("Failed to read configd pad.status", cat="Gamepad", step="pad.status", raw=str(e),
                                  hint="configd may be stopped (sudo systemctl start configd); walking still works")
                self._status_error = str(e)
            return None
        with self._lk:
            if self._status_error is not None:
                self.log.info("configd pad.status recovered", cat="Gamepad")
            self._status, self._status_at, self._status_error = r, self.clock(), None
        return r

    def _pad_in_status(self, mac):
        st = self._status if isinstance(self._status, dict) else {}
        for p in st.get("pads") or []:
            if isinstance(p, dict) and (p.get("mac") or "").upper() == (mac or "").upper():
                return p
        return None

    # ================= Watchdog Thread =================

    def _watch(self):
        """Renew page watch lease without extending drive lease."""
        now = self.clock()
        with self._lk:
            first = self._watch_until < now
            self._watch_until = now + self.w["lease"]
            if first:
                self._status_due = 0.0
                self._bindings_due = 0.0          # Refresh pad.bindings in watchdog thread
        self._ensure_worker()

    def _fetch_bindings(self):
        """Query robotd pad.bindings (cached ~10s, non-throwing)."""
        if self.robot is None:
            return None
        try:
            r = self.robot.call("pad.bindings", {}, 2.0)
        except (Unreachable, Timeout, RpcError) as e:
            with self._lk:
                self._bindings_at = self.clock()       # Back off 10s even on error
                said = self._bindings_err is not None
                self._bindings_err = str(e)
            if not said:
                self.log.info("pad.bindings unavailable (using defaults)", cat="Gamepad", step="pad.bindings", raw=str(e))
            return None
        rows = r.get("bindings") if isinstance(r, dict) else None
        rows = [b for b in rows if isinstance(b, dict)] if isinstance(rows, list) else None
        with self._lk:
            changed = rows != self.bindings
            self.bindings, self._bindings_at, self._bindings_err = rows, self.clock(), None
        if changed and rows is not None:
            text = ", ".join(f"{b.get('button')}={b.get('skill') or '(off)'}" + (f" ({b['error']})" if b.get("error") else "")
                            for b in rows)
            self.log.info(f"Gamepad bindings: {text}", cat="Gamepad", step="pad.bindings")
        return rows

    def _ensure_worker(self):
        with self._lk:
            if self._worker is not None and self._worker.is_alive():
                self._wake.set()
                return
            self._worker = threading.Thread(target=self._worker_loop, name="pad-watch", daemon=True)
            self._worker_error = None
            self._worker.start()

    def _busy_reason(self, now):
        return (self._watch_until > now or self._job_alive()
                or self.test.get("state") not in FINAL)

    def _worker_loop(self):
        try:
            while not self._closing:
                now = self.clock()
                with self._lk:
                    alive = self._busy_reason(now)
                    watching = self._watch_until > now or self.test.get("state") not in FINAL \
                        or self.wiz.get("phase") in ("padd", "keys")
                    if not alive:
                        self._worker = None
                if not alive:
                    break
                if now >= self._status_due:
                    self._status_due = now + (self.w["status_job"] if self._job_alive() else self.w["status_watch"])
                    self._poll_status()
                if now >= self._bindings_due and self._watch_until > now:
                    self._bindings_due = now + self.w["bindings"]
                    self._fetch_bindings()
                self._tap_lease(watching)
                self.mapping.poll()
                self._keys_tick()
                self._wake.wait(self.w["poll"])
                self._wake.clear()
        except Exception as e:  # noqa: BLE001  # Watchdog must not die silently
            self.log.error("Gamepad watchdog error", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="Internal Studio error; refresh the Gamepad page")
            with self._lk:
                self._worker = None
                self._worker_error = f"{type(e).__name__}: {e}"
        finally:
            self._tap_lease(False)

    # ---- Padd Tap ----

    def _tap_lease(self, want):
        if self.tap is None:
            return
        with self._lk:
            if want == self._tap_on:
                return
            self._tap_on = want
            if want:
                self.tap_state, self.tap_why = "connecting", None
                self._attach_wait_since = None
        if want:
            self.tap.start(self._on_tap_report, self._on_tap_state)
        else:
            self.tap.stop()
            with self._lk:
                self.live.reset()
                self.tap_state, self.tap_why = "off", None

    def _on_tap_state(self, state, why=None):
        with self._lk:
            if state == "waiting" and self.tap_state != "waiting":
                self._attach_wait_since = self.clock()
            if (state, why) != (self.tap_state, self.tap_why) and state in ("absent", "denied", "error"):
                self.log.warn(f"Gamepad input tap error: {why}", cat="Gamepad", step="pad.input",
                              hint=ROBOT_GROUP if state == "denied" else "padd runs alongside robotd")
            self.tap_state, self.tap_why = state, why

    def _on_tap_report(self, rep):
        """Handle padd tap report (attached / frame / detached)."""
        if not isinstance(rep, dict):
            return
        try:
            self._tap_report(rep)
        except Exception as e:  # noqa: BLE001
            if not self._tap_err_said:
                self._tap_err_said = True
                self.log.error("Error handling gamepad input event", cat="Gamepad", raw=f"{type(e).__name__}: {e}  ·  {json.dumps(rep)[:300]}",
                               exc=True, hint="Internal Studio error; export diagnostics bundle")

    def _tap_report(self, rep):
        now = self.clock()
        kind = rep.get("report")
        with self._lk:
            if kind == "attached":
                self.live.attach(rep.get("device"), now)
                self.tap_state, self.tap_why = "attached", None
                d = self.live.device or {}
                key = (d.get("name"), d.get("unique"), d.get("node"))
                if key != self._attach_said:
                    self._attach_said = key
                    self.log.info(f"padd attached gamepad: {d.get('name')} ({d.get('node')})", cat="Gamepad", dedup=False)
            elif kind == "frame":
                sel = self.live.frame(rep, now)
                mapping = self.mapping.snapshot()
                bind = ((mapping.get('profile') or {}).get('bindings') or {}).get('select')
                if bind and (mapping.get('device') or {}).get('node') == (self.live.device or {}).get('node'):
                    sel = None
                    for event in rep.get('events') or []:
                        if event.get('code') != bind.get('code'):
                            continue
                        if bind.get('kind') == 'key' and event.get('kind') == EV_KEY and event.get('value') in (0,1):
                            sel = event['value'] == 1
                        elif bind.get('kind') == 'axis_button' and event.get('kind') == EV_ABS:
                            sel = event.get('value') == bind.get('value')
                if sel is not None and not mapping.get('output_blocked'):
                    self._on_select(sel, now)
            elif kind == "detached":
                self.live.reset()
                self.tap_state, self.tap_why = "waiting", rep.get("why")
                self._attach_wait_since = now
                self.log.info(f"Gamepad detached from padd: {rep.get('why')}", cat="Gamepad")
        if kind == "attached":
            self._action_stop(None, "Physical gamepad attached")
            self._request_clear(None, "Physical gamepad attached")
            with self._lk:
                error = self.wiz.get("error") or {}
                if (self.wiz.get("state") == "failed" and error.get("code") in ("no_input", "not_connected")
                        and self._pair_input_matches(self.wiz.get("mac")) and self._mine(self._job_id)):
                    self._pair_keys_ready(self._job_id, self.wiz.get("mac"))

    # ================= Pairing Wizard =================

    def _job_alive(self):
        # Allow retry once wizard finishes or waits for selection
        return self._job is not None and self._job[1].is_alive() and self.wiz.get("state") == "running"

    def _configd_busy_left(self, now):
        t = self._configd_thread
        if t is None or not t.is_alive():
            return None
        return max(1, int(self._configd_until - now))

    def _start_job(self, kind, fn, arg):
        now = self.clock()
        with self._lk:
            if self._job_alive():
                raise Refused("Scan/pairing in progress: wait or click Stop")
            if self.test.get("state") not in FINAL:
                raise Refused("E-Stop test in progress: finish or cancel first")
            left = self._configd_busy_left(now)
            if left is not None:
                raise Refused(f"configd still finishing previous pairing (~{left}s left); please wait")
            # Refusing scan/pair while driving
            bus = (self._robot or {}).get("bus")
            if self._posture() in DRIVING or (isinstance(bus, dict) and bus.get("policy_enabled") is True):
                msg, fix = FAILS["driving"]
                raise Refused(f"{msg}: {fix}")
            self._job_id += 1
            jid = self._job_id
            self._cancel = threading.Event()
            keep = kind == "pair" and self.wiz.get("phase") == "pick"
            if not keep:
                self.wiz = _idle_wizard()
            self.wiz.update(state="running", started=self.clock(), error=None, retry=None,
                            elapsed_s=None, result_name=None)
            t = threading.Thread(target=self._run_job, args=(jid, fn, arg), name=f"pad-{kind}", daemon=True)
            self._job = (jid, t)
            t.start()
        self._ensure_worker()
        return {"job": jid}

    def _run_job(self, jid, fn, arg):
        try:
            fn(jid, arg)
        except Exception as e:  # noqa: BLE001  # Unexpected error; report to UI
            self.log.error("Pairing wizard error", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="Internal Studio error; export diagnostics bundle")
            self._fail(jid, None, "other", f"Studio error: {type(e).__name__}: {e}")
        finally:
            with self._lk:
                if self.wiz.get("state") == "running" and jid == self._job_id:
                    self.wiz["elapsed_s"] = self.clock() - (self.wiz.get("started") or self.clock())
            self._wake.set()

    def _mine(self, jid):
        return jid == self._job_id and not self._cancel.is_set()

    def _step(self, jid, key, state, note=None):
        with self._lk:
            if jid != self._job_id:
                return
            for s in self.wiz["steps"]:
                if s["key"] == key:
                    if s["state"] == "ok" and state == "doing":
                        return               # Do not revert already-completed steps
                    s["state"] = state
                    if note is not None:
                        s["note"] = note
            self.wiz["phase"] = key if state in ("doing", "fail") else self.wiz["phase"]

    def _note(self, jid, key, extra):
        with self._lk:
            for s in self.wiz["steps"]:
                if jid == self._job_id and s["key"] == key:
                    s["note"] = "; ".join(x for x in (s.get("note"), extra) if x)

    def _fail(self, jid, key, code, raw=None, retry=None):
        with self._lk:
            if jid != self._job_id:
                return
            if key:
                self._step(jid, key, "fail")
            err = explain(code, raw)
            self.wiz.update(state="failed", error=err, retry=retry)
            if self.wiz.get("started") is not None:
                self.wiz["elapsed_s"] = self.clock() - self.wiz["started"]
        lvl = "info" if code == "cancelled" else "warn"
        self.log.write(f"Gamepad pairing failed: {err['msg']}", cat="Gamepad", level=lvl, step=key, raw=raw, hint=err["fix"])

    def _set_steps(self, jid, defs, keep=False):
        with self._lk:
            if jid != self._job_id:
                return
            have = {s["key"] for s in self.wiz["steps"]} if keep else set()
            if not keep:
                self.wiz["steps"] = []
            for k, label in defs:
                if k not in have:
                    self.wiz["steps"].append({"key": k, "label": label, "state": "todo", "note": None})

    def _sleep(self, s):
        """Interruptible sleep; returns True if cancelled."""
        return self._cancel.wait(s)

    # ---- Scan ----

    def _job_scan(self, jid, seconds):
        self._set_steps(jid, SCAN_STEPS)
        if not self._prepare_adapter(jid, via_helper=self._helper_pad_ok):
            return
        bt = self.bt
        self._step(jid, "scan", "doing", f"Up to {seconds:g}s")
        self.log.info(f"Starting gamepad scan ({seconds:g}s)", cat="Gamepad")
        devices = []
        try:
            try:
                bt.scan_start()
            except BtError as e:
                self._fail(jid, "scan", e.code if e.code != "other" else "scan_failed", e.raw, retry={"op": "scan", "seconds": seconds})
                return
            started = self.clock()
            try:
                while self.clock() - started < seconds:
                    devices = self._device_list(bt.devices(), started)
                    with self._lk:
                        if jid == self._job_id:
                            self.wiz["devices"] = devices
                            n = sum(1 for d in devices if d["pad"])
                            self._step(jid, "scan", "doing", f"{max(0, seconds - (self.clock() - started)):.0f}s left · {len(devices)} devices, {n} gamepads")
                    if self._sleep(self.w["scan_tick"]):
                        break
                devices = self._device_list(bt.devices(), started)
            finally:
                try:
                    bt.scan_stop()
                except BtError as e:
                    self.log.info("Error stopping Bluetooth scan (non-fatal)", cat="Gamepad", raw=e.raw)
        finally:
            # Close bluetoothctl session after scan
            try:
                bt.close()
            except Exception:  # noqa: BLE001
                pass
        if self._cancel.is_set():
            self._fail(jid, "scan", "cancelled")
            return
        pads = [d for d in devices if d["pad"]]
        self.log.info(f"Scan complete: {len(devices)} Bluetooth devices, {len(pads)} gamepads", cat="Gamepad",
                      raw=", ".join(f"{d['name'] or '?'} {d['mac']}" for d in pads[:6]) or None)
        with self._lk:
            if jid != self._job_id:
                return
            self.wiz["devices"] = devices
            self._step(jid, "scan", "ok", f"Found {len(devices)} devices, {len(pads)} gamepads")
            fresh = [d for d in pads if not d["paired"]]
            self.wiz["picked"] = fresh[0]["mac"] if len(fresh) == 1 else None
            self.wiz["phase"] = "pick"
            self.wiz["state"] = "waiting"
            for s in self.wiz["steps"]:
                if s["key"] == "pick":
                    s["state"] = "doing"
                    s["note"] = ("Is this it? Click Pair This" if len(fresh) == 1 else
                                 "Multiple devices in pairing mode: select yours" if len(fresh) > 1 else None)
            if not pads:
                self.wiz["error"] = explain("not_found_pad" if devices else "not_found")
                self.wiz["retry"] = {"op": "scan", "seconds": seconds}

    def _device_list(self, raw_devices, since):
        """Convert bluetoothctl devices to UI list (RSSI-present only, gamepads first, merge dual-mode)."""
        out = []
        for d in raw_devices or []:
            seen = d.get("seen_at")
            if not d.get("connected") and (seen is None or seen < since - 0.5):
                continue
            name = d.get("name") or d.get("alias")
            ev = gamepad_evidence(name, d.get("icon"), d.get("cls"), d.get("appearance"))
            kind = pad_kind(name)
            out.append({"mac": d.get("mac"), "name": name, "rssi": d.get("rssi"), "pad": ev is not None, "evidence": ev,
                        "classic": isinstance(d.get("cls"), int), "kind": kind if ev else None,
                        "kind_label": KIND_LABEL.get(kind) if ev else None,
                        "why_not": None if ev else (f"Looks like {ICON_CN[d.get('icon')]}" if d.get("icon") in ICON_CN else "Not a gamepad"),
                        "paired": bool(d.get("paired")), "connected": bool(d.get("connected"))})
        pads = [x for x in out if x["pad"]]
        drop = set()
        for a in pads:
            for b in pads:
                if a is not b and same_pad(a["mac"], b["mac"]) and a["classic"] and not b["classic"]:
                    drop.add(b["mac"])          # Keep Classic face for HID
        out = [x for x in out if x["mac"] not in drop]
        out.sort(key=lambda x: (not x["pad"], x["evidence"] != "classified", -(x["rssi"] if isinstance(x["rssi"], int) else -999)))
        return out

    def _prepare_adapter(self, jid, via_helper=None):
        """Check adapter presence, rfkill, and power state."""
        self._step(jid, "ready", "doing")
        bt = self.bt
        if bt is None:
            self._fail(jid, "ready", "no_btctl", retry={"op": "pair"})
            return False
        a = bt.adapter()
        if a.get("missing"):
            self._fail(jid, "ready", "no_btctl", a.get("raw"), retry={"op": "pair"})
            return False
        if a.get("blocked"):
            self._fail(jid, "ready", "rfkill", a.get("raw"), retry={"op": "scan"})
            return False
        if a.get("error"):
            self._fail(jid, "ready", a.get("code") or classify(a["error"]), a["error"], retry={"op": "scan"})
            return False
        if not a.get("present"):
            self._fail(jid, "ready", "no_adapter", a.get("raw"), retry={"op": "scan"})
            return False
        notes = []
        if not a.get("powered"):
            try:
                bt.power_on()
                notes.append("Powered Bluetooth on")
            except BtError as e:
                self._fail(jid, "ready", e.code if e.code != "other" else "powered_off", e.raw, retry={"op": "scan"})
                return False
        if a.get("pairable") is False:
            notes.append("Adapter currently non-bondable: will enable temporarily during pairing")
        if self.exists(self.weird_ble_path):
            if via_helper is True:
                notes.append("This board requires btd pause/restart; helper will handle it automatically")
            elif via_helper is False:
                notes.append("This board requires btd pause/restart, which needs the updated helper. "
                             "Reinstall helper or run sudo robotctl pad pair on the duck")
            else:
                notes.append("This board requires btd pause/restart (handled by updated helper). "
                             "If it fails, reinstall helper or run sudo robotctl pad pair")
        self._step(jid, "ready", "ok", "; ".join(notes) or None)
        return True

    def _ensure_pairable(self, jid):
        """Temporarily enable bondable mode before pairing; returns True if changed."""
        bt = self.bt
        if bt is None:
            return False
        try:
            a = bt.adapter()
        except Exception:  # noqa: BLE001
            return False
        if a.get("pairable") is not False:
            return False
        try:
            bt.pairable_on()
        except BtError as e:
            self._note(jid, "ready", "Could not enable bondable (needs root); keys may not persist")
            self.log.warn("Failed to enable bondable", cat="Gamepad", step="pairable on", raw=e.raw, hint="sudo btmgmt bondable on")
            return False
        self._note(jid, "ready", "Temporarily enabled bondable")
        self.log.info("Temporarily enabled Bluetooth bondable", cat="Gamepad")
        return True

    # ---- Pair ----

    def _job_pair(self, jid, arg):
        mac, by = arg if isinstance(arg, tuple) else (arg, None)
        from_pick = self.wiz.get("phase") == "pick"
        try:
            available = bool(self.helper.available())
        except Exception:  # noqa: BLE001  # Helper probe failed; treat as absent
            available = False
        use_helper = self._helper_pad_ok is not False and available
        if not available:
            self._helper_pad_ok = False       # No helper: show fallback hint
        if from_pick:
            self._set_steps(jid, SCAN_STEPS + PAIR_STEPS, keep=True)
            self._step(jid, "pick", "ok", mac)
        else:
            self._set_steps(jid, SCAN_STEPS + PAIR_STEPS)
            if use_helper:
                self._step(jid, "ready", "skip", "Delegated to helper (robotctl handles btd & BT reset)")
            elif self.bt is not None:
                if not self._prepare_adapter(jid, via_helper=False):
                    return
            else:
                self._step(jid, "ready", "skip", "bluetoothctl absent; skipping check")
            note = ("Helper (robotctl) auto-discovering controller in pairing mode" if use_helper
                    else "configd auto-discovering controller in pairing mode") if mac is None else mac
            self._step(jid, "scan", "skip", note)
            self._step(jid, "pick", "skip" if mac is None else "ok", None if mac is None else mac)
        if self.bt is not None:
            try:
                self.bt.close()       # Close bluetoothctl before helper/configd resets BT
            except Exception:  # noqa: BLE001
                pass
        restore = False
        try:
            with self._lk:
                self.wiz.update(mac=mac, phase="pair", retry={"op": "pair", "mac": mac} if mac else {"op": "pair"})
            self._step(jid, "pair", "doing", "Keep controller LED flashing rapidly; may take 10-20s")
            self.log.info(f"Starting gamepad pairing {mac or '(auto)'}", cat="Gamepad", dedup=False)
            pad = None
            if use_helper:
                got = self._pair_via_helper(jid, mac, by)
                if got is _HELPER_OLD:
                    use_helper = False
                    if self.bt is not None:
                        if not self._prepare_adapter(jid, via_helper=False):
                            return
                    else:
                        self._step(jid, "ready", "skip", "bluetoothctl absent; skipping check")
                else:
                    pad = got
            if not use_helper:
                restore = self._ensure_pairable(jid)      # Fallback path only
                pad = self._pair_via_configd(jid, mac)
            if pad is None:
                return
            if not self._mine(jid):
                self._fail(jid, None, "cancelled")
                return
        finally:
            if restore:
                try:
                    self.bt.pairable_off()
                    self.log.info("Restored Bluetooth bondable to off", cat="Gamepad")
                except Exception as e:  # noqa: BLE001
                    self.log.warn("Failed to restore bondable off", cat="Gamepad", step="pairable off", raw=str(e),
                                  hint="Harmless; resets on reboot")
        self._after_pair(jid, pad)

    def _pair_via_helper(self, jid, mac, by):
        """Primary pairing path via root helper (robotctl pad pair)."""
        with self._lk:
            self.wiz["via"] = "Helper (robotctl pad pair)"
        box = {}

        def call():
            try:
                box["r"] = self.helper.pad_pair(by or "Studio", mac=mac, timeout=self.w["pad_timeout"])
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=call, name="pad-helper-pair", daemon=True)
        t.start()
        while t.is_alive():
            t.join(self.w["status_job"])
            if self._cancel.is_set():
                self.log.info("Cancelled by UI; helper robotctl pairing will finish in background (up to 1-2m)",
                              cat="Gamepad")
                self._fail(jid, "pair", "cancelled")
                return None
            if mac:
                self._tick_from_device(jid, mac)
        if "e" in box:
            e = box["e"]
            self._fail(jid, "pair", "other", f"{type(e).__name__}: {e}", retry=self.wiz.get("retry"))
            return None
        r = box.get("r")
        if not isinstance(r, tuple) or len(r) != 3:
            self._fail(jid, "pair", "other", f"Unexpected helper response: {r!r}",
                       retry=self.wiz.get("retry"))
            return None
        ok, result, detail = r
        detail = detail if isinstance(detail, dict) else {}
        if result == "bad_verb":
            self.log.info("Legacy helper lacks pad-pair; using fallback pairing", cat="Gamepad", raw=detail.get("msg"),
                          hint="Reinstall helper to enable robotctl pad pair")
            self._helper_pad_ok = False
            with self._lk:
                self.wiz["via"] = None
            return _HELPER_OLD
        self._helper_pad_ok = True
        pair = detail.get("pair") if isinstance(detail.get("pair"), dict) else {}
        if ok and result == "paired" and pair.get("outcome") == "paired":
            pad = pair.get("pad") if isinstance(pair.get("pad"), dict) else {}
            self.log.info(f"Helper paired: {pad.get('name')} {pad.get('mac')}", cat="Gamepad", dedup=False)
            return pad
        reason = detail.get("reason") or pair.get("reason") or "other"
        code = {"not_found": "not_found", "ambiguous": "ambiguous", "timeout": "timeout",
                "no_adapter": "no_adapter", "rejected": "rejected"}.get(reason, "other")
        if result == "timeout":
            code = "timeout"
        elif result == "no_robotctl":
            code = "no_robotctl"
        raw = "\n".join(str(x) for x in [detail.get("msg"), pair.get("detail")] +
                         list(detail.get("stderr") or []) if x)
        if reason == "rejected":
            sub = classify(raw)
            if sub in ("auth_failed", "key_missing", "le_abort", "dhkey", "pin", "not_bondable"):
                code = sub
        self._fail(jid, "pair", code, raw or result, retry=self.wiz.get("retry"))
        return None

    def _pair_via_configd(self, jid, mac):
        """Pair via configd pad.pair while polling progress."""
        if self.configd is None:
            return self._pair_via_bt(jid, mac, "configd_down", "configd not configured")
        with self._lk:
            self.wiz["via"] = "configd (pad.pair)"
        params = {"timeout_seconds": int(self.w["pair_window"])}
        if mac:
            params["mac"] = mac
        box = {}
        limit = self.w["pair_window"] + PAIR_CALL_MARGIN_S

        def call():
            try:
                box["r"] = self.configd.call("pad.pair", params, limit)
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=call, name="pad-configd-pair", daemon=True)
        with self._lk:
            self._configd_thread, self._configd_until = t, self.clock() + limit
        t.start()
        while t.is_alive():
            t.join(self.w["status_job"])
            if self._cancel.is_set():
                self.log.info("Cancelled by UI; configd pairing will finish in background (up to 1-2m)", cat="Gamepad")
                self._fail(jid, "pair", "cancelled")
                return None
            if mac:
                self._tick_from_device(jid, mac)
        if "e" in box:
            e = box["e"]
            if isinstance(e, RpcError) and e.code == 14:
                self.log.info("configd denied Studio pairing (root-only); falling back to bluetoothctl", cat="Gamepad", raw=str(e))
                return self._pair_via_bt(jid, mac, "permission", str(e))
            if isinstance(e, NoPermission):
                self.log.warn("Studio lacks configd permission; falling back to bluetoothctl", cat="Gamepad", raw=str(e), hint=ROBOT_GROUP)
                return self._pair_via_bt(jid, mac, "socket_perm", str(e))
            if isinstance(e, Unreachable):
                return self._pair_via_bt(jid, mac, "configd_down", str(e))
            if isinstance(e, Timeout):
                self._fail(jid, "pair", "timeout", str(e), retry=self.wiz.get("retry"))
                return None
            raw = getattr(e, "message", None) or str(e)
            self._fail(jid, "pair", classify(raw), raw, retry=self.wiz.get("retry"))
            return None
        r = box.get("r")
        if not isinstance(r, dict):
            self._fail(jid, "pair", "other", f"configd returned non-object: {r!r}")
            return None
        if r.get("outcome") == "paired":
            pad = r.get("pad") if isinstance(r.get("pad"), dict) else {}
            self.log.info(f"configd paired: {pad.get('name')} {pad.get('mac')}", cat="Gamepad", dedup=False)
            return pad
        reason, detail = r.get("reason"), r.get("detail")
        code = {"not_found": "not_found", "ambiguous": "ambiguous", "timeout": "timeout", "no_adapter": "no_adapter",
                "rejected": "rejected"}.get(reason, "other")
        if reason == "rejected":
            sub = classify(detail)          # Extract specific failure cause from raw detail
            if sub in ("auth_failed", "key_missing", "le_abort", "dhkey", "pin", "not_bondable"):
                code = sub
        if reason == "not_found" and detail and "in range" in str(detail):
            code = "not_found_pad"
        self._fail(jid, "pair", code, f"{reason}: {detail}" if detail else reason, retry=self.wiz.get("retry"))
        return None

    def _pair_via_bt(self, jid, mac, why_code, why_raw):
        """Fallback pairing using a single bluetoothctl session."""
        bt = self.bt
        if bt is None or not mac:
            # Cannot auto-pair via bluetoothctl without a target MAC
            self._fail(jid, "pair", why_code, why_raw, retry={"op": "scan", "seconds": SCAN_S} if not mac else self.wiz.get("retry"))
            return None
        with self._lk:
            self.wiz["via"] = "bluetoothctl (Studio fallback: " + {"permission": "configd denied",
                                                               "socket_perm": "configd socket denied"}.get(why_code, "configd unavailable") + ")"
        classic = next((d["classic"] for d in self.wiz.get("devices") or [] if d.get("mac") == mac), None)
        if classic is None:
            classic = isinstance(bt.info(mac).get("cls"), int)

        def step(key, state, note=None):
            if self._mine(jid):
                self._step(jid, key, state, note)

        try:
            try:
                bt.pair_device(mac, classic, self._cancel, step)
            except BtError as e:
                # bluetoothctl 5.82 may return Failed to register agent object right after Forget;
                # reopen session and retry once.
                if "Failed to register agent object" not in (e.raw or "") or self._cancel.is_set():
                    raise
                self.log.warn("bluetoothctl agent registration failed: reopening session to retry", cat="Gamepad",
                              step="agent", raw=e.raw)
                try:
                    bt.close()
                except Exception:  # noqa: BLE001
                    pass
                bt.pair_device(mac, classic, self._cancel, step)
        except BtError as e:
            if self._cancel.is_set():
                self._fail(jid, "pair", "cancelled")
                return None
            key = next((s["key"] for s in self.wiz["steps"] if s["state"] == "doing"), "pair")
            self._fail(jid, key, e.code, e.raw, retry=self.wiz.get("retry"))
            return None
        finally:
            try:
                bt.close()
            except Exception:  # noqa: BLE001
                pass
        info = bt.info(mac)
        return {"mac": mac, "name": info.get("name"), "paired": info.get("paired", True),
                "trusted": info.get("trusted", True), "connected": info.get("connected", True)}

    def _tick_from_device(self, jid, mac):
        """Poll bluetoothctl info / pad.status for Paired, Trusted, Connected state."""
        info = {}
        if self.bt is not None:
            try:
                info = self.bt.info(mac) or {}
            except Exception:  # noqa: BLE001
                info = {}
        p = self._pad_in_status(mac) or {}
        if info.get("paired") or p.get("paired"):
            self._step(jid, "pair", "ok")
        if info.get("trusted") or p.get("trusted"):
            self._step(jid, "trust", "ok")
        if info.get("connected") or p.get("connected"):
            self._step(jid, "connect", "ok")

    def _after_pair(self, jid, pad):
        mac = (pad.get("mac") or "").upper()
        name = pad.get("name") or mac
        with self._lk:
            self.wiz.update(mac=mac or self.wiz.get("mac"), result_name=name)
        self._step(jid, "pair", "ok", name)
        if pad.get("trusted") is False:
            self._step(jid, "trust", "fail", FAILS["not_trusted"][0] + ": " + FAILS["not_trusted"][1])
        else:
            self._step(jid, "trust", "ok")
        # Retry connect with cap if bonded but connection still in progress
        tries, retry_at = 0, self.clock() + self.w["reconnect_after"]

        def reconnect(end):
            nonlocal tries, retry_at
            with self._lk:
                if not self._mine(jid):
                    self._fail(jid, None, "cancelled")
                    return False
                if self.tap_state == "attached" and not self._pair_input_matches(mac):
                    self._fail(jid, "padd", "wrong_input")
                    return False
                drv = (self._status or {}).get("driver") if isinstance(self._status, dict) else None
                if drv is not None and drv != "active":
                    self._fail(jid, "padd", "padd_down", f"padd state: {drv}")
                    return False
                bus = (self._robot or {}).get("bus")
                if self._posture() in DRIVING or (isinstance(bus, dict) and bus.get("policy_enabled") is True):
                    self._fail(jid, None, "driving")
                    return False
                now = self.clock()
                if (tries >= 2 or now < retry_at or end <= now or not MAC_RE.fullmatch(mac)
                        or pad.get("trusted") is False or not callable(getattr(self.bt, "reconnect", None))):
                    return True
                tries += 1
                stage = self.wiz.get("phase")
            self._step(jid, stage, "doing", f"Waiting for input device, reconnecting ({tries}/2)...")
            try:
                self.bt.reconnect(mac, timeout=min(self.w["reconnect_timeout"], end - now))
            except BtError as e:
                self.log.info("Gamepad reconnect pending; waiting for input device", cat="Gamepad", raw=e.raw)
            retry_at = self.clock() + self.w["reconnect_interval"]
            return True

        # Wait for connection after bonding
        self._step(jid, "connect", "doing", "Connecting...")
        with self._lk:
            connected = bool(pad.get("connected")) or self._pair_input_matches(mac)
        end = self.clock() + self.w["connect"]
        while not connected and self.clock() < end:
            if self._sleep(self.w["status_job"]):
                self._fail(jid, "connect", "cancelled")
                return
            self._poll_status()
            with self._lk:
                connected = bool((self._pad_in_status(mac) or {}).get("connected")) or self._pair_input_matches(mac)
            if not connected and not reconnect(end):
                return
            with self._lk:
                connected = connected or self._pair_input_matches(mac)
            if not self._mine(jid):
                self._fail(jid, "connect", "cancelled")
                return
        if not connected:
            self._fail(jid, "connect", "not_connected", f"{name} {mac}: not connected after {self.w['connect']:g}s in pad.status",
                       retry={"op": "pair", "mac": mac} if mac else None)
            return
        self._step(jid, "connect", "ok")
        # Verify padd is active and attached
        self._step(jid, "padd", "doing", "Waiting for padd attach...")
        with self._lk:
            self.wiz["phase"] = "padd"
        self._ensure_worker()
        end = self.clock() + self.w["padd"]
        while self.clock() < end:
            self._poll_status()
            drv = (self._status or {}).get("driver") if isinstance(self._status, dict) else None
            with self._lk:
                attached = self.tap_state == "attached"
                matching = self._pair_input_matches(mac)
                tap_dead = self.tap is None or self.tap_state in ("absent", "denied", "error")
            if drv is not None and drv != "active":
                self._fail(jid, "padd", "padd_down", f"padd state: {drv}")
                return
            if attached:
                if not matching:
                    self._fail(jid, "padd", "wrong_input")
                    return
                break
            if tap_dead and drv == "active":
                self._step(jid, "padd", "ok", "padd active (input tap unavailable)")
                break
            if not reconnect(end):
                return
            if self._sleep(self.w["status_job"]):
                self._fail(jid, "padd", "cancelled")
                return
        else:
            # Accept attach event received during final connect sleep
            with self._lk:
                matching = self._pair_input_matches(mac)
            if not matching:
                actual = self._pad_in_status(mac) or {}
                if not self._mine(jid):
                    code = "cancelled"
                else:
                    code = "not_connected" if actual.get("connected") is False else "no_input"
                self._fail(jid, "padd", code, f"padd tap did not see attached within {self.w['padd']:g}s ({self.tap_state}: {self.tap_why})",
                           retry={"op": "pair", "mac": mac} if mac else None)
                return
            tap_dead = False
        self._pair_keys_ready(jid, mac, unverified=tap_dead)

    def _pair_input_matches(self, mac):
        """Verify attached event belongs to the newly paired controller (call under _lk)."""
        unique = str((self.live.device or {}).get("unique") or "").upper()
        return self.tap_state == "attached" and bool(mac) and unique == mac.upper()

    def _pair_keys_ready(self, jid, mac, *, unverified=False):
        with self._lk:
            if not self._mine(jid):
                self._fail(jid, None, "cancelled")
                return
            if self.tap_state == "attached":
                if not self._pair_input_matches(mac):
                    self._fail(jid, "padd", "wrong_input")
                    return
                unverified = False
            elif unverified and self.tap is not None and self.tap_state not in ("absent", "denied", "error"):
                self._fail(jid, "padd", "no_input")
                return
            if not unverified and not self._pair_input_matches(mac):
                self._fail(jid, "padd", "no_input")
                return
            dev = (self.live.device or {}).get("name")
            self._step(jid, "connect", "ok")
            if not unverified:
                self._step(jid, "padd", "ok", dev)
            self.live.seen = set()
            self.wiz.update(phase="keys", state="waiting", error=None, retry=None)
        self._step(jid, "keys", "doing")
        self.log.info(f"Gamepad paired, entering key test: {dev or mac}", cat="Gamepad", dedup=False)

    def _keys_tick(self):
        with self._lk:
            if self.wiz.get("phase") != "keys" or self.wiz.get("state") != "waiting":
                return
            seen = self.live.seen
            buttons = [r for r in seen if r not in ("ls", "rs")]
            enough = len(buttons) >= KEYS_NEED and "ls" in seen and "rs" in seen
        if enough:
            self._keys_done(manual=False)

    def _keys_done(self, manual):
        with self._lk:
            if self.wiz.get("phase") != "keys":
                raise Refused("Not currently in key test step")
            n = len(self.live.seen)
            for s in self.wiz["steps"]:
                if s["key"] == "keys":
                    s["state"] = "ok"
                    s["note"] = f"{n} keys pressed" + (" (manual finish)" if manual else "")
            self.wiz.update(state="done", error=None)
            if self.wiz.get("started") is not None:
                self.wiz["elapsed_s"] = self.clock() - self.wiz["started"]
        self.log.info(f"Gamepad key test complete ({n} keys)", cat="Gamepad")
        return {}

    def _cancel_job(self):
        with self._lk:
            alive = self._job_alive()
            self._cancel.set()
        self._wake.set()
        if not alive and self.wiz.get("state") in ("waiting",):
            with self._lk:
                self._fail(self._job_id, None, "cancelled")
        return {}

    def _forget(self, mac, by=None):
        with self._lk:
            p = self._pad_in_status(mac) or {}
            if p.get("connected") and self._posture() in DRIVING:
                raise Refused("Controller is active while duck is walking: click Stop (Stand) before forgetting")
        removed, via = None, None
        try:
            available = bool(self.helper.available())
        except Exception:  # noqa: BLE001
            available = False
        use_helper = self._helper_pad_ok is not False and available
        if not available:
            self._helper_pad_ok = False
        if use_helper:
            try:
                ok, result, detail = self.helper.pad_forget(by or "Studio", mac)
            except Exception as e:  # noqa: BLE001
                raise Refused(f"Helper failed: {type(e).__name__}: {e}") from None
            detail = detail if isinstance(detail, dict) else {}
            if result == "bad_verb":
                self.log.info("Legacy helper lacks pad-forget; using configd / bluetoothctl", cat="Gamepad",
                              raw=detail.get("msg"), hint="Reinstall helper for robotctl pad forget")
                self._helper_pad_ok = False
            else:
                self._helper_pad_ok = True
                if not ok:
                    raise Refused(helper_api.friendly(result, detail, "pad-forget")) from None
                removed = detail.get("removed")
                if removed is None:
                    removed = True
                via = "Helper (robotctl pad forget)"
        if removed is None and self.configd is not None:
            try:
                r = self.configd.call("pad.forget", {"mac": mac}, 5.0)
                removed, via = bool((r or {}).get("removed")), "configd"
            except RpcError as e:
                if e.code != 14:
                    raise Refused(f"configd forget failed: {e.message}") from None
            except (Unreachable, Timeout):
                pass
        if removed is None and self.bt is not None:
            try:
                removed, via = self.bt.remove(mac), "bluetoothctl"
            except BtError as e:
                err = explain(e.code, e.raw)
                raise Refused(f"{err['msg']}: {err['fix']}") from None
        if removed is None:
            err = explain("permission")
            raise Refused(f"{err['msg']}: {err['fix']} (CLI: sudo robotctl pad forget {mac})")
        self.log.info(f"Forgot gamepad {mac}: {'removed' if removed else 'not paired'} (via {via})", cat="Gamepad", dedup=False,
                      hint="Put the controller back in pairing mode before re-pairing")
        self._poll_status()
        return {"removed": removed}

    # ================= E-Stop Test =================
    #
    # Verification criteria:
    #   1. Topbar E-Stop press_count unchanged and not releasing.
    #   2. Direct busStatus probe while Select is held confirms torque is ON.
    #   3. Holding >= 2s fails (padd triggers sit-and-shutdown instead of E-Stop).
    #   4. Post-release 0.25s busStatus probes confirm torque transitions to OFF.

    def _estop_test(self, action):
        now = self.clock()
        if action == "cancel":
            with self._lk:
                if self.test.get("state") not in FINAL:
                    self.test = {"state": "idle"}
                    self.log.info("E-Stop test cancelled by UI", cat="Safety")
            self._test_evt.set()
            return {}
        if action != "start":
            raise Refused("E-Stop test action must be start or cancel")
        if self.robot is None:
            raise Refused("robotd address not configured")
        with self._lk:
            if self._job_alive():
                raise Refused("Pairing in progress: finish before testing E-Stop")
            if self.test.get("state") not in FINAL:
                raise Refused("E-Stop test already running")
            checks = self._estop_checks(now, self._status_view(now))
            bad = next((c for c in checks["checks"] if c["ok"] is not True and c["key"] in ("pad", "padd", "robotd")), None)
            if bad or not checks["ok"]:
                c = bad or next(c for c in checks["checks"] if c["ok"] is not True)
                raise Refused(f"Cannot test E-Stop: {c['label']} failed ({c['why'] or 'unknown'})")
            if self.tap_state != "attached":
                raise Refused("Gamepad input tap not active: verify buttons highlight on screen first")
            token = object()
            self.test = {"state": "need_torque", "started": now, "why": None, "fix": None, "ms": None,
                         "held_long": False, "failed_at": None, "_token": token, "_phase_at": now,
                         "_presses0": self._relax_presses, "_noted0": self._relax_noted, "_pressed_at": None, "_released_at": None,
                         "_on_while_pressed": None, "_press_err": None}
            self._test_evt.clear()
        self.log.info("Starting E-Stop test: verifying torque ON, then waiting for short Select press", cat="Safety", dedup=False,
                      ctx={"presses": self._relax_presses})
        threading.Thread(target=self._estop_runner, args=(token,), name="pad-estop-test", daemon=True).start()
        self._ensure_worker()
        return {}

    def _on_select(self, down, now):
        """Called from tap thread under lock on Select edge; wakes test thread."""
        t = self.test
        st = t.get("state")
        if down and st == "armed":
            t.update(state="pressed", _pressed_at=now, _on_while_pressed=None)
            self._test_evt.set()
        elif down and st == "need_torque":
            t["why"] = "Still confirming torque ON: wait for step 2 before pressing Select"
        elif not down and st == "pressed":
            t.update(state="released", _released_at=now)
            self.log.info(f"Select released ({now - t['_pressed_at']:.2f}s), waiting for torque-off readback", cat="Safety")
            self._test_evt.set()

    def _probe_bus(self):
        """Probe busStatus directly: (asked_at, response_or_None, err_or_None, received_at)."""
        asked = self.clock()
        try:
            r = self.robot.call("robot.busStatus", {}, 2.0)
        except (Unreachable, Timeout, RpcError) as e:
            return asked, None, str(e), self.clock()
        return asked, (r if isinstance(r, dict) else None), (None if isinstance(r, dict) else "Expected object"), self.clock()

    def _test_fail(self, token, at, why, fix=None):
        with self._lk:
            t = self.test
            if t.get("_token") is not token or t.get("state") in FINAL:
                return
            t.update(state="failed", failed_at=at, why=why, fix=fix)
        self.log.warn(f"E-Stop test failed: {why}", cat="Safety", step=at, hint=fix)

    def _nap_test(self, s):
        """Wait with wakeup on Select edge or E-Stop."""
        self._test_evt.wait(s)
        self._test_evt.clear()

    def _estop_runner(self, token):
        try:
            self._estop_loop(token)
        except Exception as e:  # noqa: BLE001
            self.log.error("E-Stop test error", cat="Safety", raw=f"{type(e).__name__}: {e}", exc=True,
                           hint="Internal Studio error; use topbar E-Stop or physical Select")
            self._test_fail(token, "studio", f"Studio error: {type(e).__name__}: {e}")

    def _estop_loop(self, token):  # noqa: C901
        w = self.w
        while not self._closing:
            with self._lk:
                t = self.test
                if t.get("_token") is not token or t.get("state") in FINAL:
                    return
                st, now = t["state"], self.clock()
                p0, pressed_at, released_at = t["_presses0"], t["_pressed_at"], t["_released_at"]
                relax_pressed = (self._relax_presses is not None and p0 is not None and self._relax_presses > p0) \
                    or self._relax_noted > t["_noted0"] or bool((self._robot or {}).get("estop_active"))
                if st == "pressed" and now - pressed_at > w["select_long"]:
                    t["held_long"] = True
            if relax_pressed:
                self._test_fail(token, "relax", "Topbar E-Stop was pressed during test; result invalid",
                                "Wait for torque-off, press Start to stand, and retry")
                return
            if st == "need_torque":
                if now - t["started"] > w["torque"]:
                    self._test_fail(token, "torque", f"Torque did not turn ON within {w['torque']:g}s", "Press Start to stand and retry")
                    return
                asked, bus, err, _ = self._probe_bus()
                if bus is not None and _torque_on(bus):
                    with self._lk:
                        if self.test is t and t["state"] == "need_torque":
                            t.update(state="armed", _phase_at=asked, why=None)
                    continue
                self._nap_test(w["probe"] * 2)
            elif st == "armed":
                if now - t["_phase_at"] > w["press"]:
                    self._test_fail(token, "press", f"Select press not seen within {w['press']:g}s",
                                    "Press Select (View on Xbox, - on Switch, Create on PS)")
                    return
                asked, bus, err, _ = self._probe_bus()
                with self._lk:
                    still = self.test is t and t["state"] == "armed"
                if still and bus is not None and (relaxed(bus) is True or torque_off(bus)):
                    self._test_fail(token, "early_off", "Torque dropped before Select was pressed",
                                    "Press Start to stand and retry")
                    return
                self._nap_test(w["probe"] * 2)
            elif st == "pressed":
                if t["_on_while_pressed"] is None and t["_press_err"] is None:
                    asked, bus, err, _ = self._probe_bus()
                    with self._lk:
                        rel = t["_released_at"]
                        if asked >= pressed_at and (rel is None or asked <= rel):
                            if bus is None:
                                t["_press_err"] = err or "no reply"
                            else:
                                t["_on_while_pressed"] = _torque_on(bus)
                    continue
                if now - pressed_at >= w["shutdown_hold"]:
                    self._test_fail(token, "long", f"Select held >= {w['shutdown_hold']:g}s (triggers sit & shutdown, not E-Stop)",
                                    "Tap Select briefly instead of holding")
                    return
                self._nap_test(0.05)
            elif st == "released":
                held = released_at - pressed_at
                if held >= w["shutdown_hold"]:
                    self._test_fail(token, "long", f"Select held {held:.1f}s (>= {w['shutdown_hold']:g}s shutdown threshold)",
                                    "Retry with a quick tap and release")
                    return
                if t["_on_while_pressed"] is not True:
                    if t["_on_while_pressed"] is False:
                        self._test_fail(token, "pressed_off", "Torque was already OFF while Select was held",
                                        "Press Start to stand and retry")
                    else:
                        self._test_fail(token, "pressed_unknown",
                                        f"Could not read torque while Select held ({t['_press_err'] or 'tap too brief'})",
                                        "Retry: hold Select ~0.5s then release (under 1.5s)")
                    return
                if now - released_at > w["readback"]:
                    self._test_fail(token, "readback", f"Torque still ON {w['readback']:g}s after Select release",
                                    "Check padd logs; use topbar E-Stop or power off")
                    return
                asked, bus, err, got = self._probe_bus()
                if bus is not None and asked >= released_at and relaxed(bus) is True:
                    with self._lk:
                        if self.test is not t or t["state"] != "released":
                            return
                        relax_pressed = (self._relax_presses is not None and p0 is not None and self._relax_presses > p0) \
                            or self._relax_noted > t["_noted0"]
                        if not relax_pressed:
                            t.update(state="passed", ms=int((got - released_at) * 1000), why=None)
                    if relax_pressed:
                        continue
                    self.log.info(f"E-Stop test passed: torque-off confirmed in {t['ms']} ms", cat="Safety", dedup=False,
                                  ctx={"held_s": round(held, 2), "asked_after_release_ms": int((asked - released_at) * 1000)})
                    self._estop_passed_record()            # Record onboarding step 4
                    return
                self._nap_test(w["probe"])
            else:
                return


def robot_view(rd, posture=None):
    """Extract robotd snapshot for PadOps on the event loop."""
    now = time.monotonic()

    def fresh(sample, key):
        age = sample.age(now)
        return sample.value if age is not None and age <= MAX_AGE[key] else None

    pol, st, health = fresh(rd.policies, "policies"), fresh(rd.state, "state"), fresh(rd.health, "health")
    if posture is None:
        posture = _posture(rd.present, rd.bus.value, pol, st)
    applied = ((st or {}).get("move") or {}).get("applied") if isinstance(st, dict) else None
    batt = ((health or {}).get("battery") or {}).get("percent") if isinstance(health, dict) else None
    return {"present": rd.present, "bus": rd.bus.value, "bus_at": rd.bus.at, "posture": posture,
            "state": st if isinstance(st, dict) else None, "state_at": rd.state.at,
            "policies_at": rd.policies.at,
            "policies": pol if isinstance(pol, dict) else None, "applied": applied, "battery_pct": batt,
            "estop_active": rd.estop.active, "estop_presses": rd.estop.press_count, "paused": rd.paused,
            "pad_status": rd.pad.value, "pad_status_at": rd.pad.at, "pad_status_error": rd.pad.error}


def _mask_mac(mac):
    """Mask MAC address for diagnostics bundle."""
    m = str(mac or "")
    return (m[:8] + ":…") if len(m) >= 8 else m


def _mask_name(name):
    n = str(name or "")
    return (n[: max(1, len(n) // 2)] + "…") if len(n) > 2 else n


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------- Real Ports: Sync JSON-RPC ----------------

def _connect(addr, timeout):
    kind, *rest = parse_addr(addr)
    try:
        if kind == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise Unreachable("Unix sockets unsupported on this Python build; use tcp:")
            path = rest[0]
            if not os.path.exists(path):
                raise Unreachable(f"{path} not found")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(timeout)
                s.connect(path)
            except BaseException:
                s.close()
                raise
            return s
        host, port = rest
        return socket.create_connection((host, port), timeout)
    except Unreachable:
        raise
    except PermissionError as e:
        raise NoPermission(f"Permission denied connecting to {addr} ({ROBOT_GROUP})") from e
    except (ConnectionRefusedError, FileNotFoundError) as e:
        raise Unreachable(f"Connection refused: {addr}") from e
    except socket.timeout:
        raise Timeout(f"Connection timed out to {addr} ({timeout:g}s)") from None
    except OSError as e:
        raise Unreachable(f"Failed to connect to {addr}: {type(e).__name__}: {e}") from e


class _Lines:
    def __init__(self, sock):
        self.s, self.buf = sock, b""

    def readline(self, deadline):
        """Read one line (bytes); returns None on EOF, raises socket.timeout."""
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout()
            self.s.settimeout(left)
            chunk = self.s.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > LINE_LIMIT:
                raise Unreachable(f"Line exceeded {LINE_LIMIT} bytes")
        line, _, self.buf = self.buf.partition(b"\n")
        return line


def rpc_call(addr, method, params=None, timeout=3.0):
    """One-shot JSON-RPC call over a fresh connection."""
    deadline = time.monotonic() + timeout
    s = _connect(addr, timeout)
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        rd = _Lines(s)
        while True:
            line = rd.readline(deadline)
            if line is None:
                raise Unreachable("Peer closed connection")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != 1:
                continue
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                raise RpcError(method, err.get("code"), err.get("message", ""), err.get("data"))
            return msg.get("result")
    except socket.timeout:
        raise Timeout(f"{method} timed out after {timeout:g}s") from None
    except (Unreachable, RpcError):
        raise
    except OSError as e:
        raise Unreachable(f"Connection lost: {type(e).__name__}: {e}") from e
    finally:
        s.close()


class ConfigdPort:
    def __init__(self, addr="unix:/run/configd.sock"):
        self.addr = addr

    def call(self, method, params=None, timeout=3.0):
        return rpc_call(self.addr, method, params, timeout)


class RobotPort:
    """Persistent notification socket for robot.move / head / mouth; requests use rpc_call."""

    def __init__(self, addr="unix:/run/robotd.sock"):
        self.addr = addr
        self._s = None
        self._lk = threading.Lock()

    def call(self, method, params=None, timeout=3.0):
        return rpc_call(self.addr, method, params, timeout)

    def notify(self, method, params):
        line = (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, separators=(",", ":")) + "\n").encode()
        with self._lk:
            for attempt in (1, 2):
                try:
                    if self._s is None:
                        self._s = _connect(self.addr, 1.0)
                    self._s.settimeout(1.0)
                    self._s.sendall(line)
                    return
                except (OSError, Unreachable, Timeout) as e:
                    self._close()
                    if attempt == 2:
                        raise e if isinstance(e, (Unreachable, Timeout)) else Unreachable(f"{type(e).__name__}: {e}")

    def _close(self):
        if self._s is not None:
            try:
                self._s.close()
            except OSError:
                pass
        self._s = None


class PaddTap:
    """padd input tap subscriber (pad.input -> stream pad.report with 2s reconnect)."""

    def __init__(self, addr="unix:/run/padd/pad.sock", reconnect_s=2.0, log=None):
        self.addr, self.reconnect_s, self.log = addr, reconnect_s, log
        self._stop = threading.Event()
        self._t = None
        self._s = None

    def start(self, on_report, on_state):
        self.stop()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, args=(on_report, on_state, self._stop), name="pad-tap", daemon=True)
        self._t.start()

    def alive(self):
        return self._t is not None and self._t.is_alive() and not self._stop.is_set()

    def stop(self):
        self._stop.set()
        s = self._s
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _loop(self, on_report, on_state, stop):
        while not stop.is_set():
            try:
                self._once(on_report, on_state, stop)
            except Exception as e:  # noqa: BLE001
                if self.log:
                    self.log.error("Gamepad tap reader error; reconnecting in 2s", cat="Gamepad", raw=f"{type(e).__name__}: {e}", exc=True,
                                   hint="Internal Studio error; export diagnostics bundle")
                try:
                    on_state("error", f"Studio error: {type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
            stop.wait(self.reconnect_s)

    def _once(self, on_report, on_state, stop):
        try:
            s = _connect(self.addr, 2.0)
        except NoPermission as e:
            on_state("denied", str(e))
            return
        except Unreachable:
            on_state("absent", "Cannot connect to /run/padd/pad.sock (padd stopped or tap disabled); live key display unavailable")
            return
        except Timeout as e:
            on_state("error", str(e))
            return
        self._s = s
        try:
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"pad.input","params":{}}\n')
            on_state("waiting", None)
            rd = _Lines(s)
            while not stop.is_set():
                try:
                    line = rd.readline(time.monotonic() + 1.0)
                except socket.timeout:
                    continue
                if line is None:
                    on_report({"report": "detached", "why": "padd closed tap"})
                    break
                # Skip high-rate gamepad IMU packets before JSON parsing
                # ("report":"imu" is unused by Studio; imu_attached is preserved)
                # Avoid unnecessary json.loads CPU overhead
                if b'"report":"imu"' in line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == 1 and "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                    on_state("error", f"padd rejected subscription: {err.get('message')}")
                    break
                if msg.get("method") == "pad.report" and isinstance(msg.get("params"), dict):
                    on_report(msg["params"])
        except (OSError, Unreachable) as e:
            if not stop.is_set():
                on_state("error", f"Tap disconnected: {e}")
        finally:
            self._s = None
            try:
                s.close()
            except OSError:
                pass


# ---------------- Real Bluetooth: bluetoothctl ----------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x01\x02]")
_PROMPT = re.compile(r"^(?:\[[^\]\[]*\][#>]\s*)+")
_EVENT = re.compile(r"^\[(NEW|CHG|DEL)\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)$")
_PROP = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*)$")
_ASK_YES = re.compile(r"\(yes/no\)")
_ASK_KINDS = ("Accept pairing", "Authorize service", "Request authorization")
_ASK_PIN = re.compile(r"Enter PIN code|Enter passkey|Confirm passkey", re.I)
BT_FAIL = r"Failed|not available|No default controller|Invalid command|org\.bluez\.Error\.(?!AlreadyExists|AlreadyConnected)|Enter PIN code|Enter passkey|Confirm passkey"
BT_ENV = {**os.environ, "LC_ALL": "C", "TERM": "dumb"}


def clean_line(raw):
    """Strip ANSI codes, CR, and bluetoothctl prompts."""
    line = _ANSI.sub("", raw or "").replace("\r", "").strip()
    return _PROMPT.sub("", line).strip()


def _yes(v):
    return None if v is None else str(v).strip().lower().startswith("yes")


def _num(v):
    if v is None:
        return None
    m = re.search(r"\((-?\d+)\)", str(v)) or re.search(r"(-?0x[0-9a-fA-F]+|-?\d+)", str(v))
    if not m:
        return None
    s = m.group(1)
    try:
        n = int(s, 16) if "0x" in s.lower() else int(s)
    except ValueError:
        return None
    return n


def parse_props(text):
    """Parse `bluetoothctl info / show` key: value lines into a dict."""
    out = {"uuids": []}
    for raw in (text or "").splitlines():
        line = clean_line(raw)
        m = _PROP.match(line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2).strip()
        if k == "uuid":
            out["uuids"].append(v)
        elif k not in out:
            out[k] = v
    return out


def device_from_props(mac, p):
    rssi = _num(p.get("rssi"))
    if isinstance(rssi, int) and rssi > 0x7FFFFFFF:
        rssi -= 1 << 32
    return {"mac": mac.upper(), "name": p.get("name"), "alias": p.get("alias"), "icon": p.get("icon"),
            "cls": _num(p.get("class")), "appearance": _num(p.get("appearance")), "rssi": rssi,
            "paired": _yes(p.get("paired")), "trusted": _yes(p.get("trusted")), "connected": _yes(p.get("connected"))}


def _rfkill_blocked(base):
    """Return True if Bluetooth is rfkill-blocked, False if unblocked, None if not found."""
    try:
        names = os.listdir(base)
    except OSError:
        return None
    found = None
    for n in names:
        try:
            if _read_text(os.path.join(base, n, "type")).strip() != "bluetooth":
                continue
            soft = _read_text(os.path.join(base, n, "soft")).strip()
            hard = _read_text(os.path.join(base, n, "hard")).strip()
        except OSError:
            continue
        found = bool(found) or soft == "1" or hard == "1"
    return found


def _run(argv, timeout=8.0):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=BT_ENV, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"Command not found: {argv[0]}"
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        return 124, f"{' '.join(argv)} timed out after {timeout:g}s: {out[-300:]}"


def _spawn(argv):
    # Unbuffered binary pipe reading to handle agent prompts without trailing newlines
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=0, env=BT_ENV)


class BtSession:
    """Persistent bluetoothctl session for scan, pair, trust, and connect.
    configd bluez.rs:995-1013)."""

    def __init__(self, argv, spawn=None):
        self.p = (spawn or _spawn)(argv)
        self.cv = threading.Condition()
        self.lines = deque(maxlen=4000)
        self.seq = 0
        self.devices = {}
        self.closed = False
        self.discovering = False     # Ignore cached [NEW] devices emitted before scan starts
        self.answer_yes = False      # Auto-answer yes only during pairing window
        self.asked = []              # Agent prompt/response log
        self._tail_answered = False
        self._quit = False
        self._t = threading.Thread(target=self._reader, name="bt-session", daemon=True)
        self._t.start()

    def _chunks(self):
        out = self.p.stdout
        try:
            fd = out.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None
        if fd is not None:
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                try:
                    b = os.read(fd, 4096)
                except OSError:
                    return
                if not b:
                    tail = dec.decode(b"", final=True)
                    if tail:
                        yield tail
                    return
                yield dec.decode(b)
        else:
            for x in out:                         # Support chunked output from test fakes
                yield x.decode("utf-8", "replace") if isinstance(x, bytes) else x

    def _reader(self):
        buf = ""
        try:
            for chunk in self._chunks():
                buf += chunk
                parts = buf.split("\n")
                buf = parts.pop()
                with self.cv:
                    for raw in parts:
                        line = clean_line(raw)
                        answered, self._tail_answered = self._tail_answered, False
                        if line:
                            self._add(line, prompt_done=answered)
                    tail = clean_line(buf)
                    if tail and not self._tail_answered and (_ASK_YES.search(tail) or _ASK_PIN.search(tail)):
                        # Reply immediately to prompts without trailing newline
                        self._tail_answered = True
                        self._add(tail, prompt_done=False)
                    self.cv.notify_all()
                if len(buf) > 65536:
                    buf = buf[-4096:]
        except (OSError, ValueError):
            pass
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def _add(self, line, prompt_done):
        self.seq += 1
        self.lines.append((self.seq, line))
        if not prompt_done:
            self._event(line)

    def _event(self, line):
        if re.search(r"Discovery started|Controller .*Discovering: yes", line):
            self.discovering = True
        elif re.search(r"Discovery stopped|Controller .*Discovering: no", line):
            self.discovering = False
        m = _EVENT.match(line)
        if m:
            what, mac, rest = m.group(1), m.group(2).upper(), m.group(3)
            if what == "DEL":
                self.devices.pop(mac, None)
                return
            d = self.devices.setdefault(mac, {"mac": mac})
            pm = _PROP.match(rest)
            if self.discovering or (pm and pm.group(1).strip().lower() == "rssi"):
                d["seen_at"] = time.monotonic()
            if what == "CHG" and pm:
                k, v = pm.group(1).strip().lower(), pm.group(2).strip()
                if k == "rssi":
                    n = _num(v)
                    d["rssi"] = n - (1 << 32) if isinstance(n, int) and n > 0x7FFFFFFF else n
                elif k == "class":
                    d["cls"] = _num(v)
                elif k == "appearance":
                    d["appearance"] = _num(v)
                elif k in ("paired", "trusted", "connected", "bonded"):
                    d[k] = _yes(v)
                elif k in ("name", "alias", "icon"):
                    d[k] = v
            elif what == "NEW" and rest and not re.match(r"^[0-9A-Fa-f]{2}[-:]", rest):
                d.setdefault("name", rest)
            return
        # Only confirm agent prompts during active pairing window
        if _ASK_YES.search(line) and any(k in line for k in _ASK_KINDS):
            ans = "yes" if self.answer_yes else "no"
            self.asked.append(f"{line} → {ans}")
            try:
                self.send(ans)
            except BtError:
                pass
        elif _ASK_YES.search(line) and _ASK_PIN.search(line):
            self.asked.append(f"{line} → no")
            try:
                self.send("no")
            except BtError:
                pass

    def send(self, cmd):
        try:
            self.p.stdin.write((cmd + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            raise BtError("bluez_down", f"bluetoothctl exited: {e}") from None

    def expect(self, ok, fail=BT_FAIL, timeout=8.0, since=None, cancel=None):
        since = self.seq if since is None else since
        end = time.monotonic() + timeout
        ok_re, fail_re = re.compile(ok, re.I), re.compile(fail, re.I) if fail else None
        with self.cv:
            while True:
                for n, line in list(self.lines):
                    if n <= since:
                        continue
                    since = n
                    if ok_re.search(line):
                        return line
                    if fail_re and fail_re.search(line):
                        raise BtError(classify(line), line)
                if self.closed:
                    raise BtError("bluez_down", "bluetoothctl exited")
                if cancel is not None and cancel.is_set():
                    raise BtError("cancelled", "Cancelled by UI")
                left = end - time.monotonic()
                if left <= 0:
                    raise BtError("timeout", f"Timed out after {timeout:g}s waiting for: {ok}")
                self.cv.wait(min(left, 0.2))

    def cmd(self, text, ok, fail=BT_FAIL, timeout=8.0, cancel=None):
        since = self.seq
        self.send(text)
        return self.expect(ok, fail, timeout, since, cancel)

    def _wait(self, t):
        try:
            self.p.wait(t)
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:  # noqa: BLE001
            return True

    def close(self):
        """Graceful quit -> close stdin -> terminate -> kill."""
        if self._quit:
            return
        self._quit = True
        try:
            self.send("quit")
        except BtError:
            pass
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        if self._wait(2.0):
            return
        for fn in ("terminate", "kill"):
            try:
                getattr(self.p, fn)()
            except Exception:  # noqa: BLE001
                pass
            if self._wait(1.0):
                return


class BluetoothCtl:
    """Real BluetoothPort using bluetoothctl + /sys/class/rfkill."""

    def __init__(self, run=_run, spawn=None, prefix=(), rfkill_dir="/sys/class/rfkill", exe="bluetoothctl", stdbuf=None):
        self.run, self.spawn, self.prefix, self.rfkill_dir, self.exe = run, spawn, tuple(prefix), rfkill_dir, exe
        self.stdbuf = bool(shutil.which("stdbuf")) if stdbuf is None else stdbuf
        self.t = {"pair": 30.0, "connect": 20.0, "trust": 8.0, "find": 15.0, "settle": 8.0}    # Step timeouts
        self._session = None
        self._lk = threading.Lock()

    def _argv(self, *a):
        return [*self.prefix, self.exe, *a]

    def _session_argv(self):
        return [*self.prefix, *(("stdbuf", "-oL") if self.stdbuf else ()), self.exe]

    def adapter(self):
        blocked = _rfkill_blocked(self.rfkill_dir)
        rc, out = self.run(self._argv("show"), 6.0)
        text = "\n".join(clean_line(x) for x in out.splitlines())
        if rc == 127:
            return {"present": False, "error": None, "blocked": blocked, "raw": text, "missing": True}
        if re.search(r"AccessDenied|Access denied", text):
            return {"present": None, "error": text, "code": "bt_permission", "blocked": blocked, "raw": text}
        # Detect bluetoothd down before waiting on adapter
        if re.search(r"Waiting to connect to bluetoothd|org\.bluez was not provided|ServiceUnknown", text):
            return {"present": None, "error": text[-400:], "code": "bluez_down", "blocked": blocked, "raw": text[-400:]}
        if "No default controller" in text or "Controller" not in text:
            return {"present": False, "blocked": blocked, "raw": text[-400:]}
        p = parse_props(text)
        return {"present": True, "powered": _yes(p.get("powered")), "pairable": _yes(p.get("pairable")),
                "discovering": _yes(p.get("discovering")), "blocked": blocked, "raw": text[-400:]}

    def _simple(self, ok, *a, timeout=8.0):
        rc, out = self.run(self._argv(*a), timeout)
        text = clean_line(out.replace("\n", " "))
        if re.search(ok, out, re.I):
            return out
        raise BtError(classify(out) if rc != 124 else "timeout", text[-400:])

    def power_on(self):
        return self._simple(r"Changing power on succeeded|Powered: yes", "power", "on")

    def pairable_on(self):
        return self._simple(r"Changing pairable on succeeded|Pairable: yes", "pairable", "on")

    def pairable_off(self):
        return self._simple(r"Changing pairable off succeeded|Pairable: no", "pairable", "off")

    def info(self, mac):
        rc, out = self.run(self._argv("info", mac), 5.0)
        if rc != 0 or "not available" in out:
            return {}
        return device_from_props(mac, parse_props(out))

    def reconnect(self, mac, timeout=5.0):
        """Reconnect already-paired controller without registering agent or scanning."""
        if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
            raise BtError("other", "Invalid controller MAC address")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BtError("timeout", "No reconnect time remaining")
        return self._simple(rf"Connection successful|AlreadyConnected|Device {re.escape(mac)} Connected: yes",
                            "connect", mac, timeout=min(timeout, self.t["connect"]))

    def _open(self):
        with self._lk:
            if self._session is None or self._session.closed:
                s = BtSession(self._session_argv(), self.spawn)
                self._session = s
                try:
                    # Unregister any default agent on startup
                    s.cmd("agent off", r"Agent unregistered|No agent is registered|Agent is not registered|not registered",
                          fail=None, timeout=2.0)
                except BtError:
                    pass
            return self._session

    def scan_start(self):
        s = self._open()
        try:
            s.cmd("scan on", r"Discovery started|Discovering: yes", timeout=8.0)
        except BtError:
            self.close()          # Close failed session so next attempt starts fresh
            raise

    def scan_stop(self):
        s = self._session
        if s is not None and not s.closed:
            try:
                s.cmd("scan off", r"Discovery stopped|Discovering: no", fail=None, timeout=5.0)
            except BtError:
                pass

    def devices(self):
        """Return discovered devices, querying info on first sight."""
        s = self._session
        if s is None:
            return []
        with s.cv:
            snap = {k: dict(v) for k, v in s.devices.items()}
        out = []
        for mac, d in snap.items():
            if "_info" not in s.devices.get(mac, {}):
                info = self.info(mac)
                with s.cv:
                    if mac in s.devices:
                        s.devices[mac]["_info"] = info
            info = s.devices.get(mac, {}).get("_info") or {}
            merged = {**info, **{k: v for k, v in d.items() if v is not None and not k.startswith("_")}}
            out.append(merged)
        return out

    def pair_device(self, mac, classic, cancel, step):
        """Pair, trust, and connect target MAC in one bluetoothctl session."""
        s = self._open()
        m = re.escape(mac)
        try:
            try:
                try:
                    s.cmd("agent NoInputNoOutput", r"Agent registered", fail=BT_FAIL + r"|already registered", timeout=5.0)
                except BtError as e:
                    # bluetoothctl 5.82 may auto-register default agent after connect;
                    # retry agent off -> agent NoInputNoOutput if already registered
                    if "already registered" not in (e.raw or ""):
                        raise
                    s.cmd("agent off", r"Agent unregistered", timeout=3.0)
                    s.cmd("agent NoInputNoOutput", r"Agent registered", fail=BT_FAIL + r"|already registered", timeout=5.0)
                s.cmd("default-agent", r"Default agent request successful", timeout=5.0)
            except BtError as e:
                raise BtError("agent" if e.code in ("other", "timeout") else e.code, e.raw) from None
            s.answer_yes = True
            if mac not in s.devices:
                s.cmd("scan on", r"Discovery started|Discovering: yes", timeout=8.0)
                try:
                    s.expect(rf"Device {m}", fail=None, timeout=self.t["find"], since=0, cancel=cancel)
                except BtError as e:
                    if e.code == "timeout":
                        raise BtError("not_found", f"Did not see {mac} within {self.t['find']:g}s; still in pairing mode?") from None
                    raise
            try:
                s.cmd("scan off", r"Discovery stopped|Discovering: no", fail=None, timeout=5.0)
            except BtError:
                pass
            ok_pair = rf"Pairing successful|AlreadyExists|Device {m} (Paired|Bonded): yes"
            if not classic:
                step("connect", "doing", "Connecting LE controller (auto-bonds on connect)")
                try:
                    s.cmd(f"connect {mac}", rf"Connection successful|AlreadyConnected|Device {m} Connected: yes",
                          timeout=self.t["connect"], cancel=cancel)
                except BtError as e:
                    if e.code == "cancelled":
                        raise
                try:
                    s.expect(rf"Device {m} (Paired|Bonded): yes", fail=None, timeout=5.0, since=0, cancel=cancel)
                    step("pair", "ok")
                except BtError as e:
                    if e.code == "cancelled":
                        raise
            if not (s.devices.get(mac, {}).get("paired")):
                step("pair", "doing")
                s.cmd(f"pair {mac}", ok_pair, timeout=self.t["pair"], cancel=cancel)
            step("pair", "ok")
            step("trust", "doing")
            s.cmd(f"trust {mac}", rf"{m} trust succeeded|Device {m} Trusted: yes", timeout=self.t["trust"], cancel=cancel)
            step("trust", "ok")
            step("connect", "doing")
            ok_conn = rf"Connection successful|AlreadyConnected|Device {m} Connected: yes"
            mark = s.seq
            try:
                s.cmd(f"connect {mac}", ok_conn, timeout=self.t["connect"], cancel=cancel)
            except BtError as e:
                # Classic controllers may initiate connection right after pairing (InProgress)
                # Wait briefly before issuing connect
                if e.code != "busy":
                    raise
                step("connect", "doing", "Controller connecting, waiting...")
                try:
                    s.expect(rf"Device {m} Connected: yes", fail=None, timeout=self.t["settle"], since=mark, cancel=cancel)
                except BtError as e2:
                    if e2.code != "timeout":
                        raise
                    s.cmd(f"connect {mac}", ok_conn, timeout=self.t["connect"], cancel=cancel)
            step("connect", "ok")
        finally:
            s.answer_yes = False
            try:
                s.cmd("agent off", r"Agent unregistered|No agent is registered|not registered", fail=None, timeout=3.0)
            except BtError:
                pass

    def remove(self, mac):
        rc, out = self.run(self._argv("remove", mac), 8.0)
        if "Device has been removed" in out:
            return True
        if "not available" in out:
            return False
        raise BtError(classify(out), clean_line(out.replace("\n", " "))[-400:])

    def close(self):
        with self._lk:
            s, self._session = self._session, None
        if s is not None:
            s.close()


def default_pad(log, configd_addr="unix:/run/configd.sock", robotd_addr="unix:/run/robotd.sock",
                tap_addr="unix:/run/padd/pad.sock", bt_prefix=(), state_dir=None):
    """Default port wiring on the duck (omits BluetoothPort if bluetoothctl is absent)."""
    bt = BluetoothCtl(prefix=bt_prefix) if shutil.which("bluetoothctl") else None
    from .audio_interaction import AudioClient
    return PadOps(log, configd=ConfigdPort(configd_addr), bt=bt, tap=PaddTap(tap_addr, log=log),
                  robot=RobotPort(robotd_addr), state_dir=state_dir, audio=AudioClient(),
                  mapping_client=MappingClient())
