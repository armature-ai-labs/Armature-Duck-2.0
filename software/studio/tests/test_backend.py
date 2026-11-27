"""rpc / robotd / estop / state item.item Python 3.13 item (item): 
    py -3.13 -I run_tests.py      item  python3 -I run_tests.py
item robotd item tests/fixtures/now-20261018.jsonl (10-18 item: item, item).
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "deps"), str(HERE.parent / "dev")]

from fake_robotd import FakeRobotd  # noqa: E402
from studio import robotd as robotd_mod  # noqa: E402
from studio.estop import EStop, relaxed, robotd_up  # noqa: E402
from studio.log import Log  # noqa: E402
from studio.robotd import Robotd  # noqa: E402
from studio.rpc import LINE_LIMIT, Conn, RpcError, Timeout, Unreachable  # noqa: E402
from studio.state import posture, summarize, tilt_deg  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"


def quiet_log(tmp="."):
    return Log(Path(tmp) / "_testlogs", echo=False, background=False)


async def raw_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"


class RpcTest(unittest.IsolatedAsyncioTestCase):
    async def test_half_lines_notifications_and_garbage(self):
        async def h(r, w):
            req = json.loads(await r.readline())
            w.write(b"not json at all\n")
            w.write(b'{"jsonrpc":"2.0","method":"robot.state","params":{}}\n')
            body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"ok": 1}}).encode() + b"\n"
            w.write(body[:7]); await w.drain(); await asyncio.sleep(0.05)
            w.write(body[7:]); await w.drain()
        server, addr = await raw_server(h)
        async with server:
            c = Conn(addr)
            self.assertEqual(await c.call("x", timeout=1), {"ok": 1})
            self.assertEqual(c.bad_lines, 1)
            c.close()

    async def test_long_line_ok_and_too_long_is_unreachable(self):
        big = "x" * 200_000

        async def h(r, w):
            while True:
                line = await r.readline()
                if not line:
                    return
                req = json.loads(line)
                pad = big if req["method"] == "big" else "y" * (LINE_LIMIT + 10)
                w.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": pad}).encode() + b"\n")
                await w.drain()
        server, addr = await raw_server(h)
        async with server:
            c = Conn(addr)
            self.assertEqual(len(await c.call("big", timeout=2)), 200_000)
            with self.assertRaises(Unreachable):
                await c.call("huge", timeout=2)
            c.close()

    async def test_timeout_closes_and_next_call_reconnects(self):
        n = {"conns": 0}

        async def h(r, w):
            n["conns"] += 1
            req = json.loads(await r.readline())
            if n["conns"] == 1:
                await asyncio.sleep(2)      # item
                return
            w.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": "late"}).encode() + b"\n")
            await w.drain()
        server, addr = await raw_server(h)
        async with server:
            c = Conn(addr)
            with self.assertRaises(Timeout):
                await c.call("slow", timeout=0.2)
            self.assertEqual(await c.call("again", timeout=1), "late")
            self.assertEqual(n["conns"], 2)
            c.close()

    async def test_unreachable_vs_rpc_error(self):
        c = Conn("tcp:127.0.0.1:1")
        with self.assertRaises(Unreachable):
            await c.call("x", timeout=3.0)     # Windows item'item'item 2 item unix socket item
        fake = FakeRobotd(FIXTURE)
        server, port = await fake.serve_tcp()
        async with server:
            c = Conn(f"tcp:127.0.0.1:{port}")
            with self.assertRaises(RpcError) as e:
                await c.call("hello", {}, timeout=1)        # item: item api_version item
            self.assertEqual(e.exception.code, -32602)
            self.assertEqual((await c.call("hello", {"api_version": 40}))["api_version"], 40)
            c.close()      # Python 3.12+ item server.wait_closed() item


class EStopTest(unittest.TestCase):
    def bus(self, **kw):
        b = {"mode": "motion", "phase": "control", "torque_off_pending": False,
             "native": {"torque_state_confirmed": True, "hd1910": {"restore_pending": False, "temporary_gains_applied": False}}}
        for k, v in kw.items():
            if k in ("torque_state_confirmed",):
                b["native"][k] = v
            elif k in ("restore_pending", "temporary_gains_applied"):
                b["native"]["hd1910"][k] = v
            else:
                b[k] = v
        return b

    def test_relaxed_rules(self):
        self.assertFalse(relaxed(self.bus()))
        self.assertTrue(relaxed(self.bus(torque_state_confirmed=False)))
        self.assertFalse(relaxed(self.bus(torque_state_confirmed=False, restore_pending=True)))
        self.assertFalse(relaxed(self.bus(torque_state_confirmed=False, torque_off_pending=True)))
        self.assertTrue(relaxed({"mode": "commissioning", "torque_state": "off", "torque_off_pending": None}))
        self.assertFalse(relaxed({"mode": "commissioning", "torque_state": "single_joint_enabled"}))
        self.assertIsNone(relaxed(None))
        self.assertIsNone(relaxed({"mode": "motion"}))

    def test_states(self):
        e = EStop()
        st = lambda b, now, present=True, age=0.1: e.status(present=present, bus=b, bus_age_s=age, now=now)["state"]  # noqa: E731
        self.assertEqual(st(self.bus(), 0), "ready")
        self.assertEqual(st(self.bus(), 0, present=False), "unavailable")
        self.assertEqual(st(self.bus(), 0, age=5), "ready")                     # item
        self.assertIn("stale", e.status(present=True, bus=self.bus(), bus_age_s=5, now=0)["why"])
        self.assertEqual(st(self.bus(phase="waiting_for_bus"), 0), "unavailable")
        limp = self.bus(torque_state_confirmed=False, restore_pending=False, temporary_gains_applied=False)
        self.assertEqual(st(limp, 0), "confirmed")                              # item
        self.assertEqual(st(limp, 0, age=2.0), "confirmed")                     # 1 Hz item
        self.assertEqual(st(limp, 0, age=3.0), "ready")                         # item'itemE-Stop'

    def test_confirm_needs_a_readback_asked_after_the_ack(self):
        """item #3: item (item)item, item."""
        e = EStop()
        limp = self.bus(torque_state_confirmed=False, restore_pending=False, temporary_gains_applied=False)
        e.press("PC", now=100)
        self.assertEqual(e.status(present=True, bus=limp, bus_age_s=0.1, now=100.05)["state"], "pending")
        self.assertFalse(e.on_bus(limp, asked_at=99.9, now=100.1))             # item
        e.sent(100.0, now=100.2)
        self.assertFalse(e.on_bus(limp, asked_at=100.15, now=100.3))           # item
        self.assertTrue(e.on_bus(limp, asked_at=100.25, now=100.4))
        self.assertEqual(e.confirm_ms, 400)
        self.assertFalse(e.active)
        self.assertEqual(e.status(present=True, bus=limp, bus_age_s=0.1, now=100.5)["state"], "confirmed")

    def test_finished_attempt_never_acts_again(self):
        """item #1: item, Studio itemE-Stop."""
        e = EStop()
        limp = self.bus(torque_state_confirmed=False, restore_pending=False, temporary_gains_applied=False)
        e.press("PC", now=100)
        e.sent(100.0, now=100.01)
        self.assertTrue(e.on_bus(limp, asked_at=100.02, now=100.3))
        self.assertFalse(e.active)
        self.assertIsNone(e.elapsed(now=400))                                   # item None item
        self.assertEqual(e.status(present=True, bus=self.bus(), bus_age_s=0.1, now=400)["state"], "ready")
        self.assertFalse(e.on_bus(self.bus(), asked_at=400, now=400))

    def test_second_press_waits_for_its_own_ack(self):
        """item #2: item, item."""
        e = EStop()
        limp = self.bus(torque_state_confirmed=False, restore_pending=False, temporary_gains_applied=False)
        e.press("PC", now=0)
        e.sent(0.0, now=0.003)
        e.press("PC", now=1.0)
        self.assertIsNone(e.sent_at)
        self.assertFalse(e.on_bus(limp, asked_at=0.95, now=1.02))             # item, item
        e.sent(0.5, now=1.05)                                                   # item (robotd item)item
        self.assertIsNone(e.sent_at)
        e.sent(1.0, now=1.05)
        self.assertTrue(e.on_bus(limp, asked_at=1.06, now=1.2))
        self.assertEqual(e.confirm_ms, 200)

    def test_failed_resend_after_ack_keeps_waiting(self):
        """item #4: item, item: item, item."""
        e = EStop()
        e.press("PC", now=0)
        e.sent(0.0, now=0.01)
        e.send_failed(2.0, "item: busy", now=2.1)
        self.assertTrue(e.active)
        self.assertEqual(e.send_error, "item: busy")
        e2 = EStop()
        e2.press("PC", now=0)
        e2.send_failed(0.0, "item: item", now=0.2)                       # item: item
        self.assertFalse(e2.active)
        self.assertEqual(e2.outcome, "send_failed")

    def test_saw_torque_off_stops_resend(self):
        e = EStop()
        half = self.bus(torque_state_confirmed=False, restore_pending=True)     # item, hd1910 item
        e.press("PC", now=0)
        e.sent(0.0, now=0.01)
        self.assertFalse(e.on_bus(half, asked_at=0.1, now=0.2))
        self.assertTrue(e.saw_off)

    def test_pending_wins_over_absent(self):
        """item #6: robotd item, item, item: itemE-Stopitem, item'item'."""
        e = EStop()
        e.press("PC", now=0)
        self.assertEqual(e.status(present=False, bus=None, bus_age_s=None, now=0.1)["state"], "pending")

    def test_timeout_shows_then_clears(self):
        e = EStop()
        e.press("Phone", now=200)
        e.finish("timeout", "item 8 itemE-Stop", now=208)
        self.assertEqual(e.status(present=True, bus=self.bus(), bus_age_s=0.1, now=209)["state"], "unavailable")
        self.assertEqual(e.status(present=True, bus=self.bus(), bus_age_s=0.1, now=215)["state"], "ready")

    def test_unavailable_wording_by_mode(self):
        e = EStop()
        why = e.status(present=False, bus=None, bus_age_s=None)["why"]
        self.assertIn("cut power", why)
        e.press("x", now=1)
        e.finish("send_failed", "item", now=1)
        why = e.status(present=True, bus=self.bus(), bus_age_s=0.1, now=2)["why"]
        self.assertIn("power", why)


