"""Overview onboarding checklist, plus CPU thermal hysteresis and local thermal/cpufreq fallback.

Six ordered steps:
    Header (unnumbered) Connect to the duck
    1 Servo Zeroing   2 IMU Orientation   3 Write to robotd   4 Pair Gamepad   5 Test E-Stop   6 Stand & Walk
Five states per step: ok (done), skip (skipped), todo (needs action/redo), no (not done), unk (unknown).
Every step can be marked Done, Skipped, or Redo at any time (with two-click confirmation when risky).
Persisted atomically to state_dir/onboarding.json; evaluated at 0.5 Hz in background so summarize() stays read-only.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

from .estop import STARTING_LONG_S, STARTING_PHASES
from .imu import mount_of
from .robotd import fresh_calib
from .state import JOINT_CN, MAX_AGE, STILL_STARTING, posture, starting_secs
from .startup_status import startup_wait

ONBOARD_FILE = "onboarding.json"
SERVO_IDS = (20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14)   # Matches servo.IDS (avoid importing serial)
STOOD_S = 3.0             # Continuous standing time required to mark 'stood'
WALKED_S = 2.0            # Cumulative walking time required to mark 'walked'
CPU_WARN_C = 80.0         # Matches state.CPU_WARN_C
CPU_CLEAR_C = 77.0        # Hysteresis clear threshold
STUDIO_SCAN_S = 10.0      # Interval to rescan servo-calib directory
THERMAL_ROOT = "/sys/class/thermal"
ROBOTD_CONFIGS = ("/etc/robot/feetech-ft5/robotd.toml", "/etc/robot/robotd.toml")
ROBOTD_CFG_S = 30.0       # Interval to reread robotd config
CPUFREQ_ROOT = "/sys/devices/system/cpu/cpufreq"
STAND = ("holding", "policy", "walking")
# Error pattern when motion mode blocks at waiting_for_bus due to incomplete calibration or IMU
CALIB_ERR = re.compile(r"calibrat|motion_enabled|calibration file|IMU mounting", re.I)

TIPS = {
    "servo": "If already calibrated, mark Done or Skip to keep existing zero positions; use Servo page to re-zero",
    "imu": "An 8.5 deg tilt error causes forward drift when walking",
    "commit": "robotd will release torque before writing; hold or support the duck first",
    "pad": "Switch G50 Pro to S mode first",
    "estop": "Re-test whenever switching gamepads",
    "walk": None,
}
TITLES = {"servo": "Servo Zeroing", "imu": "IMU Orientation", "commit": "Write to robotd", "pad": "Pair Gamepad", "estop": "Test E-Stop", "walk": "Stand & Walk"}
ORDER = ("servo", "imu", "commit", "pad", "estop", "walk")
GO = {"servo": "servos", "imu": "imu", "commit": "commit", "pad": "pad_wizard", "estop": "estop", "walk": "remote"}
SKIP_CHANGED = {"servo": "Servo zeroing", "imu": "IMU orientation (or chip)", "commit": "Calibration", "estop": "Gamepad (or robotd build)", "walk": "Calibration"}
COMMIT_TEXT = "Write servo zero offsets, directions, and IMU orientation to robotd. Switches from commissioning to motion mode and restarts (~20s) so the duck can stand and walk autonomously on boot."

KINDS = {"done": "done", "skip": "skipped"}
OTHER = {"done": "skip", "skip": "done"}
MARKED_TEXT = {"done": "Marked done", "skip": "Skipped"}
MARKED_WORD = {"done": "marked done", "skip": "skipped"}
CHANGED_WORD = {"done": "marked done", "skip": "skipped"}
ASK_Q = {"done": "mark as done", "skip": "skip"}
ASK_V = {"done": "Marked done", "skip": "Skipped"}
HARD_SERVO = "robotd reports servo calibration incomplete: even if {v}, the duck cannot stand"
HARD_IMU = "robotd has not verified IMU orientation: even if {v}, the duck cannot stand"
HARD_STUCK = "robotd refuses to arm torque (calibration or IMU unverified): even if {v}, the duck cannot stand"
UNK_WHY = "Cannot verify this step right now ({text}); Studio will trust your selection"
FP_WHY = "{what}: Studio will record this and verify once telemetry is available"
FROZEN_ASK = "Confirm {q}? Servo tuning is active, so the checklist is paused until you hand control back to robotd"
FROZEN_UNDO = "Confirm undo? Servo tuning is active, so the change will appear after handing control back to robotd"


def fill(text, kind):
    """Replace {q} and {v} placeholders in confirmation prompts."""
    return text.replace("{q}", ASK_Q[kind]).replace("{v}", ASK_V[kind]) if text else text


# ---------------- Fingerprints ----------------

def _joints(calib):
    cal = (calib or {}).get("calibration") if isinstance((calib or {}).get("calibration"), dict) else None
    js = (cal or {}).get("joints")
    return [j for j in js if isinstance(j, dict)] if isinstance(js, list) else None


def _hash(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def joints_fp(calib):
    """Fingerprint of 15 zero_raw / direction values; None if calibration is unreadable."""
    js = _joints(calib)
    if not js:
        return None
    return _hash(sorted([j.get("id"), j.get("zero_raw"), j.get("direction")] for j in js))


def mount_fp(source, quat):
    """Fingerprint of IMU source + mount quaternion (rounded to 4 decimals)."""
    if not isinstance(quat, (list, tuple)) or len(quat) != 4:
        return None
    try:
        return _hash([source, [round(float(x), 4) for x in quat]])
    except (TypeError, ValueError):
        return None


def full_fp(bus, calib):
    """Combined calibration fingerprint (joints + active IMU mount)."""
    j = joints_fp(calib)
    source, mount = mount_of(bus, calib)
    m = mount_fp(source, (mount or {}).get("quat"))
    if j is None or m is None:
        return None
    return _hash([j, m])


# ---------------- Studio Servo Calibration Records ----------------

def studio_servo_record(state_dir):
    """Union of targets minus bad across non-sim, non-undone eeprom-*.json files."""
    d = os.path.join(state_dir or ".", "servo-calib")
    ids, latest = set(), None
    try:
        names = sorted(f for f in os.listdir(d) if f.startswith("eeprom-") and f.endswith(".json"))
    except OSError:
        return {"ids": ids, "time": None}
    for fn in names:
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("sim"):
            continue
        targets = rec.get("targets") if isinstance(rec.get("targets"), dict) else {}
        bad = rec.get("bad") if isinstance(rec.get("bad"), dict) else {}
        got = {int(k) for k in targets if str(k).isdigit()} - {int(k) for k in bad if str(k).isdigit()}
        if got:
            ids |= got
            latest = rec.get("time") or latest
    return {"ids": ids, "time": latest}


# ---------------- Board Temperature (local fallback matching robotd/src/soc.rs) ----------------

def _read_int(path):
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_board(thermal_root=THERMAL_ROOT, cpufreq_root=CPUFREQ_ROOT):
    """Read board temperature and CPU throttling from sysfs (matching robotd health)."""
    try:
        zones = [e for e in os.listdir(thermal_root)]
    except OSError:
        return None
    hot = None
    for name in zones:
        if not name.startswith("thermal_zone"):
            continue
        milli = _read_int(os.path.join(thermal_root, name, "temp"))
        if milli is None or milli <= 0:
            continue
        c = milli / 1000.0
        hot = c if hot is None or c > hot else hot
    thr = None
    try:
        pols = os.listdir(cpufreq_root)
    except OSError:
        pols = []
    for name in pols:
        if not name.startswith("policy"):
            continue
        khz = _read_int(os.path.join(cpufreq_root, name, "scaling_max_freq"))
        mx = _read_int(os.path.join(cpufreq_root, name, "cpuinfo_max_freq"))
        if khz is None or not mx:
            continue
        if thr is None or khz * thr["max_khz"] < thr["khz"] * mx:
            thr = {"khz": khz, "max_khz": mx, "level": 0, "max_level": 0}
    if thr is not None:
        for name in zones:
            if not name.startswith("cooling_device"):
                continue
            try:
                with open(os.path.join(thermal_root, name, "type"), encoding="ascii") as f:
                    kind = f.read().strip()
            except OSError:
                continue
            if "cpufreq" not in kind:
                continue
            lv, mx = _read_int(os.path.join(thermal_root, name, "cur_state")), _read_int(os.path.join(thermal_root, name, "max_state"))
            if lv is None or not mx:
                continue
            if thr["max_level"] == 0 or lv * thr["max_level"] > thr["level"] * mx:
                thr.update(level=lv, max_level=mx)
    if hot is None and thr is None:
        return None
    return {"c": hot, "throttle": thr}


def read_battery_shutdown(paths=ROBOTD_CONFIGS):
    """Read [safety] battery_empty_shutdown from robotd config; None if unreadable/unset."""
    import tomllib
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        v = (data.get("safety") or {}).get("battery_empty_shutdown") if isinstance(data.get("safety"), dict) else None
        return v if isinstance(v, bool) else None
    return None


# ---------------- Step Evaluation ----------------

def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _f1(x):
    """Format to 1 decimal place, or '?' if not a number."""
    return f"{x:.1f}" if _num(x) else "?"


def _row(key, state, text, when=None, actions=(), note=None, hard=None, found=None, ask=None):
    """Build one checklist row."""
    return {"key": key, "n": ORDER.index(key) + 1, "title": TITLES[key], "tip": TIPS[key], "state": state,
            "text": text, "when": when, "note": note, "actions": [{"do": d, "label": lab} for d, lab in actions],
            "hard": hard, "found": found, "ask": ask, "go": GO[key], "mark": None, "marked": None}


def header_of(inp):
    """Checklist header status (unnumbered). kind: ok / absent / connecting / bench / servo / commissioning / stuck_calib / starting / other."""
    bus = inp.get("bus") if isinstance(inp.get("bus"), dict) else None
    if inp.get("bench"):
        return {"ok": False, "kind": "bench", "text": "Bench mode: not connected to robotd; checklist disabled"}
    if inp.get("servo_session"):
        return {"ok": False, "kind": "servo", "text": "Servo tuning active (robotd paused): checklist frozen until handback"}
    if inp.get("present") is False:
        return {"ok": False, "kind": "absent", "text": "robotd is not running: cannot connect"}
    if bus is None:
        return {"ok": False, "kind": "connecting", "text": "Connecting to robotd..."}
    mode, phase, err = bus.get("mode"), bus.get("phase"), bus.get("error")
    if phase in STARTING_PHASES:
        if err and CALIB_ERR.search(str(err)):
            return {"ok": False, "kind": "stuck_calib", "text": "robotd refuses to arm: calibration or IMU unverified", "raw": str(err)}
        wait = startup_wait(bus)
        if wait:
            text = wait["text"]
        elif (inp.get("starting_s") or 0) >= STARTING_LONG_S:
            text = f"robotd stuck starting for over {STARTING_LONG_S:.0f}s: {STILL_STARTING}"
        else:
            text = "robotd is still starting"
        return {"ok": False, "kind": "starting", "text": text, "raw": str(err) if err else None}
    if mode == "commissioning":
        if phase == "read_error":
            return {"ok": False, "kind": "commissioning", "text": "Commissioning mode: robotd is up, but servo read failed (check power & wiring)",
                    "raw": str(bus.get("last_cycle_error") or err or "") or None}
        return {"ok": False, "kind": "commissioning", "text": "Commissioning mode: run rough IMU calibration, then commit in Step 3 to switch to motion mode"}
    if mode == "motion" and phase == "control":
        return {"ok": True, "kind": "ok", "text": "Connected"}
    return {"ok": False, "kind": "other", "text": f"robotd in mode={mode or '?'}, phase={phase or '?'}"}


def _imu_mount_unverified(bus, calib):
    """Return True if robotd explicitly reports IMU orientation as unverified."""
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    _source, mount = mount_of(bus, calib)
    return native.get("imu_mount_verified") is False or (isinstance(mount, dict) and mount.get("verified") is False)


def servo_row(inp, rec, head):
    bus = inp.get("bus") or {}
    calib = inp.get("calib")
    if head["kind"] in ("absent", "connecting", "bench"):
        return _row("servo", "unk", "Connect to robotd to check", actions=[("servos", "Open Servo Page")])
    uncal = (calib or {}).get("uncalibrated_joints")
    if head["kind"] == "stuck_calib":
        if not (_imu_mount_unverified(bus, calib) and isinstance(uncal, list) and not uncal):
            return _row("servo", "no", "robotd refuses to arm: calibration or IMU orientation incomplete",
                        actions=[("servos", "Open Servo Page")], hard=HARD_STUCK)
    js = _joints(calib)
    if isinstance(uncal, list) and uncal:
        names = ", ".join(JOINT_CN.get(str(x), str(x)) for x in uncal[:4]) + ("..." if len(uncal) > 4 else "")
        return _row("servo", "no", f"robotd reports {len(uncal)} uncalibrated servos: {names}", actions=[("servos", "Open Servo Page")], hard=HARD_SERVO)
    if js and any(j.get("calibrated") is False for j in js):
        bad = [JOINT_CN.get(j.get("name"), j.get("name")) for j in js if j.get("calibrated") is False]
        return _row("servo", "no", f"{len(bad)} servos uncalibrated in robotd file: {', '.join(bad[:4])}", actions=[("servos", "Open Servo Page")],
                    hard=HARD_SERVO)
    if head["kind"] == "commissioning" and bus.get("calibrated") is False:
        return _row("servo", "no", "robotd reports servo calibration incomplete in commissioning mode", actions=[("servos", "Open Servo Page")], hard=HARD_SERVO)
    if not js:
        return _row("servo", "unk", "Calibration status not yet received from robotd on this connection", actions=[("servos", "Open Servo Page")])
    fp = joints_fp(calib)
    c = rec.get("confirm_servo") if isinstance(rec.get("confirm_servo"), dict) else None
    if c and c.get("fp") == fp:
        return _row("servo", "ok", "Verified servo zero positions and directions in robotd file", when=c.get("at"))
    off = [j for j in js if j.get("zero_raw") != 2048]
    if off:
        def one(j):
            z = j.get("zero_raw")
            d = f" ({(z - 2048) * 360 / 4096:+.1f} deg)" if isinstance(z, (int, float)) else ""
            return f"{JOINT_CN.get(j.get('name'), j.get('name'))} {z}{d}"
        text = f"robotd has {len(off)} servos with custom zero offsets: " + ", ".join(one(j) for j in off[:3]) \
            + ("..." if len(off) > 3 else "") + ". Confirm they match this duck to keep them"
        return _row("servo", "todo", text, actions=[("servos", "Open Servo Page")],
                    found="{v} will keep existing servo zero positions; confirm they match this duck")
    st = inp.get("studio_servo") or {}
    if set(SERVO_IDS) <= set(st.get("ids") or ()):
        ss = rec.get("servo_studio") if isinstance(rec.get("servo_studio"), dict) else None
        if ss and ss.get("src") == st.get("time") and ss.get("fp") == fp:
            return _row("servo", "ok", "Calibrated via Servo page; all 15 servos at 2048 in robotd", when=st.get("time"))
        if ss and ss.get("src") == st.get("time"):
            return _row("servo", "todo", "robotd calibration file changed since Servo page calibration: mark Done if verified, or recalibrate",
                        when=st.get("time"), actions=[("servos", "Open Servo Page")])
    if c:
        return _row("servo", "todo", "robotd calibration file changed since verification: mark Done if verified, or recalibrate",
                    actions=[("servos", "Open Servo Page")])
    return _row("servo", "todo", "robotd file shows calibrated (may be factory seed or manual), but no Studio record exists: "
                "mark Done if already calibrated", actions=[("servos", "Open Servo Page")])


def imu_row(inp, rec, head):
    bus = inp.get("bus") or {}
    calib = inp.get("calib")
    if head["kind"] in ("absent", "connecting", "bench"):
        return _row("imu", "unk", "Connect to robotd to check", actions=[("imu", "Open IMU Page")])
    source, mount = mount_of(bus, calib)
    if head["kind"] == "commissioning" and bus.get("imu_mount_verified") is False:
        return _row("imu", "no", "robotd reports IMU orientation unverified in commissioning mode", actions=[("imu", "Open IMU Page")], hard=HARD_IMU)
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    if native.get("imu_mount_verified") is False:
        return _row("imu", "no", "robotd reports IMU orientation unverified: misalignment causes forward drift", actions=[("imu", "Open IMU Page")],
                    hard=HARD_IMU)
    if mount and mount.get("verified") is False:
        return _row("imu", "no", "Stored IMU orientation is unverified (verified=false): robotd will not arm",
                    actions=[("imu", "Open IMU Page")], hard=HARD_IMU)
    mfp = mount_fp(source, (mount or {}).get("quat"))
    if mfp is None:
        return _row("imu", "unk", "IMU orientation not yet received from robotd on this connection", actions=[("imu", "Open IMU Page")])
    chk = rec.get("imu_check") if isinstance(rec.get("imu_check"), dict) else None
    if chk and chk.get("source") == source and chk.get("mount_fp") == mfp:
        d, t = chk.get("diff_deg"), chk.get("tilt_deg")
        if chk.get("ok") is True and _num(d):
            return _row("imu", "ok", f"Verified standing on IMU page: {_f1(d)} deg from stored mount", when=chk.get("at"))
        return _row("imu", "todo", f"Last standing check: {_f1(d)} deg from stored mount, body tilt {_f1(t)} deg "
                    "(requires < 2 deg diff, <= 5 deg tilt)", when=chk.get("at"), actions=[("imu", "Open IMU Page")],
                    found=f"Standing check differed by {_f1(d)} deg: {{v}} may still drift forward when walking")
    if chk:
        return _row("imu", "todo", "IMU orientation or chip changed since last check: re-verify standing on IMU page", actions=[("imu", "Open IMU Page")])
    return _row("imu", "todo", "File says verified, but even an 8.5 deg tilt reads verified: run standing check on IMU page", actions=[("imu", "Open IMU Page")])


def _commit_complete(bus, calib):
    """robotd completion check: motion_enabled=true, all 15 calibrated, active IMU verified."""
    cal = (calib or {}).get("calibration") if isinstance(calib, dict) else None
    cal = cal if isinstance(cal, dict) else (calib if isinstance(calib, dict) else {})
    js = _joints(calib)
    _source, mount = mount_of(bus, calib)
    return (cal.get("motion_enabled") is True and isinstance(js, list) and len(js) == len(SERVO_IDS)
            and all(j.get("calibrated") is True for j in js)
            and isinstance(mount, dict) and mount.get("verified") is True)


def commit_row(inp, rec, head):
    """Step 3 (Write to robotd): checks robotd state and attaches CommitService readiness."""
    bus = inp.get("bus") or {}
    calib = inp.get("calib")
    cv = inp.get("commit") if isinstance(inp.get("commit"), dict) else None

    def out(state, text, actions=()):
        row = _row("commit", state, text, actions=actions)
        row["desc"] = COMMIT_TEXT
        if cv is not None and cv.get("note"):
            row["desc"] += "\n" + cv["note"]
        if cv is not None:
            row["commit"] = cv
        return row

    if head["kind"] in ("absent", "connecting", "bench"):
        return out("unk", "Connect to robotd to check", [("commit", "Inspect")])
    if cv is not None and cv.get("dirty") is True:
        why = ", ".join(cv.get("dirty_why") or []) or "pending changes"
        return out("todo", f"Unwritten changes pending: {why}", [("commit", "Write to robotd")])
    if _commit_complete(bus, calib):
        return out("ok", "Written to robotd: 15 servos calibrated and IMU verified; ready to stand & walk on boot",
                   [("commit", "Rewrite")])
    js = _joints(calib)
    if (not isinstance(js, list) or not js) and not (cv is not None and cv.get("preview_ok") is True):
        return out("unk", "Calibration file not yet received from robotd; wait a few seconds", [("commit", "Inspect")])
    if cv is None:
        return out("todo", "Not written to robotd yet; waiting for readiness check", [("commit", "Inspect")])
    items = cv.get("items") if isinstance(cv.get("items"), list) else []
    bad = [i for i in items if isinstance(i, dict) and not i.get("ok")]
    if bad:
        labels = ", ".join(str(i.get("label") or "?") for i in bad[:3]) + ("..." if len(bad) > 3 else "")
        return out("todo", f"Missing {labels}: {bad[0].get('text') or 'complete the checklist below'}", [("commit", "Write to robotd")])
    if cv.get("ready") is True:
        return out("todo", "Ready: click 'Write to robotd' to save and restart robotd", [("commit", "Write to robotd")])
    return out("todo", "Not written to robotd yet; complete the prerequisites below", [("commit", "Write to robotd")])

def _pad_names(pads):
    return ", ".join(p.get("name") or p.get("mac") or "Gamepad" for p in pads[:2])


def pad_row(inp, rec, head):
    pad = inp.get("pad")
    if pad is None:
        return _row("pad", "unk", "Gamepad backend disabled (bench mode)")
    pads = [p for p in pad.get("pads") or [] if isinstance(p, dict)]
    good = [p for p in pads if p.get("paired") and p.get("trusted")]
    if good:
        on = next((p for p in good if p.get("connected")), None)
        return _row("pad", "ok", f"{on.get('name') or 'Gamepad'} · Connected" if on else
                    f"Paired {_pad_names(good)} · Offline (power on gamepad to auto-connect)")
    if any(p.get("paired") for p in pads):
        return _row("pad", "todo", "Paired but not trusted: will not auto-reconnect on reboot; click Repair", actions=[("pad_known", "Repair")])
    dev = pad.get("device") or {}
    if pad.get("attached"):
        return _row("pad", "ok", f"padd sees {dev.get('name') or 'Gamepad'} attached" + ("" if pad.get("known") else " (configd unavailable)"))
    if pad.get("known"):
        return _row("pad", "no", "No gamepad paired yet (you can also use the web gamepad below)", actions=[("pad_wizard", "Pair Gamepad")])
    seen = rec.get("pads_seen") if isinstance(rec.get("pads_seen"), dict) else {}
    tail = f"; previously paired {', '.join(list(seen.values())[:2])}" if seen else ""
    return _row("pad", "unk", f"Cannot read configd gamepad state (may be disabled){tail}", actions=[("pad_wizard", "Pair Gamepad")])


def _current_pad(pad):
    """Current gamepad (mac, name): active, attached, or sole paired pad."""
    if pad is None:
        return None, None
    a = pad.get("active") or {}
    if a.get("mac"):
        return str(a["mac"]).upper(), a.get("name")
    dev = pad.get("device") or {}
    if pad.get("attached") and dev.get("unique"):
        return str(dev["unique"]).upper(), dev.get("name")
    paired = [p for p in pad.get("pads") or [] if isinstance(p, dict) and p.get("paired") and p.get("mac")]
    if pad.get("known") and len(paired) == 1:
        return str(paired[0]["mac"]).upper(), paired[0].get("name")
    return None, None


def _no_physical_pad(pad):
    """True when configd confirms zero paired and zero attached physical gamepads."""
    return bool(pad and pad.get("known") and not pad.get("attached")
                and not [p for p in pad.get("pads") or [] if isinstance(p, dict) and p.get("paired")])


def _many_unconnected(pad):
    """Count of paired gamepads when >=2 are paired and none is connected."""
    if not pad or pad.get("attached") or (pad.get("active") or {}).get("mac"):
        return 0
    paired = [p for p in pad.get("pads") or [] if isinstance(p, dict) and p.get("paired")]
    return len(paired) if len(paired) >= 2 and not any(p.get("connected") for p in paired) else 0


ESTOP_ASK = {"never": "Confirm {q}? Untested: gamepad Select stop has not been verified",
             "web": "Confirm {q}? Web-only E-Stop uses Space / top red button; re-test if you pair a gamepad later",
             "pad": "Confirm {q}? Gamepad changed: Select on this gamepad has not been tested",
             "build": "Confirm {q}? robotd updated: Select has not been re-tested on this build",
             "bad": "Confirm {q}? E-Stop is currently not working ({why}); {v} will not fix it"}


def estop_row(inp, rec, head):
    pad = inp.get("pad")
    r = rec.get("estop") if isinstance(rec.get("estop"), dict) else None
    es = (pad or {}).get("estop") or {}
    first_bad = next((c for c in es.get("checks") or [] if c.get("ok") is False), None)
    bad_why = f"{first_bad.get('label')}: {first_bad.get('why') or 'failed'}" if first_bad else None
    many = _many_unconnected(pad)
    if r is None:
        web = _no_physical_pad(pad)
        web_only = ". Using web only: Space / top E-Stop button works directly; you may skip this step" if web else ""
        now = f" ({many} gamepads paired, none connected: connect one first)" if many else (f" (currently blocked: {bad_why})" if bad_why else "")
        ask = ESTOP_ASK["web"] if web else (ESTOP_ASK["bad"].replace("{why}", bad_why) if es.get("state") == "bad" and bad_why
                                            else ESTOP_ASK["never"])
        return _row("estop", "no", "Select E-Stop has not been tested yet" + now + web_only, actions=[("estop", "Test E-Stop")], ask=ask)
    mac, _ = _current_pad(pad)
    build = (inp.get("bus") or {}).get("build")
    if mac is None or not build:
        miss = (f"{many} gamepads paired, none connected: connect one first" if many else "cannot detect connected gamepad") if mac is None \
            else "robotd build unknown"
        return _row("estop", "unk", f"Tested on {r.get('name') or 'Gamepad'}, cannot verify now: {miss}", when=r.get("at"),
                    actions=[("estop", "Test E-Stop")])
    if str(r.get("mac") or "").upper() != mac:
        return _row("estop", "todo", "Gamepad changed: re-test E-Stop", when=r.get("at"), actions=[("estop", "Test E-Stop")], ask=ESTOP_ASK["pad"])
    if r.get("build") != build:
        return _row("estop", "todo", "robotd updated: re-test E-Stop", when=r.get("at"), actions=[("estop", "Test E-Stop")],
                    ask=ESTOP_ASK["build"])
    if es.get("state") == "bad":
        return _row("estop", "todo", f"Tested previously, but E-Stop is currently failing: {bad_why}", when=r.get("at"), actions=[("estop", "Inspect")],
                    found=f"E-Stop is currently failing ({bad_why}): {{v}} will not fix it")
    ms = r.get("ms")
    return _row("estop", "ok", f"Tested on {r.get('name') or 'Gamepad'}: Select release to limp in {ms} ms" if _num(ms)
                else f"Tested on {r.get('name') or 'Gamepad'}", when=r.get("at"))


def walk_row(inp, rec, head):
    s = rec.get("stood") if isinstance(rec.get("stood"), dict) else None
    w = rec.get("walked") if isinstance(rec.get("walked"), dict) else None
    go = [("remote", "Open Remote")]
    if s is None and w is None:
        return _row("walk", "no", "Not yet stood up: support the duck, click Stand, then Walk and push left stick", actions=go)
    fp = inp.get("fp")
    last = (w if w is not None else s).get("at")
    if fp is None:
        return _row("walk", "unk", ("Stood & walked" if w else "Stood up") + "; calibration unreadable, cannot verify", when=last, actions=go)
    s_ok = s is not None and s.get("fp") == fp
    w_ok = w is not None and w.get("fp") == fp
    if w_ok:
        return _row("walk", "ok", "Stood up and walked", when=w.get("at"))
    if (s is not None and not s_ok) or w is not None:
        return _row("walk", "todo", "Calibration changed since last " + ("stand & walk" if not s_ok else "walk"), when=last, actions=go)
    return _row("walk", "todo", "Stood up, not walked yet: click Walk and push left stick", when=s.get("at"), actions=go)


def step_fp(key, inp):
    """(bind_fingerprint, current_fingerprint) for step."""
    bus = inp.get("bus") or {}
    calib = inp.get("calib")
    if key == "servo":
        return True, joints_fp(calib)
    if key == "imu":
        source, mount = mount_of(bus, calib)
        return True, mount_fp(source, (mount or {}).get("quat"))
    if key == "commit":
        return True, inp.get("fp")             # Calibration changed: invalidate commit mark
    if key == "estop":
        mac, _ = _current_pad(inp.get("pad"))
        if mac is None and _no_physical_pad(inp.get("pad")):
            mac = "no-physical-pad"
        build = bus.get("build")
        return True, (_hash([mac, build]) if mac and build else None)
    if key == "walk":
        return True, inp.get("fp")
    return False, None                          # Gamepad pairing: no fingerprint


FP_WHAT = {"servo": "robotd calibration unreadable", "imu": "stored IMU orientation unreadable", "commit": "calibration unreadable",
           "estop": "connected gamepad or robotd build unknown", "walk": "calibration unreadable"}
BOUND = ("servo", "imu", "commit", "estop", "walk")      # Steps that bind marks to a fingerprint


def _marks(rec, key):
    """Return user marks for step: {kind: record}."""
    out = {}
    for kind, rk in KINDS.items():
        d = rec.get(rk) if isinstance(rec.get(rk), dict) else {}
        if isinstance(d.get(key), dict):
            out[kind] = d[key]
    return out


def mark_offer(row, bound, fp):
    """Determine whether Done/Skip requires two-step confirmation and build the confirmation prompt."""
    key = row["key"]
    acks = [k for k in ("found", "hard") if row.get(k)]
    if row["state"] == "unk":
        acks.append("unk")
    if bound and fp is None:
        acks.append("fp")
    if row.get("found"):
        ask = "Confirm {q}? " + row["found"]
    elif row.get("hard"):
        ask = "Confirm {q}? " + row["hard"]
    elif key == "estop" and row.get("ask"):
        ask = row["ask"]
    elif row["state"] == "unk":
        ask = "Confirm {q}? " + UNK_WHY.replace("{text}", row["text"])
    elif key == "estop":
        ask = ESTOP_ASK["never"]
    elif bound and fp is None:
        ask = "Confirm {q}? " + FP_WHY.replace("{what}", FP_WHAT[key])
    else:
        ask = None
    out = {"ack": acks}
    for kind in KINDS:
        out[kind] = {"confirm": ask is not None, "ask": fill(ask, kind)}
    return out


def with_mark(row, inp, rec):
    """Apply user Done/Skip mark onto an evaluated row."""
    key = row["key"]
    if row["state"] == "ok":
        return row
    bound, fp = step_fp(key, inp)
    for kind, m in _marks(rec, key).items():
        ack = m.get("ack") if isinstance(m.get("ack"), list) else []
        if row.get("found") and "found" not in ack:
            row["note"] = f"Previously {MARKED_WORD[kind]}, but an issue was detected so {'skip' if kind == 'skip' else 'mark'} was cleared"
            row["void"] = row.get("void", []) + [kind]
            continue
            row["void"] = row.get("void", []) + [kind]
            continue
        if row.get("hard") and "hard" not in ack:
            row["note"] = f"Previously {MARKED_WORD[kind]}, but robotd reports this step incomplete; {'skip' if kind == 'skip' else 'mark'} has no effect"
            continue
        old = m.get("fp")
        if bound and fp is not None and old is not None and old != fp:
            row = dict(row, state="todo", text=f"{SKIP_CHANGED[key]} changed after being {CHANGED_WORD[kind]}: {row['text']}")
            continue
        out = _row(key, "ok" if kind == "done" else "skip", MARKED_TEXT[kind], when=m.get("at"))
        out["note"] = row["text"] if row["state"] != "unk" else None   # Underlying status shown as subtext
        out["marked"] = kind
        for k in ("desc", "commit"):                                  # Keep Step 3 description & readiness visible
            if k in row:
                out[k] = row[k]
        return out
    row["mark"] = mark_offer(row, bound, fp)
    return row


def evaluate(inp, rec):
    """Pure evaluation: inp is collected by Onboarding._inputs(), rec is persisted records."""
    head = header_of(inp)
    rows = [servo_row(inp, rec, head), imu_row(inp, rec, head), commit_row(inp, rec, head),
            pad_row(inp, rec, head), estop_row(inp, rec, head), walk_row(inp, rec, head)]
    rows = [with_mark(r, inp, rec) for r in rows]
    nxt = next((r["key"] for r in rows if r["state"] in ("todo", "no")), None)
    return {"header": head, "rows": rows, "next": nxt, "all_done": all(r["state"] in ("ok", "skip") for r in rows),
            "skipped_n": sum(1 for r in rows if r["state"] == "skip"),
            "marked_n": sum(1 for r in rows if r.get("marked") == "done"), "frozen": False}


# ---------------- Per-entry Record Validation ----------------

def _opt(x, *types):
    return x is None or (isinstance(x, types) and not isinstance(x, bool))


def _at(d):
    return _opt(d.get("at"), int, float)


CHECKS = {
    "confirm_servo": lambda d: _at(d) and isinstance(d.get("fp"), str),
    "servo_studio": lambda d: _at(d) and isinstance(d.get("fp"), str) and _opt(d.get("src"), str),
    "imu_check": lambda d: (_at(d) and _opt(d.get("source"), str) and isinstance(d.get("mount_fp"), str)
                            and _num(d.get("diff_deg")) and _opt(d.get("tilt_deg"), int, float) and isinstance(d.get("ok"), bool)),
    "estop": lambda d: (_at(d) and _opt(d.get("ms"), int, float) and _opt(d.get("mac"), str) and _opt(d.get("name"), str)
                        and _opt(d.get("build"), str)),
    "stood": lambda d: _num(d.get("at")) and isinstance(d.get("fp"), str),
    "walked": lambda d: _num(d.get("at")) and isinstance(d.get("fp"), str),
}


def clean_records(data):
    """Return (valid_records, [(key, reason)]). Validates known keys via CHECKS; preserves unknown keys."""
    out, bad = {}, []
    for k, v in data.items():
        if k == "v":
            continue
        if k == "pads_seen":
            if isinstance(v, dict):
                out[k] = {str(m): n for m, n in v.items() if isinstance(n, str)}
            else:
                bad.append((k, f"Not an object ({type(v).__name__})"))
            continue
        if k in KINDS.values():
            if not isinstance(v, dict):
                bad.append((k, f"Not an object ({type(v).__name__})"))
                continue
            good = {}
            for step, e in v.items():
                if (step in ORDER and isinstance(e, dict) and _num(e.get("at")) and _opt(e.get("by"), str)
                        and _opt(e.get("fp"), str)
                        and (e.get("ack") is None or (isinstance(e.get("ack"), list)
                                                      and all(isinstance(a, str) for a in e["ack"])))):
                    good[step] = e
                else:
                    bad.append((f"{k}.{step}", json.dumps(e, ensure_ascii=False, default=str)[:120]))
            out[k] = good
            continue
        chk = CHECKS.get(k)
        if chk is None:
            out[k] = v
            continue
        try:
            ok = isinstance(v, dict) and bool(chk(v))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            out[k] = v
        else:
            bad.append((k, json.dumps(v, ensure_ascii=False, default=str)[:120]))
    return out, bad


def error_view(why):
    """Fallback checklist view when evaluation throws an unexpected error."""
    rows = [_row(k, "unk", f"Studio error evaluating step: {why}") for k in ORDER]
    return {"header": {"ok": False, "kind": "error", "text": "Checklist evaluation error (see System logs)", "raw": why},
            "rows": rows, "next": None, "all_done": False, "skipped_n": 0, "marked_n": 0, "frozen": False}


# ---------------- Main Service ----------------

class Onboarding:
    def __init__(self, state_dir, log, clock=time.monotonic, wall=time.time):
        self.state_dir, self.log = state_dir, log
        self.clock, self.wall = clock, wall
        self._lk = threading.Lock()
        self._save_lk = threading.Lock()      # Serialize disk writes across threads
        self.rec = {}
        self._dirty = False
        self._view = None
        self._stand_since = None
        self._walk_acc = 0.0
        self._walk_last = None
        self._studio = {"ids": set(), "time": None}
        self._studio_at = -1e9
        self._studio_mtime = None             # servo-calib directory mtime cache
        self._eval_err = None                 # Deduplicate repeated evaluation errors
        self._servo = None                    # Last seen ServoSession
        self.commit_service = None            # Wired by app.py
        self.battery_shutdown = None          # robotd [safety] battery_empty_shutdown: True / False / None
        self._cfg_at = -1e9
        self.robotd_configs = ROBOTD_CONFIGS
        self.cpu_hot = False                  # Board temperature hysteresis flag
        self.board = None                     # Local sysfs thermal fallback
        self._board_ok = os.path.isdir(THERMAL_ROOT)
        self.load()

    # ---- Persistence ----

    def path(self):
        return os.path.join(self.state_dir or ".", ONBOARD_FILE)

    def load(self):
        """Load onboarding.json. Corrupt files are renamed to .bad and treated as empty."""
        p = self.path()
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"Top-level JSON is not an object ({type(data).__name__})")
        except FileNotFoundError:
            self.rec = {}
            return
        except (OSError, ValueError) as e:
            bad = p + ".bad"
            try:
                os.replace(p, bad)
            except OSError:
                bad = None
            self.log.warn("Could not read onboarding checklist; starting fresh", cat="System", step=p, raw=f"{type(e).__name__}: {e}",
                          hint=(f"Corrupt file moved to {bad}" if bad else "Failed to rename corrupt file; check state directory permissions"))
            self.rec = {}
            return
        self.rec, bad = clean_records(data)
        for k, raw in bad:
            self.log.warn(f"Invalid onboarding record '{k}'; ignoring entry", cat="System", step=p, raw=raw,
                          hint="Likely edited manually or from an older version; step will be re-evaluated")

    def save(self):
        """Write atomically (temp file then rename). Runs in worker thread; logs on error."""
        with self._save_lk:
            return self._save_locked()

    def _save_locked(self):
        with self._lk:
            if not self._dirty:
                return False
            data = dict(self.rec, v=1)
            self._dirty = False
        d = self.state_dir or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".onboarding-", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path())
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            with self._lk:
                self._dirty = True              # Retry on next tick
            self.log.warn("Failed to save onboarding checklist", cat="System", step=self.path(), raw=f"{type(e).__name__}: {e}",
                          hint="state directory full or missing permissions; standing/walking unaffected")
            return False
        return True

    def _put(self, key, value, say=None, cat="System"):
        with self._lk:
            if self.rec.get(key) == value:
                return False
            self.rec[key] = value
            self._dirty = True
        if say:
            self.log.info(say, cat=cat, dedup=False)
        return True

    # ---- External Events ----

    def note_estop(self, r):
        """Record E-Stop test pass (called from pad.py thread; in-memory only). r = {ms, mac, name, build}."""
        try:
            rec = {"at": self.wall(), "ms": r.get("ms"), "mac": (str(r["mac"]).upper() if r.get("mac") else None),
                   "name": r.get("name"), "build": r.get("build")}
            self._put("estop", rec, say=f"Onboarding: recorded E-Stop test ({rec['name'] or rec['mac'] or 'Gamepad'}, robotd {rec['build']})",
                      cat="Safety")
            return True
        except Exception:  # noqa: BLE001
            return False

    def note_imu_check(self, c):
        """Record latest standing IMU check (called from imu.py)."""
        rec = {"at": self.wall(), "source": c.get("source"), "mount_fp": mount_fp(c.get("source"), c.get("quat")),
               "diff_deg": c.get("diff_deg"), "tilt_deg": c.get("tilt_deg"), "ok": bool(c.get("ok")), "by": c.get("by")}
        if rec["mount_fp"] is None:
            return False
        return self._put("imu_check", rec, say=f"Onboarding: recorded IMU check (diff {rec['diff_deg']} deg, {'pass' if rec['ok'] else 'out of tolerance'})",
                         cat="IMU") or True

    def confirm_servo(self, rd, by=None):
        """Verify robotd's stored servo zero offsets and record joint fingerprint."""
        calib = fresh_calib(rd)
        js = _joints(calib)
        if not js:
            return False, "Calibration file not yet received from robotd; wait a few seconds"
        if (calib or {}).get("uncalibrated_joints"):
            return False, "robotd reports uncalibrated joints; calibrate on Servo page first"
        off = [j for j in js if type(j.get("zero_raw")) is not int or not 0 <= j["zero_raw"] <= 4095
               or j.get("calibrated") is not True]
        if off:
            return False, f"{len(off)} servos have invalid or uncalibrated zero_raw; check on Servo page"
        if len(js) != len(SERVO_IDS) or {j.get("id") for j in js} != set(SERVO_IDS):
            return False, "Servo IDs in robotd calibration do not match the 15 official joints"
        fp = joints_fp(calib)
        self._put("confirm_servo", {"at": self.wall(), "fp": fp, "by": by},
                  say=f"Onboarding: verified and kept existing 15 servo zero positions ({by or 'web'})", cat="Calibration")
        return True, None

    def mark(self, step, kind, rd, pad=None, servo=None, by=None, confirmed=False):
        """Mark step as 'done' or 'skip' (mutually exclusive). Requires confirmed=True when risky/frozen."""
        if step not in ORDER:
            return False, f"Unknown step: {step}"
        if kind not in KINDS:
            return False, f"Unknown mark kind: {kind}"
        if servo is not None:
            self._servo = servo
        inp = self._inputs(rd, pad, servo, self.clock())
        if inp["bench"]:
            return False, "Bench mode: checklist is disabled"
        with self._lk:
            rec = dict(self.rec)
        row = next(r for r in evaluate(inp, rec)["rows"] if r["key"] == step)
        if row.get("marked") == kind:
            return True, "Already skipped" if kind == "skip" else "Already marked done"
        if row.get("marked"):
            return False, "Step is already skipped: click Undo Skip first" if row["marked"] == "skip" else "Step is already marked done: click Undo first"
        if row["state"] == "ok":
            return False, "Step is already completed (recorded by Studio)"
        offer = row.get("mark") or {}
        ask = (offer.get(kind) or {}).get("ask")
        if inp["servo_session"] and not ask:
            ask = fill(FROZEN_ASK, kind)             # Checklist frozen during servo session
        if ask and confirmed is not True:
            return False, f"Confirmation required: {ask}"
        bound, fp = step_fp(step, inp)
        entry = {"at": self.wall(), "by": by, "fp": fp if bound else None}
        if offer.get("ack"):
            entry["ack"] = list(offer["ack"])
        with self._lk:
            mine = dict(self.rec.get(KINDS[kind]) or {})
            theirs = dict(self.rec.get(KINDS[OTHER[kind]]) or {})
        mine[step] = entry
        word = "skipped" if kind == "skip" else "marked done"
        self._put(KINDS[kind], mine, cat="Calibration",
                  say=f"Onboarding: {TITLES[step]} {word} ({by or 'web'})" + (f"; confirmed: {ask}" if ask else ""))
        if theirs.pop(step, None) is not None:
            self._put(KINDS[OTHER[kind]], theirs)
        return True, None

    def skip(self, step, rd, pad=None, servo=None, by=None, confirmed=False):
        return self.mark(step, "skip", rd, pad, servo, by, confirmed)

    def mark_done(self, step, rd, pad=None, servo=None, by=None, confirmed=False):
        return self.mark(step, "done", rd, pad, servo, by, confirmed)

    def unmark(self, step, kind, by=None, servo=None, confirmed=False):
        """Remove a 'done' or 'skip' mark so the step is evaluated live again."""
        if step not in ORDER:
            return False, f"Unknown step: {step}"
        if kind not in KINDS:
            return False, f"Unknown mark kind: {kind}"
        sv = servo if servo is not None else self._servo
        if sv is not None and getattr(sv, "state", None) in ("open", "opening") and confirmed is not True:
            return False, f"Confirmation required: {FROZEN_UNDO}"
        with self._lk:
            mine = dict(self.rec.get(KINDS[kind]) or {})
        if step not in mine:
            return True, "Was not skipped" if kind == "skip" else "Was not marked done"
        mine.pop(step)
        word = "unskipped" if kind == "skip" else "unmarked done"
        self._put(KINDS[kind], mine, say=f"Onboarding: {TITLES[step]} {word} ({by or 'web'})", cat="Calibration")
        return True, None

    def unskip(self, step, by=None, servo=None, confirmed=False):
        return self.unmark(step, "skip", by, servo, confirmed)

    def servo_mark_record(self):
        with self._lk:
            for kind, key in KINDS.items():
                row = (self.rec.get(key) or {}).get("servo")
                if isinstance(row, dict):
                    return kind, row.get("at")
        return None

    def user_mark(self, step):
        """Return active user mark ('done' / 'skip' / None) from cached evaluation."""
        rows = (self._view or {}).get("rows") or []
        r = next((x for x in rows if isinstance(x, dict) and x.get("key") == step), None)
        return r.get("marked") if r else None

    # ---- Background File I/O ----

    def need_board(self, rd):
        """Whether Studio should read /sys/class/thermal locally (when robotd omits cpu_temp_c)."""
        ha = rd.health.age()
        h = rd.health.value if ha is not None and ha <= MAX_AGE["health"] else None
        return self._board_ok and not (isinstance(h, dict) and _num(h.get("cpu_temp_c")))

    def io(self, board=False, now=None):
        """Worker-thread I/O: scan servo-calib and optionally read board thermal/config."""
        now = self.clock() if now is None else now
        try:
            d = os.path.join(self.state_dir or ".", "servo-calib")
            try:
                mt = os.stat(d).st_mtime
            except OSError:
                mt = None
            if mt != self._studio_mtime or now - self._studio_at >= STUDIO_SCAN_S:
                self._studio = studio_servo_record(self.state_dir)
                self._studio_mtime, self._studio_at = mt, now
        except Exception:  # noqa: BLE001
            pass
        if board:
            try:
                self.board = read_board()
            except Exception:  # noqa: BLE001
                self.board = None
        else:
            self.board = None
        if now - self._cfg_at >= ROBOTD_CFG_S:
            self._cfg_at = now
            self.battery_shutdown = read_battery_shutdown(self.robotd_configs)

    # ---- 0.5 Hz Tick ----

    def _inputs(self, rd, pad, servo, now):
        bus = rd.bus.value if isinstance(rd.bus.value, dict) else None
        calib = fresh_calib(rd)
        pv = None
        if pad is not None:
            try:
                pv = pad.onboard_view()
            except Exception as e:  # noqa: BLE001
                pv = {"known": False, "error": f"{type(e).__name__}: {e}", "pads": []}
        return {"bench": getattr(rd, "addr", None) == "none", "present": rd.present, "bus": bus, "calib": calib,
                "servo_session": servo is not None and getattr(servo, "state", None) in ("open", "opening"),
                "pad": pv, "studio_servo": self._studio, "fp": full_fp(bus, calib),
                "commit": self._commit_view(rd, servo), "starting_s": starting_secs(rd, bus)}

    def _commit_view(self, rd, servo):
        """Fetch Step 3 readiness from CommitService without letting errors break the checklist."""
        svc = self.commit_service
        if svc is None:
            return None
        try:
            return svc.view(rd, servo)
        except Exception as e:  # noqa: BLE001
            return {"ready": False, "items": [], "job": None, "error": f"{type(e).__name__}: {e}"}

    def tick(self, rd, pad=None, servo=None, now=None):
        """Evaluate checklist and update in-memory state; returns True if dirty (caller saves in thread)."""
        now = self.clock() if now is None else now
        if servo is not None:
            self._servo = servo
        try:
            self._board_tick(rd)
        except Exception as e:  # noqa: BLE001
            self.log.error("Error updating board thermal hysteresis", cat="System", raw=f"{type(e).__name__}: {e}", exc=True)
        try:
            inp = self._inputs(rd, pad, servo, now)
            if not inp["servo_session"]:
                self._track(rd, inp, now)
                self._bind_marks(inp)
            with self._lk:
                rec = dict(self.rec)
            v = evaluate(inp, rec)
            if not inp["servo_session"]:
                self._void_marks(v)
            if inp["servo_session"] and self._view is not None and self._view["header"].get("kind") != "error":
                # Freeze checklist display while servo session owns the bus
                v = dict(self._view, header=v["header"], frozen=True,
                         frozen_ask={"done": fill(FROZEN_ASK, "done"), "skip": fill(FROZEN_ASK, "skip"), "undo": FROZEN_UNDO})
            self._eval_err = None
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {e}"[:200]
            if why != self._eval_err:
                self._eval_err = why
                self.log.error("Onboarding evaluation error; showing fallback '?'", cat="System", raw=why, exc=True,
                               hint="This is a Studio bug; export a diagnostic bundle")
            v = error_view(why)
        self._view = v
        with self._lk:
            return self._dirty

    def _void_marks(self, v):
        """Invalidate unacknowledged marks when a step actually detects a problem (found)."""
        for kind, rk in KINDS.items():
            gone = [r for r in v["rows"] if kind in (r.get("void") or ())]
            if not gone:
                continue
            with self._lk:
                d = dict(self.rec.get(rk) or {})
            for r in gone:
                d.pop(r["key"], None)
            word = "skip" if kind == "skip" else "'Done' mark"
            self._put(rk, d, cat="Calibration",
                      say="Onboarding: " + ", ".join(f"{r['title']} {word} invalidated ({fill(r['found'], kind)})" for r in gone))

    def _bind_marks(self, inp):
        """Bind fingerprint to marks created while fingerprint was unavailable."""
        for kind, rk in KINDS.items():
            with self._lk:
                d = dict(self.rec.get(rk) or {}) if isinstance(self.rec.get(rk), dict) else {}
            named = []
            for step, e in list(d.items()):
                if step not in BOUND or not isinstance(e, dict) or e.get("fp") is not None:
                    continue
                _bound, fp = step_fp(step, inp)
                if fp is not None:
                    d[step] = dict(e, fp=fp)
                    named.append(TITLES[step])
            if named:
                word = "skip" if kind == "skip" else "'Done' mark"
                self._put(rk, d, cat="Calibration", say=f"Onboarding: bound fingerprint to {', '.join(named)} {word}")

    def _track(self, rd, inp, now):
        """Track standing, walking, Studio servo-calib sync, and paired gamepads."""
        bus = inp["bus"] or {}
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        pa, sa = rd.policies.age(), rd.state.age()
        pol = rd.policies.value if pa is not None and pa <= MAX_AGE["policies"] else None
        st = rd.state.value if sa is not None and sa <= MAX_AGE["state"] else None
        pose = posture(rd.present, inp["bus"], pol, st)
        fp = inp["fp"]
        if pose in STAND and native.get("torque_state_confirmed") is True:
            if self._stand_since is None:
                self._stand_since = now
            elif now - self._stand_since >= STOOD_S and fp is not None:
                old = self.rec.get("stood") if isinstance(self.rec.get("stood"), dict) else None
                if old is None or old.get("fp") != fp:
                    self._put("stood", {"at": self.wall(), "fp": fp}, say="Onboarding: recorded 'stood up'", cat="Gamepad")
        else:
            self._stand_since = None
        if pose == "walking":
            if self._walk_last is not None:
                self._walk_acc += min(now - self._walk_last, 1.0)
            self._walk_last = now
            old = self.rec.get("walked") if isinstance(self.rec.get("walked"), dict) else None
            if self._walk_acc >= WALKED_S and fp is not None and (old is None or old.get("fp") != fp):
                self._walk_acc = 0.0
                self._put("walked", {"at": self.wall(), "fp": fp}, say="Onboarding: recorded 'walked'", cat="Gamepad")
        else:
            self._walk_last = None
        stu = inp["studio_servo"] or {}
        js = _joints(inp["calib"])
        jfp = joints_fp(inp["calib"])
        if set(SERVO_IDS) <= set(stu.get("ids") or ()) and js and jfp and all(j.get("zero_raw") == 2048 for j in js):
            ss = self.rec.get("servo_studio") if isinstance(self.rec.get("servo_studio"), dict) else None
            if ss is None or ss.get("src") != stu.get("time"):
                self._put("servo_studio", {"at": self.wall(), "fp": jfp, "src": stu.get("time")},
                          say="Onboarding: Servo page calibration matches robotd file (all 15 at 2048)", cat="Calibration")
        pv = inp["pad"] or {}
        seen = dict(self.rec.get("pads_seen") or {}) if isinstance(self.rec.get("pads_seen"), dict) else {}
        changed = False
        for p in pv.get("pads") or []:
            if isinstance(p, dict) and p.get("paired") and p.get("mac") and seen.get(str(p["mac"]).upper()) != p.get("name"):
                seen[str(p["mac"]).upper()] = p.get("name") or p.get("mac")
                changed = True
        if changed:
            self._put("pads_seen", seen)

    def _board_tick(self, rd):
        """Board thermal hysteresis (>=80 C sets, <77 C clears), with local sysfs fallback."""
        ha = rd.health.age()
        h = rd.health.value if ha is not None and ha <= MAX_AGE["health"] else None
        c = (h or {}).get("cpu_temp_c") if isinstance(h, dict) else None
        if not _num(c):
            c = (self.board or {}).get("c") if isinstance(self.board, dict) else None
        if not _num(c):
            self.cpu_hot = False
        elif c >= CPU_WARN_C:
            self.cpu_hot = True
        elif c < CPU_CLEAR_C:
            self.cpu_hot = False

    def view(self):
        return self._view
