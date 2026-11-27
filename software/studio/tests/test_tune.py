"""'item' (item v2 T1–T3, item)item: item, item, item, item, item, item.

item: dev/duckfs.py (item JSON / TOML)item robotd: dev/fake_robotd.py + dev/fake_tune.py.
item (item / item / item)item: item active_* item fake_tune item.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "deps"), str(HERE.parent / "dev")]

import duckfs  # noqa: E402
from fake_robotd import FakeRobotd  # noqa: E402
from studio import startup as S  # noqa: E402
from studio import tune as T  # noqa: E402
from studio.log import Log  # noqa: E402
from studio.estop import EStop  # noqa: E402
from studio.robotd import Robotd, Sample  # noqa: E402
from studio.state import summarize  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"


class StubRd:
    """item Robotd: Sample item robotd item."""

    def __init__(self, fake, log, present=True):
        self.log = log
        self.addr = "tcp:127.0.0.1:1"
        self.hello, self.health, self.bus = Sample(), Sample(), Sample()
        self.policies, self.calib, self.state = Sample(), Sample(), Sample()
        self.pad, self.sysinfo = Sample(), Sample()
        self.present, self.paused, self.estop = present, None, EStop()
        self.calib_valid_after = None
        self.startup = S.StartupTrack()
        self.fake = fake
        self.hello.set({"api_version": 40})
        self.refresh()

    def refresh(self):
        f = self.fake
        self.bus.set(json.loads(json.dumps(f.bus)))
        self.health.set(json.loads(json.dumps(f.health)))
        if f.bus.get("mode") == "motion":
            self.policies.set(dict(json.loads(json.dumps(f.policies)), sitting=False))
        self.calib.set(f.calib)
        self.sysinfo.set(f.sysinfo)


def report(*statuses, details=None):
    details = details or {}
    return {"steps": {str(i + 1): {"status": s, "detail": details.get(i + 1, f"item {i + 1} item")}
                      for i, s in enumerate(statuses) if s}, "build": "b"}


class TrackTest(unittest.TestCase):
    def test_progress_keeps_first_seen_time_and_finishes(self):
        t = S.StartupTrack()
        t.reset(1.0)
        t.update(report("success", "starting"), now=10.0)
        self.assertEqual(t.current()[0], "2")
        self.assertFalse(t.done)
        t.update(report("success", "starting"), now=11.0)                # item: item
        self.assertEqual(t.steps["2"]["at"], 10.0)
        t.update(report("success", "waiting", details={2: "item"}), now=12.0)
        self.assertEqual(t.steps["2"]["at"], 12.0)                       # item: item
        self.assertEqual(t.steps["1"]["at"], 10.0)
        t.update(report("success", "success", "success", "success"), now=13.0)
        self.assertTrue(t.done)
        self.assertEqual(t.current(), (None, None))
        v = t.view()
        self.assertEqual([r["status"] for r in v["rows"]], ["success"] * 4)
        self.assertIsNone(v["now"])

    def test_failed_step_is_terminal(self):
        t = S.StartupTrack()
        t.update(report("success", "success", "failed", details={3: "item"}))
        self.assertTrue(t.done)
        self.assertEqual(t.current()[0], "3")

    def test_policy_off_detected_only_on_step3_success(self):
        t = S.StartupTrack()
        t.update(report("success", "success", "success", "success", details={3: "policy disabled"}))
        self.assertTrue(t.policy_off())
        t.update(report("success", "success", "starting", details={3: "policy disabled"}))
        self.assertFalse(t.policy_off())

    def test_garbage_ignored(self):
        t = S.StartupTrack()
        t.update("x")
        t.update({"steps": {"1": "bad", "2": {"status": 3}}})
        self.assertEqual(t.steps["2"]["status"], None)
        self.assertNotIn("1", t.steps)


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def rd(self, posture="holding"):
        return StubRd(FakeRobotd(FIXTURE, posture=posture), self.log)

    def test_starting_hint_names_the_step(self):
        rd = self.rd("starting")
        rd.startup.reset(rd.hello.at)
        rd.startup.update(report("success", "waiting", details={2: "item"}))
        todo = summarize(rd, {})["todo"]
        it = next(i for i in todo if i["key"] == "starting")
        self.assertIn("Step 2/4 (Servo Bus & IMU): item", it["hint"])
        self.assertIn("raw: torque readback", it["hint"])            # busStatus.error item

    def test_old_track_from_previous_hello_is_ignored(self):
        rd = self.rd("starting")
        rd.startup.reset(rd.hello.at - 100)                               # item hello item
        rd.startup.update(report("success", "waiting", details={2: "item"}))
        it = next(i for i in summarize(rd, {})["todo"] if i["key"] == "starting")
        self.assertNotIn("item", it["hint"])

    def test_walk_unloaded_todo(self):
        rd = self.rd("walk_unloaded")
        rd.startup.reset(rd.hello.at)
        rd.startup.update(report("success", "success", "success", "success", details={3: "disabled by config"}))
        todo = summarize(rd, {})["todo"]
        self.assertEqual(todo[0]["key"], "walk_unloaded")
        self.assertEqual(todo[0]["level"], "error")
        self.assertIn("loaded_models", todo[0]["hint"])

    def test_walk_slot_error_named(self):
        f = FakeRobotd(FIXTURE, posture="holding")
        f.policies = json.loads(json.dumps(f.policies))
        f.policies["slots"][0]["error"] = "item"
        rd = StubRd(f, self.log)
        todo = summarize(rd, {})["todo"]
        self.assertIn("Walk slot failed to load: item", todo[0]["hint"])

    def test_no_walk_unloaded_when_fine_paused_or_disabled(self):
        rd = self.rd("holding")
        self.assertFalse(any(i["key"] == "walk_unloaded" for i in summarize(rd, {})["todo"]))
        rd2 = self.rd("walk_unloaded")
        rd2.paused = "item"
        self.assertIsNone(S.walk_unloaded(rd2, rd2.policies.value, rd2.bus.value))
        rd3 = self.rd("walk_unloaded")
        pol = dict(rd3.policies.value, enabled=False)                   # item: item
        self.assertIsNone(S.walk_unloaded(rd3, pol, rd3.bus.value))


class TuneViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "duck"
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, posture="holding", fake_set=None, **tree):
        duckfs.build(self.root, **tree)
        f = FakeRobotd(FIXTURE, posture=posture)
        if fake_set:
            f.dispatch.__self__  # noqa: B018 —— item
            asyncio.run(f.dispatch("fake.set", fake_set))
        rd = StubRd(f, self.log)
        t = T.Tune(rd, self.log, self.tmp.name, root=self.root, duck={"label": "② item"})
        t.io()
        return t, rd, f

    def test_walk_values_from_model_dir_not_policy_section(self):
        t, _, _ = self.make()
        w = t.view()["feel"]["walk"]
        self.assertTrue(w["joint"])
        self.assertEqual(w["id"], duckfs.WALK)
        self.assertEqual(w["values"]["action_scale"], {"file": 1.0, "loaded": 1.0})   # item [policy] item 0.7
        self.assertEqual(w["values"]["legs_lowpass"], {"file": 0.7, "loaded": 0.7})
        self.assertTrue(w["same"])
        self.assertEqual(w["default"], T.BUNDLED_WALK_DEFAULT)
        g = t.view()["feel"]["global"]
        self.assertEqual(g["action_scale"], 0.7)
        self.assertIn("lowpass only applies to sit/stand", g["note"])

    def test_file_differs_from_what_main_program_uses(self):
        t, _, _ = self.make(exec_walk={"legs_lowpass": 0.6})
        w = t.view()["feel"]["walk"]
        self.assertEqual(w["values"]["legs_lowpass"], {"file": 0.6, "loaded": 0.7})
        self.assertFalse(w["same"])

    def test_bad_exec_file_reported_not_crashing(self):
        t, _, _ = self.make(bad_exec="{ not json")
        w = t.view()["feel"]["walk"]
        self.assertTrue(w["file_error"])
        self.assertFalse(w["file"])
        self.assertEqual(w["loaded"]["action_scale"], 1.0)              # item

    def test_default_from_orig_only_for_other_models(self):
        old = T.BUNDLED_WALK
        try:
            T.BUNDLED_WALK = "0" * 32                                    # item
            t, _, _ = self.make(orig=True)
            w = t.view()["feel"]["walk"]
            self.assertEqual(w["default_src"], "Pre-edit backup (.orig)")
            t2, _, _ = self.make(orig=False)
            self.assertIsNone(t2.view()["feel"]["walk"]["default"])        # item .orig: item (item T-R17)
        finally:
            T.BUNDLED_WALK = old

    def test_active_only_when_policy_on(self):
        t, _, _ = self.make("holding")
        self.assertIsNone(t.view()["feel"]["active"])
        t2, _, _ = self.make("policy")
        a = t2.view()["feel"]["active"]
        self.assertAlmostEqual(a["action_scale"], 0.981386, places=5)
        self.assertEqual((a["head_lowpass"], a["legs_lowpass"]), (0.5, 0.7))
        r = t2.view()["run"]
        self.assertEqual(r["net"]["scale_use"], "joint_uniform")
        self.assertEqual(r["scale"]["base"], 1.0)

    def test_servo_v167_default_when_no_file(self):
        t, _, _ = self.make()
        s = t.view()["feel"]["servo"]
        self.assertTrue(s["fork"])
        self.assertEqual(s["effective"], {"kp": 5, "kd": 0, "torque_limit": 1000})
        self.assertFalse(s["file_exists"])
        self.assertEqual(s["written_p"]["p"], 5)
        self.assertEqual(s["regs"]["eeprom"], [{"pdi": [32, 40, 0], "n": 15}])
        self.assertEqual(s["warn"], [])

    def test_servo_file_written_but_not_loaded_yet(self):
        t, _, _ = self.make(servo={"kp": 12, "kd": 4, "torque_limit": 400})
        s = t.view()["feel"]["servo"]
        self.assertEqual(s["file"], {"kp": 12, "kd": 4, "torque_limit": 400})
        self.assertIsNone(s["loaded"])
        self.assertFalse(s["file_vs_loaded"])
        self.assertEqual(s["effective"]["kp"], 5)                          # item: item

    def test_servo_loaded_and_warnings(self):
        p = {"kp": 12, "kd": 4, "torque_limit": 400}
        t, _, _ = self.make(servo=p, fake_set={"servo_parameters": p})
        s = t.view()["feel"]["servo"]
        self.assertTrue(s["file_vs_loaded"])
        self.assertEqual(s["effective"], p)
        self.assertEqual(s["written_p"]["src"], "Parameter file")
        self.assertEqual(len(s["warn"]), 3)                                # P > 10, item < 500, D > 0

    def test_servo_bad_file(self):
        t, _, _ = self.make(bad_servo='{"kp": "5", "kd": 0, "torque_limit": 1000}')
        self.assertIn("Invalid format", t.view()["feel"]["servo"]["file_error"])
        t2, _, _ = self.make(bad_servo="not json")
        self.assertTrue(t2.view()["feel"]["servo"]["file_error"])

    def test_original_firmware_cannot_edit(self):
        t, _, _ = self.make(fake_set={"servo_parameters": "absent"})
        v = t.view()
        self.assertIs(v["cap"]["fork"], False)
        self.assertIn("does not expose", v["cap"]["why"])
        self.assertFalse(v["cap"]["can_edit"])
        t2, _, _ = self.make()
        self.assertFalse(t2.view()["cap"]["can_edit"])                     # item
        self.assertIn("Read-only mode", t2.view()["cap"]["why"])

    def test_regs_only_after_arm(self):
        t, _, _ = self.make(fake_set={"regs": None})
        s = t.view()["feel"]["servo"]
        self.assertIsNone(s["regs"])
        self.assertIn("standing up", s["regs_why"])

    def test_pad_mouth_zero_is_flagged(self):
        t, _, _ = self.make()
        p = t.view()["feel"]["pad"]
        self.assertTrue(p["mouth_warn"])
        self.assertEqual(p["current"]["mouth_percent"], 0.0)
        self.assertEqual(p["previous"]["mouth_percent"], 6.0)
        self.assertTrue(p["same"])
        t2, _, _ = self.make(mouth=80.0, pad_live=False)
        p2 = t2.view()["feel"]["pad"]
        self.assertFalse(p2["mouth_warn"])
        self.assertIsNone(p2["live"])
        t3, _, _ = self.make(no_pad_file=True)
        self.assertEqual(t3.view()["feel"]["pad"]["current"], T.PAD_DEFAULT)   # item = item 100% / 2.5

    def test_tomls_differ(self):
        t, _, _ = self.make(saved_differs=True)
        self.assertIs(t.view()["feel"]["tomls_same"], False)
        t2, _, _ = self.make()
        self.assertIs(t2.view()["feel"]["tomls_same"], True)

    def test_models_rows(self):
        t, _, _ = self.make()
        m = t.view()["models"]
        rows = {r["slot"]: r for r in m["slots"]}
        self.assertEqual(rows["walk"]["rounds"], 9600)
        self.assertEqual(rows["sitstand"]["rounds"], 10000)                # item manifest: item
        self.assertEqual(rows["sitstand"]["buttons"], ["D-Pad Down"])
        self.assertEqual(rows["kick_left"]["buttons"], ["LB"])
        self.assertEqual(rows["sitstand"]["name"], "Sit / Stand 10000")   # item model-info: item management-profile
        self.assertIsNone(rows["stand"]["path"])
        self.assertTrue(rows["walk"]["loaded"])
        self.assertTrue(rows["ground_pick"]["unfiltered"])
        self.assertEqual(rows["walk"]["sha12"], "1cbf8730eadd")
        rl = {r["id"]: r for r in m["rollers"]}
        self.assertTrue(rl["standup"]["installed"])
        self.assertEqual(rl["standup"]["action_scale"], 1.0)
        self.assertEqual(rl["crouch"]["rounds"], 8000)

    def test_slot_error_and_change_error(self):
        t, _, _ = self.make(fake_set={"slot_error": {"kick_left": "item"}, "change_error": "item"})
        m = t.view()["models"]
        self.assertEqual(next(r for r in m["slots"] if r["slot"] == "kick_left")["error"], "item")
        self.assertEqual(m["change_error"], "item")

    def test_phase_words(self):
        t, rd, _ = self.make("commissioning")
        self.assertEqual(t.view()["run"]["phase"]["word"], "Commissioning")
        t, rd, _ = self.make("starting")
        self.assertEqual(t.view()["run"]["phase"]["word"], "Waiting for servo bus response")
        rd.present = False
        self.assertEqual(t.view()["run"]["phase"]["word"], "robotd Not Running")
        rd.present, rd.paused = True, "item"
        self.assertEqual(t.view()["run"]["phase"]["word"], "Servo Tuning")

    def test_run_card_bits(self):
        t, _, _ = self.make()
        r = t.view()["run"]
        self.assertTrue(r["can_walk"])
        self.assertEqual(r["guard"]["joint_tolerance_deg"], 5.0)
        self.assertEqual([w["id"] for w in r["bus"]["worst"]], [11, 10, 13])
        self.assertEqual(r["imu"]["source"], "lsm6dsv16x")
        self.assertIn("not reported", r["infer_note"])
        t2, _, _ = self.make("walk_unloaded")
        self.assertFalse(t2.view()["run"]["can_walk"])
        self.assertTrue(t2.view()["run"]["walk_unloaded"])

    def test_missed_per_minute(self):
        t, rd, f = self.make()
        h = rd.health.value
        base = time.monotonic()
        t._missed.clear()
        t._missed.extend([(base - 30, 100, 1000), (base, 130, 2500)])
        self.assertEqual(t._missed_per_min(base), 60.0)
        rd.health.set(dict(h, control_loop=dict(h["control_loop"], missed=5, ticks=10)))   # item = robotd item
        t.io()
        self.assertEqual(len(t._missed), 1)

    def test_rounds_of(self):
        self.assertEqual(T.rounds_of(9599), 9600)
        self.assertEqual(T.rounds_of(11000), 11000)
        self.assertEqual(T.rounds_of("a/b/model_7999.pt"), 8000)
        self.assertIsNone(T.rounds_of(True))
        self.assertIsNone(T.rounds_of("abc"))

    def test_file_cache_rereads_on_change(self):
        c = T.FileCache()
        p = Path(self.tmp.name) / "x.json"
        self.assertTrue(c.get(p, T._json)["missing"])
        p.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(c.get(p, T._json)["value"], {"a": 1})
        p.write_text('{"a": 22}', encoding="utf-8")                        # item: item
        self.assertEqual(c.get(p, T._json)["value"], {"a": 22})
        p.write_text("x" * 50, encoding="utf-8")
        self.assertIn("error", c.get(p, T._json, limit=10))

    def test_refuses_edits(self):
        t, _, _ = self.make()
        for op in ("tune_walk", "tune_servo", "tune_pad", "undo", "default"):
            with self.assertRaises(T.Refused):
                asyncio.run(t.handle({"op": op}))
        with self.assertRaises(T.Refused):
            asyncio.run(t.handle({"op": "rm -rf"}))

    def test_profile_and_body(self):
        t, _, _ = self.make()
        p = t.profile()
        self.assertEqual(p["schema"], "microduck-studio-duck-profile/v1")
        self.assertIsNone(p["body"])
        self.assertIsNone(asyncio.run(t.handle({"op": "body"})))
        self.assertEqual(len(p["joints"]), 15)
        self.assertEqual(p["imu"]["chip"], "lsm6dsv16x")
        self.assertEqual(p["servo"]["effective"], {"kp": 5, "kd": 0, "torque_limit": 1000})
        self.assertEqual(p["walk_model"]["rounds"], 9600)
        self.assertEqual(len(p["servo"]["registers_before_arm"]), 15)
        for bad in ({"total_g": -1}, {"total_g": 99999}, {"total_g": "item"}, {"foot": "x\ny"}, {"foot": "item" * 41}, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(T.Refused):
                    t.save_body(bad)
        out = t.save_body({"total_g": "806", "trunk_g": 300, "head_g": None, "foot": "item"}, by="PC 127.0.0.1")
        self.assertEqual(out["total_g"], 806.0)
        self.assertEqual(t.profile()["body"]["foot"], "item")
        saved = json.loads((Path(self.tmp.name) / T.BODY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["trunk_g"], 300.0)
        self.assertIsNone(saved["head_g"])
        #  body : 
        got = asyncio.run(t.handle({"op": "body"}))
        self.assertEqual((got["total_g"], got["foot"], got["head_g"]), (806.0, "item", None))

    def test_view_without_files(self):
        """item (item / item): item, item."""
        f = FakeRobotd(FIXTURE, posture="holding")
        rd = StubRd(f, self.log)
        t = T.Tune(rd, self.log, self.tmp.name, root=Path(self.tmp.name) / "nothing")
        self.assertFalse(t.view()["ready"])
        t.io()
        v = t.view()
        self.assertTrue(v["ready"])
        self.assertEqual(v["feel"]["walk"]["values"]["action_scale"]["loaded"], 1.0)
        self.assertIsNone(v["feel"]["pad"]["file"])
        self.assertIsNone(v["feel"]["tomls_same"])


class StartupLoopTest(unittest.IsolatedAsyncioTestCase):
    """item Robotd + item robotd: item, item, item, item robotd item, robotd item."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.fake = FakeRobotd(FIXTURE, posture="holding")
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", self.log)

    async def asyncTearDown(self):
        await self.rd.stop()
        self.s1.close()
        self.s2.close()
        self.tmp.cleanup()

    async def until(self, cond, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return False

    async def test_follows_steps_then_closes(self):
        await self.fake.dispatch("fake.set", {"startup": report("success", "waiting", details={2: "item"})})
        self.rd.start()
        self.assertTrue(await self.until(lambda: self.rd.startup.current()[0] == "2"))
        self.assertTrue(await self.until(lambda: getattr(self.fake, "startup_subs", 0) == 1))
        self.assertFalse(self.rd.startup.done)
        await self.fake.dispatch("fake.set", {"startup": report("success", "success", "success", "success")})
        self.assertTrue(await self.until(lambda: self.rd.startup.done))
        self.assertTrue(await self.until(lambda: self.fake.startup_subs == 0))   # item: item
        n = sum(1 for _, m in self.fake.calls if m == "hello")
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.startup_subs, 0)                             # item hello item
        self.assertEqual(sum(1 for _, m in self.fake.calls if m == "hello"), n)

    async def test_old_robotd_without_method(self):
        self.fake.errors["robot.startupSubscribe"] = {"code": -32601, "message": "method not found"}
        self.rd.start()
        self.assertTrue(await self.until(lambda: self.rd.startup.unsupported is not None))
        self.assertIn("does not report startup steps", self.rd.startup.unsupported)

    async def test_restart_starts_over(self):
        self.rd.start()
        self.assertTrue(await self.until(lambda: self.rd.startup.done))
        first = self.rd.startup.epoch
        await self.fake.dispatch("fake.set", {"startup": report("success", "starting")})
        self.fake.restart(0.3)
        self.assertTrue(await self.until(lambda: self.rd.startup.epoch not in (None, first), timeout=10))
        self.assertTrue(await self.until(lambda: self.rd.startup.current()[0] == "2", timeout=5))