class StateTest(unittest.TestCase):
    def test_posture_from_real_fixture(self):
        fake = FakeRobotd(FIXTURE)
        self.assertEqual(posture(True, fake.bus, fake.policies, fake.frames[0]), "holding")
        # homed  true,  HOME ( robotd  busStatus  home_start_guard )
        self.assertTrue(fake.bus["home_start_guard"]["ready"])
        lagging = dict(fake.bus, home_start_guard=dict(fake.bus["home_start_guard"], ready=False, max_joint_error_deg=12.3))
        self.assertEqual(posture(True, lagging, fake.policies, fake.frames[0]), "homing")
        legacy = dict(fake.bus)
        legacy.pop("home_start_guard", None)          # item robotd item: item homed
        self.assertEqual(posture(True, legacy, fake.policies, fake.frames[0]), "holding")
        for p, want in (("limp", "limp"), ("policy", "policy"), ("commissioning", "commissioning"), ("starting", "starting")):
            fake.set_posture(p)
            self.assertEqual(posture(True, fake.bus, fake.policies, fake.frames[0]), want, p)
        self.assertEqual(posture(False, None, None, None), "absent")
        fake.set_posture("policy")
        walking = dict(fake.frames[0], move={"applied": [0.2, 0, 0]})
        self.assertEqual(posture(True, fake.bus, fake.policies, walking), "walking")
        self.assertEqual(posture(True, fake.bus, dict(fake.policies, sitting=True), walking), "sitting")

    def test_tilt(self):
        self.assertAlmostEqual(tilt_deg([0, 0, -1]), 0.0)
        g = [0.17364817766693033, 0, -0.984807753012208]          # item 10°
        self.assertAlmostEqual(tilt_deg(g), 10.0, places=4)
        self.assertIsNone(tilt_deg(None))


class RobotdTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeRobotd(FIXTURE)
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", quiet_log())
        self.rd.start()
        for _ in range(100):
            if self.rd.bus.value and self.rd.health.value and self.rd.pad.value:
                break
            await asyncio.sleep(0.02)

    async def asyncTearDown(self):
        await self.rd.stop()
        self.s1.close()
        self.s2.close()

    async def test_snapshot_from_real_data(self):
        s = summarize(self.rd, {"label": "② item"})
        self.assertEqual(s["posture"], "holding")
        self.assertTrue(s["robotd_ok"])
        self.assertEqual(s["mode"], "motion")
        self.assertAlmostEqual(s["battery"]["pct"], 58.77, places=1)
        self.assertEqual(s["servos"]["online"], 15)
        self.assertEqual(s["imu"]["status"], "ok")
        self.assertFalse(s["pad"]["connected"])
        self.assertEqual(s["estop"]["state"], "ready")
        self.assertFalse(any("SoC" in t["msg"] or "CPU" in t["msg"] for t in s["todo"]))  # item 78 °C: item 80 °C item (item)

    async def wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    def relax_calls(self):
        return len([m for _, m in self.fake.calls if m == "robot.relax"])

    async def test_no_resend_after_confirm_and_restand(self):
        """item #1: E-StopitemStand Up, Studio item."""
        await asyncio.sleep(0.2)
        self.rd.set_viewers(0)                                         # item
        self.assertTrue((await self.rd.relax("item"))[0])
        self.assertTrue(await self.wait_for(lambda: self.rd.estop.outcome == "confirmed"))
        self.fake.stand_up()
        for _ in range(30):                                            # 3 item, item 2 item
            summarize(self.rd, {})
            await asyncio.sleep(0.1)
        self.assertEqual(self.relax_calls(), 1)
        self.assertEqual(summarize(self.rd, {})["posture"], "holding")

    async def test_first_relax_after_restart_succeeds(self):
        """item #2: robotd itemE-Stopitem."""
        await asyncio.sleep(0.3)
        self.fake.restart(0.05)                                        # item, 0.05 item
        accepted, why = await self.rd.relax("item")
        self.assertTrue(accepted, why)
        self.assertTrue(await self.wait_for(lambda: self.rd.estop.outcome == "confirmed", 4))

    async def test_second_press_while_robotd_stuck_sends_again(self):
        """item #3: robotd item, 1 item: item, item."""
        self.fake.delays["robot.relax"] = 2.0
        await asyncio.sleep(0.2)
        first = asyncio.create_task(self.rd.relax("item"))
        await asyncio.sleep(1.0)
        second = asyncio.create_task(self.rd.relax("item"))
        await asyncio.sleep(0.2)
        self.assertEqual(self.relax_calls(), 2)
        await asyncio.gather(first, second)

    async def test_log_failure_does_not_block_relax(self):
        """item #1: item (SSH item)item, E-Stopitem."""
        class Broken:
            def write(self, *_):
                raise OSError(5, "Input/output error")

            def flush(self):
                raise OSError(5, "Input/output error")
        noisy = Log(Path(".") / "_testlogs", echo=True, background=False)
        self.rd.log = noisy
        with mock.patch("sys.stdout", Broken()):
            accepted, why = await self.rd.relax("item")
        self.assertTrue(accepted, why)
        self.assertEqual(self.relax_calls(), 1)
        self.assertGreater(noisy.write_errors, 0)

    async def test_slow_calibration_status_does_not_block_bus(self):
        """item #8: item (item), busStatus item 1 item."""
        await self.rd.stop()
        self.fake.delays["robot.busCommand"] = 4.0
        self.rd = Robotd(self.rd.addr, self.rd.cfg.addr, quiet_log())
        self.rd.start()
        await asyncio.sleep(2.0)
        self.assertIsNotNone(self.rd.bus.age())
        self.assertLess(self.rd.bus.age(), 1.5)

    async def test_relax_right_after_robotd_comes_back(self):
        """item #6: robotd item, item: itemE-Stopitem, item."""
        self.fake.restart(2.0)
        await asyncio.sleep(2.1)
        accepted, why = await self.rd.relax("item")
        self.assertTrue(accepted, why)
        self.assertIn(summarize(self.rd, {})["estop"]["state"], ("pending", "confirmed"))
        self.assertTrue(await self.wait_for(lambda: self.rd.estop.outcome == "confirmed", 3))

    async def test_relax_waits_for_hd1910_flags(self):
        self.fake.hd_delay = 0.6                                       # item OFF, hd1910 item 0.6 item
        await asyncio.sleep(0.2)
        await self.rd.relax("item")
        await asyncio.sleep(0.6)                                       # 0.3 item OFF, item
        self.assertTrue(self.rd.estop.active)
        self.assertTrue(await self.wait_for(lambda: self.rd.estop.outcome == "confirmed"))
        self.assertGreaterEqual(self.rd.estop.confirm_ms, 850)

    async def test_relax_in_commissioning_mode(self):
        self.fake.set_posture("commissioning")
        await asyncio.sleep(1.2)
        self.assertTrue((await self.rd.relax("item"))[0])
        self.assertTrue(await self.wait_for(lambda: self.rd.estop.outcome == "confirmed"))
        self.assertEqual(self.fake.bus["mode"], "commissioning")

    async def test_garbage_lines_do_not_kill_polling(self):
        """item #4: robotd item [1,2,3], item."""
        self.fake.inject = [b"[1,2,3]\n", b'"x"\n', b"null\n"]
        await asyncio.sleep(1.5)
        self.assertLess(self.rd.bus.age(), 1.5)
        self.assertFalse(any(t.done() for t in self.rd._tasks))

    async def test_health_error_does_not_block_bus_status(self):
        """item #5: item."""
        self.fake.errors["robot.health"] = {"code": -32603, "message": "boom"}
        await asyncio.sleep(2.5)
        self.assertLess(self.rd.bus.age(), 1.5)
        n_bus = len([m for _, m in self.fake.calls if m == "robot.busStatus"])
        self.assertLess(n_bus, 6, "item 20 Hz item")

    async def test_relax_is_not_blocked_by_a_slow_poll(self):
        self.fake.delays["robot.busStatus"] = 10.0                   # poll item 10 item
        await asyncio.sleep(1.2)
        t0 = time.monotonic()
        accepted, _ = await self.rd.relax("item")
        took = time.monotonic() - t0
        self.assertTrue(accepted)
        self.assertLess(took, 0.3, f"E-Stopitem {took:.2f} item")
        relax_at = [t for t, m in self.fake.calls if m == "robot.relax"][0]
        self.assertLess(relax_at - t0, 0.1)

    async def test_relax_confirms_from_bus_status(self):
        await asyncio.sleep(0.2)                                      # item 1 item
        accepted, _ = await self.rd.relax("item")
        self.assertTrue(accepted)
        for _ in range(50):
            if summarize(self.rd, {})["estop"]["state"] == "confirmed":
                break
            await asyncio.sleep(0.05)
        s = summarize(self.rd, {})
        self.assertEqual(s["estop"]["state"], "confirmed")
        self.assertEqual(s["posture"], "limp")
        #  robotd 0.3  OFF,  0.2  hd1910  0.1 , 
        #  0.8  ( 1 )
        self.assertLess(self.rd.estop.confirm_ms, 800)

    async def test_subscribe_only_while_someone_looks(self):
        await asyncio.sleep(0.3)
        self.assertEqual(self.fake.subscribers, 0)
        self.rd.set_viewers(1)
        for _ in range(60):
            if self.fake.subscribers and self.rd.state.value:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.fake.subscribers, 1)
        self.assertEqual(len(self.rd.state.value["joints"]), 15)
        with mock.patch.object(robotd_mod, "STREAM_LINGER_S", 0.2):
            self.rd.set_viewers(0)
            for _ in range(60):
                if self.fake.subscribers == 0:
                    break
                await asyncio.sleep(0.05)
        self.assertEqual(self.fake.subscribers, 0)

    async def test_background_music_keeps_real_telemetry_after_last_viewer_leaves(self):
        setter = getattr(self.rd, "set_background_music", None)
        self.assertTrue(callable(setter), "background music needs its own telemetry subscriber")
        setter(True)
        for _ in range(60):
            if self.fake.subscribers and self.rd.state.at:
                break
            await asyncio.sleep(.05)
        self.assertEqual(self.rd.viewers, 0)
        self.assertEqual(self.fake.subscribers, 1)
        before = self.rd.state.at
        self.rd._last_viewer_at = time.monotonic() - 11.
        await asyncio.sleep(.25)
        self.assertEqual(self.fake.subscribers, 1)
        self.assertGreater(self.rd.state.at, before)
        setter(False)
        for _ in range(60):
            if not self.fake.subscribers:
                break
            await asyncio.sleep(.05)
        self.assertEqual(self.fake.subscribers, 0)
        before = self.rd.state.at
        await asyncio.sleep(.15)
        self.assertEqual(self.rd.state.at, before, "no synthetic freshness after unsubscribing")

    async def test_robotd_gone_and_back(self):
        self.fake.restart(1.5)                                         # item: item
        for _ in range(60):
            if self.rd.present is False:
                break
            await asyncio.sleep(0.05)
        self.assertIs(self.rd.present, False)
        self.assertEqual(summarize(self.rd, {})["posture"], "absent")
        self.assertEqual(summarize(self.rd, {})["estop"]["state"], "unavailable")
        for _ in range(80):
            if self.rd.present is True:
                break
            await asyncio.sleep(0.05)
        self.assertIs(self.rd.present, True)


