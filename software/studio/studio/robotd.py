"""Communicate with on-duck daemons: 4 robotd connections + 1 configd connection.

| Conn    | Purpose                                                       |
| stream  | robot.subscribe {"hz":10}. Active only while viewers > 0 (+10s linger) |
| poll    | hello (once), health 2s, busStatus 1s (10 Hz on E-Stop/telemetry), policies 2s, calibration-status |
| cmd     | Motion commands                                               |
| relax   | robot.relax only; fresh connection per press                  |
| startup | robot.startupSubscribe: active until all 4 hello steps succeed or one fails |
| configd | pad.status 5s, system.info 60s                                |

All timestamps use time.monotonic() because the duck's wall clock stays at 1970 before NTP sync.
This module does not import any serial libraries.
"""
import asyncio
import time

from .estop import CONFIRM_TIMEOUT_S, JOIN_S, RESEND_AT_S, STARTING_PHASES, EStop, evidence
from .log import log_cmd
from .rpc import Conn, RpcError, Timeout, Unreachable, call_once, encode, open_conn, read_message
from .startup import StartupTrack, startup_loop

API_VERSION = 42                     # duck-ipc-proto; mismatch logs a warning without refusing
KNOWN_BUILDS = ("1fa8438-feetech-ft6-control.43", "4734cbb-feetech-imu-auto-control.44",
                "0791fcd-feetech-qinmo-control.45", "0791fcd-feetech-qinmo-control.46",
                "0791fcd-feetech-qinmo-control.47")
BACKOFF = (0.5, 1.0, 2.0, 5.0)
STREAM_HZ = 10
STREAM_LINGER_S = 10.0


class Sample:
    __slots__ = ("value", "at", "error")

    def __init__(self):
        self.value, self.at, self.error = None, None, None

    def set(self, value):
        self.value, self.at, self.error = value, time.monotonic(), None

    def fail(self, error):
        self.error = str(error)

    def age(self, now=None):
        if self.at is None:
            return None
        return (time.monotonic() if now is None else now) - self.at


