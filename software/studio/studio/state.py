"""Summarize raw robotd / configd data into a compact payload pushed to the UI (all arrays; missing fields are treated as unknown).

Posture determination checks mode first, then phase:
    absent         robotd socket missing / connection refused
    starting       phase is configuring_bus / waiting_for_bus / configuration_error (also startup reports before first
                   commissioning read succeeds; older forks stay here indefinitely, so after 30s the UI switches to
                   'stuck starting' via starting_secs)
    commissioning  live snapshot when mode == "commissioning" (ready / moving / read_error / stop_unconfirmed / power_pending:
                   robotd is up, read-only; init/enable/do are rejected)
    sitting        robot.policies.sitting
    limp           native.torque_state_confirmed == False
    walking        policy_enabled and move.applied is non-zero
    policy         policy_enabled (standing with policy running)
    holding        torque on, homed, home_start_guard.ready, policy off (holding HOME after init; joints are in position)
    homing         torque on, still moving to HOME
    unknown        insufficient data

Outputs status fields only (action buttons are computed separately).
"""
import math
import time

from .estop import STARTING_LONG_S, STARTING_PHASES
from .startup import startup_todo
from .startup_status import imu_context_current, imu_diagnostic, servo_observations, startup_wait

JOINT_NAMES = ["left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee", "left_ankle",
               "neck_pitch", "head_pitch", "head_yaw", "head_roll", "mouth",
               "right_hip_yaw", "right_hip_roll", "right_hip_pitch", "right_knee", "right_ankle"]
JOINT_CN = {"left_hip_yaw": "Left Hip Yaw", "left_hip_roll": "Left Hip Roll", "left_hip_pitch": "Left Hip Pitch", "left_knee": "Left Knee",
            "left_ankle": "Left Ankle", "neck_pitch": "Neck Pitch", "head_pitch": "Head Pitch", "head_yaw": "Head Yaw", "head_roll": "Head Roll",
            "mouth": "Mouth", "right_hip_yaw": "Right Hip Yaw", "right_hip_roll": "Right Hip Roll", "right_hip_pitch": "Right Hip Pitch",
            "right_knee": "Right Knee", "right_ankle": "Right Ankle"}
STALE_S = 3.0
MAX_AGE = {"health": 8.0, "policies": 6.0, "state": 1.5, "pad": 15.0}
CPU_WARN_C = 80


def _fresh(sample, key, now):
    age = sample.age(now)
    return sample.value if age is not None and age <= MAX_AGE[key] else None


def _ms(age):
    return None if age is None else int(age * 1000)


def starting_secs(rd, bus, now=None):
    """Seconds robotd has continuously reported a starting phase since this connection (tracked in polling).
    Returns None when not in a starting phase or unknown. Past STARTING_LONG_S, UI stops saying 'usually finishes within 10s'."""
    since = getattr(rd, "starting_since", None)
    if since is None or not isinstance(bus, dict) or bus.get("phase") not in STARTING_PHASES:
        return None
    return max(0, int((time.monotonic() if now is None else now) - since))


STILL_STARTING = "Controller may already be up with stale status, or stuck in startup; check Logs"


def posture(present, bus, policies, state):
    if present is False:
        return "absent"
    if not isinstance(bus, dict):
        return "unknown"
    mode, phase = bus.get("mode"), bus.get("phase")
    if phase in STARTING_PHASES:
        return "starting"
    if mode == "commissioning":
        return "commissioning"
    if mode != "motion":
        return "unknown"
    if isinstance(policies, dict) and policies.get("sitting") is True:
        return "sitting"
    native = bus.get("native") if isinstance(bus.get("native"), dict) else {}
    confirmed = native.get("torque_state_confirmed")
    if confirmed is False:
        return "limp"
    if bus.get("policy_enabled") is True:
        applied = ((state or {}).get("move") or {}).get("applied") if isinstance(state, dict) else None
        if isinstance(applied, list) and any(abs(v) > 0.01 for v in applied if isinstance(v, (int, float))):
            return "walking"
        return "policy"
    if confirmed is True:
        if bus.get("homed") is not True:
            return "homing"
        # homed only means the control loop thinks it reached HOME; actual joint positions may lag slightly.
        # robotd's own home_start_guard checks both actual joint positions (<= joint_tolerance_deg) and body tilt,
        # and requires stability across several ticks. Only fall back to homed when robotd omits this field (older builds).
        guard = bus.get("home_start_guard")
        if isinstance(guard, dict) and isinstance(guard.get("ready"), bool):
            return "holding" if guard["ready"] else "homing"
        return "holding"
    return "unknown"