class TuneHttpTest(unittest.IsolatedAsyncioTestCase):
    """item Studio: item / item {t:'tune'}item/api/profileitemtune.op save_bodyitem."""

    async def asyncSetUp(self):
        import uvicorn
        from studio.app import build_app
        from studio.auth import Auth
        self.tmp = tempfile.TemporaryDirectory()
        log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.auth = Auth(Path(self.tmp.name) / "state", log)
        self.fake = FakeRobotd(FIXTURE, posture="holding")
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", log)
        root = duckfs.build(Path(self.tmp.name) / "duck")
        self.tune = T.Tune(self.rd, log, str(Path(self.tmp.name) / "state"), root=root, duck={"label": "item"})
        app = build_app(auth=self.auth, robotd=self.rd, log=log, web_dir=HERE.parent / "web", duck={"label": "item"},
                        state_dir=str(Path(self.tmp.name) / "state"), tune=self.tune)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=0, ws="wsproto", http="h11", lifespan="on", log_level="critical")
        self.server = uvicorn.Server(cfg)
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        self.server.should_exit = True
        await self.task
        self.s1.close()
        self.s2.close()
        self.tmp.cleanup()

    async def ws(self, route):
        from tests.test_http import WsClient
        c = WsClient(self.port, origin=self.origin)
        await c.open()
        await c.send({"t": "hb", "seq": 1, "visible": True, "route": route})
        return c

    async def test_pushed_on_overview_not_on_servo_page(self):
        c = await self.ws("/")
        ok = lambda m: m.get("t") == "tune" and m.get("ready") and (m["run"].get("startup") or {}).get("done")  # noqa: E731
        await c.pump(until=lambda: any(ok(m) for m in c.msgs), timeout=8)
        tv = [m for m in c.msgs if ok(m)]
        self.assertTrue(tv, [m.get("t") for m in c.msgs][:20])
        self.assertEqual(tv[-1]["feel"]["walk"]["values"]["action_scale"]["file"], 1.0)
        self.assertEqual(tv[-1]["run"]["startup"]["rows"][3]["status"], "success")
        c.close()
        c2 = await self.ws("/servos")
        await c2.pump(until=lambda: False, timeout=2.5)
        self.assertFalse([m for m in c2.msgs if m.get("t") == "tune"])
        c2.close()

    async def test_profile_and_save_body_and_diag(self):
        from tests.test_http import http_call
        c = await self.ws("/settings")
        await c.send({"t": "cmd", "id": "b1", "name": "tune.op", "args": {"op": "save_body", "body": {"total_g": 806, "foot": "item"}}})
        await c.send({"t": "cmd", "id": "b2", "name": "tune.op", "args": {"op": "tune_servo"}})
        await c.pump(until=lambda: sum(1 for m in c.msgs if m.get("t") == "result") >= 2, timeout=5)
        res = {m["id"]: m for m in c.msgs if m.get("t") == "result"}
        self.assertTrue(res["b1"]["ok"], res)
        self.assertFalse(res["b2"]["ok"])
        self.assertIn("Read-only mode", res["b2"]["reason"])
        c.close()
        await asyncio.sleep(2.5)                                       # item hello, calibration-status, item
        st, h, body = await asyncio.to_thread(http_call, self.port, "GET", "/api/profile")
        self.assertEqual(st, 200)
        self.assertIn("attachment", h["content-disposition"])
        p = json.loads(body)
        self.assertEqual(p["body"]["total_g"], 806.0)
        self.assertEqual(p["walk_model"]["rounds"], 9600)
        st, _, z = await asyncio.to_thread(http_call, self.port, "GET", "/api/diag")
        self.assertEqual(st, 200)
        import io
        raw = json.loads(zipfile.ZipFile(io.BytesIO(z)).read("robotd-raw.json"))
        self.assertIn("tune", raw)
        self.assertEqual(raw["tune"]["files"]["pad_file"], "present")


if __name__ == "__main__":
    unittest.main()
