"""Synthetic runtime/tuning payloads for fake_robotd.py."""
import asyncio
import copy
import json

POLICY_OFF = "Policy disabled by config"


def _ok(detail):
    return {"status": "success", "detail": detail}


def startup_report(fake):
    if getattr(fake, "startup_override", None) is not None:
        return {"steps": copy.deepcopy(fake.startup_override.get("steps") or {}), "build": fake.bus.get("build")}
    p = fake.posture
    s1 = _ok("Config, instance lock, IPC, and control thread ready")
    if p == "starting":
        steps = {"1": s1, "2": {"status": "waiting", "detail": fake.bus.get("error") or "Waiting for fresh joint feedback; no target sent"}}
    elif p == "stuck_calib":
        steps = {"1": s1, "2": {"status": "waiting", "detail": fake.bus.get("error")}}
    elif p == "commissioning":
        # Commissioning startup steps
        # Legacy comm_stuck still reports 4 ok steps while busStatus stays in boot report
        cp = fake.comm_phase() if hasattr(fake, "comm_phase") and fake.mode == "commissioning" else "up"
        if cp == "waiting":
            steps = {"1": s1, "2": {"status": "waiting", "detail": "torque readback at register 40: Feetech Sync Read timeout"}}
        elif cp == "boot" and not getattr(fake, "comm_stuck", False):
            steps = {"1": s1, "2": {"status": "starting", "detail": "Commissioning: loading calibration, confirming torque off, init IMU"}}
        else:
            steps = {"1": s1, "2": _ok("Commissioning: first valid feedback"), "3": _ok("Commissioning skips policy models"),
                     "4": _ok("Commissioning first cycle complete; policy off")}
    elif p == "walk_unloaded":
        steps = {"1": s1, "2": _ok("First valid joint & IMU feedback"), "3": _ok(POLICY_OFF), "4": _ok("First control cycle complete")}
    else:
        steps = {"1": s1, "2": _ok("First valid joint & IMU feedback"), "3": _ok("Bundled models ready"), "4": _ok("First control cycle complete")}
    return {"steps": steps, "build": fake.bus.get("build")}


def after_posture(fake, p):
    bus = fake.bus
    if not isinstance(bus.get("native"), dict):
        return                                    # Debug / waiting_for_bus
    b0 = fake._bus0
    if p == "walk_unloaded":
        bus["loaded_models"] = None
        bus["active_model"] = None
    elif bus.get("loaded_models") is None and b0.get("loaded_models") is not None:
        bus["loaded_models"] = copy.deepcopy(b0["loaded_models"])
    wp = getattr(fake, "walk_profile", None)
    walk = (bus.get("loaded_models") or {}).get("walk") if isinstance(bus.get("loaded_models"), dict) else None
    if wp and isinstance(walk, dict) and isinstance(walk.get("runtime_execution_profile"), dict):
        walk["runtime_execution_profile"].update(wp)
    prof = (walk or {}).get("runtime_execution_profile") if isinstance(walk, dict) else None
    if p == "policy" and isinstance(prof, dict):
        mult = bus.get("voltage_scale_mult") or 1.0
        bus["active_action_scale"] = round(prof["action_scale"] * mult, 6)
        bus["active_target_filters"] = {"head_lowpass": prof["head_lowpass"], "legs_lowpass": prof["legs_lowpass"]}
        bus["scale_use"] = "joint_uniform"
        bus["active_model"] = {k: walk.get(k) for k in ("path", "sha256", "task")}
    else:
        bus["active_action_scale"] = None
        bus["active_target_filters"] = None
        bus["scale_use"] = "held"
        if p != "walk_unloaded":
            bus["active_model"] = None


def fake_set(fake, params):
    bus = fake.bus
    native = bus.get("native") if isinstance(bus.get("native"), dict) else None
    hd = native.get("hd1910") if native is not None and isinstance(native.get("hd1910"), dict) else None
    if "startup" in params:
        fake.startup_override = params["startup"] if isinstance(params["startup"], dict) else None
    if "servo_parameters" in params and hd is not None:
        v = params["servo_parameters"]
        if v == "absent":
            hd.pop("servo_parameters", None)
        else:
            hd["servo_parameters"] = v
    if isinstance(params.get("walk_profile"), dict):
        fake.walk_profile = dict(params["walk_profile"])
        after_posture(fake, fake.posture)
    if isinstance(params.get("guard"), dict) and isinstance(bus.get("home_start_guard"), dict):
        bus["home_start_guard"].update(params["guard"])
    if "last_enable_refusal" in params:
        bus["last_enable_refusal"] = params["last_enable_refusal"]
    if isinstance(params.get("slot_error"), dict) or "change_error" in params:
        fake.policies = copy.deepcopy(fake.policies or {})
        for s in fake.policies.get("slots") or []:
            if isinstance(s, dict) and s.get("slot") in (params.get("slot_error") or {}):
                s["error"] = params["slot_error"][s["slot"]]
        if "change_error" in params:
            fake.policies["change_error"] = params["change_error"]
    if "regs" in params and hd is not None and params["regs"] is None:
        hd["registers_before_arm"] = []


async def startup_stream(fake, reader, writer, rid):
    err = (getattr(fake, "errors", None) or {}).get("robot.startupSubscribe")
    if err:
        writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, "error": err}) + "\n").encode())
        await writer.drain()
        return
    last = startup_report(fake)
    writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, "result": last}, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    fake.startup_subs = getattr(fake, "startup_subs", 0) + 1
    line = asyncio.ensure_future(reader.readline())
    try:
        while True:
            done, _ = await asyncio.wait({line}, timeout=0.1)
            if done:
                return                            # Client sent a line or disconnected
            cur = startup_report(fake)
            if cur != last:
                last = cur
                writer.write((json.dumps({"jsonrpc": "2.0", "method": "robot.startup", "params": cur},
                                         ensure_ascii=False) + "\n").encode())
                await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        line.cancel()
        fake.startup_subs -= 1
