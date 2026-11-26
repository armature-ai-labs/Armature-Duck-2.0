"""Call the privileged root helper (/usr/local/sbin/microduck-helper) over sudo -n."""
import json
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import time

HELPER = "/usr/local/sbin/microduck-helper"
FLAG = "/run/microduck-helper/bus-held"
LAST_RESULT = "/run/microduck-helper/last-result.json"
IMU_REDO_MANIFEST = "/var/lib/microduck-helper/imu-recalibrate/manifest.json"
NATIVE_UPDATE_JOURNAL = "/var/lib/microduck-helper/native-update/journal.json"
NATIVE_UPDATE_ACTIVE = "/var/lib/microduck-helper/native-update/active"
CONTROLLER_UPDATE_JOURNAL = "/var/lib/microduck-helper/controller-update/journal.json"
CONTROLLER_UPDATE_PREPARED = "/var/lib/microduck-helper/controller-update/prepared.json"
NATIVE_UPDATE_EXCLUSIONS = (NATIVE_UPDATE_ACTIVE, "/run/microduck-helper/native-update-restore",
    "/etc/systemd/system/robotd.service.d/zzzzzzz-microduck-native-update.conf", *(
        "/etc/systemd/system/"+unit+".service.d/zzzzzzz-microduck-native-update.conf"
        for unit in ("padd", "gamesir-xboxd", "duck-echo")))

_avail = None


def flag_present():
    """Return True if the bus-held guard flag exists on Linux."""
    if not sys.platform.startswith("linux"):
        return False
    return os.path.exists(FLAG)


def available(refresh=False):
    """Check whether microduck-helper exists and is runnable via sudo -n."""
    global _avail
    if refresh:
        _avail = None
    if _avail is not None:
        return _avail
    if not sys.platform.startswith("linux") or not os.path.exists(HELPER):
        _avail = False
        return False
    try:
        r = subprocess.run(["sudo", "-n", HELPER, "bus-nope"], capture_output=True, text=True, timeout=5)
        _avail = r.returncode == 2
    except (OSError, subprocess.TimeoutExpired):
        _avail = False
    return _avail


