"""Track robotd's 4-step startup sequence via robot.startupSubscribe."""
import asyncio
import sys
import time

from .rpc import RpcError, Unreachable, encode, open_conn, read_message

STEPS = ("1", "2", "3", "4")
STEP_NAMES = {"1": "Config & Control Thread", "2": "Servo Bus & IMU", "3": "Load Models", "4": "First Control Cycle"}
READ_TIMEOUT_S = 2.0
RETRY_S = (1.0, 2.0, 5.0)
POLICY_OFF = "\u7b56\u7565\u6309\u914d\u7f6e\u5173\u95ed"   # Exact upstream robotd detail string when policy is disabled by config


class StartupTrack:
    def __init__(self):
        self.epoch = None
        self.steps = {}
        self.build = None
        self.done = False
        self.unsupported = None
        self.error = None
        self.at = None

    def reset(self, epoch):
        self.__init__()
        self.epoch = epoch

    def update(self, report, now=None):
        """Apply a startup report or notification dict."""
        if not isinstance(report, dict):
            return
        now = time.monotonic() if now is None else now
        steps = report.get("steps") if isinstance(report.get("steps"), dict) else {}
        for k in STEPS:
            row = steps.get(k)
            if not isinstance(row, dict):
                continue
            status, detail = row.get("status"), row.get("detail")
            old = self.steps.get(k)
            if old and old["status"] == status and old["detail"] == detail:
                continue
            at = old["at"] if old and old["status"] == status else now
            self.steps[k] = {"status": status if isinstance(status, str) else None,
                             "detail": detail if isinstance(detail, str) else None, "at": at}
        if isinstance(report.get("build"), str):
            self.build = report["build"]
        self.at = now
        self.error = None
        self.done = (all((self.steps.get(k) or {}).get("status") == "success" for k in STEPS)
                     or any((self.steps.get(k) or {}).get("status") == "failed" for k in STEPS))

    def current(self):
        """Return (step_key, step_dict) for the first non-success step, or (None, None)."""
        for k in STEPS:
            row = self.steps.get(k)
            if not row or row.get("status") != "success":
                return k, row
        return None, None

    def policy_off(self):
        """Return True if step 3 succeeded with policy disabled by config."""
        row = self.steps.get("3") or {}
        return row.get("status") == "success" and (POLICY_OFF in (row.get("detail") or "") or "policy disabled" in (row.get("detail") or "").lower())

    def view(self, now=None):
        """Return 4-step startup summary for the UI."""
        rows = []
        for k in STEPS:
            row = self.steps.get(k)
            rows.append({"n": int(k), "name": STEP_NAMES[k], "status": (row or {}).get("status"),
                         "detail": (row or {}).get("detail"),
                         "at_s": round(row["at"], 1) if row and row.get("at") is not None else None})
        n, row = self.current()
        return {"rows": rows, "known": bool(self.steps), "done": self.done, "now": int(n) if n else None,
                "unsupported": self.unsupported, "error": self.error, "build": self.build,
                "boot_clock": sys.platform.startswith("linux")}


async def startup_loop(rd):
    """Background task that subscribes to robot.startupSubscribe."""
    track = rd.startup
    fails = 0
    while True:
        if rd.addr == "none":
            return
        epoch = rd.hello.at
        if rd.paused or rd.present is not True or epoch is None:
            await asyncio.sleep(1.0)
            continue
        if track.epoch != epoch:
            track.reset(epoch)
        if track.done or track.unsupported:
            await asyncio.sleep(1.0)
            continue
        try:
            await _follow(rd, track, epoch)
            fails = 0
        except (Unreachable, OSError, asyncio.TimeoutError) as e:
            fails += 1
            track.error = f"Startup subscription failed: {e}"
            await asyncio.sleep(RETRY_S[min(fails - 1, len(RETRY_S) - 1)])