class Robotd:
    def __init__(self, robotd_addr, configd_addr, log):
        self.log = log
        self.addr = robotd_addr
        self.poll = Conn(robotd_addr, "poll", log)
        self.cmd = Conn(robotd_addr, "cmd", log)
        self.cfg = Conn(configd_addr, "configd", log)
        self.hello, self.health, self.bus = Sample(), Sample(), Sample()
        self.policies, self.calib, self.state = Sample(), Sample(), Sample()
        self.pad, self.sysinfo = Sample(), Sample()
        self.present = None          # Whether robotd socket is reachable; None = not probed yet
        self.absent_since = None
        self.estop = EStop()
        self.viewers = 0             # Active UI viewer count
        self.telemetry_viewers = 0
        self.force_subscribe = False
        self.background_music = False  # Internal motion consumer; does not masquerade as a visible page
        self.stream_open = False
        self._last_viewer_at = 0.0
        self._tasks = []
        self._wake_stream = asyncio.Event()
        self._due = {"health": 0.0, "bus": 0.0, "policies": 0.0}   # Next poll timestamps (monotonic)
        self._fails = {}             # Consecutive poll failure counts per key
        self._relax_send = None      # In-flight relax task (asyncio.Task)
        self._relax_send_started = 0.0
        self._relax_watch = None     # E-Stop resend / timeout watcher
        self._calib_task = None
        # Only trust calibration-status read after the current hello (avoid stale cached results during startup).
        self.calib_valid_after = None
        self._last_phase = None
        # Monotonic timestamp since busStatus continuously reported a starting phase on this connection.
        self.starting_since = None
        self._wake_poll = asyncio.Event()   # Wakes poll loop from backoff sleep on E-Stop
        self._loop = None                   # Event loop for thread-safe wakeups from set_paused
        self.paused = None                  # Paused during servo tuning so Studio does not report robotd absent
        self.startup = StartupTrack()       # 4-step startup tracker (updated by startup_loop)

    # ---- Lifecycle ----

    def start(self):
        loop = asyncio.get_running_loop()
        self._loop = loop
        if self.addr == "none":
            # Bench mode: USB-TTL servo tuning only, do not connect to robotd
            self.log.info("Bench mode: not connecting to robotd", cat="robotd")
            self._tasks = []
            return
        self._tasks = [loop.create_task(self._supervise("poll", self._poll_loop), name="robotd-poll"),
                       loop.create_task(self._supervise("stream", self._stream_loop), name="robotd-stream"),
                       loop.create_task(self._supervise("configd", self._configd_loop), name="configd-poll"),
                       loop.create_task(self._supervise("startup", lambda: startup_loop(self)), name="robotd-startup")]

    async def stop(self):
        # Let an in-flight E-Stop finish (up to 1s) instead of cancelling it on shutdown
        if self._relax_send is not None and not self._relax_send.done():
            await asyncio.wait({self._relax_send}, timeout=1.0)
        tasks = [t for t in self._tasks + [self._relax_send, self._relax_watch, self._calib_task]
                 if t is not None]
        for t in tasks:
            t.cancel()
        for c in (self.poll, self.cmd, self.cfg):
            c.close()
        # Wait briefly for cancelled tasks to settle before exiting
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=2.0)

    async def _supervise(self, name, fn):
        """Never let a background loop die silently: log full traceback and restart after 1s."""
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log.error(f"Background task '{name}' crashed; restarting in 1s", cat="System", raw=f"{type(e).__name__}: {e}",
                               hint="This is a Studio bug; please export a diagnostic bundle", exc=True)
                await asyncio.sleep(1.0)

    def set_paused(self, on, why=None):
        if on and not self.paused:
            self.paused = why or "paused"
            self.log.info(f"Paused robotd polling ({self.paused})", cat="robotd", hint="robotd is intentionally paused")
        elif not on and self.paused:
            self.log.info(f"Resumed robotd polling ({self.paused} ended)", cat="robotd")
            self.paused = None
            # When called from the servo thread, use call_soon_threadsafe to avoid cross-thread Event.set issues
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._wake_poll.set)
            else:
                self._wake_poll.set()

    def set_viewers(self, n):
        n = max(0, n)
        if n and not self.viewers:
            self._wake_stream.set()
        if self.viewers and not n:
            self._last_viewer_at = time.monotonic()
        self.viewers = n

    def set_telemetry_viewers(self, n):
        n = max(0, n)
        if n and not self.telemetry_viewers:
            # The connected loop checks due times every 50 ms. Leave reconnect backoff alone.
            self._due["bus"] = 0.
        self.telemetry_viewers = n

    def bus_poll_interval(self):
        return .1 if self.telemetry_viewers or (self.bus.value or {}).get("mode") == "commissioning" else 1.

    def set_background_music(self, on):
        on = bool(on)
        if on != self.background_music:
            self.background_music = on
            self._wake_stream.set()

    # ---- E-Stop (Torque Release) ----

    async def relax(self, by):
        """Trigger torque release; returns (sent_ok, reason). Confirmation comes from busStatus readback (estop.on_bus)."""
        now = time.monotonic()
        self.estop.press(by, now)
        self._due["bus"] = 0.0          # Prioritize busStatus polling at 10 Hz for readback confirmation
        self._wake_poll.set()           # Wake poll loop if sleeping in backoff
        loop = asyncio.get_running_loop()
        if (self._relax_send is None or self._relax_send.done()
                or now - self._relax_send_started > JOIN_S):
            self._start_send(by, "E-Stop")
        if self._relax_watch is None or self._relax_watch.done():
            self._relax_watch = loop.create_task(self._watch_relax())
        return await asyncio.shield(self._relax_send)

    def _start_send(self, by, what):
        started = time.monotonic()
        self._relax_send_started = started
        self._relax_send = asyncio.get_running_loop().create_task(self._send_relax(by, what, started))

    async def _send_relax(self, by, what, started):
        """Send on a fresh connection each time (long-lived idle connections may be dead after robotd restart).
        Retries once after 0.1s on Unreachable."""
        with log_cmd(self.log, what, "robot.relax", who=by,
                     ctx={"mode": self._mode(), "phase": self._phase()}) as t:
            r = None
            for attempt in (1, 2):
                try:
                    r = await call_once(self.addr, "robot.relax", {}, timeout=3.0, name="E-Stop", log=self.log)
                    break
                except Unreachable as e:
                    if attempt == 1:
                        await asyncio.sleep(0.1)
                        continue
                    return self._relax_failed(t, started, f"Send failed: {e}")
                except (Timeout, RpcError) as e:
                    return self._relax_failed(t, started, f"Send failed: {e}")
            if isinstance(r, dict) and r.get("accepted") is False:
                return self._relax_failed(t, started, f"robotd rejected E-Stop: {r.get('reason') or 'no reason given'}", reply=r)
            self.estop.sent(started)
            self._mark_present(True)        # robotd replied, so it is present
            note = "Sent, waiting for readback confirmation"
            if not (isinstance(r, dict) and "accepted" in r):
                note += " (no 'accepted' field in reply; relying on readback)"
            t.ok(r, note=note)
            return True, None

    def _relax_failed(self, t, started, why, reply=None):
        t.fail(why, hint="To stop immediately: press gamepad '-' or cut power", raw=reply)
        self.estop.send_failed(started, why)
        return False, why

    async def _watch_relax(self):
        while self.estop.active:
            el = self.estop.elapsed()
            if el >= CONFIRM_TIMEOUT_S:
                bus = self.bus.value
                why = "E-Stop pressed 8s ago with no torque-off confirmation"
                if self.estop.send_error:
                    why += f" (resend error: {self.estop.send_error})"
                self.estop.finish("timeout", why)
                self.log.error("E-Stop unconfirmed after 8s", cat="Safety", step="busStatus readback",
                               ctx=dict(evidence(bus), bus_age_s=_round(self.bus.age()),
                                        saw_torque_off=self.estop.saw_off, send_error=self.estop.send_error),
                               hint="Servos may still be holding: press gamepad '-' or cut power, then export diagnostics")
                return
            # If not confirmed after 2s, resend once. Skip if readback already shows torque off (only hd1910 flag pending),
            # since another controller may have already stood the duck back up.
            if el >= RESEND_AT_S and not self.estop.resent and not self.estop.saw_off:
                self.estop.resent = True
                if self._relax_send is None or self._relax_send.done():
                    self._start_send(self.estop.last_by, "E-Stop (unconfirmed after 2s, resending)")
            await asyncio.sleep(0.1)

    # ---- Polling ----

    async def _ask(self, key, method, params=None, timeout=2.0, every=1.0):
        """Query one endpoint. On success schedule next in `every`; on error/timeout back off this key only."""
        asked = time.monotonic()
        try:
            r = await self.poll.call(method, params or {}, timeout=timeout)
            if not isinstance(r, dict):
                raise RpcError(method, None, f"Non-object response ({type(r).__name__})")
        except (RpcError, Timeout) as e:
            if key == "bus" or isinstance(e, Timeout):
                self.bus.fail(e)
            n = self._fails[key] = self._fails.get(key, 0) + 1
            self._due[key] = time.monotonic() + BACKOFF[min(n - 1, len(BACKOFF) - 1)] * 2
            if n in (1, 10) or n % 60 == 0:      # Log on 1st, 10th, and every 60th consecutive failure
                self.log.warn("robotd query failed", cat="robotd", step=method, raw=f"{e} (attempt #{n})",
                              hint="Occasional failures are harmless; if persistent, check board temperature and robotd logs")
            return None, asked
        if self._fails.pop(key, 0) >= 10:
            self.log.info("robotd query recovered", cat="robotd", step=method)
        self._due[key] = time.monotonic() + every
        return r, asked

    async def _poll_loop(self):
        due = self._due
        fails = 0
        hello_done = False
        hello_link = None             # Reconnect count of poll connection when hello succeeded
        while True:
            if self.paused:
                hello_done = False
                await self._nap(1.0)
                continue
            if hello_done and self.poll.reconnects != hello_link:
                # Poll connection reconnected: robotd may have restarted, so re-hello and invalidate cached calibration.
                hello_done = False
                self.log.info("robotd poll connection reconnected; re-running hello and refreshing calibration", cat="robotd")
            try:
                if not hello_done:
                    self.bus.fail("Waiting for bus status on this connection")
                    r = await self.poll.call("hello", {"api_version": API_VERSION}, timeout=2.0)
                    self.hello.set(r)
                    self.calib_valid_after = self.hello.at    # Invalidate calibration read before this hello
                    self._last_phase = None
                    self.starting_since = None                # Reset starting timer for this connection
                    self._mark_present(True)
                    hello_done = True
                    hello_link = self.poll.reconnects
                    self._ask_calib(again=True)
                    v = r.get("api_version") if isinstance(r, dict) else None
                    if v != API_VERSION:
                        self.log.warn(f"robotd API version is {v}, Studio expects {API_VERSION}", cat="robotd",
                                      hint="Usually compatible; any unsupported feature will report its own error")
                    ver = r.get("daemon_version") if isinstance(r, dict) else None
                    self.log.info(f"Connected to robotd (API v{v}, daemon {ver})", cat="robotd")
                now = time.monotonic()
                if now >= due["bus"]:
                    # Commissioning mode polls at 10 Hz for live IMU readings & standing calibration
                    every = self.bus_poll_interval()
                    bus, asked = await self._ask("bus", "robot.busStatus", every=every)
                    if bus is not None:
                        self.bus.set(bus)
                        self._check_build(bus)
                        # Invalidate calibration if leaving control; re-query when transitioning out of starting phases.
                        phase = bus.get("phase") if isinstance(bus, dict) else None
                        if self._last_phase == "control" and phase != "control":
                            self.calib_valid_after = time.monotonic()
                        if (phase != self._last_phase and self._last_phase is not None and phase is not None
                                and phase not in STARTING_PHASES):
                            self._ask_calib(again=True)
                        self._last_phase = phase
                        if phase in STARTING_PHASES:
                            if self.starting_since is None:
                                self.starting_since = time.monotonic()
                        else:
                            self.starting_since = None
                        if self.estop.on_bus(bus, asked):
                            self.log.info(f"Torque release confirmed ({self.estop.confirm_ms} ms from press)", cat="Safety",
                                          step="busStatus readback")
                    if self.estop.active:
                        due["bus"] = min(due["bus"], time.monotonic() + 0.1)
                if now >= due["health"]:
                    r, _ = await self._ask("health", "robot.health", every=2.0)
                    if r is not None:
                        self.health.set(r)
                if now >= due["policies"]:
                    if self._mode() == "motion" and self._phase() == "control":
                        r, _ = await self._ask("policies", "robot.policies", every=2.0)
                        if r is not None:
                            self.policies.set(r)
                    else:
                        due["policies"] = now + 2.0
                fails = 0
            except Unreachable as e:
                self.bus.fail(e)
                hello_done = False
                self._mark_present(False, e)
                fails += 1
                await self._nap(BACKOFF[min(fails - 1, len(BACKOFF) - 1)])
                continue
            except (Timeout, RpcError) as e:
                # Only hello reaches here
                fails += 1
                self.log.warn("robotd hello failed", cat="robotd", raw=str(e),
                              hint="robotd may still be starting; if persistent, check robotd logs")
                await self._nap(BACKOFF[min(fails - 1, len(BACKOFF) - 1)])
                continue
            await asyncio.sleep(0.05)

    async def _nap(self, seconds):
        """Backoff sleep, woken immediately on E-Stop press."""
        try:
            await asyncio.wait_for(self._wake_poll.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake_poll.clear()

    def _ask_calib(self, again=False):
        """Start _calib_once (no-op if already running unless again=True cancels retry sleep)."""
        if again and self._calib_task is not None and not self._calib_task.done():
            self._calib_task.cancel()
            self._calib_task = None
        if self._calib_task is None or self._calib_task.done():
            self._calib_task = asyncio.get_running_loop().create_task(self._calib_once())

    def calib_fresh(self):
        """Return calibration-status reply only if read after the current hello (None otherwise)."""
        return fresh_calib(self)

    async def _calib_once(self):
        """Query calibration status once on connect and when phase becomes control, on a separate connection
        so slow busCommand does not block busStatus. Retries twice 10s apart."""
        for attempt in range(3):
            try:
                r = await call_once(self.addr, "robot.busCommand", {"action": "calibration-status"},
                                    timeout=5.0, name="calibration-status", log=self.log)
                if isinstance(r, dict):
                    self.calib.set(r)
                    return
                self.calib.fail(f"Non-object response ({type(r).__name__})")
            except (Unreachable, Timeout, RpcError) as e:
                self.calib.fail(e)
                if attempt == 2:
                    self.log.warn("Cannot read calibration status", cat="Calibration", raw=str(e), hint="Standing/walking unaffected; calibration shows unknown in UI")
            await asyncio.sleep(10.0)

    def _mark_present(self, ok, why=None):
        if ok and self.present is not True:
            if self.present is False:
                gone = time.monotonic() - (self.absent_since or time.monotonic())
                self.log.info(f"robotd reconnected (was down for {gone:.0f}s)", cat="robotd")
            self.present, self.absent_since = True, None
        elif not ok and self.present is not False:
            self.present, self.absent_since = False, time.monotonic()
            self.log.warn("Cannot connect to robotd", cat="robotd", raw=str(why),
                          hint="robotd is not running or is restarting (~2s). Note: stopping robotd does not release servo torque")

    def _check_build(self, bus):
        build = (bus or {}).get("build")
        if build and build not in KNOWN_BUILDS and not getattr(self, "_warned_build", False):
            self._warned_build = True
            self.log.warn(f"robotd build {build} has not been verified with this Studio version", cat="robotd",
                          hint="Read-only telemetry and E-Stop still work; motion controls may be restricted")

    def _mode(self):
        b = self.bus.value
        return b.get("mode") if isinstance(b, dict) else None

    def _phase(self):
        b = self.bus.value
        return b.get("phase") if isinstance(b, dict) else None

    # ---- Stream Subscription ----

    def _want_stream(self):
        if self.paused:
            return False
        if self.force_subscribe or self.background_music or self.viewers > 0:
            return True
        return time.monotonic() - self._last_viewer_at < STREAM_LINGER_S

    async def _stream_loop(self):
        fails = 0
        while True:
            if not self._want_stream() or self._mode() != "motion" or self._phase() != "control":
                self._wake_stream.clear()
                try:
                    await asyncio.wait_for(self._wake_stream.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._stream_once()
                fails = 0
            except (Unreachable, RpcError) as e:
                fails += 1
                self.log.warn("Live state stream disconnected", cat="robotd", raw=str(e))
                await asyncio.sleep(BACKOFF[min(fails - 1, len(BACKOFF) - 1)])
            except asyncio.TimeoutError:
                fails += 1
                self.log.warn("No live state frames for 3s; re-subscribing", cat="robotd")

    async def _stream_once(self):
        r, w = await asyncio.wait_for(open_conn(self.addr), 3.0)
        self.stream_open = True
        self.log.info(f"Subscribed to live state stream ({STREAM_HZ} Hz, {self.viewers} viewer(s))", cat="robotd")
        try:
            try:
                w.write(encode(1, "robot.subscribe", {"hz": int(STREAM_HZ)}))   # hz must be integer (u32)
                await w.drain()
            except (ConnectionError, OSError) as e:
                raise Unreachable(f"Failed to send subscribe request: {type(e).__name__}: {e}") from e
            while True:
                msg = await read_message(r, 3.0)
                if msg is None:
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        err = msg["error"] or {}
                        raise RpcError("robot.subscribe", err.get("code"), err.get("message", ""))
                    continue
                if msg.get("method") == "robot.state":
                    self.state.set(msg.get("params"))
                if not self._want_stream():
                    self.log.info("No active viewers; stopping live state stream", cat="robotd")
                    return
        finally:
            self.stream_open = False
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- configd ----

    async def _configd_loop(self):
        """Poll system.info every 60s and pad.status every 5s with independent backoff."""
        due = {"info": 0.0, "pad": 0.0}
        down = {"info": False, "pad": False}
        while True:
            now = time.monotonic()
            for key, method, sample, every in (("info", "system.info", self.sysinfo, 60.0),
                                               ("pad", "pad.status", self.pad, 5.0)):
                if now < due[key]:
                    continue
                try:
                    sample.set(await self.cfg.call(method, {}, timeout=2.0))
                    due[key] = now + every
                    if down[key]:
                        down[key] = False
                        self.log.info(f"configd {method} recovered", cat="Gamepad")
                except (Unreachable, Timeout, RpcError) as e:
                    sample.fail(e)
                    due[key] = now + 5.0
                    if not down[key]:
                        down[key] = True
                        self.log.warn(f"configd {method} unavailable", cat="Gamepad", raw=str(e),
                                      hint="configd may be disabled; standing/walking unaffected, gamepad panel will show unknown")
            await asyncio.sleep(0.5)

    # ---- Read-only self-check (--selfcheck) ----

    async def selfcheck(self):
        """Read-only check: inspect user/groups, socket permissions, and one-shot queries."""
        import grp
        import os
        import pwd
        out = {}
        try:
            me = pwd.getpwuid(os.getuid()).pw_name
            groups = sorted(grp.getgrgid(g).gr_name for g in os.getgroups())
            out["user"] = f"{me} · groups {','.join(groups)}"
        except (ImportError, KeyError, OSError) as e:
            out["user"] = f"(unavailable: {e})"
        for path in ("/run/robotd.sock", "/run/configd.sock", "/run/padd/pad.sock"):
            if not os.path.exists(path):
                out[path] = "missing"
            else:
                out[path] = "read/write" if os.access(path, os.R_OK | os.W_OK) else "no permission (add user to robot group)"
        for name, method, params, addr in (
            ("direct_query", "robot.health", {}, self.addr),
            ("hello", "hello", {"api_version": API_VERSION}, self.addr),
            ("busStatus", "robot.busStatus", {}, self.addr),
            ("pad.status", "pad.status", {}, self.cfg.addr),
            ("system.info", "system.info", {}, self.cfg.addr),
        ):
            try:
                r = await call_once(addr, method, params, timeout=3.0)
                if name == "busStatus":
                    out[name] = f"ok · mode={r.get('mode')} phase={r.get('phase')} build={r.get('build')}"
                elif name == "hello":
                    out[name] = f"ok · api v{r.get('api_version')} daemon {r.get('daemon_version')}"
                else:
                    out[name] = "ok"
            except Exception as e:  # noqa: BLE001
                out[name] = f"failed: {e}"
        return out


def _round(x, nd=2):
    return None if x is None else round(x, nd)


def fresh_calib(rd):
    """Return rd.calib.value only if read after the current hello (or if calib_valid_after is unset). Non-dict -> None."""
    s = getattr(rd, "calib", None)
    if s is None or not isinstance(s.value, dict) or s.at is None:
        return None
    after = getattr(rd, "calib_valid_after", None)
    if after is not None and s.at < after:
        return None
    return s.value