# ----------------  busStatus ( 16: 10-20 ,  fork  configuring_bus) ----------------

STATUS_KEYS = ("build", "port", "mode", "phase", "session", "target_hz", "achieved_hz", "total_cycles", "total_success",
               "total_communication_failures", "errors_by_id", "device_communication", "last_cycle_error", "last_failure",
               "last_sample", "calibrated", "imu_mount_verified", "torque_state", "torque_off_pending", "power_pending",
               "motion", "policy_motion_enabled", "updated_at_us")     # fork feetech_commissioning.rs item Status


class FakeCommissioningTest(unittest.IsolatedAsyncioTestCase):
    """item robotd item fork item (item: item, item, item): 
    item (item), item--commissioning-stuck-status item."""

    async def bus(self, fake):
        return (await fake.dispatch("robot.busStatus", {}))[0]

    async def test_boot_report_then_snapshot(self):
        fake = FakeRobotd(FIXTURE, posture="commissioning", mode="commissioning")
        fake.comm_read_s = 0.2
        b = await self.bus(fake)
        self.assertEqual(sorted(b), sorted(("build", "mode", "phase", "port")))
        self.assertEqual((b["mode"], b["phase"], b["port"]), ("commissioning", "configuring_bus", "/dev/ttyS2"))
        self.assertEqual(posture(True, b, None, None), "starting")
        self.assertFalse(robotd_up(b))
        self.assertIsNotNone((await fake.dispatch("robot.busCommand", {"action": "calibration-status"}))[0])   # item
        await asyncio.sleep(0.25)
        b = await self.bus(fake)
        self.assertEqual(b["phase"], "ready")
        for k in STATUS_KEYS:
            self.assertIn(k, b)
        self.assertEqual(len(b["last_sample"]["servos"]), 15)
        self.assertIsNone(b["last_cycle_error"])
        self.assertTrue(robotd_up(b))
        self.assertEqual(posture(True, b, None, None), "commissioning")
        # : , phase read_error (, )
        fake.servo_power = False
        b = await self.bus(fake)
        self.assertEqual(b["phase"], "read_error")
        self.assertIn("Sync Read timeout", b["last_cycle_error"])
        self.assertTrue(robotd_up(b))
        fake.servo_power = True
        # : , 
        fake.restart(0.05)
        self.assertEqual((await self.bus(fake))["phase"], "configuring_bus")
        await asyncio.sleep(0.35)
        self.assertEqual((await self.bus(fake))["phase"], "ready")

    async def test_stuck_status_never_clears(self):
        import fake_tune
        fake = FakeRobotd(FIXTURE, posture="commissioning", mode="commissioning")
        fake.comm_read_s = 0.05
        fake.comm_stuck = True
        await asyncio.sleep(0.15)
        for _ in range(3):
            b = await self.bus(fake)
            self.assertEqual(sorted(b), sorted(("build", "mode", "phase", "port")))
            self.assertEqual(b["phase"], "configuring_bus")
        steps = fake_tune.startup_report(fake)["steps"]
        self.assertEqual(steps["4"]["status"], "success")                    # item fork: item, item
        await fake.dispatch("fake.set", {"commissioning_stuck": False})
        self.assertEqual((await self.bus(fake))["phase"], "ready")           # item fork: item

    async def test_no_servo_power_before_first_read_is_waiting_for_bus(self):
        fake = FakeRobotd(FIXTURE, posture="commissioning", mode="commissioning")
        fake.servo_power = False
        await asyncio.sleep(0.4)
        b = await self.bus(fake)
        self.assertEqual((b["mode"], b["phase"]), ("commissioning", "waiting_for_bus"))
        self.assertIn("Sync Read timeout", b["error"])
        self.assertEqual(posture(True, b, None, None), "starting")

    def test_cli_flag_exists(self):
        src = (HERE.parent / "dev" / "fake_robotd.py").read_text(encoding="utf-8")
        self.assertIn('"--commissioning-stuck-status"', src)