async def _follow(rd, track, epoch):
    r, w = await asyncio.wait_for(open_conn(rd.addr), 3.0)
    try:
        try:
            w.write(encode(1, "robot.startupSubscribe", {}))
            await w.drain()
        except (ConnectionError, OSError) as e:
            raise Unreachable(f"Failed to send request: {type(e).__name__}: {e}") from e
        while True:
            if rd.hello.at != epoch or rd.paused:
                return
            try:
                msg = await read_message(r, READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            if msg is None:
                continue
            if msg.get("id") == 1:
                if "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                    e = RpcError("robot.startupSubscribe", err.get("code"), err.get("message", ""))
                    track.unsupported = f"Controller build does not report startup steps ({e})"
                    rd.log.info("Controller does not support robot.startupSubscribe", cat="robotd", raw=str(e))
                    return
                track.update(msg.get("result"))
            elif msg.get("method") == "robot.startup":
                track.update(msg.get("params"))
            else:
                continue
            _log_change(rd, track)
            if track.done:
                return
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


def _log_change(rd, track):
    """Log startup step failures or unloaded walk policy."""
    n, row = track.current()
    if row and row.get("status") == "failed":
        rd.log.error(f"Controller startup failed at step {n}/4", cat="robotd", step=STEP_NAMES[n], raw=row.get("detail"),
                     hint="Check robotd logs and export diagnostics")
    elif not n and track.policy_off():
        rd.log.warn("Controller started, but walking policy is not loaded (step 3: disabled by config)", cat="robotd", step="Load Models",
                    hint="Check walking model execution config or contract; standing works, walking will not")


def _hello_at(rd):
    return getattr(getattr(rd, "hello", None), "at", None)


def walk_unloaded(rd, pol=None, bus=None):
    """Return human-readable reason if walk policy is unloaded in motion mode, else None."""
    if getattr(rd, "paused", None) or getattr(rd, "present", None) is False:
        return None
    bus = bus if isinstance(bus, dict) else (rd.bus.value if isinstance(rd.bus.value, dict) else None)
    if not bus or bus.get("mode") != "motion" or bus.get("phase") != "control":
        return None
    if isinstance(pol, dict) and pol.get("enabled") is False:
        return None
    walk_err = None
    if isinstance(pol, dict):
        for s in pol.get("slots") or []:
            if isinstance(s, dict) and s.get("slot") == "walk" and s.get("error"):
                walk_err = str(s["error"])
    lm = bus.get("loaded_models")
    track = getattr(rd, "startup", None)
    off = bool(track is not None and track.epoch == _hello_at(rd) and track.policy_off())
    missing = "loaded_models" in bus and (not isinstance(lm, dict) or not isinstance(lm.get("walk"), dict))
    if walk_err:
        return f"Walk slot failed to load: {walk_err}"
    if missing or off:
        return "Controller reported policy disabled by config; walk model not loaded" if off else "Controller did not load walk model (missing in loaded_models)"
    return None


def startup_todo(rd, items, pol=None, bus=None):
    """Augment todo items with startup step details and unloaded walk policy warnings."""
    try:
        track = getattr(rd, "startup", None)
        if track is not None and track.epoch is not None and track.epoch == _hello_at(rd):
            n, row = track.current()
            for it in items:
                if it.get("key") == "starting" and n and row and row.get("detail"):
                    raw = (bus or {}).get("error") if isinstance(bus, dict) else None
                    hint = f"Step {n}/4 ({STEP_NAMES[n]}): {row['detail']}"
                    if raw and raw not in row["detail"]:
                        hint += f" — raw: {raw}"
                    it["hint"] = hint
        why = walk_unloaded(rd, pol, bus)
        if why and not any(it.get("key") == "walk_unloaded" for it in items):
            items.insert(0, {"key": "walk_unloaded", "level": "error", "msg": "Walk model not loaded",
                             "hint": f"{why}; standing works, see Runtime & Tuning below"})
    except Exception:  # noqa: BLE001
        pass
    return items
