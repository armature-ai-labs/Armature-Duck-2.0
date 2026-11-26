"""Write calibration to robotd: commit servo zero offsets, directions, and IMU orientation to robotd's calibration file.

The Servo page midpoint calibration only writes each servo's EEPROM, servo directions are stored in Studio's
servo-directions.json, and IMU orientation is computed in the UI. robotd reads
/etc/robot/feetech-ft5/hd1910-calibration.toml; without it in motion mode, robotd stalls at waiting_for_bus.

This module handles three responsibilities:
- Track Studio-side progress (state_dir/servo-centered.json, imu-pending.json);
- Compute readiness and build/render/validate the calibration TOML (shared verbatim with deploy/microduck-helper);
- Handle calib.op: preview (dry_run) and commit (calls root helper to write and restart robotd).
"""
import json
import math
import os
import tempfile
import threading
import time
import tomllib
import uuid

JOINT_ORDER = (
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee", "left_ankle",
    "neck_pitch", "head_pitch", "head_yaw", "head_roll", "mouth",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch", "right_knee", "right_ankle",
)
JOINT_ID = {
    "left_hip_yaw": 20, "left_hip_roll": 21, "left_hip_pitch": 22, "left_knee": 23, "left_ankle": 24,
    "neck_pitch": 30, "head_pitch": 31, "head_yaw": 32, "head_roll": 33, "mouth": 34,
    "right_hip_yaw": 10, "right_hip_roll": 11, "right_hip_pitch": 12, "right_knee": 13, "right_ankle": 14,
}
JOINT_IDS = JOINT_ID       # Legacy alias used by other Studio modules / tests
# Official HOME pose from robotd (deployment.rs DEFAULT_POSITION), in JOINT_ORDER
HOME_RAD = {
    "left_hip_yaw": 0.0, "left_hip_roll": -0.0873, "left_hip_pitch": -0.4579, "left_knee": -0.0049,
    "left_ankle": 0.453, "neck_pitch": 0.3491, "head_pitch": 0.3491, "head_yaw": 0.0, "head_roll": 0.0,
    "mouth": 0.0, "right_hip_yaw": 0.0, "right_hip_roll": 0.0873, "right_hip_pitch": 0.4579,
    "right_knee": 0.0049, "right_ankle": -0.453,
}
# Standard joint limits (deg) used on real ducks: left/right hip yaw are mirrored
LIMITS_DEG = {
    "left_hip_yaw": (-25.0, 30.0), "left_hip_roll": (-22.0, 22.0), "left_hip_pitch": (-90.0, 90.0),
    "left_knee": (-90.0, 90.0), "left_ankle": (-70.0, 70.0),
    "neck_pitch": (-90.0, 70.0), "head_pitch": (-90.0, 90.0), "head_yaw": (-170.0, 170.0),
    "head_roll": (-20.0, 20.0), "mouth": (-5.0, 30.0),
    "right_hip_yaw": (-30.0, 25.0), "right_hip_roll": (-22.0, 22.0), "right_hip_pitch": (-90.0, 90.0),
    "right_knee": (-90.0, 90.0), "right_ankle": (-70.0, 70.0),
}
IMU_SOURCES = ("bus", "bno08x", "lsm6dsv16x")
CHIP_CN = {"bus": "Bus IMU (ID 200)", "bno08x": "BNO085 (I2C)", "lsm6dsv16x": "LSM6DSV16X (I2C)"}
DEFAULT_BUS_QUAT = [math.sqrt(0.5), 0.0, math.sqrt(0.5), 0.0]     # SflpDecoder::DEFAULT_MOUNT
# Fallback default IMU mounts when step 2 was skipped/completed without IMU calibration
DEFAULT_MOUNTS = {
    "bno08x": [0.5, 0.5, 0.5, 0.5],                                  # BNO08X_DEFAULT_MOUNT
    # Recommended replica mount: module x down, z forward
    "lsm6dsv16x": [0.7071067811865476, 0.0, 0.7071067811865476, 0.0],
}
DEFAULT_HOW = {"bus": "Factory mount", "bno08x": "Default fork mount", "lsm6dsv16x": "Replica mount (x down, z forward)"}
TOP_KEYS = ("motion_enabled", "imu_mount_verified", "imu_mount_quat", "imu_bus_protocol", "imu_bus_profile", "current_ma_per_count", "units_verified")
TAU = 2.0 * math.pi

SERVO_CENTERED = "servo-centered.json"
IMU_PENDING = "imu-pending.json"
COMMIT_LAST = "commit-last.json"
COMMIT_WAIT_S = 180.0


class CalibError(Exception):
    """Calibration failed robotd validation; message is user-facing."""


class Refused(Exception):
    """Commit cannot proceed (prerequisites missing, calibration unreadable, or helper absent)."""


# ---------------- Build / Validate TOML (mirrored in helper) ----------------

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _quat(q, what):
    if not isinstance(q, (list, tuple)) or len(q) != 4 or not all(_is_num(x) for x in q):
        raise CalibError(f"{what} must be 4 finite numbers [w, x, y, z]")
    return [float(x) for x in q]


def normalize_quat(q):
    """Normalize quaternion; reject zero-vector, NaN, and Inf."""
    v = _quat(q, "Quaternion")
    n = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(n) or n <= 1e-12:
        raise CalibError("Quaternion is zero; cannot normalize")
    return [x / n for x in v]


def _num_text(v):
    return repr(float(v))


def _bool_text(v):
    return "true" if v else "false"