class StartingTimerTest(unittest.IsolatedAsyncioTestCase):
    """Studio item'item': item (hello item), item 30 item."""

    async def asyncSetUp(self):
        self.fake = FakeRobotd(FIXTURE, posture="commissioning", mode="commissioning")
        self.fake.comm_stuck = True                                       # item fork
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", quiet_log())
        self.rd.start()

    async def asyncTearDown(self):
        await self.rd.stop()
        self.s1.close()
        self.s2.close()

    async def wait_for(self, cond, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    async def test_stuck_commissioning_then_cleared(self):
        self.assertTrue(await self.wait_for(lambda: self.rd.starting_since is not None))
        self.assertEqual(self.rd.bus.value["phase"], "configuring_bus")
        since = self.rd.starting_since
        s = summarize(self.rd, {})
        self.assertEqual(s["posture"], "starting")
        self.assertFalse(s["starting_long"])
        await asyncio.sleep(1.2)
        self.assertEqual(self.rd.starting_since, since)                      # item: item
        s = summarize(self.rd, {}, now=since + 31)                          # item: item 31 item
        self.assertTrue(s["starting_long"])
        self.assertIn("robotd stuck in starting state", [t["msg"] for t in s["todo"]])
        self.fake.comm_stuck = False                                        # item fork: item, item
        self.assertTrue(await self.wait_for(lambda: self.rd.starting_since is None
                                            and (self.rd.bus.value or {}).get("phase") == "ready"))
        self.assertEqual(summarize(self.rd, {})["posture"], "commissioning")
        self.fake.comm_stuck = True                                         # item (item fork): item hello item
        self.fake.restart(0.2)
        self.assertTrue(await self.wait_for(lambda: self.rd.starting_since is not None and self.rd.starting_since > since, 6.0))


if __name__ == "__main__":
    unittest.main()
