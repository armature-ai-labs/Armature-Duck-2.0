"""IMU page backend: pushes IMU telemetry/status and handles UI commands.

Standing calibration results are persisted to state_dir/imu-pending.json and committed via Step 3.
Commissioning mode also supports coarse calibration + robotd verify-imu.
"""
import asyncio
import json
import math
import os
import time

from . import helper, imu_calib
from .robotd import fresh_calib
from .rpc import RpcError, Timeout, Unreachable, call_once
from .startup_status import imu_context_current, imu_diagnostic
from .estop import STARTING_PHASES
from .communication import EvidenceAge, ImuHealth, number

MAX_SAMPLES = 6000          # Cap samples per solve() call (5 steps x <=600 each + margin)
ON_DUCK = ("Bus IMU (ID 200) is auto-detected by robotd on boot. I2C IMUs must be wired before first boot; "
           "if added later, run over SSH: sudo microduck-firstboot --detect-imu-only --force")
CHECK_MAX_DIFF_DEG = 2.0
IMU_VERIFICATION_CONTRACT = "upright_nose_down_v1"
IMU_HOME_CONTRACT = "home_mount_v1"


class Refused(Exception):
    """Operation cannot proceed right now; message is user-facing."""


def imu_bus_metadata(bus):
    """Validate raw presence before projection; mirrored in the standalone helper."""
    bus = bus if isinstance(bus, dict) else {}
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    infos = [part["imu_bus"] for part in (native, bus) if "imu_bus" in part]
    profiles = [part["configured_imu_profile"] for part in (native, bus) if "configured_imu_profile" in part]
    error = None
    for info in infos:
        if (not isinstance(info, dict) or info.get("protocol") not in ("feetech", "dxl2")
                or info.get("profile", "legacy") not in ("legacy", "qinmo")
                or (info.get("profile") == "qinmo" and info.get("protocol") != "feetech")):
            error = "robotd reported an unsupported or inconsistent IMU protocol/profile; cannot use old mount or calibrate"
    if any(profile not in ("legacy", "qinmo") for profile in profiles):
        error = "robotd reported an unsupported or inconsistent IMU protocol/profile; cannot use old mount or calibrate"
    if not error:
        bindings = [(info["protocol"], info.get("profile", "legacy")) for info in infos]
        if (any(binding != bindings[0] for binding in bindings[1:])
                or any(profile != profiles[0] for profile in profiles[1:])
                or (bindings and profiles and bindings[0][1] != profiles[0])):
            error = "robotd reported an unsupported or inconsistent IMU protocol/profile; cannot use old mount or calibrate"
    # Startup diagnostics are display-only. Cached legacy identity cannot bind a
    # mounting calibration while the new daemon is still refusing initialization.
    if "imu_observation" in native and bus.get("phase") in STARTING_PHASES:
        infos = []
    return (infos[0] if infos and isinstance(infos[0], dict) else None,
            profiles[0] if profiles else None, error)


def bus_imu_of(bus):
    """Detected bus IMU protocol and board info (native in motion mode, top-level in commissioning)."""
    info, _configured, _error = imu_bus_metadata(bus)
    if info is None:
        return None
    out = {"protocol": info.get("protocol"), "board": info.get("board"),
           "identity": list(info["identity"]) if isinstance(info.get("identity"), list) else None}
    for key in ("profile", "gyro_dps_per_lsb", "identification"):
        if key in info:
            out[key] = info[key]
    return out


def configured_profile(bus):
    return imu_bus_metadata(bus)[1]


def bus_profile_error(bus, calib=None, source="bus"):
    """Missing old fields mean legacy; explicit unsupported evidence never does."""
    if source not in (None, "bus"):
        return None
    info, configured, error = imu_bus_metadata(bus)
    if error:
        return error
    calib = calib if isinstance(calib, dict) else {}
    cal = calib.get("calibration") if isinstance(calib.get("calibration"), dict) else calib
    bound = cal.get("imu_bus_profile", "legacy")
    protocol = cal.get("imu_bus_protocol", "feetech")
    if (bound not in ("legacy", "qinmo") or protocol not in ("feetech", "dxl2")
            or (bound == "qinmo" and protocol != "feetech")):
        return "robotd reported an unsupported IMU profile; check robotd and config"
    if info is None and (configured is not None or bound != "legacy"):
        return "Bus IMU is not connected; cannot verify profile before calibration"
    return None


def bus_protocol_mismatch(bus, calib, source="bus"):
    info = bus_imu_of(bus) if source == "bus" else None
    calib = calib if isinstance(calib, dict) else {}
    cal = calib.get("calibration") if isinstance(calib.get("calibration"), dict) else calib
    configured = configured_profile(bus) if source == "bus" else None
    return bool(bus_profile_error(bus, calib, source)
                or (info and (info["protocol"] != cal.get("imu_bus_protocol", "feetech")
                    or info.get("profile", "legacy") != cal.get("imu_bus_profile", "legacy")
                    or (configured is not None and configured != info.get("profile", "legacy")))))


def calibration_context(bus, source, redo):
    metadata_error = imu_bus_metadata(bus)[2] if source in (None, "bus") else None
    info = bus_imu_of(bus) if source == "bus" else None
    transport = (info["protocol"], info["board"], tuple(info["identity"] or []),
                 info.get("profile", "legacy"), configured_profile(bus), metadata_error) if info else (
                     (None, configured_profile(bus), metadata_error)
                     if configured_profile(bus) is not None or metadata_error else None)
    return source, transport, bus.get("mode"), redo.get("session_id"), bool(redo.get("active"))


def mount_of(bus, calib):
    """Return (source, {"quat", "verified"} or None). Shared by IMU page and onboarding checklist."""
    bus = bus if isinstance(bus, dict) else {}
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    calib = calib if isinstance(calib, dict) else {}
    cal = calib.get("calibration") if isinstance(calib.get("calibration"), dict) else calib
    # Resolve source: busStatus native.imu_source -> sample.source -> calib.imu_source -> "bus" fallback
    last_sample = bus.get("last_sample") if isinstance(bus.get("last_sample"), dict) else {}
    sample = last_sample.get("imu") if isinstance(last_sample.get("imu"), dict) else {}
    source = (native.get("imu_source") or sample.get("source") or calib.get("imu_source")
              or ("bus" if native or bus_imu_of(bus) else None))
    top = calib.get("imu_mount") if isinstance(calib.get("imu_mount"), dict) else None
    mounts = cal.get("imu_mounts") if isinstance(cal.get("imu_mounts"), dict) else {}
    if top and top.get("quat") and calib.get("imu_source") in (None, source):
        mount = {"quat": top.get("quat"), "verified": top.get("verified")}     # Selected by robotd
    elif source in mounts and isinstance(mounts[source], dict):
        mount = {"quat": mounts[source].get("quat"), "verified": mounts[source].get("verified")}
    elif cal.get("imu_mount_quat"):
        mount = {"quat": cal.get("imu_mount_quat"), "verified": cal.get("imu_mount_verified")}
    else:
        mount = None
    if mount and bus_protocol_mismatch(bus, calib, source):
        mount["verified"] = False
    return source, mount