def build_calibration(current, joints, imu, joint_mode="recorded"):
    """Build a complete calibration dict from current robotd calibration + new joint directions and IMU mount."""
    if not isinstance(current, dict):
        raise CalibError("Current robotd calibration is not an object")
    rows = current.get("joints")
    if not isinstance(rows, list):
        raise CalibError("Current robotd calibration is missing joints")
    by_name = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise CalibError("A joint entry in robotd calibration is missing 'name'")
        name = row["name"]
        if name not in JOINT_ID:
            raise CalibError(f"Unknown joint in robotd calibration: {name}")
        if name in by_name:
            raise CalibError(f"Duplicate joint in robotd calibration: {name}")
        by_name[name] = row
    if set(by_name) != set(JOINT_ORDER):
        missing = [n for n in JOINT_ORDER if n not in by_name]
        raise CalibError("robotd calibration does not match the 15 official joints" + (f" (missing {', '.join(missing)})" if missing else ""))

    if not isinstance(joints, list) or len(joints) != 15:
        raise CalibError("joints must contain exactly 15 {id, direction} entries")
    dirs = {}
    centered_ids = set()
    for row in joints:
        if not isinstance(row, dict) or not _is_int(row.get("id")) or row.get("direction") not in (-1, 1):
            raise CalibError("Each joint entry must be {id: int, direction: 1 or -1}")
        if row["id"] in dirs:
            raise CalibError(f"Duplicate joint id {row['id']}")
        dirs[row["id"]] = row["direction"]
        if joint_mode == "recorded" and row.get("centered") is True:
            centered_ids.add(row["id"])
    file_ids = {row.get("id") for row in by_name.values()}
    if set(dirs) != file_ids:
        raise CalibError(f"Provided 15 joint IDs ({sorted(dirs)}) do not match robotd file ({sorted(file_ids, key=str)})")

    if joint_mode not in ("recorded", "keep", "imu_only"):
        raise CalibError("Unknown joint calibration write mode")
    out_joints = []
    for name in JOINT_ORDER:
        src = by_name[name]
        jid = src.get("id")
        if not _is_int(jid):
            raise CalibError(f"{name} id is not an integer")
        row = dict(src)
        if joint_mode == "recorded":
            row["direction"] = dirs[jid]
            if jid in centered_ids:
                row["zero_raw"] = 2048
        if joint_mode != "imu_only":
            if row.get("min_rad") is None and row.get("max_rad") is None and row.get("calibrated") is False:
                lo, hi = LIMITS_DEG[name]
                row.update(min_rad=math.radians(lo), max_rad=math.radians(hi))
            row["calibrated"] = True
        out_joints.append(row)

    out = {
        "motion_enabled": True,
        "imu_mount_verified": bool(current.get("imu_mount_verified", False)),
        "imu_mount_quat": DEFAULT_BUS_QUAT,
        "current_ma_per_count": float(current.get("current_ma_per_count") or 0.0),
        "units_verified": bool(current.get("units_verified", False)),
        "imu_mounts": {},
        "joints": out_joints,
    }
    raw_bus = current.get("imu_mount_quat")
    if raw_bus is not None:
        out["imu_mount_quat"] = _quat(raw_bus, "imu_mount_quat")
    if "imu_bus_protocol" in current:
        protocol = current["imu_bus_protocol"]
        if protocol not in ("feetech", "dxl2"):
            raise CalibError("imu_bus_protocol must be 'feetech' or 'dxl2'")
        out["imu_bus_protocol"] = protocol
    if "imu_bus_profile" in current:
        if current["imu_bus_profile"] not in ("legacy", "qinmo"):
            raise CalibError("imu_bus_profile must be 'legacy' or 'qinmo'")
        out["imu_bus_profile"] = current["imu_bus_profile"]

    raw_mounts = current.get("imu_mounts")
    if isinstance(raw_mounts, dict):
        for kind in ("bno08x", "lsm6dsv16x"):
            mount = raw_mounts.get(kind)
            if mount is None:
                continue
            if not isinstance(mount, dict):
                raise CalibError(f"imu_mounts.{kind} must be an object")
            out["imu_mounts"][kind] = {"quat": _quat(mount.get("quat"), f"imu_mounts.{kind}.quat"),
                                       "verified": bool(mount.get("verified", False))}

    if joint_mode == "imu_only":
        out = {**current, "imu_mounts": dict(current.get("imu_mounts") or {})}
    if not isinstance(imu, dict):
        raise CalibError("imu must be an object")
    source = imu.get("source")
    if source not in IMU_SOURCES:
        raise CalibError(f"imu.source must be bus / bno08x / lsm6dsv16x, got {source!r}")
    quat = normalize_quat(imu.get("quat"))
    if source == "bus":
        out["imu_mount_quat"], out["imu_mount_verified"] = quat, True
    else:
        out["imu_mounts"][source] = {"quat": quat, "verified": True}
    return out


def _raw_position(zero_raw, radians, direction):
    return float(zero_raw) + float(radians) * float(direction) * 4096.0 / TAU