def tilt_deg(gravity):
    """Matches robotd home_start.rs: angle between gravity vector and straight down (0, 0, -1)."""
    if not isinstance(gravity, (list, tuple)) or len(gravity) != 3:
        return None
    n = math.sqrt(sum(g * g for g in gravity))
    if n < 1e-6:
        return None
    c = max(-1.0, min(1.0, -gravity[2] / n))
    return math.degrees(math.acos(c))


def cpu_view(health, health_age, bus, present, servo=None, ob=None):
    """Board temperature card fields:
        c          °C: hottest thermal zone (robotd health cpu_temp_c); None if unavailable
        throttled  kernel thermal throttling active (cooling device level > 0) -> UI 'Throttled x / y MHz'; None = unknown (not 'normal')
        limited    level == 0, but allowed frequency is below max: frequency-capped, likely non-thermal
        khz / max_khz / level / max_level   raw values (khz is max allowed, not instantaneous clock)
        hot        >= 80 °C (with hysteresis: clears below 77 °C, tracked in onboard.Onboarding.cpu_hot)
        src        robotd / studio (Studio reads /sys/class/thermal when robotd does not report it)
        why        reason unavailable (not reported / servo tuning / commissioning / waiting for bus / health stale)
    robotd only samples once per second in motion mode control loop, not in commissioning or bus-wait phases."""
    out = {"c": None, "throttled": None, "limited": None, "khz": None, "max_khz": None, "level": None, "max_level": None,
           "hot": False, "src": None, "why": None}
    h = health if isinstance(health, dict) else None
    c = (h or {}).get("cpu_temp_c")
    thr = (h or {}).get("cpu_throttle") if isinstance((h or {}).get("cpu_throttle"), dict) else None
    src = "robotd" if isinstance(c, (int, float)) else None
    board = getattr(ob, "board", None) if ob is not None else None
    if not isinstance(c, (int, float)) and isinstance(board, dict) and isinstance(board.get("c"), (int, float)):
        c, thr, src = board["c"], board.get("throttle"), "studio"
    if isinstance(c, (int, float)):
        out.update(c=round(float(c), 1), src=src)
        out["hot"] = bool(ob.cpu_hot) if ob is not None else c >= CPU_WARN_C
    else:
        if servo is not None and getattr(servo, "state", None) in ("open", "opening"):
            why = "Servo tuning active: robotd is paused and not reporting temperature"
        elif present is False:
            why = "robotd is not running"
        elif (bus or {}).get("mode") == "commissioning":
            why = "robotd does not report temperature in commissioning mode"
        elif (bus or {}).get("phase") in STARTING_PHASES:
            wait = startup_wait(bus)
            why = f"robotd {wait['text'] if wait else 'is still starting'}; temperature not reported yet"
        elif h is None:
            why = f"Health check not updated for {health_age:.0f}s" if health_age is not None else "Health check not received yet"
        else:
            why = "robotd did not report temperature"
        out["why"] = why
    if isinstance(thr, dict):
        lv, mx, khz, mk = thr.get("level"), thr.get("max_level"), thr.get("khz"), thr.get("max_khz")
        out.update(level=lv, max_level=mx, khz=khz, max_khz=mk)
        nums = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (lv, khz, mk))
        if nums:
            out["throttled"] = lv > 0
            out["limited"] = lv == 0 and mk > 0 and khz < mk
    elif out["c"] is not None:
        out["throttled"] = None          # Temperature present without throttle data: unknown, do not report normal
    return out


def servo_session_view(servo):
    """Short status summary for the servo session (used in banners); returns None when closed.
    unknown = servos whose torque state cannot be read (may still be holding);
    restored = session resumed after Studio restart."""
    if servo is None or getattr(servo, "state", None) not in ("open", "opening"):
        return None
    holding = servo.holding() if hasattr(servo, "holding") else None
    return {"state": servo.state, "holding": holding, "latched": bool(getattr(servo, "latched", False)),
            "unknown": servo.unknown() if hasattr(servo, "unknown") else 0,
            "restored": bool(getattr(servo, "restored", False)), "closing": bool(getattr(servo, "closing", False)),
            "port": "Demo (no servos connected)" if getattr(servo, "sim", False) else getattr(servo, "port", None)}