def _call(verb, payload, timeout, on_line=None):
    """Run `sudo -n microduck-helper <verb>` with JSON on stdin. Returns (ok, result, detail)."""
    data = json.dumps(payload, ensure_ascii=False)
    if on_line is None:
        try:
            proc = subprocess.run(["sudo", "-n", HELPER, verb], input=data,
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{type(e).__name__}", None
        result, detail = None, None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("step") == "done":
                d = obj.get("detail") or {}
                result, detail = d.get("result"), d
        if result is None:
            err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            return False, err or f"exit {proc.returncode}", None
        return detail.get("exit") == 0, result, detail
    return _call_stream(verb, data, timeout, on_line)


def _call_stream(verb, data, timeout, on_line):
    """Run `sudo -n microduck-helper <verb>` and stream progress JSON lines to `on_line`."""
    try:
        proc = subprocess.Popen(["sudo", "-n", HELPER, verb], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as e:
        return False, f"{type(e).__name__}", None
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except OSError:
        pass
    lines = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    result, detail, noise = None, None, []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            try:
                proc.kill()
            except OSError:
                pass
            return False, "TimeoutExpired", None
        try:
            line = lines.get(timeout=min(0.2, left))
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            noise.append(line)
            continue
        if obj.get("step") == "done":
            d = obj.get("detail") or {}
            result, detail = d.get("result"), d
        elif on_line is not None:
            on_line(obj)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    if result is None:
        return False, "\n".join(noise[-5:]) or f"exit {proc.returncode}", None
    return detail.get("exit") == 0, result, detail


def bus_take(by, allow_seated=False, timeout=30):
    """Stop robotd and hand the servo bus to Studio (without relaxing servos)."""
    return _call("bus-take", {"by": by, "allow_seated": allow_seated}, timeout)


def bus_give_back(by, *, soft=True, marker=None, timeout=60):
    """Return the servo bus and restart robotd."""
    payload = {"by": by, "soft": True} if soft else {"by": by, "marker": marker}
    return _call("bus-give-back", payload, timeout)


def bus_abort(by, timeout=10):
    """Abort bus handback, stop robotd, and return bus control to Studio."""
    return _call("bus-abort", {"by": by}, timeout)


def pad_pair(by, mac=None, timeout=60):
    """Run `robotctl pad pair --json` as root via the helper."""
    payload = {"by": by, "timeout": timeout}
    if mac:
        payload["mac"] = mac
    return _call("pad-pair", payload, timeout + 45)


def pad_forget(by, mac, timeout=60):
    """Run `robotctl pad forget MAC` as root via the helper."""
    return _call("pad-forget", {"by": by, "mac": mac}, timeout)


def wifi_hotspot_down(by, ssid=None, timeout=30):
    """Bring down the robot's hotspot before connecting to Wi-Fi."""
    payload = {"by": by}
    if ssid:
        payload["ssid"] = ssid
    return _call("wifi-hotspot-down", payload, timeout)


def wifi_hotspot_up(by, timeout=40):
    """Bring up the robot's Microduck-Hotspot AP."""
    return _call("wifi-hotspot-up", {"by": by}, timeout)


def calib_read(by=None, timeout=10):
    """Read current calibration file and active daemon mode (calib-read)."""
    payload = {"by": by} if by else {}
    return _call("calib-read", payload, timeout)


def imu_recalibrate(by, action, *, confirmed=None, session_id=None, source=None, quat=None, profile=None, timeout=180):
    """Fixed root verb; commands, services and file paths are never accepted."""
    payload = {"by": by, "action": action}
    for name, value in (("confirmed", confirmed), ("session_id", session_id), ("source", source), ("quat", quat), ("profile", profile)):
        if value is not None:
            payload[name] = value
    return _call("imu-recalibrate", payload, timeout)


def imu_redo_state(*, root="/"):
    """Read the persistent root-owned public manifest. No sudo in the 10 Hz view loop."""
    empty = {"active": False, "phase": "idle", "session_id": None, "source": None}
    if root == "/" and not sys.platform.startswith("linux"):
        return empty
    path = os.path.join(root, IMU_REDO_MANIFEST.lstrip("/"))
    try:
        for target in (os.path.dirname(os.path.dirname(os.path.dirname(path))), os.path.dirname(os.path.dirname(path)),
                       os.path.dirname(path), path):
            st = os.lstat(target)
            if stat.S_ISLNK(st.st_mode) or (root == "/" and (st.st_uid != 0 or st.st_mode & 0o022)):
                raise ValueError("Unsafe ownership or permissions on session manifest")
        if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            raise ValueError("Invalid session manifest format")
        with open(path, encoding="utf-8") as stream:
            m = json.load(stream)
        if (m.get("schema") != "microduck-imu-recalibrate/1" or type(m.get("active")) is not bool
                or not re.fullmatch(r"[0-9a-f]{32}", m.get("session_id", ""))
                or m.get("source") not in ("bus", "bno08x", "lsm6dsv16x")):
            raise ValueError("Invalid session manifest content")
        if not m["active"]:
            return empty
        return {key: m.get(key) for key in ("active", "phase", "session_id", "source", "reason", "expected_quat", "profile", "original_mode")}
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, TypeError, AttributeError) as err:
        return {**empty, "active": True, "phase": "recovery_required", "reason": f"Failed to read IMU recalibration session: {err}"}


def robotd_update(by, action, *, confirmed=None, timeout=180):
    """Separate fixed native update stage; no caller paths, binaries or commands."""
    payload = {"by": by, "action": action}
    if confirmed is not None:
        payload["confirmed"] = confirmed
    return _call("robotd-update", payload, timeout)


def native_update_state(*, root="/"):
    if root == "/" and not sys.platform.startswith("linux"):
        return {"active": False, "phase": "idle", "running_verified": False}
    legacy=_native_update_state(root=root,journal=NATIVE_UPDATE_JOURNAL)
    path=CONTROLLER_UPDATE_PREPARED
    if not os.path.lexists(os.path.join(root,path.lstrip('/'))):path=CONTROLLER_UPDATE_JOURNAL
    if not os.path.lexists(os.path.join(root,path.lstrip('/'))):return legacy
    bundle=_native_update_state(root=root,journal=path)
    if legacy.get('active'):bundle['active']=True
    return bundle


def _native_update_state(*,root,journal):
    """Bounded persistent motion guard. Malformed/unreadable state fails closed."""
    empty = {"active": False, "phase": "idle", "running_verified": False}
    path = os.path.join(root, journal.lstrip("/"))
    exclusions = any(os.path.lexists(os.path.join(root,p.lstrip('/'))) for p in NATIVE_UPDATE_EXCLUSIONS)
    try:
        parts = journal.strip("/").split("/")
        for index in range(1, len(parts)+1):
            target = os.path.join(root, *parts[:index])
            st = os.lstat(target)
            if ((index < len(parts) and not stat.S_ISDIR(st.st_mode))
                    or (index == len(parts) and not stat.S_ISREG(st.st_mode))
                    or (root == "/" and (st.st_uid != 0 or st.st_mode & 0o022))):
                raise ValueError("Unsafe ownership or permissions on update journal")
        if st.st_size > 65536:
            raise ValueError("Update journal exceeds size limit")
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        bundle=journal in (CONTROLLER_UPDATE_JOURNAL,CONTROLLER_UPDATE_PREPARED)
        phases=("prepared","guarding","replacing","verifying","restoring","completed","restored","recovery_required") if bundle else (
            "prepared","guarding","guarded","replacing","replaced","verified_commissioning","restoring_normal","verified_motion",
            "factory_handoff_pending","completed","rolled_back","recovering","old_restored","recovery_required")
        if (not isinstance(value, dict) or value.get("schema") != ("microduck-controller-update/1" if bundle else "microduck-native-update/1")
                or type(value.get("active")) is not bool
                or type(value.get("restore_pending", False)) is not bool
                or type(value.get("cleanup_pending", False)) is not bool
                or not re.fullmatch(r"[0-9a-f]{32}", value.get("transaction_id", ""))
                or value.get("phase") not in phases
                or (value.get("reason") is not None and (not isinstance(value["reason"], str) or len(value["reason"]) > 300))):
            raise ValueError("Invalid update journal format")
        public = ("active", "phase", "transaction_id", "available", "installed_sha256", "target_sha256", "running_verified", "mode", "reason","bundle_schema")
        result = {key:value.get(key) for key in public if key in value}
        result['active'] = result['active'] or value.get('restore_pending') is True or value.get('cleanup_pending') is True or exclusions
        return result
    except FileNotFoundError:
        if exclusions:
            return {**empty,"active":True,"phase":"recovery_required","reason":"Controller update guard present without journal"}
        return empty
    except (OSError, ValueError, TypeError, AttributeError):
        return {**empty, "active": True, "phase": "recovery_required", "reason": "Controller update journal unreadable; recover update first"}


def ssh_status(timeout=10):
    """Query SSH password login status, sshd service, and registered keys."""
    return _call("ssh-status", {}, timeout)


def ssh_password(by, on, password=None, timeout=40):
    """Enable or disable SSH password login for the radxa user via stdin."""
    payload = {"by": by, "on": bool(on)}
    if on:
        payload["password"] = password
    return _call("ssh-password", payload, timeout)


def sensors_apply(by, camera=None, tof=None, timeout=90):
    """Switch camera video source (camera/test) or enable tofd at boot."""
    payload = {"by": by}
    if camera is not None:
        payload["camera"] = camera
    if tof:
        payload["tof"] = True
    return _call("sensors-apply", payload, timeout)


def skill_table(by, timeout=30):
    """Rewrite legacy inline policy.skill table to [[policy.skill]] sections."""
    return _call("skill-table", {"by": by}, timeout)


def last_result():
    """Read the last completed helper result JSON, or None if unavailable."""
    try:
        with open(LAST_RESULT, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def calib_commit(by, joints, imu, dry_run=False, timeout=180, on_line=None, *, joint_mode="recorded"):
    """Commit servo zero/direction and IMU mount calibration into robotd config."""
    payload = {"by": by, "joints": joints, "imu": imu, "dry_run": bool(dry_run), "joint_mode": joint_mode}
    return _call("calib-commit", payload, timeout, on_line=on_line)


_COMMIT_WHY = {
    "committed": "Calibration committed; controller switched to motion mode",
    "preview": "Preview generated (no files modified)",
    "bus_held": "Servo bus is still held by the Servos page; hand back control first",
    "no_calibration": "Controller calibration file not found on robot",
    "bad_calibration": "Current controller calibration file is unreadable",
    "joint_mismatch": "15 servo joints do not match the controller calibration file",
    "invalid": "Calibration payload failed validation",
    "bad_toml": "Written calibration TOML failed readback verification",
    "restart_failed": "Calibration written, but controller failed to restart; restored original file",
    "not_motion": "Calibration written, but controller did not enter motion mode within 30s",
    "calib_rejected": "Calibration failed controller validation checks",
    "bus_unavailable": "Controller failed to open servo bus",
    "readback_mismatch": "Controller entered motion mode, but readback calibration differed",
    "write_failed": "Write interrupted mid-way; restored original file",
    "foreign_commissioning": "Commissioning mode was not set by the stock image; refusing automatic switch",
    "refused": "Controller refused write while standing or walking",
    "backup_failed": "Failed to back up existing calibration file",
    "read_failed": "Failed to read current calibration file",
    "bad_verb": "Root helper is outdated; reinstall via deploy/install.sh",
    "busy": "Root helper is busy with another operation",
    "detach_failed": "Failed to launch helper in an isolated systemd unit",
    "TimeoutExpired": "Timed out after 180s waiting for helper completion",
}

_TAKE_WHY = {
    "taken": "Controller stopped; servo bus handed to Studio",
    "refused": "Controller refused stop (policy active, homing, or seated guard)",
    "no_guard": "Controller bus-held drop-in guard missing; refusing stop",
    "busy": "Root helper is busy with another board or bus operation",
    "stop_failed": "Failed to stop controller daemon",
    "port_busy": "Serial port is still in use",
}

_GIVE_WHY = {
    "soft": "Controller restarted",
    "soft_failed": "Controller failed to start",
    "adopted": "Controller adopted held servo state without relaxing",
    "limp": "Controller ignored handover marker and relaxed servos",
    "retake": "Controller failed to start; bus reclaimed by Studio",
    "retake_failed": "Failed to reclaim servo bus",
    "aborted": "Handover aborted (E-Stop pressed)",
    "refused": "Serial port is still in use",
    "no_session": "No active servo session (bus-held flag missing)",
    "detach_failed": "Handover failed to launch (systemd-run error)",
}

_PAD_WHY = {
    "paired": "Paired",
    "pair_failed": "Pairing failed (robotctl error)",
    "no_robotctl": "robotctl not found (/usr/local/bin/robotctl)",
    "timeout": "robotctl timed out and stopped",
    "bad_output": "Unrecognized robotctl output",
    "forgotten": "Device forgotten",
    "forget_failed": "Failed to forget device",
    "busy": "Root helper is busy with another gamepad operation",
}

_WIFI_WHY = {
    "hotspot_down": "Hotspot disabled",
    "already_down": "Hotspot was already disabled",
    "down_failed": "Failed to disable hotspot",
    "hotspot_up": "Hotspot enabled",
    "already_up": "Hotspot was already active",
    "up_failed": "Failed to start hotspot",
    "timeout": "Timed out",
    "nmcli_failed": "Failed to query network connections (nmcli error)",
    "busy": "Root helper is busy with another Wi-Fi operation",
    "bad_verb": "Root helper is outdated; reinstall via deploy/install.sh",
}

_COMMON = {
    "bad_args": "Invalid arguments",
    "bad_marker": "Invalid handover marker",
    "bad_verb": "Unknown helper command",
    "not_root": "Root privileges required",
    "error": "Internal helper error",
}


def friendly(result, detail, verb):
    """Map helper result codes and detail messages to readable UI text."""
    if verb == "imu-recalibrate":
        if result in ("bad_verb", "denied", "not_root") or "sudo" in str(result).lower():
            return "IMU recalibration requires root helper v0.7.0+ and updated sudoers; run deploy/install.sh"
        return ((detail or {}).get("reason") or (detail or {}).get("msg") or
                f"IMU recalibration switch incomplete ({result}); session can continue or be cancelled")
    if verb in ("pad-pair", "pad-forget"):
        table = _PAD_WHY
    elif verb in ("wifi-hotspot-down", "wifi-hotspot-up"):
        table = _WIFI_WHY
    elif verb == "calib-commit":
        table = _COMMIT_WHY
    else:
        table = _TAKE_WHY if verb == "bus-take" else _GIVE_WHY
    msg = detail.get("msg") if isinstance(detail, dict) else None
    if verb in ("ssh-status", "ssh-password"):
        if msg and result not in ("ssh_status", "ssh_password_on", "ssh_password_off"):
            return msg
        if result == "bad_verb":
            return "Root helper is outdated and lacks SSH support; reinstall deploy/install.sh (v0.6.0+)"
        return _COMMON.get(result) or (result or "failed")
    if msg and verb == "calib-commit" and result not in ("committed", "preview"):
        return msg
    if msg and verb == "bus-give-back" and isinstance(detail, dict) and detail.get("still_starting"):
        return msg
    text = table.get(result) or _COMMON.get(result) or (result or "failed")
    if result == "pair_failed" and isinstance(detail, dict) and detail.get("reason"):
        text = f"{text} ({detail['reason']})"
    if msg and msg not in text:
        text = f"{text}: {msg}"
    return text