def validate_calibration(cal, source=None):
    """Validate against robotd Calibration::validate rules; raises CalibError with details."""
    if not isinstance(cal, dict):
        raise CalibError("Calibration must be an object")
    allowed = set(TOP_KEYS) | {"imu_mounts", "joints"}
    extra = sorted(set(cal) - allowed)
    if extra:
        raise CalibError(f"Unknown top-level keys: {extra} (robotd will reject the file)")

    if not isinstance(cal.get("motion_enabled"), bool):
        raise CalibError("motion_enabled must be true or false")
    if not isinstance(cal.get("imu_mount_verified"), bool):
        raise CalibError("imu_mount_verified must be true or false")
    if "imu_bus_protocol" in cal and cal["imu_bus_protocol"] not in ("feetech", "dxl2"):
        raise CalibError("imu_bus_protocol must be 'feetech' or 'dxl2'")
    if "imu_bus_profile" in cal and cal["imu_bus_profile"] not in ("legacy", "qinmo"):
        raise CalibError("imu_bus_profile must be 'legacy' or 'qinmo'")
    if cal.get("imu_bus_profile") == "qinmo" and cal.get("imu_bus_protocol", "feetech") != "feetech":
        raise CalibError("Qinmo profile only supports Feetech protocol")
    if not _is_num(cal.get("current_ma_per_count")):
        raise CalibError("current_ma_per_count must be a finite number")
    if not isinstance(cal.get("units_verified"), bool):
        raise CalibError("units_verified must be true or false")

    bus_q = _quat(cal.get("imu_mount_quat"), "imu_mount_quat")
    mounts = cal.get("imu_mounts")
    mounts = mounts if isinstance(mounts, dict) else {}
    if set(mounts) - {"bno08x", "lsm6dsv16x"}:
        raise CalibError(f"Unknown IMU sources in imu_mounts: {sorted(set(mounts) - {'bno08x', 'lsm6dsv16x'})}")
    checked = [("Bus IMU", bus_q)]
    for kind in ("bno08x", "lsm6dsv16x"):
        mount = mounts.get(kind)
        if mount is None:
            continue
        if not isinstance(mount, dict):
            raise CalibError(f"imu_mounts.{kind} must be an object")
        if not isinstance(mount.get("verified"), bool):
            raise CalibError(f"imu_mounts.{kind}.verified must be true or false")
        checked.append((CHIP_CN[kind], _quat(mount.get("quat"), f"imu_mounts.{kind}.quat")))
    for label, q in checked:
        norm = sum(x * x for x in q)
        if not math.isfinite(norm) or abs(norm - 1.0) > 1e-6:
            raise CalibError(f"{label} mount quaternion is not unit length (error {norm - 1.0:+.2e}, max 1e-6)")

    if cal.get("units_verified") and float(cal["current_ma_per_count"]) <= 0.0:
        raise CalibError("current_ma_per_count must be positive when units_verified = true")

    joints = cal.get("joints")
    if not isinstance(joints, list) or len(joints) != 15:
        raise CalibError("Exactly 15 joints (in official order) are required")
    seen = set()
    for i, name in enumerate(JOINT_ORDER):
        j = joints[i]
        if not isinstance(j, dict):
            raise CalibError(f"{name} (joint #{i + 1}) must be an object")
        if j.get("name") != name:
            raise CalibError(f"Joint #{i + 1} must be {name}, got {j.get('name')!r} (official order required)")
        jid = j.get("id")
        if not _is_int(jid) or jid >= 254 or jid == 200:
            raise CalibError(f"{name}: id must be < 254 and not 200 (IMU board), got {jid!r}")
        if jid in seen:
            raise CalibError(f"{name}: duplicate id {jid}")
        seen.add(jid)
        zero = j.get("zero_raw")
        if not _is_int(zero) or not 0 <= zero <= 4095:
            raise CalibError(f"{name}: zero_raw must be 0-4095, got {zero!r}")
        direction = j.get("direction")
        if direction not in (-1, 1):
            raise CalibError(f"{name}: direction must be 1 or -1, got {direction!r}")
        if not isinstance(j.get("calibrated"), bool):
            raise CalibError(f"{name}: calibrated must be true or false")
        lo, hi = j.get("min_rad"), j.get("max_rad")
        if lo is None and hi is None:
            if j.get("calibrated"):
                raise CalibError(f"{name}: limits are required when calibrated = true")
            continue
        if not (_is_num(lo) and _is_num(hi)):
            raise CalibError(f"{name}: both min_rad and max_rad must be finite numbers")
        lo, hi = float(lo), float(hi)
        if not lo < hi:
            raise CalibError(f"{name}: min_rad must be < max_rad ({lo:g} / {hi:g})")
        for edge, value in (("min", lo), ("max", hi)):
            raw = _raw_position(zero, value, direction)
            if not math.isfinite(raw) or not 0.0 <= round(raw) <= 4095.0:
                raise CalibError(f"{name}: {edge}_rad maps to raw {raw:.1f}, outside 0-4095")
        home = HOME_RAD[name]
        if not lo <= home <= hi:
            raise CalibError(f"{name}: limits {lo:.4f}..{hi:.4f} do not include official HOME {home:.4f}")

    if cal.get("motion_enabled"):
        if any(not j.get("calibrated") or j.get("min_rad") is None or j.get("max_rad") is None for j in joints):
            raise CalibError("All 15 joints must be calibrated with limits when motion_enabled = true")
        if source in ("bno08x", "lsm6dsv16x"):
            mount = mounts.get(source) or {}
            verified = bool(mount.get("verified")) if isinstance(mount, dict) else False
            label = CHIP_CN[source]
        elif source == "bus":
            verified, label = bool(cal.get("imu_mount_verified")), CHIP_CN["bus"]
        else:
            verified = bool(cal.get("imu_mount_verified")) or any(
                isinstance(m, dict) and m.get("verified") for m in mounts.values())
            label = "active"
        if not verified:
            raise CalibError(f"Active {label} IMU mount must be verified when motion_enabled = true")
    return cal


def render_calibration(cal):
    """Render calibration TOML: top-level keys -> [imu_mounts.*] -> [[joints]]."""
    lines = [
        f"motion_enabled = {_bool_text(cal.get('motion_enabled'))}",
        f"imu_mount_verified = {_bool_text(cal.get('imu_mount_verified'))}",
        "imu_mount_quat = [" + ", ".join(_num_text(x) for x in cal.get("imu_mount_quat") or []) + "]",
        f"current_ma_per_count = {_num_text(cal.get('current_ma_per_count') or 0.0)}",
        f"units_verified = {_bool_text(cal.get('units_verified'))}",
    ]
    if "imu_bus_protocol" in cal:
        if cal["imu_bus_protocol"] not in ("feetech", "dxl2"):
            raise CalibError("imu_bus_protocol must be 'feetech' or 'dxl2'")
        lines.append(f'imu_bus_protocol = "{cal["imu_bus_protocol"]}"')
    if "imu_bus_profile" in cal:
        if cal["imu_bus_profile"] not in ("legacy", "qinmo"):
            raise CalibError("imu_bus_profile must be 'legacy' or 'qinmo'")
        lines.append(f'imu_bus_profile = "{cal["imu_bus_profile"]}"')
    mounts = cal.get("imu_mounts") or {}
    for kind in ("bno08x", "lsm6dsv16x"):
        mount = mounts.get(kind)
        if mount is None:
            continue
        lines += ["", f"[imu_mounts.{kind}]",
                  "quat = [" + ", ".join(_num_text(x) for x in mount.get("quat") or []) + "]",
                  f"verified = {_bool_text(mount.get('verified'))}"]
    for j in cal.get("joints") or []:
        lines += ["", "[[joints]]",
                  f'name = "{j.get("name")}"',
                  f'id = {int(j.get("id"))}',
                  f'zero_raw = {int(j.get("zero_raw"))}',
                  f'direction = {int(j.get("direction"))}',
                  f'calibrated = {_bool_text(j.get("calibrated"))}']
        if j.get("min_rad") is not None:
            lines.append(f'min_rad = {_num_text(j.get("min_rad"))}')
        if j.get("max_rad") is not None:
            lines.append(f'max_rad = {_num_text(j.get("max_rad"))}')
    return "\n".join(lines) + "\n"


def normalized_calibration(cal):
    """For round-trip comparison: empty [imu_mounts] is omitted when writing TOML."""
    out = dict(cal)
    out["imu_mounts"] = dict(cal.get("imu_mounts") or {})
    return out