def servo_orphan_view(servo):
    """Previous Studio exit left a servo session open that was not reclaimed: servos may still be holding; reclaim on Servo page before releasing torque."""
    o = getattr(servo, "orphan", None) if servo is not None else None
    if not o or getattr(servo, "state", None) in ("open", "opening"):
        return None
    return {"why": o.get("why")}


def summarize(rd, duck, servo=None, now=None, ob=None, bat=None, net=None, commit=None, auth=None):
    """bat: battery estimator (studio/battery_svc.BatteryService, fed in 1 Hz loop); read-only here. Falls back to robotd linear if omitted."""
    now = time.monotonic() if now is None else now
    bus = rd.bus.value
    health, pol, st = _fresh(rd.health, "health", now), _fresh(rd.policies, "policies", now), _fresh(rd.state, "state", now)
    bus_age, st_age = rd.bus.age(now), rd.state.age(now)
    native = (bus or {}).get("native") if isinstance((bus or {}).get("native"), dict) else {}

    # Freshness: how long since Studio received new telemetry (browser adds transport age on top)
    freshest = min([a for a in (bus_age, st_age) if a is not None], default=None)
    robotd_ok = rd.present is True and bus_age is not None and bus_age < STALE_S

    # Telemetry strip
    b = (health or {}).get("battery") or {}
    battery = {"pct": b.get("percent"), "volts": b.get("volts")}
    joints = native.get("joints") if isinstance(native.get("joints"), list) else []
    fresh = [j for j in joints if isinstance(j, dict) and j.get("feedback_fresh") is True]
    motors = (health or {}).get("motors") or {}
    hottest = None
    if isinstance(motors.get("max_c"), (int, float)):
        hottest = {"name": JOINT_CN.get(motors.get("hottest"), motors.get("hottest")), "c": motors.get("max_c")}
    servos = {"online": len(fresh) if joints else None, "total": len(joints) or 15, "hottest": hottest,
              "stale": [JOINT_CN.get(j.get("name"), j.get("name")) for j in joints
                        if isinstance(j, dict) and j.get("feedback_fresh") is False]}
    observations = servo_observations(bus, present=rd.present, bus_age_s=bus_age,
                                      bus_error=getattr(rd.bus, "error", None))
    if observations is not None:
        rows = observations["rows"]
        temperatures = [r for r in rows if r["temperature_c"] is not None]
        hot = max(temperatures, key=lambda r: r["temperature_c"]) if temperatures else None
        servos = {"online": observations["online"], "total": len(rows) or 15,
                  "hottest": {"name": JOINT_CN.get(hot["name"], hot["name"]), "c": hot["temperature_c"]} if hot else None,
                  "stale": [JOINT_CN.get(r["name"], r["name"]) for r in rows if r["responding"] is False]}
    imu_h = (health or {}).get("imu") or {}
    if imu_h.get("ready") is False:
        imu_status = "missing"
    elif (imu_h.get("consecutive_stale_blocks") or 0) >= 25:
        imu_status = "frozen"
    elif native.get("imu_mount_verified") is False:
        imu_status = "unverified"      # Orientation unverified: misaligned IMU causes forward drift
    elif imu_h.get("ready") is True:
        imu_status = "ok"
    else:
        imu_status = None
    pad_v = _fresh(rd.pad, "pad", now)
    pads = (pad_v.get("pads") or []) if isinstance(pad_v, dict) else None
    pad = {"known": pads is not None, "connected": bool(pads and any(p.get("connected") for p in pads if isinstance(p, dict))),
           "name": next((p.get("name") for p in (pads or []) if isinstance(p, dict) and p.get("connected")), None),
           "driver": pad_v.get("driver") if isinstance(pad_v, dict) else None}
    pad["estop_ok"] = bool(pad["connected"] and pad["driver"] == "active" and (bus or {}).get("mode") == "motion")

    cpu = cpu_view(health, rd.health.age(now), bus, rd.present, servo, ob)
    loop = (health or {}).get("control_loop") or {}

    # 3D: joint angles in JOINT_NAMES order (raw, without direction multiplier); gravity from safety.gravity.
    # Keep stale joints too (UI dims by joints_age_ms); posture still requires fresh st.
    st_any = rd.state.value if isinstance(rd.state.value, dict) else {}
    js = st_any.get("joints")
    grav = (st_any.get("safety") or {}).get("gravity")

    st_age_live = st_age
    state_t_ns = st_any.get("t_ns")
    hold = (bus or {}).get("hold") if isinstance((bus or {}).get("hold"), dict) else None
    if (bus or {}).get("mode") == "commissioning":
        # Commissioning mode has no robot.state: read IMU from busStatus.last_sample (polled at 10 Hz)
        # so the IMU page and standing calibration have live readings.
        ls = (bus or {}).get("last_sample") if isinstance((bus or {}).get("last_sample"), dict) else {}
        s_imu = ls.get("imu") if isinstance(ls.get("imu"), dict) else {}
        g = s_imu.get("gravity")
        if s_imu.get("valid") is True and isinstance(g, list) and len(g) == 3:
            grav = g
            st_age_live = bus_age
            state_t_ns = ls.get("at_us") * 1000 if isinstance(ls.get("at_us"), int) else None
    pose = posture(rd.present, bus, pol, st)
    est = rd.estop.status(present=rd.present is not False, bus=bus, bus_age_s=bus_age, now=now)
    if servo_orphan_view(servo) is not None and rd.present is not True:
        # Previous exit left a session open and it was not reclaimed: robotd is absent and bus is not held; reclaim on Servo page first
        est = {"state": "unavailable", "why": "Servos may still be holding from the previous Studio session; reclaim on the Servo page before releasing torque"}
    if servo is not None and servo.state in ("open", "opening"):
        # Servo tuning: robotd is intentionally paused. E-Stop uses bus readback; battery uses average of 15 servo voltages.
        pose = "servo_session"
        robotd_ok = True
        freshest = 0.0                  # robotd polling is paused: freshness comes from the servo bus (50 Hz)
        est = servo.estop_status()
        snap = servo.snapshot()
        if snap.get("avg_volts"):
            battery = {"pct": snap.get("battery_pct"), "volts": snap.get("avg_volts")}
        servos = {"online": snap.get("online"), "total": snap.get("total"), "hottest": None, "stale": []}
    if bat is not None:
        battery = bat.view(now)         # Voltage-lookup estimate (reads from Studio's bus during servo tuning)
    starting_s = starting_secs(rd, bus, now) if pose == "starting" else None
    return {
        "t": "state",
        "robotd_ok": robotd_ok,
        "robotd_age_ms": _ms(freshest),
        "health_age_ms": _ms(rd.health.age(now)),
        "present": rd.present,
        "mode": (bus or {}).get("mode"),
        "phase": (bus or {}).get("phase"),
        "build": (bus or {}).get("build"),
        "posture": pose,
        "start_error": (bus or {}).get("error") if pose == "starting" else None,
        "start_wait": startup_wait(bus) if pose == "starting" else None,
        # Seconds continuously in a starting phase since this connection, and whether > 30s (UI switches to stuck-starting message)
        "starting_s": starting_s,
        "starting_long": starting_s is not None and starting_s >= STARTING_LONG_S,
        "battery": battery,
        "servos": servos,
        "imu": {"status": imu_status, "source": native.get("imu_source"),
                "configured_imu_profile": native.get("configured_imu_profile", (bus or {}).get("configured_imu_profile")),
                "context_current": imu_context_current(rd, now),
                "generation": getattr(getattr(rd, "hello", None), "at", None),
                "diagnostic": imu_diagnostic(rd, now)},
        "pad": pad,
        "cpu": cpu,
        "loop": {"hz": loop.get("achieved_hz"), "target": loop.get("target_hz"), "missed": loop.get("missed")},
        "joints": js if isinstance(js, list) and len(js) == 15 else None,
        "joints_age_ms": _ms(st_age_live),
        "state_t_ns": state_t_ns,   # robotd frame timestamp: IMU page uses this to deduplicate frames
        "gravity": grav if isinstance(grav, list) and len(grav) == 3 else None,
        "tilt_deg": tilt_deg(grav),
        "guard": (bus or {}).get("home_start_guard"),
        "hold": hold,                  # Commissioning stand hold: {stage: ramping|holding, max_error_deg, directions}
        "estop": est,
        # startup_todo: replace generic starting message with step detail and add missing walk model hint
        "todo": startup_todo(rd, todo(health, bus, servos, imu_status, cpu, loop, battery, pose, rd.health.age(now),
                     battery_shutdown=getattr(ob, "battery_shutdown", None) if ob is not None else None,
                     starting_s=starting_s), pol, bus),
        # Onboarding checklist: evaluated at 0.5 Hz in background (onboard.Onboarding.tick); read cached view here
        "onboarding": ob.view() if ob is not None else None,
        # Write to robotd: readiness + background job progress (CommitService; None if unavailable)
        "commit": (commit.view(rd, servo) if commit is not None else None),
        # Servo session (banner on every page): stays open when navigating away if any servo is holding
        "servo_session": servo_session_view(servo),
        # Seconds since handback: shown until robotd reconnects
        "servo_handback_s": (int(now - servo.handed_back_at) if servo is not None and servo.state == "closed"
                             and getattr(servo, "handed_back_at", None) is not None else None),
        "servo_helper": bool(getattr(servo, "helper_available", False)),   # Root helper available: banner shows "Restarting robotd..."
        # Handback warnings (unconfirmed limp / unresponsive servos / restart failure reason & hint)
        "servo_handback_note": getattr(servo, "handback_note", None) if servo is not None else None,
        "servo_giveback_error": getattr(servo, "giveback_error", None) if servo is not None else None,
        "servo_giveback_hint": getattr(servo, "giveback_hint", None) if servo is not None else None,
        # Session marker write failure: Studio would not know session was open after restart
        "servo_marker_error": getattr(servo, "marker_error", None) if servo is not None else None,
        # Orphaned session from previous exit; or robotd manually started during session (shown for 10 min)
        "servo_orphan": servo_orphan_view(servo),
        "servo_conflict_s": (int(now - servo.conflict_at) if servo is not None and getattr(servo, "conflict_at", None) is not None
                             and servo.state == "closed" and now - servo.conflict_at < 600 else None),
        # Networking (studio/net.py): missing net.json -> known False, Wi-Fi UI hidden
        "net": net.view(now) if net is not None else {"known": False},
        "auth": {"has_password": auth.has_password() if auth is not None else None},
        "duck": duck,
        "name_on_duck": (rd.sysinfo.value or {}).get("name"),
    }