class ImuBackend:
    def __init__(self, robotd, log, state_dir=".", root="/"):
        self.rd, self.log, self.state_dir = robotd, log, state_dir
        self.root = root
        self._seen_context = None
        self._context_generation = 0
        self._redo_busy = False
        self._capture_basis = None
        self._result_basis = None
        self._verify_lock = asyncio.Lock()
        self.home_verification_task = None
        self.job = None              # Coarse sampling / verify-imu job progress
        self.detected = None         # Latest chip detection result
        self.last_result = None      # Latest solve() result (required for saving)
        # Onboarding check callback (wired by app)
        self.on_check = None
        self.commit = None           # CommitService backend for commissioning stand-hold
        self._health = ImuHealth()
        self._values_age = EvidenceAge()

    # ---- UI View ----

    def view(self):
        bus = self.rd.bus.value if isinstance(self.rd.bus.value, dict) else {}
        current = not hasattr(self.rd, "present") or imu_context_current(self.rd)
        if not current:
            bus = {}  # Preserve the raw cache; expired metadata cannot bind a board or mounting.
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        # Only trust calibration read after current hello
        calib = fresh_calib(self.rd) if current else None
        source, mount = mount_of(bus, calib)
        redo = self._redo_view(bus, source)
        context = calibration_context(bus, source, redo)
        if self._result_basis is not None and self._result_basis != self._sampling_basis(bus):
            self.last_result = None
            self._result_basis = None
        if self._seen_context is not None and context != self._seen_context:
            self._context_generation += 1
            self.last_result = None
            if context[1] != self._seen_context[1] and self.job and (self.job.get("done") or self.job.get("failed")):
                self.job = None
        self._seen_context = context
        ls = bus.get("last_sample") if isinstance(bus.get("last_sample"), dict) else {}
        s_imu = ls.get("imu") if isinstance(ls.get("imu"), dict) else {}
        reader = native.get("imu_reader")
        if reader is None and bus.get("mode") == "commissioning":
            reader = {"state": ("streaming" if s_imu.get("ready") else "missing"), "errors": 0}
        health, metadata = self._health.view(self.rd, source=source,
            info=bus_imu_of(bus) if source == "bus" else None, sample=s_imu)
        stream = getattr(getattr(self.rd, "state", None), "value", None)
        stream = stream if isinstance(stream, dict) else {}
        stream_imu = stream.get("imu") if isinstance(stream.get("imu"), dict) else {}
        use_stream = bool(stream_imu and bus.get("mode") == "motion")
        values = stream_imu if use_stream else s_imu
        values_age = self._values_age.observe((self._health.identity, stream.get("t_ns")),
            getattr(getattr(self.rd, "state", None), "at", None), 0., time.monotonic()) if use_stream else health["age_ms"]
        def vector(value, length):
            return list(value) if isinstance(value, list) and len(value) == length and all(number(x) for x in value) else None
        return {
            "t": "imu", "mode": bus.get("mode"), "source": source, "chip_setting": None, "mount": mount,
            "diagnostic": imu_diagnostic(self.rd),
            "imu_bus": bus_imu_of(bus) if source == "bus" else None,
            "configured_imu_profile": configured_profile(bus),
            "profile_error": bus_profile_error(bus, calib, source),
            "metadata_error": imu_bus_metadata(bus)[2] if source in (None, "bus") else None,
            "profile_selection": self._profile_view(bus, source, redo),
            "mount_protocol_mismatch": bus_protocol_mismatch(bus, calib, source),
            "reader": reader, "age_ms": native.get("imu_age_ms", s_imu.get("age_ms")),
            "ready": native.get("imu_ready", s_imu.get("ready")),
            "health": health, "metadata": metadata,
            "gyro": vector(values.get("gyro_rad_s", values.get("gyro")), 3),
            "quaternion_wxyz": vector(values.get("quaternion_wxyz", values.get("quat")), 4),
            "accel_m_s2": vector(values.get("accel_m_s2"), 3),
            "temperature_c": values.get("temperature_c") if number(values.get("temperature_c")) else None,
            "values_age_ms": values_age,
            "valid": s_imu.get("valid"), "gravity": s_imu.get("gravity"),
            "hold": bus.get("hold") if isinstance(bus.get("hold"), dict) else None,
            "detected": self.detected, "stand_calib_at": self._stand_calib_at(), "job": self.job,
            "redo": redo,
            "coarse_sampling": self._motion_sampling(bus, source, redo),
        }

    def _sampling_basis(self, bus):
        source, mount = mount_of(bus, fresh_calib(self.rd))
        quat = (mount or {}).get("quat")
        return (calibration_context(bus, source, {}), tuple(quat) if isinstance(quat, list) else None,
                getattr(getattr(self.rd, "hello", None), "at", None))

    def _motion_sampling(self, bus, source, redo):
        """Read-only collection in motion HOME; no mode/torque command here."""
        calib = fresh_calib(self.rd)
        _, mount = mount_of(bus, calib)
        try:
            self._normalized_quat((mount or {}).get("quat"))
            known_mount = True
        except Refused:
            known_mount = False
        available = bus.get("mode") == "motion" and source in ("bus", "lsm6dsv16x", "bno08x") and known_mount and not redo.get("active")
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        hd = native.get("hd1910") if isinstance(native.get("hd1910"), dict) else {}
        off = (native.get("torque_state_confirmed") is False and native.get("torque_enabled") is False
               and hd.get("restore_pending") is False and hd.get("temporary_gains_applied") is False)
        guard = bus.get("home_start_guard") or {}
        error = guard.get("max_joint_error_deg") if isinstance(guard, dict) else None
        legacy_ready = (isinstance(error, (int, float)) and not isinstance(error, bool)
                        and math.isfinite(error) and 0 <= error <= 5)
        hold_ready = bus.get("imu_calibration_hold_ready", legacy_ready)
        held = (native.get("torque_state_confirmed") is True and native.get("torque_enabled") is True
                and bus.get("homed") is True and hold_ready is True)
        age = native.get("imu_age_ms")
        if age is None:
            age = native.get("imu_age_upper_bound_ms")
        reason = None
        if not available:
            reason = "Cannot sample orientation in motion HOME right now; read current mount or enter initial calibration mode first"
        elif self._redo_busy:
            reason = "Calibration mode switch in progress; wait for it to finish"
        elif bus_profile_error(bus, fresh_calib(self.rd), source):
            reason = bus_profile_error(bus, fresh_calib(self.rd), source)
        elif (bus.get("phase") != "control" or bus.get("policy_enabled") is not False
              or bus.get("policy_motion_enabled") is True or bus.get("home_recovery")
              or bus.get("torque_off_pending") or bus.get("power_pending") or bus.get("motion")):
            reason = "Stop active policy and hold HOME first; cannot sample during recovery or policy control"
        elif not (held or off):
            reason = "Wait for joints to settle at HOME, or support the duck and release torque before sampling"
        elif (native.get("imu_ready") is not True or not isinstance(age, (int, float))
              or isinstance(age, bool) or not math.isfinite(age) or not 0 <= age <= 250
              or bus.get("all_joints_fresh") is not True or bus.get("fresh_sample") is not True):
            reason = "IMU or joint telemetry is stale; wait for live data to recover"
        elif helper.flag_present():
            reason = "Servo page owns the bus; hand control back to robotd first"
        live_verify = (available and isinstance(calib, dict)
                       and calib.get("imu_home_verification_contract") == IMU_HOME_CONTRACT)
        return {"available": available, "reason": reason,
                "hold_ready": held,
                "live_verify": live_verify,
                "verify_reason": None if held else "Enable standing pose and wait for HOME to settle before verifying & saving",
                "needs_unload": not (off or live_verify)}

    def _redo_view(self, bus, source):
        redo = helper.imu_redo_state(root=self.root)
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        hd = native.get("hd1910") if isinstance(native.get("hd1910"), dict) else {}
        reason = redo.get("reason")
        if redo.get("active"):
            reason = reason or "Initial IMU calibration session is active; continue or cancel"
        elif self._redo_busy:
            reason = "Switching initial IMU calibration mode"
        elif source in (None, "bus") and imu_bus_metadata(bus)[2]:
            reason = imu_bus_metadata(bus)[2]
        elif bus.get("mode") != "motion" or bus.get("phase") != "control":
            reason = "Redo is available in motion control phase; in commissioning mode, run coarse calibration directly"
        elif (bus.get("policy_enabled") is not False or bus.get("home_recovery")
              or native.get("torque_state_confirmed") is not False or bus.get("torque_off_pending") is True
              or hd.get("restore_pending") is not False or hd.get("temporary_gains_applied") is not False):
            reason = "Support the duck and release torque; wait for all servos to disarm and gains to restore"
        elif source not in ("bus", "bno08x", "lsm6dsv16x"):
            reason = "IMU source not yet reported by robotd"
        elif helper.flag_present():
            reason = "Servo page owns the bus; hand control back to robotd first"
        return {**redo, "can_start": reason is None, "reason": reason}

    def _profile_view(self, bus, source, redo):
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        hd = native.get("hd1910") if isinstance(native.get("hd1910"), dict) else {}
        reason = None
        if imu_bus_metadata(bus)[2]:
            reason = imu_bus_metadata(bus)[2]
        elif configured_profile(bus) not in ("legacy", "qinmo"):
            reason = "robotd does not support web profile selection yet; update robotd"
        elif source not in (None, "bus"):
            reason = "Using I2C IMU; bus board profile selection not needed"
        elif redo.get("active") or self._redo_busy:
            reason = "Finish or cancel the active orientation calibration session first"
        elif helper.flag_present():
            reason = "Servo page owns the bus; hand control back to robotd first"
        elif bus.get("mode") == "motion" and bus.get("phase") == "control":
            if (bus.get("policy_enabled") is not False or bus.get("home_recovery")
                    or native.get("torque_state_confirmed") is not False or bus.get("torque_off_pending") is True
                    or hd.get("restore_pending") is not False or hd.get("temporary_gains_applied") is not False):
                reason = "Support the duck and release torque; wait for all servos to disarm and gains to restore"
        elif bus.get("mode") not in ("motion", "commissioning") or bus.get("phase") not in (
                "configuring_bus", "waiting_for_bus", "ready"):
            reason = "Support the duck and release torque; wait for robotd to become ready"
        return {"can_start": reason is None, "reason": reason}

    def blocks_updates(self):
        return self._redo_busy or helper.imu_redo_state(root=self.root).get("active", False)

    def _coarse_guard(self, bus):
        if self._redo_busy:
            raise Refused("Stand-hold or calibration mode switch is still in progress; wait until settled")
        calib = fresh_calib(self.rd)
        source, _ = mount_of(bus, calib)
        if error := bus_profile_error(bus, calib, source):
            self.last_result = None
            raise Refused(error)
        redo = helper.imu_redo_state(root=self.root)
        if bus.get("mode") == "motion":
            sampling = self._motion_sampling(bus, source, redo)
            if sampling["reason"]:
                raise Refused(sampling["reason"])
            return redo
        native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
        hold = bus.get("hold") if isinstance(bus.get("hold"), dict) else {}
        transitioning = (bus.get("torque_off_pending") not in (None, False)
                         or bus.get("power_pending") or bus.get("motion") or bus.get("home_recovery")
                         or bus.get("policy_enabled") is True or bus.get("policy_motion_enabled") is True)
        error = hold.get("max_error_deg")
        legacy_ready = (isinstance(error, (int, float)) and not isinstance(error, bool)
                        and math.isfinite(error) and 0 <= error <= 5)
        settled_hold = (bus.get("mode") == "commissioning"
                        and bus.get("phase") == "holding" and bus.get("torque_state") == "all_enabled"
                        and hold.get("stage") == "holding" and native.get("torque_state_confirmed") is not False
                        and hold.get("imu_calibration_ready", legacy_ready) is True)
        relaxed = (bus.get("torque_state") == "off" and native.get("torque_state_confirmed") is not True
                   and not hold)
        if transitioning or not (relaxed or settled_hold):
            raise Refused("Support the duck; wait for standing pose to settle or torque-off to confirm")
        if redo.get("active"):
            if redo.get("phase") not in ("commissioning", "finish_failed"):
                raise Refused(redo.get("reason") or "Initial IMU redo mode switch incomplete; cancel or recover session first")
            source, _ = mount_of(bus, fresh_calib(self.rd))
            if source != redo.get("source"):
                self.last_result = None
                raise Refused("IMU source changed during redo session; cancel and restart")
        return redo

    async def _redo_op(self, args, by=None):
        if args.get("confirmed") is not True:
            raise Refused("Confirm that the duck is supported and torque is off before starting or cancelling IMU redo")
        if self._redo_busy:
            raise Refused("Initial IMU calibration switch already in progress")
        selecting = args["op"] == "profile-select"
        if selecting and (set(args) - {"op", "profile", "confirmed"} or args.get("profile") not in ("legacy", "qinmo")):
            raise Refused("IMU profile must be 'legacy' (auto-detect) or 'qinmo'")
        action = "enter" if args["op"] in ("redo-start", "profile-select") else "cancel"
        redo = helper.imu_redo_state(root=self.root)
        if action == "enter":
            bus = await self._bus_once()
            source, _ = mount_of(bus, fresh_calib(self.rd))
            metadata_error = imu_bus_metadata(bus)[2]
            if metadata_error and (selecting or source in (None, "bus")):
                raise Refused(metadata_error)
            if selecting and configured_profile(bus) not in ("legacy", "qinmo"):
                raise Refused("robotd does not support web profile selection yet; update robotd")
            info = bus_imu_of(bus)
            if selecting and info is not None and info.get("profile", "legacy") not in ("legacy", "qinmo"):
                raise Refused(bus_profile_error(bus))
            if not selecting and bus.get("mode") != "motion":
                raise Refused("Run coarse calibration directly in commissioning mode; redo session is only for motion mode")
            if redo.get("active"):
                raise Refused("Initial IMU redo session is already active; continue or cancel")
            if not helper.available():
                raise Refused("Redoing initial IMU calibration requires the updated root helper (deploy/install.sh)")
        elif not redo.get("active") or not redo.get("session_id"):
            raise Refused("No active initial IMU redo session to cancel")
        # _bus_once above yields: a hold request may have acquired the operation
        # slot while this mode switch was still doing its preflight.
        if self._redo_busy:
            raise Refused("Stand-hold or calibration mode switch is still in progress; wait until finished")
        self._redo_busy = True
        self.last_result = None
        self.job = {"kind": "coarse", "title": "Switching IMU Calibration Mode", "msg": "Keep duck supported and torque off while robotd switches"}
        try:
            kw = {"confirmed": True}
            if selecting:
                kw["profile"] = args["profile"]
            if action == "cancel":
                kw["session_id"] = redo["session_id"]
            ok, result, detail = await asyncio.to_thread(helper.imu_recalibrate, by or "Studio", action, **kw)
            if not ok:
                raise Refused(helper.friendly(result, detail, "imu-recalibrate"))
            if selecting:
                from .commit import clear_pending
                clear_pending(self.state_dir)
            self.job = None
            self.view()
            note = ("Restored previous profile and mount while staying in commissioning mode; re-select and verify"
                    if action == "cancel" and (detail or {}).get("original_mode") in ("startup", "commissioning") else None)
            return {"ok": True, "result": result, "redo": detail, "note": note}
        except Refused as err:
            self.job = {"kind": "coarse", "title": "IMU Calibration Switch Failed", "failed": True, "msg": str(err)}
            raise
        finally:
            self._redo_busy = False

    def _stand_calib_at(self):
        try:
            with open(os.path.join(self.state_dir, "imu-stand-calib.json"), encoding="utf-8") as f:
                return json.load(f).get("at")
        except (OSError, ValueError):
            return None

    # ---- UI Commands ----

    async def _bus_once(self):
        """Query busStatus on a fresh connection for commissioning sampling; fall back to cached value in tests/bench."""
        addr = getattr(self.rd, "addr", None)
        if addr and addr != "none":
            r = await call_once(addr, "robot.busStatus", {}, timeout=1.5, name="coarse-sample")
            return r if isinstance(r, dict) else {}
        v = getattr(getattr(self.rd, "bus", None), "value", None)
        return v if isinstance(v, dict) else {}

    async def _rpc_once(self, method, params, timeout, name):
        """Send a one-shot RPC on a fresh connection; fall back to rd.cmd in tests/bench."""
        addr = getattr(self.rd, "addr", None)
        if addr and addr != "none":
            return await call_once(addr, method, params, timeout=timeout, name=name)
        conn = getattr(self.rd, "cmd", None)
        call = getattr(conn, "call", None)
        if call is None:
            raise Unreachable("Not connected to robotd; wait for it to start")
        return await call(method, params, timeout)

    @staticmethod
    def _sample_g(bus):
        ls = bus.get("last_sample") if isinstance(bus.get("last_sample"), dict) else {}
        imu = ls.get("imu") if isinstance(ls.get("imu"), dict) else {}
        g = imu.get("gravity")
        if imu.get("valid") is not True or not isinstance(g, list) or len(g) != 3:
            return None, imu
        try:
            v = [float(x) for x in g]
        except (TypeError, ValueError):
            return None, imu
        if not all(math.isfinite(x) for x in v) or sum(x * x for x in v) < 1e-9:
            return None, imu
        return v, imu

    async def _capture(self, args, by=None):
        phase = args.get("phase")
        if phase not in ("still", "fwd", "back"):
            raise Refused("Coarse sampling phase must be 'still', 'fwd', or 'back'")
        seconds = args.get("seconds", 3.0 if phase != "back" else 2.0)
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0.5 <= float(seconds) <= 8.0:
            raise Refused("Sampling duration must be between 0.5 and 8 seconds")
        out, t0, last_tilt = [], time.monotonic(), None
        capture_context = None
        last_state_key = None
        capture_generation = self._context_generation
        label = {"still": "Hold Upright", "fwd": "Tilt Nose Down", "back": "Tilt Nose Up"}[phase]
        self.job = {"kind": "coarse", "title": "Coarse Sampling", "msg": f"{label}: {float(seconds):g}s",
                    "target_deg": 20 if phase == "fwd" else 0, "hold": 0.0}
        while time.monotonic() - t0 < float(seconds):
            bus = await self._bus_once()
            if bus.get("mode") not in ("commissioning", "motion"):
                self.job = {"kind": "coarse", "title": "Coarse Sampling", "failed": True,
                            "msg": "robotd is no longer in commissioning mode"}
                if bus.get("mode") == "motion":
                    raise Refused("robotd is in motion mode; use standing calibration once upright")
                raise Refused("robotd is not in commissioning mode; wait for commissioning mode")
            self._coarse_guard(bus)
            source, _ = mount_of(bus, fresh_calib(self.rd))
            redo = helper.imu_redo_state(root=self.root)
            context = calibration_context(bus, source, redo)
            motion = bus.get("mode") == "motion"
            if motion:
                basis = self._sampling_basis(bus)
                if phase == "still" and capture_context is None:
                    self._capture_basis = basis
                if self._capture_basis != basis:
                    raise Refused("Mount or robotd connection changed during sampling; re-sample Still")
            if (self._context_generation != capture_generation
                    or (capture_context is not None and context != capture_context)):
                self.last_result = None
                raise Refused("IMU source, bus board, mode, or session changed during sampling; re-sample")
            capture_context = context
            g, imu = self._sample_g(bus)
            if motion:
                sample = getattr(self.rd, "state", None)
                age = sample.age() if sample is not None else None
                state = sample.value if sample is not None and isinstance(sample.value, dict) else {}
                key = state.get("t_ns")
                gravity = (state.get("safety") or {}).get("gravity")
                fresh = (age is not None and age <= .5 and isinstance(key, int) and not isinstance(key, bool)
                         and (last_state_key is None or key > last_state_key))
                g, imu = self._sample_g({"last_sample": {"imu": {"valid": fresh, "gravity": gravity}}})
                if g is not None:
                    last_state_key = key
            if g is not None:
                out.append({"step": phase, "t": time.monotonic() - t0, "g": g})
                tilt = imu.get("tilt_deg")
                if isinstance(tilt, (int, float)):
                    last_tilt = float(tilt)
            self.job.update({"hold": min(1.0, (time.monotonic() - t0) / float(seconds)),
                             "tilt_deg": last_tilt})
            await asyncio.sleep(0.1)
        if self._context_generation != capture_generation:
            self.last_result = None
            raise Refused("IMU context changed during sampling; re-sample")
        if len(out) < 15:
            self.job = {"kind": "coarse", "title": "Coarse Sampling", "failed": True,
                        "msg": f"Only captured {len(out)} valid samples"}
            raise Refused(f"Only captured {len(out)} valid IMU samples (need >= 15); check IMU wiring and retry")
        self.job = {"kind": "coarse", "title": "Coarse Sampling", "msg": f"{label} captured",
                    "done": True, "tilt_deg": last_tilt, "target_deg": 20 if phase == "fwd" else 0}
        self.log.info(f"IMU coarse sampling captured '{label}' ({len(out)} samples)", cat="IMU",
                      ctx={"by": by, "phase": phase, "source": self.view().get("source")})
        return {"phase": phase, "n": len(out), "samples": out}

    @staticmethod
    def _translate_verify_error(err):
        text = getattr(err, "message", None) or str(err)
        low = text.lower()
        if "needs a live" in low or "last_sample" in low:
            msg = "robotd has no live IMU readings: check wiring and confirm commissioning telemetry is updating"
        elif "from upright" in low or "stand the robot upright" in low:
            msg = "Not upright enough: hold the duck so tilt <= 5 deg; if impossible, mount orientation may be wrong"
        elif "from nose-down" in low or "about the vertical axis" in low:
            msg = "Nose-down direction mismatch: module orientation may be reversed or rotated ~90 deg; check physical mount and retry"
        elif "no nose-down tip" in low or "within 20 s" in low:
            msg = "Timed out waiting for nose-down tilt: after holding upright, tilt nose down 15-30 deg within 20s and hold"
        elif "verify-imu-home cancelled" in low or "the calibration hold is not steady" in low:
            msg = "HOME hold interrupted or feedback stale: steady the duck and re-verify"
        elif "cancelled by relax" in low:
            msg = "Verification cancelled by E-Stop: support the duck and retry"
        else:
            msg = f"robotd verification failed: {text}"
        return msg + " (Nothing saved)"

    async def _hold(self, by=None):
        """Commissioning stand-hold (hold-home): ramps all 15 servos to HOME over 3s using Step 3 directions."""
        bus = await self._bus_once()
        if self._redo_busy or (self.job and not self.job.get("done") and not self.job.get("failed")):
            raise Refused("Sampling, verification, or pose switch already in progress; wait until finished")
        if bus.get("mode") != "commissioning":
            raise Refused("robotd is not in commissioning mode; use Stand on the Remote page in motion mode")
        if helper.imu_redo_state(root=self.root).get("active"):
            self._coarse_guard(bus)  # validates the redo phase and source before enabling torque
        if self.commit is None:
            raise Refused("Commit service is disabled on this duck; cannot resolve 15 joint directions")
        from .commit import Refused as CommitRefused
        try:
            dirs = self.commit.hold_directions()
        except CommitRefused as e:
            raise Refused(str(e)) from e
        self._redo_busy = True
        try:
            try:
                r = await self._rpc_once("robot.busCommand",
                                         {"action": "hold-home", "directions": dirs, "duration_ms": 3000},
                                         20.0, "hold-home")
            except (RpcError, Timeout, Unreachable) as e:
                text = getattr(e, "message", None) or str(e)
                low = text.lower()
                if "unknown variant" in low or "hold-home" in low and "unknown" in low:
                    msg = "Main firmware is too old for calibration-mode stand; update required"
                elif "still" in low and "from home" in low:
                    msg = f"Joint could not reach stand pose ({text}): likely reversed direction or obstructed; torque released. Check direction on the Servo page"
                elif "relax" in low:
                    msg = "Interrupted by torque-off while standing up"
                else:
                    msg = f"Failed to stand up: {text}"
                self.log.warn(f"Calibration-mode stand enable failed: {msg}", cat="IMU", raw=text, ctx={"by": by, "directions": dirs})
                raise Refused(msg) from e
            self.log.info("Calibration-mode stand enable: standing", cat="IMU", raw=json.dumps(r, ensure_ascii=False)[:400],
                          ctx={"by": by, "directions": dirs})
            return r
        finally:
            self._redo_busy = False

    async def _require_verification_contract(self, source):
        if source != "bus":
            return
        status = await self._rpc_once("robot.busCommand", {"action": "calibration-status"},
                                      5.0, "Check independent IMU verification support")
        if not isinstance(status, dict) or status.get("imu_verification_contract") != IMU_VERIFICATION_CONTRACT:
            raise Refused("Current robotd does not support ID200 independent upright / forward-lean verification: install the API43-compatible robotd and verify again. No verify or save request sent")

    @staticmethod
    def _require_verification_proof(reply, source):
        proof = reply.get("imu_check") if isinstance(reply, dict) else None
        upright = proof.get("upright") if isinstance(proof, dict) else None
        nose_down = proof.get("nose_down") if isinstance(proof, dict) else None
        residual = proof.get("residual_tilt_deg") if isinstance(proof, dict) else None
        tilt = nose_down.get("tilt_from_upright_deg") if isinstance(nose_down, dict) else None
        heading = nose_down.get("lean_heading_deg") if isinstance(nose_down, dict) else None
        if (not isinstance(upright, dict) or upright.get("imu_source") != source
                or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
                       for x in (residual, tilt, heading))
                or not 0 <= residual <= 5 or tilt < 12 or abs(heading) > 20):
            raise Refused("robotd did not return valid independent upright / forward-lean verification: source must match, upright residual 0-5 deg, forward lean >=12 deg, heading error <=20 deg. Cannot confirm; check API43-compatible robotd and verify again")

    async def _verify(self, args, by=None):
        bus = await self._bus_once()
        if bus.get("mode") == "motion":
            return await self._verify_from_home(bus, args, by)
        if bus.get("mode") != "commissioning":
            raise Refused("robotd is already in motion mode: stand up and use Standing Calibration instead")
        redo = self._coarse_guard(bus)
        if redo.get("active") and (bus.get("torque_state") != "off" or bus.get("hold")):
            raise Refused("Re-calibration sampling can keep standing posture; before verifying with robotd, support the robot and click 'Disable (Torque Off)' first. Captured samples are kept")
        self.view()  # invalidate a result from a different mode/source/session before consuming it
        source, _ = mount_of(bus, fresh_calib(self.rd))
        context = calibration_context(bus, source, redo)
        generation = self._context_generation
        verified_info = bus_imu_of(bus)
        verified_bus = {"imu_bus": verified_info} if verified_info is not None else {}
        if self._seen_context != context:
            self.last_result = None
        mount = args.get("mount_wxyz") or args.get("mount")
        if args.get("confirmed") is not True or not isinstance(mount, list) or len(mount) != 4:
            raise Refused("Check the box confirming the physical module mounting before sending to robotd for verification")
        last = self.last_result if isinstance(self.last_result, dict) else {}
        if not last.get("ok") or last.get("mode") != "coarse" or last.get("mount") != mount:
            raise Refused("This orientation was not computed from the current coarse calibration: solve again before verifying")
        await self._require_verification_contract(source)
        self.job = {"kind": "coarse", "phase": "verify", "title": "Verifying with robotd",
                    "msg": "Hold upright, then pitch nose forward 15-30 deg within 20s", "target_deg": 20}
        try:
            # fork command_timeout is 20s window + 10s; add 5s margin
            r = await self._rpc_once("robot.busCommand",
                                     {"action": "verify-imu", "confirmed": True, "mount_wxyz": mount},
                                     35.0, "Verify IMU orientation")
        except (RpcError, Timeout, Unreachable) as e:
            msg = self._translate_verify_error(e)
            self.job = {"kind": "coarse", "title": "robotd verification failed", "failed": True, "msg": msg,
                        "raw": getattr(e, "message", None) or str(e)}
            self.log.warn(f"IMU coarse verification failed: {msg}", cat="IMU", raw=str(e),
                          ctx={"by": by, "source": self.view().get("source")})
            raise Refused(msg) from e
        # A verifier reply alone is not success: read the exact active source and normalized mount back.
        try:
            self._require_verification_proof(r, source)
            status = await self._verified_readback(source, mount, transport=context[1], generation=generation)
            current_redo = helper.imu_redo_state(root=self.root)
            if redo.get("active"):
                if not current_redo.get("active") or current_redo.get("session_id") != redo.get("session_id"):
                    raise Refused("Redo session changed after IMU verification: did not switch back to motion mode")
                ok, result, detail = await asyncio.to_thread(
                    helper.imu_recalibrate, by or "Studio", "finish", session_id=redo["session_id"],
                    source=source, quat=self._normalized_quat(mount))
                if not ok:
                    raise Refused(helper.friendly(result, detail, "imu-recalibrate"))
                status = await self._verified_readback(source, mount,
                    motion=redo.get("original_mode") != "commissioning", transport=context[1])
            elif current_redo.get("active"):
                raise Refused("Another redo session appeared during IMU verification: not completed")
            from .commit import clear_pending, load_pending, pending_matches
            pending = load_pending(self.state_dir)
            if pending and not pending_matches(pending, source, verified_bus):
                try:
                    clear_pending(self.state_dir)
                except OSError as err:
                    raise Refused("robotd verified mounting orientation, but failed to clear old standing calibration: redo standing calibration before committing") from err
        except (RpcError, Timeout, Unreachable, Refused) as err:
            msg = str(err) if isinstance(err, Refused) else f"Calibration readback not confirmed after robotd verification: {err}"
            self.job = {"kind": "coarse", "title": "IMU verification or calibration readback unconfirmed", "failed": True, "msg": msg}
            self.last_result = None
            raise Refused(msg) from err
        self.last_result = None
        self.job = {"kind": "coarse", "title": "robotd verification passed", "done": True,
                    "msg": "Written to calibration file; IMU orientation confirmed"}
        self.log.info("IMU coarse verification passed: robotd wrote mounting orientation to calibration file", cat="IMU",
                      ctx={"by": by, "source": self.view().get("source"), "mount": mount})
        return {"ok": True, "note": "robotd verification passed; mounting orientation written to calibration file", "reply": r, "calibration": status}

    async def _verify_from_home(self, bus, args, by):
        """Use the running controller when it supports an in-place verification."""
        self._coarse_guard(bus)
        self.view()
        mount = args.get("mount_wxyz") or args.get("mount")
        last = self.last_result
        if (args.get("confirmed") is not True or not isinstance(last, dict) or not last.get("ok")
                or (last.get("mode") != "coarse" and last.get("live_verify") is not True)
                or last.get("mount") != mount):
            raise Refused("This orientation was not computed from the current calibration: sample and confirm again before verifying")
        if self._result_basis != self._sampling_basis(bus):
            self.last_result = None
            raise Refused("Live IMU source, bus board, previous orientation, or connection changed: re-sample upright and forward-lean")
        source, old_mount = mount_of(bus, fresh_calib(self.rd))
        transport = calibration_context(bus, source, {})[1]
        status = await self._rpc_once("robot.busCommand", {"action": "calibration-status"},
                                      5.0, "Check in-place IMU verification support")
        if isinstance(status, dict) and status.get("imu_home_verification_contract") == IMU_HOME_CONTRACT:
            if reason := self._motion_sampling(bus, source, {})["verify_reason"]:
                raise Refused(reason)
            actual = mount_of(bus, status)
            if status.get("imu_source") != source or actual != (source, old_mount):
                if actual[0] == source and actual[1] and status.get("imu_source") == source:
                    self.rd.calib.set(status)
                self.last_result = None
                raise Refused("robotd's previous IMU orientation changed: re-sample upright and forward-lean")
            self.job = {"kind": "coarse", "phase": "verify", "title": "Verifying IMU orientation while standing",
                        "msg": "Hold upright, then pitch nose forward 15-30 deg within 20s", "target_deg": 20}
            self._redo_busy = True
            self.home_verification_task = asyncio.current_task()
            try:
                try:
                    reply = await self._rpc_once(
                        "robot.busCommand",
                        {"action": "verify-imu-home", "confirmed": True, "mount_wxyz": mount,
                         "expected_mount_wxyz": old_mount["quat"]},
                        35.0, "Verify IMU orientation from HOME")
                except (RpcError, Timeout, Unreachable) as err:
                    if isinstance(err, RpcError) and "timed out" not in str(err).lower():
                        raise Refused(self._translate_verify_error(err)) from err
                    raise Refused("Connection lost before receiving verification result: reconnect and check saved orientation") from err
                self._require_verification_proof(reply, source)
                status = await self._verified_readback(source, mount, motion=True, transport=transport)
            except asyncio.CancelledError:
                self.job = {"kind": "coarse", "title": "IMU verification connection closed", "failed": True,
                            "msg": "Reconnect and check the currently saved orientation"}
                raise
            except (RpcError, Timeout, Unreachable, Refused) as err:
                # The request can lose its reply after the on-board commit.
                # Reconcile a live readback without claiming an unreceived proof.
                try:
                    await self._verified_readback(source, mount, motion=True, transport=transport)
                except (RpcError, Timeout, Unreachable, Refused):
                    pass
                self.job = {"kind": "coarse", "title": "IMU verification or readback unconfirmed", "failed": True, "msg": str(err)}
                raise Refused(str(err)) from err
            finally:
                self.home_verification_task = None
                self._redo_busy = False
            self.last_result = None
            from .commit import clear_pending, load_pending, pending_matches
            pending = load_pending(self.state_dir)
            if pending_matches(pending, source, bus):
                try:
                    clear_pending(self.state_dir)
                except OSError as err:
                    self.log.warn("New IMU orientation saved, but failed to clear old fine-calibration draft", cat="IMU", raw=str(err))
            note = "Mounting orientation verified, saved, and active"
            if reply.get("home_held") is True:
                note += ", keeping current standing posture"
            self.job = {"kind": "coarse", "title": "robotd verification passed", "done": True, "msg": note}
            self.log.info(note, cat="IMU", ctx={"by": by, "source": source, "mount": mount})
            return {"ok": True, "note": note, "reply": reply, "calibration": status}
        if last.get("live_verify") is True:
            raise Refused("Restore HOME posture and keep policy stopped before verifying and saving this fine calibration")
        # The live reply supersedes any previously advertised HOME capability.
        # _coarse_guard already requires a confirmed hold or a confirmed unload.
        if self._motion_sampling(bus, source, {})["hold_ready"]:
            raise Refused("Upright and forward-lean sampling can keep HOME; support the robot and disable torque before verify & write (captured samples are kept)")
        await self._require_verification_contract(source)
        if not helper.available():
            raise Refused("Verification write requires updated root helper and sudoers")
        basis = self._sampling_basis(bus)
        self._redo_busy = True
        self.job = {"kind": "coarse", "title": "Entering orientation verification", "msg": "Captured samples kept; backing up old orientation and switching to verification mode"}
        try:
            ok, result, detail = await asyncio.to_thread(helper.imu_recalibrate, by or "Studio", "enter", confirmed=True)
            if not ok:
                raise Refused(helper.friendly(result, detail, "imu-recalibrate"))
            deadline = time.monotonic() + 8
            while hasattr(self.rd, "present") and self.rd.present is not True:
                if time.monotonic() >= deadline:
                    raise Refused("Entered verification mode; waiting for Studio to reconnect to robotd")
                await asyncio.sleep(.1)
            current = await self._bus_once()
            status = await self._rpc_once("robot.busCommand", {"action": "calibration-status"}, 5.0, "Verify previous orientation before check")
            now_source, now_mount = mount_of(current, status)
            if (current.get("mode") != "commissioning" or now_source != source or now_mount != old_mount
                    or calibration_context(current, source, {})[1] != transport):
                self.last_result = None
                raise Refused("IMU source, bus board, or previous orientation mismatched after entering verification mode: write aborted, please re-sample")
            self.rd.bus.set(current)
            self.rd.calib.set(status)
            self._result_basis = None
            self.view()  # expected mode/session transition invalidates the old cached context
            self.last_result = last
        except BaseException as err:
            self.job = {"kind": "coarse", "title": "Orientation verification did not start", "failed": True, "msg": str(err)}
            # The existing persistent transaction owns recovery if entering succeeded.
            if helper.imu_redo_state(root=self.root).get("active"):
                self.last_result = None
            elif self._sampling_basis(self.rd.bus.value) != basis:
                self.last_result = None
            raise
        finally:
            self._redo_busy = False
        return await self._verify(args, by)

    @staticmethod
    def _normalized_quat(quat):
        if (not isinstance(quat, list) or len(quat) != 4
                or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in quat)):
            raise Refused("Invalid IMU quaternion readback")
        norm = math.sqrt(sum(x*x for x in quat))
        if norm < 1e-9:
            raise Refused("Invalid IMU quaternion readback")
        return [x/norm for x in quat]

    async def _verified_readback(self, source, mount, motion=False, transport=None, generation=None):
        status = await self._rpc_once("robot.busCommand", {"action": "calibration-status"}, 5.0, "Re-read calibration status")
        bus = await self._bus_once()
        if generation is not None and self._context_generation != generation:
            raise Refused("Context changed during IMU verification: re-sample and verify again")
        if calibration_context(bus, source, {})[1] != transport:
            raise Refused("Bus board changed during IMU verification: re-sample and verify again")
        if not isinstance(status, dict) or status.get("imu_source") != source:
            raise Refused("robotd current IMU source did not read back accurately")
        got_source, got_mount = mount_of(bus, status)
        if got_source != source or not got_mount or got_mount.get("verified") is not True:
            raise Refused("robotd IMU source / verified flag did not read back accurately")
        expected, got = self._normalized_quat(mount), self._normalized_quat(got_mount.get("quat"))
        if not all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8) for a, b in zip(got_mount["quat"], got)):
            raise Refused("robotd returned an unnormalized IMU quaternion")
        if not all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8) for a, b in zip(expected, got)):
            raise Refused("robotd IMU orientation readback does not match the verified orientation")
        cal = status.get("calibration") if isinstance(status.get("calibration"), dict) else {}
        stored = ({"quat": cal.get("imu_mount_quat"), "verified": cal.get("imu_mount_verified")}
                  if source == "bus" else (cal.get("imu_mounts") or {}).get(source) or {})
        raw = stored.get("quat")
        if (stored.get("verified") is not True or not isinstance(raw, list) or len(raw) != 4
                or not all(isinstance(a, (int, float)) and not isinstance(a, bool)
                           and math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8) for a, b in zip(raw, expected))):
            raise Refused("robotd raw calibration field readback for current IMU does not match")
        if motion and (bus.get("mode") != "motion" or bus.get("phase") != "control"):
            raise Refused("IMU orientation verified, but motion mode readback is not yet confirmed")
        if hasattr(self.rd, "calib"):
            self.rd.calib.set(status)
        return status

    def _import_mount(self, args, by=None):
        """Import an existing mounting orientation: accepts only IMU type and quaternion (extracted from file by frontend; other file contents are not sent or stored).
        Staged as pending orientation (same imu-pending.json as standing calibration), written to the duck only in Onboarding Step 3 'Write to robotd'; nothing is written to the duck here."""
        from .commit import CHIP_CN, IMU_SOURCES, save_pending
        from .commit import Refused as CommitRefused
        view = self.view()
        if view.get("profile_error"):
            raise Refused(view["profile_error"])
        current = view.get("source")
        if current not in IMU_SOURCES:
            raise Refused("robotd has not reported which IMU is active yet: wait a few seconds and retry")
        if helper.imu_redo_state(root=self.root).get("active"):
            raise Refused("Re-calibration is in progress: cancel re-calibration before importing")
        source, quat = args.get("source"), args.get("quat")
        if not isinstance(source, str) or source not in IMU_SOURCES:
            raise Refused("File does not specify which IMU type the orientation is for")
        if source != current:
            raise Refused(f"File contains orientation for '{CHIP_CN[source]}', but this duck currently uses '{CHIP_CN[current]}': cannot import")
        if (not isinstance(quat, list) or len(quat) != 4
                or any(isinstance(c, bool) or not isinstance(c, (int, float)) or abs(c) > 2 or not math.isfinite(c) for c in quat)):
            raise Refused("Orientation in file is not 4 numbers")
        norm = math.sqrt(sum(c * c for c in quat))
        if abs(norm - 1.0) > 1e-3:
            raise Refused("Orientation in file is not a unit quaternion")
        unit = [c / norm for c in quat]
        if self.commit is None:
            row = save_pending(self.state_dir, source, unit, {"imported": True}, by, imu_bus=view.get("imu_bus"))
        else:
            try:
                row = self.commit.stage_import(source, unit, by, view.get("imu_bus"))
            except CommitRefused as exc:
                raise Refused(str(exc)) from exc
        self.last_result = None                 # Previous un-written calculation result no longer applies: pending is now the imported one
        self._result_basis = None
        self.log.info("Imported IMU mounting orientation (not yet written to robotd)", cat="IMU", raw=json.dumps(row["quat"]),
                      ctx={"by": by, "source": source})
        return {"ok": True, "source": source, "mount": row["quat"], "standard": imu_calib.describe_mount(row["quat"])}

    async def handle(self, args, by=None):
        op = args.get("op")
        if self._verify_lock.locked():
            raise Refused("Mounting orientation is being verified: wait for the current operation to finish (torque off is still available in emergency)")
        if (op in ("coarse-capture", "solve", "verify", "redo-start", "profile-select", "import-mount")
                and self.job and not self.job.get("done") and not self.job.get("failed")):
            raise Refused("Previous sampling or calibration operation is still running: wait for it to finish")
        if op in ("redo-start", "redo-cancel", "profile-select"):
            return await self._redo_op(args, by)
        if op == "import-mount":
            return self._import_mount(args, by)
        if op == "solve":
            samples = args.get("samples")
            if not isinstance(samples, list) or not samples:
                raise Refused("No samples received")
            if len(samples) > MAX_SAMPLES:
                raise Refused(f"Too many samples ({len(samples)}, max {MAX_SAMPLES})")
            mode = args.get("mode", "full")
            if mode not in ("full", "coarse"):
                raise Refused(f"Unknown calibration mode: {mode}")
            # Old orientation only trusted from robotd (never from frontend); rules / home_gravity from frontend are also ignored (audit A3)
            view = self.view()
            if view.get("profile_error"):
                raise Refused(view["profile_error"])
            context = self._seen_context
            generation = self._context_generation
            redo = helper.imu_redo_state(root=self.root)
            if redo.get("active") and mode != "coarse":
                raise Refused("During initial IMU re-calibration, use Upright / Forward-Lean coarse calibration (support with torque off, or enable standing posture and wait until steady)")
            solve_basis = None
            live_full = mode == "full" and "basis_mount_wxyz" in args
            if mode == "coarse":
                bus = await self._bus_once()
                self._coarse_guard(bus)
                if bus.get("mode") == "motion":
                    solve_basis = self._sampling_basis(bus)
                    if self._capture_basis != solve_basis:
                        self.last_result = None
                        raise Refused("Mounting orientation or robotd connection changed since sampling: re-sample upright and forward-lean")
            old = (view["mount"] or {}).get("quat")
            if live_full:
                bus = await self._bus_once()
                self._coarse_guard(bus)
                sampling = self._motion_sampling(bus, view["source"], redo)
                if not sampling["live_verify"] or sampling["verify_reason"]:
                    raise Refused("Restore HOME posture and keep policy stopped before solving this fine calibration")
                solve_basis = self._sampling_basis(bus)
                if args["basis_mount_wxyz"] != old or solve_basis[1] != tuple(old or ()):
                    self.last_result = None
                    raise Refused("Previous IMU orientation changed during sampling: re-sample standing calibration")
            old_verified = bool((view["mount"] or {}).get("verified"))
            res = await asyncio.to_thread(lambda: imu_calib.solve(
                samples, frame="body", old_mount=old, mode=mode, old_verified=old_verified))
            self.view()
            if (self._seen_context != context or self._context_generation != generation
                    or solve_basis is not None and self._sampling_basis(self.rd.bus.value) != solve_basis):
                self.last_result = None
                raise Refused("IMU source / bus board / mode / session changed after sampling: re-sample and solve again")
            self.last_result = res
            self._result_basis = solve_basis
            detail = json.dumps({k: res.get(k) for k in ("mount", "quality", "confirm", "parts", "redo") if k in res},
                                ensure_ascii=False)[:1500]
            if res.get("ok"):
                self.log.info("IMU mounting orientation solved (not yet written to robotd)", cat="IMU", raw=detail,
                              ctx={"by": by, "source": view["source"], "mode": mode, "old_verified": old_verified})
                if mode == "full":
                    if live_full:
                        res["live_verify"] = True
                    else:
                        try:
                            from .commit import save_pending
                            save_pending(self.state_dir, view["source"], res.get("mount"), res.get("quality"), by,
                                         imu_bus=view.get("imu_bus"))
                        except Exception as e:  # noqa: BLE001 -- saving failure doesn't prevent returning the result to UI
                            self.log.error("Failed to save pending IMU orientation", cat="IMU", raw=f"{type(e).__name__}: {e}", exc=True,
                                           hint="It will be saved again on re-calibration; export diagnostics if this persists")
                    res["check"] = self._check(res, view, old, old_verified, by)
            else:
                redo = ", ".join(res.get("redo") or []) or "—"
                self.log.warn(f"IMU calibration failed: {res.get('reason')} (redo: {redo})", cat="IMU", raw=detail, step="Solve Mounting",
                              ctx={"by": by, "source": view["source"], "mode": mode},
                              hint="Follow the on-screen prompt to redo that step; export diagnostics if it still fails")
            return res
        if op == "hold":
            return await self._hold(by)
        if op == "coarse-capture":
            try:
                return await self._capture(args, by)
            except (Exception, asyncio.CancelledError) as err:
                self.job = {"kind": "coarse", "title": "Coarse calibration sampling incomplete", "failed": True,
                            "msg": str(err) or "Sampling aborted: re-sample or cancel re-calibration"}
                raise
        if op == "verify":
            async with self._verify_lock:
                return await self._verify(args, by)
        if op in ("save", "coarse", "detect", "set_chip"):
            if op == "save":
                raise Refused("New IMU orientation is staged: go to Onboarding Step 3 and click 'Write to robotd'")
            if op == "coarse":
                raise Refused("For coarse calibration, use the 3-step Coarse buttons: Sample Upright, Sample Forward-Lean, Verify with robotd")
            raise Refused(ON_DUCK)
        raise Refused(f"Unknown command: {op}")

    def _check(self, res, view, old, old_verified, by):
        """After full standing calibration (solve full, ok): compare against robotd's active orientation; if close enough, record 'checked' in onboarding (audit O4).
        Records only the check, **writes nothing to the duck** (writing requires connecting to the duck). Returns {recorded, diff_deg, tilt_deg, why} for UI."""
        parts = res.get("parts") or {}
        diff, tilt = parts.get("total_deg"), parts.get("stand_tilt_deg")
        why = None
        if not old or not old_verified:
            why = "robotd's stored orientation is not verified yet (verified is not true): run coarse calibration and write first before checking"
        elif not isinstance(diff, (int, float)) or not isinstance(tilt, (int, float)):
            why = "Angular difference was not computed this time"
        elif tilt > imu_calib.RULES["stand_confirm_deg"]:
            why = (f"Standing tilt {tilt:.1f} deg (must be <= {imu_calib.RULES['stand_confirm_deg']:.0f} deg): likely uncalibrated leg zeros, "
                   "uneven surface, or touched while stationary")
        elif diff >= CHECK_MAX_DIFF_DEG:
            why = f"Difference from robotd active orientation is {diff:.1f} deg (must be < {CHECK_MAX_DIFF_DEG:g} deg): write the new orientation (requires connecting to the duck)"
        ok = why is None
        out = {"recorded": False, "ok": ok, "diff_deg": diff, "tilt_deg": tilt, "why": why}
        hook = self.on_check
        # Do not record if orientation was unverified (that checklist step is 'not done'); if diff is large, record it so checklist shows 'redo needed'
        if hook is not None and old and old_verified and isinstance(diff, (int, float)):
            try:
                out["recorded"] = bool(hook({"source": view["source"], "quat": old, "verified": True,
                                             "diff_deg": diff, "tilt_deg": tilt, "ok": ok, "by": by}))
            except Exception as e:  # noqa: BLE001 -- recording failure doesn't block returning result
                self.log.error("Failed to record 'IMU checked'", cat="IMU", raw=f"{type(e).__name__}: {e}", exc=True,
                               hint="Studio issue; calibration result is still usable")
        if ok:
            self.log.info(f"IMU orientation checked: {diff:.1f} deg diff from robotd, standing tilt {tilt:.1f} deg", cat="IMU",
                          ctx={"by": by, "source": view["source"]})
        else:
            self.log.info(f"IMU orientation did not pass check: {why}", cat="IMU", ctx={"by": by, "source": view["source"]})
        return out