def same_calibration(a, b):
    """Strict structural comparison after TOML round-trip."""
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(same_calibration(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_calibration(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and float(a) == float(b)
    return a == b


# ---------------- Studio State Files ----------------

def _path(state_dir, name):
    return os.path.join(state_dir or ".", name)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_centered(state_dir, ids, by, at=None):
    """Assign a fresh event ID on each midpoint calibration so prior records are not re-applied."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    p = _path(state_dir, SERVO_CENTERED)
    data = _read_json(p) or {}
    now = time.time() if at is None else at
    for jid in ids:
        data[str(jid)] = {"at": now, "by": by or "Servo Page", "event": uuid.uuid4().hex}
    _write_json(p, data)
    return data


def load_centered(state_dir):
    data = _read_json(_path(state_dir, SERVO_CENTERED)) or {}
    out = {}
    for key, row in data.items():
        try:
            jid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), (int, float)):
            out[jid] = row
    return out


def save_pending(state_dir, source, quat, quality=None, by=None, at=None, *, imu_bus=None):
    """Persist verified standing IMU calibration so it survives Studio restarts."""
    if source not in IMU_SOURCES:
        raise Refused(f"Unknown IMU source: {source!r}")
    row = {"at": time.time() if at is None else at, "by": by, "source": source,
           "quat": normalize_quat(quat), "quality": quality if isinstance(quality, dict) else {}}
    if source == "bus" and imu_bus is not None:
        row["imu_bus"] = imu_bus
    _write_json(_path(state_dir, IMU_PENDING), row)
    return row


def load_pending(state_dir):
    row = _read_json(_path(state_dir, IMU_PENDING))
    if not isinstance(row, dict) or row.get("source") not in IMU_SOURCES:
        return None
    try:
        q = normalize_quat(row.get("quat"))
    except CalibError:
        return None
    out = dict(row)
    out["quat"] = q
    return out


def clear_pending(state_dir):
    try:
        os.unlink(_path(state_dir, IMU_PENDING))
        return True
    except FileNotFoundError:
        return False


def pending_matches(pending, source, bus):
    """Legacy results match Feetech only; newer results must match the exact bus IMU board."""
    from .imu import bus_imu_of, configured_profile, imu_bus_metadata
    if not pending or pending.get("source") != source:
        return False
    if source != "bus":
        return True
    if imu_bus_metadata(bus)[2]:
        return False
    if "imu_bus" in pending and imu_bus_metadata({"imu_bus": pending["imu_bus"]})[2]:
        return False
    actual, saved = bus_imu_of(bus), pending.get("imu_bus")
    if saved is None:
        return ((actual is None and not configured_profile(bus)) or
                (actual is not None and actual["protocol"] == "feetech" and actual.get("profile", "legacy") == "legacy"))
    if not isinstance(actual, dict) or not isinstance(saved, dict):
        return False
    return (all(actual.get(key) == saved.get(key) for key in ("protocol", "board", "identity"))
            and actual.get("profile", "legacy") == saved.get("profile", "legacy")
            and saved.get("profile", "legacy") in ("legacy", "qinmo"))


def forget_centered(state_dir, ids):
    """Remove specified joint IDs from servo-centered.json when undoing midpoint calibration."""
    p = _path(state_dir, SERVO_CENTERED)
    data = _read_json(p) or {}
    gone = []
    for jid in ids:
        if data.pop(str(int(jid)), None) is not None:
            gone.append(int(jid))
    if gone:
        _write_json(p, data)
    return gone


def _load_directions(state_dir):
    """Load directions explicitly set on the Servo page; empty if absent."""
    try:
        with open(_path(state_dir, "servo-directions.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).isdigit() and v in (1, -1) and not isinstance(v, bool):
                out[int(k)] = int(v)
    return out


def _file_directions(cal):
    """Existing joint directions {id: +/-1} from robotd's calibration file."""
    out = {}
    for j in (cal or {}).get("joints") or []:
        if isinstance(j, dict) and isinstance(j.get("id"), int) and j.get("direction") in (1, -1):
            out[j["id"]] = j["direction"]
    return out


# ---------------- Readiness / Preview / Commit ----------------

class CommitService:
    def __init__(self, robotd, log, state_dir, simulate=False):
        self.rd, self.log, self.state_dir = robotd, log, state_dir
        self.simulate = simulate
        self.centered, self.pending, self.directions = {}, None, {}
        self._mt = {}
        self.file_calib = None
        self._file_calib_at = -1e9
        self.cal_from_file = False
        self.job = None
        self.onboard = None                    # Onboarding checklist (wired by app) for step 1/2 marks
        self._writing = threading.Lock()
        self._reload_at = -1e9
        self.reload(force=True)
        self.job = self._job_from_last_result()

    # ---- Cached state files (view() runs every frame) ----

    def stage_import(self, source, quat, by, imu_bus):
        if not self._writing.acquire(blocking=False):
            raise Refused("Calibration write in progress; wait until it finishes before importing")
        try:
            return save_pending(self.state_dir, source, quat, {"imported": True}, by, imu_bus=imu_bus)
        finally:
            self._writing.release()

    def _sig(self, path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
        except OSError:
            return None

    def reload(self, force=False):
        now = time.monotonic()
        if not force and now - self._reload_at < 1.0:
            return
        self._reload_at = now
        specs = (
            ("centered", SERVO_CENTERED, lambda: load_centered(self.state_dir)),
            ("pending", IMU_PENDING, lambda: load_pending(self.state_dir)),
            ("directions", "servo-directions.json", lambda: _load_directions(self.state_dir)),
        )
        for key, name, fn in specs:
            path = _path(self.state_dir, name)
            sig = self._sig(path)
            if force or self._mt.get(key) != sig:
                self._mt[key] = sig
                if key == "centered":
                    self.centered = fn()
                elif key == "pending":
                    self.pending = fn()
                else:
                    self.directions = fn()

    def _calibration(self, rd=None):
        """Prefer calibration-status after current hello; fall back to helper calib-read (throttled to every 5s)."""
        from .robotd import fresh_calib
        from . import helper
        rd = rd or self.rd
        calib = fresh_calib(rd)
        if isinstance(calib, dict) and isinstance(calib.get("calibration"), dict):
            self.cal_from_file = False
            return calib.get("calibration"), calib
        if isinstance(calib, dict) and isinstance(calib.get("joints"), list):
            self.cal_from_file = False
            return calib, calib
        now = time.monotonic()
        if self.file_calib is None or now - self._file_calib_at >= 5.0:
            self._file_calib_at = now
            self.file_calib = None
            if helper.available():
                ok, _result, detail = helper.calib_read(by="Studio readiness", timeout=10)
                if ok and isinstance(detail, dict) and isinstance(detail.get("calibration"), dict):
                    self.file_calib = {"calibration": detail["calibration"],
                                       "imu_source": detail.get("imu_source"),
                                       "mode": detail.get("mode"), "path": detail.get("path")}
        if self.file_calib is not None:
            self.cal_from_file = True
            return self.file_calib["calibration"], self.file_calib
        self.cal_from_file = False
        return None, (calib if isinstance(calib, dict) else None)

    def _current_bus(self, rd=None):
        rd = rd or self.rd
        return rd.bus.value if isinstance(rd.bus.value, dict) else {}

    def _last_commit(self):
        row = _read_json(_path(self.state_dir, COMMIT_LAST))
        return row if isinstance(row, dict) else None

    def _dirty(self, cal):
        why = []
        if self.pending:
            why.append("New IMU orientation calibration has not been written yet")
        mark, marked_at = self._servo_mark()
        _, _, fresh = self._center_events(mark, marked_at)
        if fresh:
            why.append("Servo zero positions are newer than the last write")
        rows = {j.get("id"): j for j in (cal or {}).get("joints") or [] if isinstance(j, dict)}
        if not mark and rows and any(jid in rows and rows[jid].get("direction") != d for jid, d in self.directions.items()):
            why.append("Servo page directions differ from robotd calibration file")
        return why

    def _user_mark(self, step):
        """Return user's onboarding mark (done / skip / None) for step; None if unavailable."""
        ob = self.onboard
        if ob is None:
            return None
        try:
            return ob.user_mark(step)
        except Exception:  # noqa: BLE001 — readiness runs every frame
            return None

    def _joint_mode(self, mark):
        if self.pending and (self.pending.get("quality") or {}).get("imported") is True:
            return "imu_only"
        return "keep" if mark else "recorded"

    def _center_events(self, mark, marked_at):
        applied = (self._last_commit() or {}).get("center_events") or {}
        events = {str(jid): row["event"] for jid, row in self.centered.items() if isinstance(row.get("event"), str)}
        fresh = {jid for jid, row in self.centered.items() if str(jid) in events
                 and events[str(jid)] != applied.get(str(jid))
                 and (not mark or isinstance(marked_at, (int, float)) and row["at"] > marked_at)}
        return applied, events, fresh

    def _servo_mark(self):
        # A mark takes effect as soon as it is saved, even before the next UI tick.
        get_mark = getattr(self.onboard, "servo_mark_record", None)
        saved = get_mark() if callable(get_mark) else None
        if isinstance(saved, tuple):
            return saved
        return self._user_mark("servo"), None

    def _default_imu(self, source, mount, cal, mark=None):
        """Fallback default IMU mount when step 2 was marked done/skip and IMU is unverified with no pending calibration."""
        if self.pending or source not in IMU_SOURCES or (mount or {}).get("verified") is True:
            return None
        if source == "bus" and (cal or {}).get("imu_bus_profile") == "qinmo":
            return None
        mark = self._user_mark("imu") if mark is None else mark
        if mark not in ("done", "skip"):
            return None
        if source == "bus":
            quat = list(DEFAULT_BUS_QUAT)
            raw = (cal or {}).get("imu_mount_quat") if isinstance(cal, dict) else None
            if raw is not None:
                try:
                    quat = normalize_quat(raw)
                except CalibError:
                    pass
        else:
            quat = list(DEFAULT_MOUNTS[source])
        return {"source": source, "quat": quat, "label": CHIP_CN[source], "how": DEFAULT_HOW[source], "mark": mark}

    def readiness(self, rd=None, servo=None):
        from .estop import STARTING_LONG_S, STARTING_PHASES
        from .imu import bus_protocol_mismatch, mount_of
        from .state import JOINT_CN, STILL_STARTING, starting_secs
        from .startup_status import startup_wait
        self.reload()
        rd = rd or self.rd
        cal, calib = self._calibration(rd)
        cal = cal if isinstance(cal, dict) else {}
        bus = self._current_bus(rd)
        mode = bus.get("mode") if isinstance(bus, dict) else None
        phase = bus.get("phase") if isinstance(bus, dict) else None
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        items = []

        online = rd.present is True and isinstance(bus, dict)
        items.append({"key": "robotd", "label": "robotd", "ok": online,
                      "text": "robotd online" if online else "robotd not running (or still connecting); wait a few seconds or check Logs", "action": None})

        sample = bus.get("last_sample") if isinstance(bus.get("last_sample"), dict) else {}
        servos = sample.get("servos") if isinstance(sample.get("servos"), list) else []
        try:
            age = rd.bus.age()
        except Exception:  # noqa: BLE001
            age = None
        fresh = age is not None and age <= 3.0
        starting_s = starting_secs(rd, bus)
        err = bus.get("error") if isinstance(bus, dict) else None
        wait = startup_wait(bus)
        if not online:
            mode_ok, mode_text = False, "Connect to robotd to inspect current mode"
        elif mode == "commissioning" and phase in STARTING_PHASES:
            mode_ok = False
            if wait:
                mode_text = f"Commissioning, {wait['text']} (phase={phase})" + (f": {err}" if err else "")
            elif starting_s is not None and starting_s >= STARTING_LONG_S:
                mode_text = f"Commissioning, robotd stuck starting (phase={phase}, {starting_s}s): {STILL_STARTING}"
            else:
                mode_text = f"Commissioning, robotd still starting (phase={phase}); servo telemetry appears once ready"
        elif mode == "commissioning" and phase == "read_error":
            mode_ok = False
            cycle_err = bus.get("last_cycle_error")
            mode_text = "Commissioning, robotd running but servo read failed" + (f": {cycle_err}" if cycle_err else "") + ". Check servo power and cabling"
        elif mode == "commissioning":
            mode_ok = len(servos) == 15 and fresh
            mode_text = ("Commissioning: all 15 servos reporting fresh telemetry" if mode_ok
                         else f"Commissioning, but only {len(servos)} servos in last_sample" +
                              ("" if fresh else " (stale)") + "; power on servos and wait a few seconds")
        elif mode == "motion":
            mode_ok = phase == "control"
            if mode_ok:
                mode_text = "Motion mode: bus open"
            elif wait:
                mode_text = f"Motion mode, {wait['text']} (phase={phase})" + (f": {err}" if err else "")
            else:
                err = bus.get("error") if isinstance(bus, dict) else None
                mode_text = (f"Motion mode, but robotd is not in control phase yet (phase={phase or '?'})"
                             + (f": {err}" if err else "")
                             + ". Follow hints in Logs")
        else:
            mode_ok, mode_text = False, f"robotd in mode={mode or '?'}, phase={phase or '?'}"
        items.append({"key": "mode", "label": "robotd Mode", "ok": mode_ok, "text": mode_text,
                      "action": {"do": "robotd", "label": "View Logs"} if online and not mode_ok else None,
                      "mode": mode, "phase": phase})

        safe_ok = True
        safe_text = "Duck is standing/walking: sit down or release torque before writing"
        if mode == "motion" and phase == "control":
            if bus.get("policy_enabled") is True or native.get("torque_state_confirmed") is True:
                safe_ok = False
        elif mode == "commissioning" and isinstance(bus.get("hold"), dict):
            # Commissioning stand-hold active: writing restarts robotd and drops all 15 servos limp
            safe_ok = False
            safe_text = "Duck is standing in commissioning hold: click Disable on IMU page or E-Stop before writing"
        items.append({"key": "safety", "label": "Posture", "ok": safe_ok,
                      "text": "Idle/limp, safe to write" if safe_ok else safe_text,
                      "action": None})

        missing = [name for name in JOINT_ORDER if JOINT_ID[name] not in self.centered]
        servo_mark = self._user_mark("servo")
        servo_ok = not missing or servo_mark in ("done", "skip")
        joint_mode = self._joint_mode(servo_mark)
        if joint_mode == "imu_only":
            servo_ok, servo_text = True, "Writing imported IMU orientation only; preserving all servo calibrations"
        elif servo_mark in ("done", "skip"):
            servo_text = (("Step 1 marked done" if servo_mark == "done" else "Step 1 skipped")
                          + ": keeping existing servo zero offsets and directions in robotd")
        elif not missing:
            servo_text = "All 15 servo zero positions recorded in servo-centered.json"
        else:
            names = ", ".join(JOINT_CN.get(n, n) for n in missing[:6]) + ("..." if len(missing) > 6 else "")
            servo_text = f"{len(missing)} servos not yet midpoint-calibrated: {names}"
        items.append({"key": "servo", "label": "Servo Zeroing", "ok": servo_ok, "text": servo_text,
                      "action": {"do": "servos", "label": "Open Servo Page"} if not servo_ok else None,
                      "missing": missing, "done": 15 - len(missing), "by_mark": servo_mark if missing else None})

        # Use Servo page directions where explicitly set; preserve file directions otherwise.
        file_dirs = _file_directions(cal)
        final = {jid: self.directions.get(jid, file_dirs.get(jid, -1)) for jid in JOINT_ID.values()}
        changed = [{"id": jid, "name": name, "cn": JOINT_CN.get(name, name), "direction": final[jid]}
                   for name, jid in JOINT_ID.items() if final[jid] != -1]
        changed.sort(key=lambda r: JOINT_ORDER.index(r["name"]))
        dir_text = ("Set to +1: " + ", ".join(f"{r['cn']}" for r in changed) + " (verify direction)"
                    if changed else "All 15 set to -1")
        if joint_mode in ("keep", "imu_only"):
            dir_text, changed = "Keeping existing joint directions from robotd calibration file", []
        elif self.directions:
            dir_text += " (using Servo page overrides where set; keeping file defaults otherwise)"
        else:
            dir_text += " (no directions changed on Servo page; keeping existing file directions)"
        items.append({"key": "directions", "label": "Directions", "ok": True, "text": dir_text,
                      "action": {"do": "servos", "label": "Check Directions"} if changed else None, "changed": changed})

        source, mount = mount_of(bus, calib if isinstance(calib, dict) else {})
        mount = mount if isinstance(mount, dict) else {}
        verified = mount.get("verified") is True
        protocol_changed = bus_protocol_mismatch(bus, calib, source)
        if not source:
            imu_ok, imu_text = False, "robotd has not reported which IMU is active yet"
        elif protocol_changed:
            imu_ok, imu_text = False, "Bus IMU protocol does not match mount calibration: recalibrate on IMU page and verify with robotd"
        elif self.pending and self.pending.get("source") != source:
            imu_ok = False
            imu_text = (f"IMU chip changed (active {CHIP_CN.get(source, source)}, "
                        f"pending {self.pending.get('source')}): recalibrate on IMU page")
        elif self.pending and not pending_matches(self.pending, source, bus):
            imu_ok, imu_text = False, "Bus IMU board changed: recalibrate on IMU page"
        elif self.pending:
            imu_ok = True
            imu_text = (f"Imported IMU orientation ({CHIP_CN.get(source, source)}) will be written"
                        if (self.pending.get("quality") or {}).get("imported") is True
                        else f"Calibrated on IMU page ({CHIP_CN.get(source, source)}); new orientation will be written")
        elif verified:
            imu_ok, imu_text = True, f"IMU orientation already verified in robotd ({CHIP_CN.get(source, source)})"
        elif mode == "commissioning":
            imu_ok, imu_text = False, "Commissioning mode: run rough calibration on IMU page and verify with robotd"
        else:
            imu_ok, imu_text = False, "IMU mount orientation not calibrated on IMU page yet"
        imu_mark = self._user_mark("imu")
        imu_default = None
        if not imu_ok and imu_mark and not protocol_changed:
            said = "Step 2 marked done" if imu_mark == "done" else "Step 2 skipped"
            imu_default = self._default_imu(source, mount, cal, imu_mark)
            if imu_default:
                imu_ok = True
                imu_text = (f"{said}: writing default mount for {imu_default['label']} ({imu_default['how']}). "
                            "If mounted differently it will lean; run standing calibration on IMU page after writing")
            else:
                imu_text = f"{said}, but cannot use default mount: {imu_text}"
        items.append({"key": "imu", "label": "IMU Orientation", "ok": imu_ok, "text": imu_text,
                      "action": {"do": "imu", "label": "Open IMU Page"} if not imu_ok else None, "source": source,
                      "default": bool(imu_default)})

        servo_open = servo is not None and getattr(servo, "state", None) in ("open", "opening")
        items.append({"key": "bus", "label": "Servo Bus", "ok": not servo_open,
                      "text": "Servo page owns the bus: click 'Hand back to robotd' on Servo page first" if servo_open else "Servo page bus is released",
                      "action": {"do": "servos", "label": "Open Servo Page"} if servo_open else None})

        why = self._dirty(cal)
        note = ("robotd is not currently using this calibration; showing contents from file" if self.cal_from_file else None)
        return {"ready": all(i["ok"] for i in items), "items": items, "source": source,
                "imu_default": ({k: imu_default[k] for k in ("source", "label", "how", "quat")} if imu_default else None),
                "pending": bool(self.pending), "centered": len(self.centered),
                "preview_ok": bool(cal.get("joints")), "mode": mode, "phase": phase,
                "from_file": bool(self.cal_from_file), "note": note,
                "dirty": bool(why), "dirty_why": why}

    def view(self, rd=None, servo=None):
        out = self.readiness(rd, servo)
        job = dict(self.job) if isinstance(self.job, dict) else None
        # Collapse progress 2 min after success; keep failures visible
        if job and job.get("ok") and job.get("phase") == "done":
            finished = job.get("finished")
            if isinstance(finished, (int, float)) and time.time() - finished > 120:
                job = None
                self.job = None
        out["job"] = job
        out["simulate"] = self.simulate
        return out

    def hold_directions(self, rd=None):
        """15 joint directions (in JOINT_ORDER) for commissioning stand-hold, matching what commit writes."""
        self.reload()
        cal, _calib = self._calibration(rd or self.rd)
        if not isinstance(cal, dict) or not isinstance(cal.get("joints"), list) or len(cal["joints"]) != 15:
            raise Refused("Have not received 15-joint calibration from robotd yet; wait a few seconds")
        by_name = {j.get("name"): j for j in cal["joints"] if isinstance(j, dict)}
        out = []
        for name in JOINT_ORDER:
            row = by_name.get(name)
            if not isinstance(row, dict) or not _is_int(row.get("id")):
                raise Refused(f"Invalid robotd calibration entry for {name}: missing id")
            file_dir = row.get("direction") if row.get("direction") in (1, -1) else -1
            out.append(self.directions.get(row["id"], file_dir))
        return out

    def _payload(self, rd=None):
        from .imu import bus_protocol_mismatch, mount_of
        rd = rd or self.rd
        cal, calib = self._calibration(rd)
        if not isinstance(cal, dict) or not isinstance(cal.get("joints"), list) or len(cal["joints"]) != 15:
            raise Refused("Have not received 15-joint calibration from robotd yet; wait a few seconds")
        by_name = {j.get("name"): j for j in cal["joints"] if isinstance(j, dict)}
        mark, marked_at = self._servo_mark()
        joint_mode = self._joint_mode(mark)
        applied, events, fresh_ids = self._center_events(mark, marked_at)
        use_directions = joint_mode == "recorded"
        if joint_mode == "keep" and fresh_ids:
            joint_mode = "recorded"
        joints = []
        for name in JOINT_ORDER:
            row = by_name.get(name)
            if not isinstance(row, dict) or not _is_int(row.get("id")):
                raise Refused(f"Invalid robotd calibration entry for {name}: missing id")
            file_dir = row.get("direction") if row.get("direction") in (1, -1) else -1
            jid = row["id"]
            joint = {"id": jid, "direction": self.directions.get(jid, file_dir) if use_directions else file_dir}
            if joint_mode == "recorded" and jid in fresh_ids:
                joint["centered"] = True
            joints.append(joint)
        bus = self._current_bus(rd)
        source, mount = mount_of(bus, calib if isinstance(calib, dict) else {})
        if bus_protocol_mismatch(bus, calib, source):
            raise Refused("Bus IMU protocol does not match mount calibration: recalibrate on IMU page and verify with robotd")
        mount = mount if isinstance(mount, dict) else {}
        if source not in IMU_SOURCES:
            if self.pending and self.pending.get("source") in IMU_SOURCES:
                source = self.pending["source"]
            else:
                raise Refused("robotd has not reported which IMU is active yet; wait a few seconds")
        # Priority: standing IMU calibration (pending) > verified mount > step 2 default > unverified mount (preview only)
        dflt = None
        if self.pending:
            if self.pending.get("source") != source:
                raise Refused("IMU chip changed; recalibrate on IMU page")
            if not pending_matches(self.pending, source, bus):
                raise Refused("Bus IMU board changed; recalibrate on IMU page")
            quat = self.pending["quat"]
        elif mount.get("verified") is True and mount.get("quat"):
            quat = mount["quat"]
        elif (dflt := self._default_imu(source, mount, cal)) is not None:
            quat = dflt["quat"]
        elif mount.get("quat"):
            quat = mount["quat"]
        else:
            raise Refused("IMU orientation unavailable yet; wait for robotd or calibrate on IMU page")
        out = {"joints": joints, "imu": {"source": source, "quat": list(quat)}, "joint_mode": joint_mode}
        out["center_events"] = applied if joint_mode == "imu_only" else events
        if dflt:
            out["imu_default"] = dflt["how"]          # Preview/log only; helper receives {source, quat}
        return out

    def _summary(self, payload, toml, used_helper):
        from .imu_calib import describe_mount
        from .state import JOINT_CN
        q = payload["imu"]["quat"]
        desc = describe_mount(q)
        rows = []
        for row in tomllib.loads(toml)["joints"]:
            name = row["name"]
            rows.append({"name": name, "cn": JOINT_CN.get(name, name), "id": row["id"],
                         "direction": row["direction"], "zero_raw": row["zero_raw"],
                         "min_deg": round(math.degrees(row["min_rad"]), 3) if row.get("min_rad") is not None else None,
                         "max_deg": round(math.degrees(row["max_rad"]), 3) if row.get("max_rad") is not None else None})
        return {"imu": {"source": payload["imu"]["source"], "label": CHIP_CN.get(payload["imu"]["source"]),
                        "text": desc["text"], "off_deg": desc["off_deg"], "quat": list(q),
                        "default": payload.get("imu_default")},
                "joints": rows, "toml": toml, "helper": used_helper,
                "from_file": bool(self.cal_from_file), "note": "robotd is not currently using this calibration; showing contents from file" if self.cal_from_file else None}

    def preview(self, by=None, rd=None):
        from . import helper
        self.reload(force=True)
        payload = self._payload(rd)
        cal, _ = self._calibration(rd)
        if helper.available():
            ok, result, detail = helper.calib_commit(by or "Studio", payload["joints"], payload["imu"],
                                                     dry_run=True, timeout=90, joint_mode=payload["joint_mode"])
            if not ok:
                raise Refused(helper.friendly(result, detail, "calib-commit"))
            toml = (detail or {}).get("toml")
            if not isinstance(toml, str) or not toml.strip():
                raise Refused("Helper returned empty calibration preview")
            return self._summary(payload, toml, True)
        built = build_calibration(cal, payload["joints"], payload["imu"], payload["joint_mode"])
        validate_calibration(built, payload["imu"]["source"])
        return self._summary(payload, render_calibration(built), False)

    def _record_success(self, payload):
        row = {"at": time.time(), "center_events": payload["center_events"], "directions": {str(j["id"]): j["direction"] for j in payload["joints"]},
               "imu_source": payload["imu"]["source"],
               "imu_quat": list(payload["imu"]["quat"])}
        try:
            _write_json(_path(self.state_dir, COMMIT_LAST), row)
            self._mt["commit-last"] = self._sig(_path(self.state_dir, COMMIT_LAST))
        except OSError as e:
            self.log.warn("Failed to record commit-last.json (robotd calibration is unaffected)", cat="Calibration", raw=f"{type(e).__name__}: {e}")

    @staticmethod
    def _job_from_result(row):
        if not isinstance(row, dict) or row.get("verb") != "calib-commit":
            return None
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        ok = row.get("exit") == 0
        return {"phase": "done" if ok else "failed", "ok": ok, "reason": row.get("reason") or detail.get("msg"),
                "started": row.get("wall"), "finished": row.get("wall"), "detail": detail,
                "from_last_result": True, "msg": "Last write result" if ok else "Last write failed"}

    def _job_from_last_result(self):
        from . import helper
        return self._job_from_result(helper.last_result())

    def commit(self, by=None, progress=None, rd=None):
        from . import helper
        if not self._writing.acquire(blocking=False):
            return {"ok": False, "reason": "Write already in progress; please wait", "busy": True}
        started_wall = time.time()
        try:
            by = by or "Studio"
            self.reload(force=True)
            payload = self._payload(rd)
            self.job = {"phase": "running", "msg": "Preparing write", "started": started_wall, "steps": []}

            def say(msg, detail=None):
                self.job["msg"] = msg
                self.job["steps"] = (self.job.get("steps") or [])[-30:] + [{"msg": msg, "at": time.time()}]
                if progress is not None:
                    try:
                        progress(msg, detail)
                    except Exception:  # noqa: BLE001
                        pass

            say("Preparing write")
            if payload.get("imu_default"):
                src = payload["imu"]["source"]
                self.log.info(f"Writing default IMU mount ({CHIP_CN.get(src, src)}: {payload['imu_default']}): step 2 marked done/skip "
                              "with no verified or pending IMU calibration", cat="Calibration", raw=json.dumps(payload["imu"]),
                              hint="If mounted differently, the duck will lean; run standing calibration on IMU page and re-commit")
            if self.simulate:
                cal, _ = self._calibration(rd)
                built = build_calibration(cal, payload["joints"], payload["imu"], payload["joint_mode"])
                validate_calibration(built, payload["imu"]["source"])
                toml = render_calibration(built)
                say("Desktop demo: did not write robotd config")
                clear_pending(self.state_dir)
                self._record_success(payload)
                self.reload(force=True)
                done = time.time()
                self.job.update({"phase": "done", "ok": True, "reason": "Desktop demo: simulated write succeeded (no files modified)",
                                 "toml": toml, "finished": done})
                self.log.info("Write to robotd (desktop demo, simulated)", cat="Calibration", raw=toml[:2000])
                return {"ok": True, "reason": self.job["reason"], "detail": {"simulate": True}}
            if not helper.available():
                reason = "Cannot write to robotd on desktop"
                self.job.update({"phase": "failed", "ok": False, "reason": reason, "finished": time.time()})
                self.log.warn(reason, cat="Calibration", hint="Writing to robotd requires running on the duck with microduck-helper installed")
                return {"ok": False, "reason": reason, "detail": {"rolled_back": False}}

            def on_line(obj):
                step, msg = obj.get("step"), obj.get("msg")
                if step in ("mode", "gate", "backup", "write", "check", "restart", "wait", "rollback", "done") and isinstance(msg, str):
                    if step == "done":
                        return
                    say(msg, {"step": step, "level": obj.get("level")})

            say("Writing config, switching mode, and restarting robotd (up to 180s)")
            ok, result, detail = helper.calib_commit(by, payload["joints"], payload["imu"], dry_run=False,
                                                     timeout=COMMIT_WAIT_S, on_line=on_line, joint_mode=payload["joint_mode"])
            from_last = False
            if result == "TimeoutExpired":
                last = helper.last_result()
                if isinstance(last, dict) and last.get("verb") == "calib-commit" and (last.get("wall") or 0) >= started_wall - 2.0:
                    d = dict(last.get("detail") or {})
                    d["msg"] = last.get("reason") or d.get("msg")
                    ok, result, detail, from_last = last.get("exit") == 0, last.get("result"), d, True
                else:
                    reason = "Helper did not return final result within 180s; it may still be running. Check Logs or reopen Studio shortly"
                    self.job.update({"phase": "failed", "ok": False, "reason": reason, "finished": time.time()})
                    self.log.warn(reason, cat="Calibration", hint="Helper writes final status to /run/microduck-helper/last-result.json")
                    return {"ok": False, "reason": reason, "detail": {"rolled_back": False}}
            if ok:
                clear_pending(self.state_dir)
                self._record_success(payload)
                self.reload(force=True)
                reason = ("Wrote IMU orientation and preserved all servo calibrations" if payload["joint_mode"] == "imu_only" else
                          "Calibration written to robotd; switched to motion mode")
                self.job.update({"phase": "done", "ok": True, "reason": reason, "finished": time.time(),
                                 "from_last_result": from_last, "detail": detail or {}})
                self.log.info(reason, cat="Calibration", raw=json.dumps(detail or {}, ensure_ascii=False)[:2000])
                return {"ok": True, "reason": reason, "detail": detail or {}}
            reason = helper.friendly(result, detail, "calib-commit")
            rolled = bool((detail or {}).get("rolled_back"))
            self.job.update({"phase": "failed", "ok": False, "reason": reason, "rolled_back": rolled,
                             "finished": time.time(), "from_last_result": from_last, "detail": detail or {}})
            self.log.error(f"Write to robotd failed: {reason}", cat="Calibration", raw=json.dumps(detail or {}, ensure_ascii=False)[:4000],
                           hint="Check the trailing robotd lines in Logs; previous config was restored" if rolled else "Check helper details in Logs")
            return {"ok": False, "reason": reason, "detail": detail or {}}
        except (Refused, CalibError) as e:
            reason = str(e)
            self.job = {"phase": "failed", "msg": reason, "ok": False, "reason": reason,
                        "started": started_wall, "finished": time.time()}
            self.log.warn(f"Write to robotd did not start: {reason}", cat="Calibration")
            return {"ok": False, "reason": reason, "detail": {"rolled_back": False}}
        except Exception as e:  # noqa: BLE001
            reason = f"Studio error: {type(e).__name__}: {e}"
            self.job = {"phase": "failed", "msg": reason, "ok": False, "reason": reason,
                        "started": started_wall, "finished": time.time()}
            self.log.error("Studio error while writing calibration", cat="Calibration", raw=reason, exc=True,
                           hint="Original files untouched; export a diagnostic bundle")
            return {"ok": False, "reason": reason, "detail": {"rolled_back": False}}
        finally:
            self._writing.release()