def todo(health, bus, servos, imu_status, cpu, loop, battery, pose, health_age=None, battery_shutdown=None, starting_s=None):
    """Translate health status into plain actionable items, most urgent first. Only include issues; omit normal states.
    Each item carries a key so the overview checklist can deduplicate items already shown in card headers.
    starting_s: seconds spent continuously in a starting phase; past 30s, switch to 'stuck starting' instead of 'usually finishes within 10s'."""
    items = []
    # Battery: if battery comes from the estimator (has mode), use battery.todo_items; critical/red-line items go first even during servo tuning.
    # Without estimator (battery only has pct / volts), fall back to the direct robotd checks below.
    bat_items = None
    if isinstance(battery, dict) and "mode" in battery:
        from .battery import todo_items as battery_todo
        bat_items = battery_todo(battery)
        items += [i for i in bat_items if i["level"] == "error"]
    if health is None and pose not in ("absent", "unknown", "servo_session"):   # During servo tuning, robotd is intentionally stopped
        # Stale or missing health check: never show "all clear" when telemetry is missing
        when = f"not updated for {health_age:.0f}s" if health_age is not None else "never received"
        items.append({"key": "health", "level": "warn", "msg": "Health check unavailable",
                      "hint": f"Battery, temperature, and IMU readings {when}; check robotd lines in Logs"})
    if pose == "starting":
        raw = (bus or {}).get("error")
        wait = startup_wait(bus)
        if wait:
            items.append({"key": "starting", "level": "warn", "msg": wait["text"],
                          "hint": f"Raw: {raw}" if raw else "Check the last robotd lines in Logs"})
        elif starting_s is not None and starting_s >= STARTING_LONG_S:
            items.append({"key": "starting", "level": "warn", "msg": "robotd stuck in starting state",
                          "hint": f"Elapsed {starting_s}s. " + (f"Raw: {raw}. Check robotd lines in Logs" if raw
                                                                  else STILL_STARTING)})
        else:
            items.append({"key": "starting", "level": "warn", "msg": "Duck robotd is still starting",
                          "hint": f"Raw: {raw}" if raw else "Usually finishes within 10s"})
    if pose == "servo_session":
        items.append({"key": "servo_session", "level": "warn", "msg": "Servo tuning active",
                      "hint": "robotd is paused so gamepad, remote, and walk are disabled; servos stay held until you click 'Hand back to robotd'"})
        if bat_items is not None:
            items += [i for i in bat_items if i["level"] != "error"]
        else:
            # Without estimator: show battery todo during servo session using the average across 15 servos.
            # robotd is stopped here, so auto-shutdown on empty battery is inactive.
            pct = battery.get("pct") if isinstance(battery, dict) else None
            if isinstance(pct, (int, float)) and pct < 15:
                items.append({"key": "battery", "level": "error", "msg": f"Battery low: {pct:.0f}%",
                              "hint": "Charge soon; robotd is paused so it will not sit down automatically when empty"})
            elif isinstance(pct, (int, float)) and pct < 30:
                items.append({"key": "battery", "level": "warn", "msg": f"Battery at {pct:.0f}%",
                              "hint": "Running low; robotd is paused so it will not sit down automatically when empty"})
        return items
    if pose == "absent":
        items.append({"key": "absent", "level": "error", "msg": "Duck robotd is not running", "hint": "Stopping robotd does not release torque; servos may still be holding"})
    items += [i for i in bat_items or () if i["level"] != "error"]
    pct = battery.get("pct") if bat_items is None else None
    if isinstance(pct, (int, float)):
        if pct < 15:
            empty = ("At 6.6 V it will sit down and shut off automatically" if battery_shutdown is True
                     else "It will not sit down automatically when empty; stop, sit it down, and charge")
            items.append({"key": "battery", "level": "error", "msg": f"Battery low: {pct:.0f}%", "hint": f"Charge soon; {empty}"})
        elif pct < 30:
            items.append({"key": "battery", "level": "warn", "msg": f"Battery at {pct:.0f}%", "hint": "Running low"})
    if imu_status == "frozen":
        items.append({"key": "imu_frozen", "level": "error", "msg": "IMU readings frozen", "hint": "Attitude telemetry is not updating; do not walk and check IMU wiring"})
    elif imu_status == "missing":
        items.append({"key": "imu_missing", "level": "error", "msg": "IMU not detected", "hint": "Check IMU module wiring"})
    elif imu_status == "unverified":
        items.append({"key": "imu_unverified", "level": "warn", "msg": "IMU orientation not verified",
                      "hint": "Misaligned IMU causes forward drift; verify on the IMU page first"})
    if servos.get("stale"):
        items.append({"key": "servo_stale", "level": "warn", "msg": f"{', '.join(servos['stale'][:3])} telemetry stale",
                      "hint": "Usually a loose cable or power issue"})
    khz, mk = cpu.get("khz"), cpu.get("max_khz")
    if cpu.get("throttled"):
        mhz = f": {khz // 1000} / {mk // 1000} MHz" if isinstance(khz, int) and isinstance(mk, int) and mk else ""
        items.append({"key": "cpu", "level": "warn", "msg": f"Board thermal throttling{mhz}", "hint": "Control loop may slow down; add a heatsink or small fan"})
    elif cpu.get("hot") and isinstance(cpu.get("c"), (int, float)):
        items.append({"key": "cpu", "level": "warn", "msg": f"Board {cpu['c']:.0f} °C, running hot", "hint": "Further heating will throttle CPU; add a heatsink or small fan"})
    elif cpu.get("limited") and isinstance(khz, int):
        items.append({"key": "cpu", "level": "warn", "msg": f"Board clock capped at {khz // 1000} MHz (not thermal)",
                      "hint": "A process or system setting is limiting max CPU frequency; control may slow down"})
    hz, target = loop.get("achieved_hz") or loop.get("hz"), loop.get("target_hz") or loop.get("target")
    if isinstance(hz, (int, float)) and isinstance(target, (int, float)) and target and hz < 0.95 * target:
        items.append({"key": "loop", "level": "warn", "msg": f"Control rate only {hz:.1f} Hz", "hint": f"Target {target:.0f} Hz; check board temperature and load"})
    return items
