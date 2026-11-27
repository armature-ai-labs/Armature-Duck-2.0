"""item (overview-merge-plan.md §5, item overview-merge-review.md)item: item (studio/onboard.py), 
item (state.cpu_view), item hello item (robotd.fresh_calib), IMU item'item'item.

item'item'item (§5 O14 item 6 item, item 11 item'item')item: ✓ ok · ● todo · ○ no · ? unk.
item (studio item): 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.','dev'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_onboard'])"
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "deps"), str(HERE.parent / "dev")]

from fake_robotd import FakeRobotd  # noqa: E402
from studio import onboard as O  # noqa: E402
from studio.imu import ImuBackend  # noqa: E402
from studio.log import Log  # noqa: E402
from studio.robotd import Robotd, Sample, fresh_calib  # noqa: E402
from studio.state import cpu_view, summarize, todo  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"
XBOX = "78:86:2E:BB:13:28"


def recorded():
    f = FakeRobotd(FIXTURE)
    return copy.deepcopy(f.bus), copy.deepcopy(f.calib), copy.deepcopy(f.health)


BUS, CALIB, HEALTH = recorded()
BUILD = BUS["build"]


def calib_with(dirs=None, **joint_zero):
    c = copy.deepcopy(CALIB)
    for j in c["calibration"]["joints"]:
        if j["name"] in joint_zero:
            j["zero_raw"] = joint_zero[j["name"]]
        if dirs and j["name"] in dirs:
            j["direction"] = dirs[j["name"]]
    return c


def pad_view(known=True, pads=(), attached=False, device=None, estop="ok", checks=None):
    pads = [dict(p) for p in pads]
    active = next((p for p in pads if p.get("connected")), None)
    return {"known": known, "error": None if known else "unavailable", "pads": pads, "active": active, "attached": attached,
            "device": device, "estop": {"state": estop, "checks": checks or []}}


XPAD = {"mac": XBOX, "name": "Xbox Wireless Controller", "paired": True, "trusted": True, "connected": True}


def inp(bus=None, calib="rec", pad=None, studio=None, present=True, servo_session=False, commit=None, starting_s=None):
    bus = copy.deepcopy(BUS) if bus is None else bus
    calib = copy.deepcopy(CALIB) if calib == "rec" else calib
    return {"bench": False, "present": present, "bus": bus, "calib": calib, "servo_session": servo_session,
            "pad": pad if pad is not None else pad_view(), "studio_servo": studio or {"ids": set(), "time": None},
            "fp": O.full_fp(bus, calib), "commit": commit, "starting_s": starting_s}


def row(view, key):
    return next(r for r in view["rows"] if r["key"] == key)


class FakeRd:
    """robotd.Robotd item (onboard / summarize item)."""

    def __init__(self, bus=None, calib="rec", health="rec", present=True):
        self.addr = "tcp:x"
        self.present = present
        self.bus, self.calib, self.health = Sample(), Sample(), Sample()
        self.policies, self.state, self.pad, self.sysinfo, self.hello = Sample(), Sample(), Sample(), Sample(), Sample()
        self.calib_valid_after = None
        self.bus.set(copy.deepcopy(BUS) if bus is None else bus)
        if calib is not None:
            self.calib.set(copy.deepcopy(CALIB) if calib == "rec" else calib)
        if health is not None:
            self.health.set(copy.deepcopy(HEALTH) if health == "rec" else health)
        self.policies.set({"sitting": False, "homed": True})
        from studio.estop import EStop
        self.estop = EStop()


# ----------------  ----------------

class RowTest(unittest.TestCase):
    def test_header(self):
        self.assertTrue(O.header_of(inp())["ok"])
        self.assertEqual(O.header_of(inp(present=False))["kind"], "absent")
        stuck = {"build": BUILD, "mode": "motion", "phase": "waiting_for_bus", "error": "motion requires all 15 calibrated joints, measured limits and verified IMU mounting for the lsm6dsv16x IMU"}
        h = O.header_of(inp(bus=stuck, calib=None))
        self.assertEqual(h["kind"], "stuck_calib")
        self.assertIn("calibration or IMU unverified", h["text"])
        self.assertIn("verified IMU mounting", h["raw"])
        h = O.header_of(inp(bus=dict(stuck, error="Feetech Sync Read timeout; missing IDs [21]"), calib=None))
        self.assertEqual(h["kind"], "starting")
        self.assertIn("Waiting for servo bus response", h["text"])
        self.assertEqual(O.header_of(inp(bus={"mode": "commissioning", "phase": "ready"}))["kind"], "commissioning")
        self.assertEqual(O.header_of(inp(servo_session=True))["kind"], "servo")

    def test_header_commissioning_boot_report_and_snapshot(self):
        """item 16: item busStatus item (starting)item.
        item 30 item, item'item' (10-20 item: item fork item configuring_bus)."""
        boot = {"build": BUILD, "mode": "commissioning", "phase": "configuring_bus", "port": "/dev/ttyS2"}
        h = O.header_of(inp(bus=boot))
        self.assertEqual((h["kind"], h["text"]), ("starting", "robotd is still starting"))
        h = O.header_of(inp(bus=boot, starting_s=31))
        self.assertEqual(h["kind"], "starting")
        self.assertIn("robotd stuck starting for over 30s", h["text"])
        self.assertIn("stale status", h["text"])
        self.assertIn("check Logs", h["text"])
        self.assertNotIn("10 item", h["text"])
        h = O.header_of(inp(bus=dict(boot, phase="waiting_for_bus", error="Feetech Sync Read timeout"), starting_s=40))
        self.assertIn("Waiting for servo bus response", h["text"])
        self.assertEqual(h["raw"], "Feetech Sync Read timeout")
        for phase in ("ready", "moving", "stop_unconfirmed", "power_pending", "read_error"):
            with self.subTest(phase=phase):
                self.assertEqual(O.header_of(inp(bus={"mode": "commissioning", "phase": phase}))["kind"], "commissioning")
        h = O.header_of(inp(bus={"mode": "commissioning", "phase": "read_error", "last_cycle_error": "missing IDs [21]"}))
        self.assertIn("servo read failed", h["text"])
        self.assertEqual(h["raw"], "missing IDs [21]")                         # item

    def test_servo_four_states(self):
        rec = {}
        # ●  15  2048,  Studio  (, ):  +'''Skip'
        #  (', ''':  10-20)
        r = row(O.evaluate(inp(), rec), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertEqual([a["do"] for a in r["actions"]], ["servos"])
        self.assertEqual(r["mark"]["done"], {"confirm": False, "ask": None})
        self.assertIn("mark Done", r["text"])
        # ● zero_raw  2048: , , 
        r = row(O.evaluate(inp(calib=calib_with(left_knee=1990)), rec), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("custom zero offsets", r["text"])
        self.assertIn("Left Knee 1990 (-5.1 deg)", r["text"])
        # ✓ Studio  15 ,  ( C9-3: '')
        stu = {"ids": set(O.SERVO_IDS), "time": "2026-10-18 03:12:00"}
        ss = {"servo_studio": {"at": 1.7e9, "fp": O.joints_fp(CALIB), "src": stu["time"]}}
        r = row(O.evaluate(inp(studio=stu), ss), "servo")
        self.assertEqual((r["state"], r["when"]), ("ok", "2026-10-18 03:12:00"))
        r = row(O.evaluate(inp(studio=stu), {"servo_studio": dict(ss["servo_studio"], fp="old")}), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("changed since Servo page calibration", r["text"])
        # ✓ ', ',  → ●
        fp = O.joints_fp(CALIB)
        self.assertEqual(row(O.evaluate(inp(), {"confirm_servo": {"at": 1.7e9, "fp": fp}}), "servo")["state"], "ok")
        r = row(O.evaluate(inp(), {"confirm_servo": {"at": 1.7e9, "fp": "old"}}), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("changed", r["text"])
        # ○  / 
        c = copy.deepcopy(CALIB)
        c["uncalibrated_joints"] = ["left_knee"]
        self.assertEqual(row(O.evaluate(inp(calib=c), rec), "servo")["state"], "no")
        stuck = {"build": BUILD, "mode": "motion", "phase": "waiting_for_bus", "error": "motion requires all 15 calibrated joints"}
        r = row(O.evaluate(inp(bus=stuck, calib=None), rec), "servo")
        self.assertEqual(r["state"], "no")
        self.assertIn("calibration or IMU orientation incomplete", r["text"])
        self.assertEqual(row(O.evaluate(inp(bus={"mode": "commissioning", "phase": "ready", "calibrated": False}, calib=None), rec),
                             "servo")["state"], "no")
        # ? 
        self.assertEqual(row(O.evaluate(inp(calib=None), rec), "servo")["state"], "unk")

    def test_imu_four_states(self):
        src, mount = "lsm6dsv16x", CALIB["imu_mount"]["quat"]
        mfp = O.mount_fp(src, mount)
        r = row(O.evaluate(inp(), {}), "imu")
        self.assertEqual(r["state"], "todo")                                  # item, Studio item
        self.assertIn("8.5 deg", r["text"])
        ok = {"at": 1.7e9, "source": src, "mount_fp": mfp, "diff_deg": 0.8, "tilt_deg": 1.2, "ok": True}
        self.assertEqual(row(O.evaluate(inp(), {"imu_check": ok}), "imu")["state"], "ok")
        r = row(O.evaluate(inp(), {"imu_check": dict(ok, diff_deg=3.4, ok=False)}), "imu")
        self.assertEqual(r["state"], "todo")
        self.assertIn("3.4 deg", r["text"])
        r = row(O.evaluate(inp(), {"imu_check": dict(ok, mount_fp="other")}), "imu")      # item
        self.assertEqual(r["state"], "todo")
        self.assertIn("changed", r["text"])
        c = copy.deepcopy(CALIB)
        c["imu_mount"]["verified"] = False
        self.assertEqual(row(O.evaluate(inp(calib=c), {"imu_check": ok}), "imu")["state"], "no")
        self.assertEqual(row(O.evaluate(inp(bus={"mode": "commissioning", "phase": "ready", "imu_mount_verified": False},
                                            calib=None), {}), "imu")["state"], "no")
        self.assertEqual(row(O.evaluate(inp(calib=None), {"imu_check": ok}), "imu")["state"], "unk")

    def test_stuck_calib_distinguishes_servo_and_imu(self):
        """F5: item IMU item IMU, item."""
        stuck = {"build": BUILD, "mode": "motion", "phase": "waiting_for_bus",
                 "error": "motion requires all 15 calibrated joints, measured limits and verified IMU mounting"}

        # , IMU : IMU  hard (Skip),  hard
        c = copy.deepcopy(CALIB)
        c["uncalibrated_joints"] = ["left_knee"]
        bus = copy.deepcopy(stuck)
        bus["native"] = {"imu_mount_verified": True}
        r_servo = row(O.evaluate(inp(bus=bus, calib=c), {}), "servo")
        r_imu = row(O.evaluate(inp(bus=bus, calib=c), {}), "imu")
        self.assertEqual(r_servo["state"], "no")
        self.assertTrue(r_servo["hard"])
        self.assertIsNone(r_imu["hard"])

        # IMU ,  (uncalibrated_joints ): IMU  hard,  hard
        c = copy.deepcopy(CALIB)
        c["uncalibrated_joints"] = []
        c["imu_mount"]["verified"] = False
        bus = copy.deepcopy(stuck)
        bus["native"] = {"imu_mount_verified": False}
        r_servo = row(O.evaluate(inp(bus=bus, calib=c), {}), "servo")
        r_imu = row(O.evaluate(inp(bus=bus, calib=c), {}), "imu")
        self.assertIsNone(r_servo["hard"])
        self.assertTrue(r_imu["hard"])
        self.assertEqual(r_imu["state"], "no")

        # : IMU  hard (unk, Skip),  hard
        bus = copy.deepcopy(stuck)
        r_servo = row(O.evaluate(inp(bus=bus, calib=None), {}), "servo")
        r_imu = row(O.evaluate(inp(bus=bus, calib=None), {}), "imu")
        self.assertTrue(r_servo["hard"])
        self.assertIsNone(r_imu["hard"])
        self.assertEqual(r_imu["state"], "unk")

    def test_pad_four_states(self):
        self.assertEqual(row(O.evaluate(inp(pad=pad_view(pads=[XPAD])), {}), "pad")["state"], "ok")
        r = row(O.evaluate(inp(pad=pad_view(pads=[dict(XPAD, connected=False)])), {}), "pad")
        self.assertEqual(r["state"], "ok")
        self.assertIn("Offline", r["text"])
        r = row(O.evaluate(inp(pad=pad_view(pads=[dict(XPAD, trusted=False)])), {}), "pad")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Repair", r["text"])
        self.assertEqual(row(O.evaluate(inp(pad=pad_view()), {}), "pad")["state"], "no")
        r = row(O.evaluate(inp(pad=pad_view(known=False)), {"pads_seen": {XBOX: "Xbox Wireless Controller"}}), "pad")
        self.assertEqual(r["state"], "unk")                                    # item configd: item'item'
        self.assertIn("previously paired Xbox", r["text"])
        r = row(O.evaluate(inp(pad=pad_view(known=False, attached=True, device={"name": "Pro Controller"})), {}), "pad")
        self.assertEqual(r["state"], "ok")                                     # item attached item
        self.assertEqual(row(O.evaluate(dict(inp(), pad=None), {}), "pad")["state"], "unk")   # item

    def test_estop_four_states(self):
        rec = {"estop": {"at": 1.7e9, "ms": 180, "mac": XBOX, "name": "Xbox", "build": BUILD}}
        p = pad_view(pads=[XPAD])
        self.assertEqual(row(O.evaluate(inp(pad=p), {}), "estop")["state"], "no")
        r = row(O.evaluate(inp(pad=p), rec), "estop")
        self.assertEqual(r["state"], "ok")
        self.assertIn("180 ms", r["text"])
        other = dict(XPAD, mac="98:B6:E9:28:06:09", name="Pro Controller")
        r = row(O.evaluate(inp(pad=pad_view(pads=[other])), rec), "estop")
        self.assertEqual((r["state"], r["text"]), ("todo", "Gamepad changed: re-test E-Stop"))
        r = row(O.evaluate(inp(pad=p, bus=dict(copy.deepcopy(BUS), build="new-build")), rec), "estop")
        self.assertEqual((r["state"], r["text"]), ("todo", "robotd updated: re-test E-Stop"))
        bad = pad_view(pads=[XPAD], estop="bad", checks=[{"key": "netpad", "label": "item UDP item (netpad)", "ok": False, "why": "item [netpad]"}])
        r = row(O.evaluate(inp(pad=bad), rec), "estop")
        self.assertEqual(r["state"], "todo")
        self.assertIn("netpad", r["text"])
        r = row(O.evaluate(inp(pad=pad_view(known=False)), rec), "estop")      # item, item
        self.assertEqual(r["state"], "unk")
        # : ''
        self.assertEqual(row(O.evaluate(inp(pad=pad_view(pads=[dict(XPAD, connected=False)])), rec), "estop")["state"], "ok")

    def test_walk_four_states(self):
        fp = O.full_fp(BUS, CALIB)
        self.assertEqual(row(O.evaluate(inp(), {}), "walk")["state"], "no")
        r = row(O.evaluate(inp(), {"stood": {"at": 1.7e9, "fp": fp}}), "walk")
        self.assertEqual(r["state"], "todo")
        self.assertIn("not walked yet", r["text"])
        self.assertEqual(row(O.evaluate(inp(), {"stood": {"at": 1.7e9, "fp": fp}, "walked": {"at": 1.7e9, "fp": fp}}),
                             "walk")["state"], "ok")
        r = row(O.evaluate(inp(), {"stood": {"at": 1.7e9, "fp": "old"}, "walked": {"at": 1.7e9, "fp": "old"}}), "walk")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Calibration changed since last stand & walk", r["text"])
        r = row(O.evaluate(inp(calib=None), {"stood": {"at": 1.7e9, "fp": fp}}), "walk")
        self.assertEqual(r["state"], "unk")
        self.assertIn("cannot verify", r["text"])

    def test_next_points_at_first_not_done_and_not_unknown(self):
        v = O.evaluate(inp(calib=None, pad=pad_view(pads=[XPAD])), {})
        self.assertEqual([r["state"] for r in v["rows"]][:2], ["unk", "unk"])
        self.assertEqual(v["next"], "estop")                                   # ? itemSkip, item ● / ○
        self.assertFalse(v["all_done"])
        fp, mfp = O.full_fp(BUS, CALIB), O.mount_fp("lsm6dsv16x", CALIB["imu_mount"]["quat"])
        rec = {"confirm_servo": {"at": 1.7e9, "fp": O.joints_fp(CALIB)},
               "imu_check": {"at": 1.7e9, "source": "lsm6dsv16x", "mount_fp": mfp, "diff_deg": 0.5, "tilt_deg": 1, "ok": True},
               "estop": {"at": 1.7e9, "ms": 1, "mac": XBOX, "name": "x", "build": BUILD},
               "stood": {"at": 1.7e9, "fp": fp}, "walked": {"at": 1.7e9, "fp": fp}}
        v = O.evaluate(inp(pad=pad_view(pads=[XPAD])), rec)
        self.assertTrue(v["all_done"])
        self.assertIsNone(v["next"])
        json.dumps(v)                                                          # item


# ---------------- ,  ----------------

class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.dir = Path(self.tmp.name) / "state"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_file_is_moved_aside_and_does_not_crash(self):
        (self.dir / "onboarding.json").write_text("{item", encoding="utf-8")
        ob = O.Onboarding(str(self.dir), self.log)
        self.assertEqual(ob.rec, {})
        self.assertTrue((self.dir / "onboarding.json.bad").exists())
        self.assertTrue(any("Could not read onboarding checklist" in e["msg"] for e in self.log.recent(20)))
        ob.tick(FakeRd())                                                     # item
        self.assertEqual(len(ob.view()["rows"]), 6)
        (self.dir / "onboarding.json").write_text("[1, 2]", encoding="utf-8")  # item
        self.assertEqual(O.Onboarding(str(self.dir), self.log).rec, {})

    def test_save_is_atomic_and_reloads(self):
        ob = O.Onboarding(str(self.dir), self.log)
        self.assertTrue(ob.note_estop({"ms": 120, "mac": XBOX.lower(), "name": "Xbox", "build": BUILD}))
        self.assertTrue(ob.save())
        self.assertFalse(ob.save())                                           # item
        data = json.loads((self.dir / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(data["estop"]["mac"], XBOX)                          # MAC item
        self.assertEqual(data["v"], 1)
        self.assertFalse([f for f in os.listdir(self.dir) if f.startswith(".onboarding-")])   # item
        self.assertEqual(O.Onboarding(str(self.dir), self.log).rec["estop"]["ms"], 120)

    def test_stood_and_walked_are_recorded_with_fingerprint(self):
        ob = O.Onboarding(str(self.dir), self.log)
        rd = FakeRd()
        t = 1000.0
        for _ in range(8):                                                    # item 3.5 item
            ob.tick(rd, now=t)
            t += 0.5
        self.assertEqual(ob.rec["stood"]["fp"], O.full_fp(BUS, CALIB))
        self.assertNotIn("walked", ob.rec)
        walking = copy.deepcopy(BUS)
        walking["policy_enabled"] = True
        rd.bus.set(walking)
        rd.state.set({"move": {"applied": [0.2, 0, 0]}})
        for _ in range(6):                                                    # item 2.5 item
            rd.state.set({"move": {"applied": [0.2, 0, 0]}})
            ob.tick(rd, now=t)
            t += 0.5
        self.assertIn("walked", ob.rec)
        self.assertEqual(row(ob.view(), "walk")["state"], "ok")
        #  (): 
        rd.calib.set(calib_with(left_knee=2000))
        ob.tick(rd, now=t)
        self.assertEqual(row(ob.view(), "walk")["state"], "todo")

    def test_servo_session_freezes_rows(self):
        ob = O.Onboarding(str(self.dir), self.log)
        rd = FakeRd()
        ob.tick(rd)
        before = [r["state"] for r in ob.view()["rows"]]

        class Servo:
            state = "open"
        rd.calib.set(None)
        ob.tick(rd, servo=Servo())
        v = ob.view()
        self.assertTrue(v["frozen"])
        self.assertEqual(v["header"]["kind"], "servo")
        self.assertEqual([r["state"] for r in v["rows"]], before)

    def test_confirm_servo_checks_the_file(self):
        ob = O.Onboarding(str(self.dir), self.log)
        ok, why = ob.confirm_servo(FakeRd(calib=calib_with(left_knee=1990)), "PC")
        self.assertFalse(ok)
        self.assertIn("2048", why)
        ok, why = ob.confirm_servo(FakeRd(calib=None), "PC")
        self.assertFalse(ok)
        ok, why = ob.confirm_servo(FakeRd(), "PC")
        self.assertTrue(ok, why)
        self.assertEqual(ob.rec["confirm_servo"]["fp"], O.joints_fp(CALIB))
        ob.tick(FakeRd())
        self.assertEqual(row(ob.view(), "servo")["state"], "ok")

    def test_studio_servo_record_reads_eeprom_backups(self):
        d = self.dir / "servo-calib"
        d.mkdir()
        (d / "eeprom-20261018-031200001.json").write_text(json.dumps(
            {"time": "2026-10-18 03:12:00", "targets": {str(i): 2048 for i in O.SERVO_IDS}, "bad": {"23": "unavailable"}, "sim": False}),
            encoding="utf-8")
        (d / "eeprom-20261018-031300001.json").write_text(json.dumps(
            {"time": "2026-10-18 03:13:00", "targets": {"23": 2048}, "bad": {}, "sim": False}), encoding="utf-8")
        (d / "eeprom-20261018-031400001.json").write_text(json.dumps(
            {"time": "2026-10-18 03:14:00", "targets": {"99": 2048}, "sim": True}), encoding="utf-8")       # item
        (d / "undone-eeprom-20261018-031500001.json").write_text("{}", encoding="utf-8")                   # item
        (d / "eeprom-broken.json").write_text("{item", encoding="utf-8")
        r = O.studio_servo_record(str(self.dir))
        self.assertEqual(r["ids"], set(O.SERVO_IDS))
        self.assertEqual(r["time"], "2026-10-18 03:13:00")
        self.assertEqual(O.studio_servo_record(str(self.dir / "nope")), {"ids": set(), "time": None})

    def test_cpu_hysteresis(self):
        ob = O.Onboarding(str(self.dir), self.log)
        rd = FakeRd()
        for c, hot in ((79.0, False), (80.0, True), (78.0, True), (77.5, True), (76.9, False), (79.9, False)):
            rd.health.set(dict(HEALTH, cpu_temp_c=c))
            ob.tick(rd)
            self.assertEqual(ob.cpu_hot, hot, c)
            #  hot: 78 °C 
            v = cpu_view(rd.health.value, 0.1, BUS, True, None, ob)
            items = todo(rd.health.value, BUS, {"stale": []}, "ok", v, {}, {"pct": 60}, "holding")
            self.assertEqual(any(i["key"] == "cpu" for i in items), hot, c)

    def test_read_board_like_robotd(self):
        th, cf = self.dir / "thermal", self.dir / "cpufreq"
        for name, temp in (("thermal_zone0", "76875"), ("thermal_zone1", "81250"), ("thermal_zone2", "-1000")):
            (th / name).mkdir(parents=True)
            (th / name / "temp").write_text(temp)
        for name, kind, cur, mx in (("cooling_device0", "cpufreq-cpu0", "3", "6"), ("cooling_device1", "devfreq-gpu", "6", "6")):
            (th / name).mkdir()
            for k, v in (("type", kind), ("cur_state", cur), ("max_state", mx)):
                (th / name / k).write_text(v)
        (cf / "policy0").mkdir(parents=True)
        (cf / "policy0" / "scaling_max_freq").write_text("1008000")
        (cf / "policy0" / "cpuinfo_max_freq").write_text("1800000")
        b = O.read_board(str(th), str(cf))
        self.assertEqual(b["c"], 81.25)                                        # item≤ 0 itemSkip
        self.assertEqual(b["throttle"], {"khz": 1008000, "max_khz": 1800000, "level": 3, "max_level": 6})   # GPU item
        self.assertIsNone(O.read_board(str(self.dir / "nope"), str(self.dir / "nope2")))


# ----------------  ----------------

class CpuViewTest(unittest.TestCase):
    def test_health_stale_is_not_normal(self):
        """item O5: health item'Normal': c, throttled item None, item'—'item."""
        v = cpu_view(None, 12.4, BUS, True)
        self.assertIsNone(v["c"])
        self.assertIsNone(v["throttled"])
        self.assertIn("not updated for 12s", v["why"])
        self.assertIn("commissioning mode", cpu_view(None, None, {"mode": "commissioning"}, True)["why"])
        self.assertIn("Waiting for servo bus response", cpu_view(None, None, {"mode": "motion", "phase": "waiting_for_bus"}, True)["why"])
        self.assertIn("not running", cpu_view(None, None, None, False)["why"])

        class Servo:
            state = "open"
        self.assertIn("Servo tuning active", cpu_view(None, None, BUS, True, Servo())["why"])
        self.assertIn("did not report temperature", cpu_view({"battery": {}}, 0.5, BUS, True)["why"])

    def test_throttled_limited_hot(self):
        h = dict(HEALTH, cpu_temp_c=92.4, cpu_throttle={"level": 4, "max_level": 6, "khz": 1008000, "max_khz": 1800000})
        v = cpu_view(h, 0.5, BUS, True)
        self.assertEqual((v["c"], v["throttled"], v["limited"], v["src"]), (92.4, True, False, "robotd"))
        self.assertTrue(v["hot"])
        items = todo(h, BUS, {"stale": []}, "ok", v, {}, {"pct": 60}, "holding")
        self.assertIn("thermal throttling: 1008 / 1800 MHz", next(i for i in items if i["key"] == "cpu")["msg"])
        h = dict(HEALTH, cpu_throttle={"level": 0, "max_level": 6, "khz": 1200000, "max_khz": 1800000})
        v = cpu_view(h, 0.5, BUS, True)
        self.assertEqual((v["throttled"], v["limited"]), (False, True))       # item, item
        v = cpu_view(HEALTH, 0.5, BUS, True)
        self.assertEqual((v["c"], v["throttled"], v["limited"], v["hot"]), (76.9, False, False, False))

    def test_studio_reads_board_when_robotd_does_not(self):
        class Ob:
            cpu_hot = True
            board = {"c": 83.0, "throttle": {"level": 0, "max_level": 6, "khz": 1800000, "max_khz": 1800000}}
        v = cpu_view(None, None, {"mode": "commissioning"}, True, None, Ob())
        self.assertEqual((v["c"], v["src"], v["hot"], v["throttled"]), (83.0, "studio", True, False))

    def test_summarize_carries_onboarding_and_todo_keys(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            log = Log(Path(tmp.name) / "logs", echo=False, background=False)
            ob = O.Onboarding(tmp.name, log)
            rd = FakeRd()
            ob.tick(rd)
            s = summarize(rd, {}, ob=ob)
            self.assertEqual(len(s["onboarding"]["rows"]), 6)
            self.assertEqual(s["cpu"]["c"], 76.9)
            self.assertTrue(all("key" in t for t in s["todo"]))
            rd.health = Sample()                                               # health item
            s = summarize(rd, {}, ob=ob)
            self.assertIsNone(s["cpu"]["c"])
            self.assertIsNone(s["cpu"]["throttled"])
            self.assertTrue(s["cpu"]["why"])
        finally:
            tmp.cleanup()


# ----------------  hello  ( O5) ----------------

class CalibFreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeRobotd(FIXTURE)
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", Log(Path(self.tmp.name), echo=False, background=False))
        self.rd.start()

    async def asyncTearDown(self):
        await self.rd.stop()
        self.s1.close()
        self.s2.close()
        self.tmp.cleanup()

    async def wait_for(self, cond, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    def calib_asks(self):
        return sum(1 for _, m in self.fake.calls if m == "robot.busCommand")

    async def test_old_calib_does_not_count_after_restart(self):
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_fresh() is not None))
        # robotd  ( / IMU ): '15 '
        self.fake.set_posture("stuck_calib")
        self.fake.restart(0.2)
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_valid_after is not None and self.rd.calib.at < self.rd.calib_valid_after
                                            and (self.rd.bus.value or {}).get("phase") == "waiting_for_bus"))
        self.assertIsNone(self.rd.calib_fresh())
        self.assertIsNotNone(self.rd.calib.value)                              # item, item
        #  (phase  control): 
        n = self.calib_asks()
        self.fake.set_posture("holding")
        self.assertTrue(await self.wait_for(lambda: self.calib_asks() > n and self.rd.calib_fresh() is not None, 6.0))


class FreshCalibTest(unittest.TestCase):
    def test_fresh_calib_without_hello_info(self):
        class R:
            calib = Sample()
        r = R()
        self.assertIsNone(fresh_calib(r))
        r.calib.set({"x": 1})
        self.assertEqual(fresh_calib(r), {"x": 1})                             # item calib_valid_after: item
        r.calib_valid_after = time.monotonic() + 1
        self.assertIsNone(fresh_calib(r))


# ---------------- IMU '' ( O4: , ) ----------------

class ImuCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.ob = O.Onboarding(self.tmp.name, self.log)
        self.imu = ImuBackend(FakeRd(), self.log, self.tmp.name)
        self.imu.on_check = self.ob.note_imu_check
        self.view = self.imu.view()
        self.old = self.view["mount"]["quat"]

    def tearDown(self):
        self.tmp.cleanup()

    def res(self, diff, tilt):
        return {"ok": True, "parts": {"total_deg": diff, "stand_tilt_deg": tilt}}

    def test_close_enough_is_recorded(self):
        c = self.imu._check(self.res(0.9, 1.5), self.view, self.old, True, "PC")
        self.assertTrue(c["ok"] and c["recorded"])
        rec = self.ob.rec["imu_check"]
        self.assertEqual(rec["mount_fp"], O.mount_fp("lsm6dsv16x", self.old))
        self.ob.tick(FakeRd())
        self.assertEqual(row(self.ob.view(), "imu")["state"], "ok")

    def test_far_or_tilted_is_recorded_but_not_ok(self):
        c = self.imu._check(self.res(3.2, 1.0), self.view, self.old, True, "PC")
        self.assertFalse(c["ok"])
        self.assertIn("3.2 deg", c["why"])
        self.assertFalse(self.ob.rec["imu_check"]["ok"])
        c = self.imu._check(self.res(0.5, 7.0), self.view, self.old, True, "PC")
        self.assertIn("Standing tilt", c["why"])

    def test_unverified_mount_is_not_recorded(self):
        c = self.imu._check(self.res(0.5, 1.0), self.view, self.old, False, "PC")
        self.assertFalse(c["ok"] or c["recorded"])
        self.assertNotIn("imu_check", self.ob.rec)


# ----------------  C1–C9 (scratchpad console/overviewreview/repro_onboard.py ) ----------------

class ReviewFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.dir = Path(self.tmp.name) / "state"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        (self.dir / "onboarding.json").write_text(json.dumps(data), encoding="utf-8")

    def test_c1_bad_single_records_are_dropped_and_hot_still_works(self):
        """C1: item JSON, item (diff_deg item null, walked item): item, item warn, item
        85 °C item'item'item."""
        src, m = "lsm6dsv16x", CALIB["imu_mount"]["quat"]
        self.write({"v": 1, "imu_check": {"source": src, "mount_fp": O.mount_fp(src, m), "ok": True, "diff_deg": None},
                    "walked": {}, "stood": {"at": 1.7e9, "fp": O.full_fp(BUS, CALIB)}, "future_key": {"x": 1}})
        ob = O.Onboarding(str(self.dir), self.log)
        self.assertNotIn("imu_check", ob.rec)
        self.assertNotIn("walked", ob.rec)
        self.assertIn("stood", ob.rec)                                     # item
        self.assertIn("future_key", ob.rec)                                # item
        warns = [e["msg"] for e in self.log.recent(30) if "Invalid onboarding record" in e["msg"]]
        self.assertEqual(len(warns), 2)
        rd = FakeRd()
        rd.health.set(dict(HEALTH, cpu_temp_c=85.0))
        ob.tick(rd)
        self.assertTrue(ob.cpu_hot)
        self.assertEqual(row(ob.view(), "imu")["state"], "todo")
        self.assertEqual(row(ob.view(), "walk")["state"], "todo")
        #  null  ( evaluate, )
        v = O.evaluate(inp(), {"imu_check": {"source": src, "mount_fp": O.mount_fp(src, m), "ok": False, "diff_deg": None},
                               "estop": {"at": 1.7e9, "ms": None, "mac": XBOX, "build": BUILD}})
        self.assertIn("? deg", row(v, "imu")["text"])

    def test_c1_evaluate_error_shows_question_marks_and_hysteresis_keeps_going(self):
        ob = O.Onboarding(str(self.dir), self.log)
        rd = FakeRd()
        rd.health.set(dict(HEALTH, cpu_temp_c=85.0))
        real = O.evaluate
        O.evaluate = lambda inp, rec: 1 / 0
        try:
            ob.tick(rd)
            ob.tick(rd)
        finally:
            O.evaluate = real
        v = ob.view()
        self.assertEqual(v["header"]["kind"], "error")
        self.assertEqual([r["state"] for r in v["rows"]], ["unk"] * 6)
        self.assertIn("ZeroDivisionError", v["rows"][0]["text"])
        self.assertTrue(ob.cpu_hot)                                        # item
        self.assertEqual(sum(1 for e in self.log.recent(30) if "Onboarding evaluation error" in e["msg"]), 1)   # item
        ob.tick(rd)
        self.assertEqual(ob.view()["header"]["kind"], "ok")                # item

    def test_c2_no_fingerprint_does_not_overwrite_records(self):
        """C2: robotd item, item (item None)item 3 item: item, item."""
        ob = O.Onboarding(str(self.dir), self.log)
        fp = O.full_fp(BUS, CALIB)
        ob.rec = {"stood": {"at": 1.7e9, "fp": fp}, "walked": {"at": 1.7e9, "fp": fp}}
        rd = FakeRd()
        rd.calib_valid_after = rd.calib.at + 1.0                           # item hello item
        walking = copy.deepcopy(BUS)
        walking["policy_enabled"] = True
        rd.bus.set(walking)
        t = 1000.0
        for _ in range(10):
            rd.state.set({"move": {"applied": [0.2, 0, 0]}})
            ob.tick(rd, now=t)
            t += 0.5
        self.assertEqual(ob.rec["stood"]["fp"], fp)
        self.assertEqual(ob.rec["walked"]["fp"], fp)
        rd.calib.set(copy.deepcopy(CALIB))                                 # item, item
        rd.calib_valid_after = rd.calib.at - 0.001
        ob.tick(rd, now=t)
        self.assertEqual(row(ob.view(), "walk")["state"], "ok")

    def test_c4_native_says_unverified_is_not_unknown(self):
        """C4: busStatus item IMU item, item: item 2 item ○, item ?."""
        bus = copy.deepcopy(BUS)
        bus["native"]["imu_mount_verified"] = False
        r = row(O.evaluate(inp(bus=bus, calib=None), {}), "imu")
        self.assertEqual(r["state"], "no")
        self.assertIn("unverified", r["text"])
        ob = O.Onboarding(str(self.dir), self.log)
        rd = FakeRd(bus=bus, calib=None)
        ob.tick(rd)
        s = summarize(rd, {}, ob=ob)
        self.assertIn("imu_unverified", [t["key"] for t in s["todo"]])    # item (item ○ item)
        self.assertEqual(row(s["onboarding"], "imu")["state"], "no")

    def test_c9_tick_does_not_touch_files(self):
        """C9-1: item servo-calib item, /sys/class/thermal item io() (item)itemtick item."""
        ob = O.Onboarding(str(self.dir), self.log)
        calls = []
        real_scan, real_board = O.studio_servo_record, O.read_board
        O.studio_servo_record = lambda d: calls.append("scan") or {"ids": set(O.SERVO_IDS), "time": "2026-10-18 03:12:00"}
        O.read_board = lambda *a: calls.append("board") or {"c": 83.0, "throttle": None}
        try:
            rd = FakeRd(health=None)                                       # robotd item
            ob._board_ok = True
            for _ in range(3):
                ob.tick(rd)
            self.assertEqual(calls, [])
            self.assertTrue(ob.need_board(rd))
            ob.io(board=True, now=0.0)
            self.assertEqual(calls, ["scan", "board"])
            ob.io(board=False, now=1.0)                                    # item, item 10 item: item
            self.assertEqual(calls, ["scan", "board"])
            ob.io(board=False, now=20.0)
            self.assertEqual(calls.count("scan"), 2)
            ob.tick(rd)
            self.assertEqual(row(ob.view(), "servo")["state"], "ok")       # item Studio item
            self.assertIn("servo_studio", ob.rec)                          # item: item (C9-3)
            self.assertFalse(rd.health.value)
            self.assertFalse(ob.need_board(FakeRd()))                      # robotd item: item
        finally:
            O.studio_servo_record, O.read_board = real_scan, real_board

    def test_c9_studio_record_fingerprint_change_needs_recheck(self):
        ob = O.Onboarding(str(self.dir), self.log)
        ob._studio = {"ids": set(O.SERVO_IDS), "time": "2026-10-18 03:12:00"}
        rd = FakeRd()
        ob.tick(rd)
        self.assertEqual(row(ob.view(), "servo")["state"], "ok")
        rd.calib.set(calib_with(left_knee=2048, mouth=2048))
        js = rd.calib.value["calibration"]["joints"]
        js[0]["direction"] = 1                                             # item: item, item 2048
        ob.tick(rd)
        r = row(ob.view(), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("changed", r["text"])

    def test_c9_concurrent_saves_keep_the_newest(self):
        """C9-2: item (0.5 item, 'item'item), item."""
        import threading
        ob = O.Onboarding(str(self.dir), self.log)
        real = O.os.replace
        first = {"slow": True}

        def slow_replace(a, b):
            if first["slow"]:
                first["slow"] = False
                time.sleep(0.3)
            return real(a, b)
        O.os.replace = slow_replace
        try:
            ob._put("stood", {"at": 1.0, "fp": "old"})
            ta = threading.Thread(target=ob.save)
            ta.start()
            time.sleep(0.05)
            ob._put("stood", {"at": 2.0, "fp": "new"})
            tb = threading.Thread(target=ob.save)
            tb.start()
            ta.join()
            tb.join()
        finally:
            O.os.replace = real
        data = json.loads((self.dir / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stood"]["fp"], "new")


class PollReconnectTest(unittest.IsolatedAsyncioTestCase):
    """C8: item, item robotd item control: item, item hello, item."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeRobotd(FIXTURE)
        self.s1, p1 = await self.fake.serve_tcp()
        self.s2, p2 = await self.fake.serve_tcp()
        self.rd = Robotd(f"tcp:127.0.0.1:{p1}", f"tcp:127.0.0.1:{p2}", Log(Path(self.tmp.name), echo=False, background=False))
        self.rd.start()

    async def asyncTearDown(self):
        await self.rd.stop()
        await asyncio.sleep(0.05)
        self.s1.close()
        self.s2.close()
        self.tmp.cleanup()

    async def wait_for(self, cond, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    def hellos(self):
        return sum(1 for _, m in self.fake.calls if m == "hello")

    async def test_timeout_reconnect_is_a_new_hello(self):
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_fresh() is not None))
        n, after0 = self.hellos(), self.rd.calib_valid_after
        self.fake.delays["robot.busStatus"] = 2.6                          # robotd item > 2 item: item, item
        self.assertTrue(await self.wait_for(lambda: self.rd.poll.reconnects > 0, 8.0))
        self.fake.delays.pop("robot.busStatus", None)
        self.assertTrue(await self.wait_for(lambda: self.hellos() > n, 8.0))
        #  robotd  hello , Studio : , 
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_valid_after > after0, 4.0))
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_fresh() is not None and self.rd.calib.at > self.rd.calib_valid_after))

    async def test_leaving_control_invalidates_calib(self):
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_fresh() is not None
                                            and (self.rd.bus.value or {}).get("phase") == "control"))
        self.fake.set_posture("starting")                                  # item, item
        self.assertTrue(await self.wait_for(lambda: (self.rd.bus.value or {}).get("phase") == "waiting_for_bus"))
        self.assertIsNone(self.rd.calib_fresh())
        self.fake.set_posture("holding")
        self.assertTrue(await self.wait_for(lambda: self.rd.calib_fresh() is not None))


# ---------------- Skip /  ( 10-18 : 'Skip, ') ----------------

class FakePad:
    def __init__(self, view):
        self.view = view

    def onboard_view(self):
        return copy.deepcopy(self.view)


class SkipTest(unittest.TestCase):
    """item'item''Skip' (item 10-20)item, item confirmed item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)
        self.ob = O.Onboarding(self.tmp.name, self.log)
        self.rd = FakeRd()
        self.pad = FakePad(pad_view(pads=[XPAD]))

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self):
        self.ob.tick(self.rd, self.pad)
        return self.ob.view()

    def test_each_step_skip_and_unskip(self):
        rec_fp = {"servo": O.joints_fp(CALIB), "imu": O.mount_fp("lsm6dsv16x", CALIB["imu_mount"]["quat"]),
                  "estop": O._hash([XBOX, BUILD]), "walk": O.full_fp(BUS, CALIB)}
        self.pad.view = pad_view(pads=[XPAD])
        v = self.tick()
        for key in ("servo", "imu", "estop", "walk"):
            with self.subTest(step=key):
                r = row(v, key)
                if r["state"] in ("todo", "no"):
                    self.assertEqual(r["mark"]["skip"]["confirm"], key == "estop")   # item
                    if key == "estop":
                        ok, why = self.ob.skip(key, self.rd, self.pad, by="PC 1.2.3.4")
                        self.assertFalse(ok)                                         # item
                        self.assertIn("Confirmation required: Confirm skip? Untested", why)
                    ok, why = self.ob.skip(key, self.rd, self.pad, by="PC 1.2.3.4", confirmed=key == "estop")
                    self.assertTrue(ok, why)
                    self.assertEqual(self.ob.rec["skipped"][key]["fp"], rec_fp[key])
                    self.assertEqual(self.ob.rec["skipped"][key]["by"], "PC 1.2.3.4")
                    r = row(self.tick(), key)
                    self.assertEqual((r["state"], r["marked"]), ("skip", "skip"))
                    self.assertIn("Skipped", r["text"])
                    self.assertEqual(r["go"], O.GO[key])                       # 'item'/'item'item
                    ok, _ = self.ob.unskip(key, by="PC 1.2.3.4")
                    self.assertTrue(ok)
                    self.assertNotIn(key, self.ob.rec["skipped"])
                    self.assertIn(row(self.tick(), key)["state"], ("todo", "no"))
        # : , 
        self.pad.view = pad_view()
        self.assertEqual(row(self.tick(), "pad")["state"], "no")
        self.assertTrue(self.ob.skip("pad", self.rd, self.pad, by="Phone")[0])
        self.assertIsNone(self.ob.rec["skipped"]["pad"]["fp"])
        self.assertEqual(row(self.tick(), "pad")["state"], "skip")
        msgs = [e["msg"] for e in self.log.recent(50)]
        self.assertIn("Onboarding: IMU Orientation skipped (PC 1.2.3.4)", msgs)
        self.assertIn("Onboarding: IMU Orientation unskipped (PC 1.2.3.4)", msgs)
        self.assertTrue(any(e["cat"] == "Calibration" for e in self.log.recent(50) if "skipped" in e["msg"]))
        self.assertTrue(self.ob.save())                                       # item, item
        self.assertEqual(O.Onboarding(self.tmp.name, self.log).rec["skipped"], self.ob.rec["skipped"])

    def test_every_row_offers_done_and_skip(self):
        """item: item (● ○ ?)item'item''Skip' (mark)itemStudio item ✓ item (item)."""
        self.ob.rec["walked"] = {"at": 1.7e9, "fp": O.full_fp(BUS, CALIB)}
        v = self.tick()
        for r in v["rows"]:
            with self.subTest(step=r["key"]):
                if r["state"] == "ok":
                    self.assertIsNone(r["mark"])
                else:
                    self.assertEqual(set(r["mark"]), {"done", "skip", "ack"})
        ok, why = self.ob.skip("walk", self.rd, self.pad)
        self.assertFalse(ok)
        self.assertIn("already completed", why)
        self.assertFalse(self.ob.skip("nope", self.rd)[0])
        self.assertFalse(self.ob.mark("imu", "bogus", self.rd)[0])

    def test_unknown_row_needs_two_clicks_and_binds_fingerprint_later(self):
        """item (?)item: item, item null, item tick item (item)."""
        self.rd.calib_valid_after = self.rd.calib.at + 1                       # item hello item
        r = row(self.tick(), "imu")
        self.assertEqual(r["state"], "unk")
        self.assertTrue(r["mark"]["skip"]["confirm"])
        self.assertIn("Confirm skip? Cannot verify this step right now", r["mark"]["skip"]["ask"])
        self.assertIn("Confirm mark as done? Cannot verify this step right now", r["mark"]["done"]["ask"])
        ok, why = self.ob.skip("imu", self.rd, self.pad)
        self.assertFalse(ok)
        self.assertIn("Confirmation required", why)
        self.assertTrue(self.ob.skip("imu", self.rd, self.pad, by="PC", confirmed=True)[0])
        rec = self.ob.rec["skipped"]["imu"]
        self.assertIsNone(rec["fp"])
        self.assertIn("unk", rec["ack"])
        self.assertEqual(row(self.tick(), "imu")["state"], "skip")
        # : , 
        self.rd.calib_valid_after = self.rd.calib.at - 0.001
        self.assertEqual(row(self.tick(), "imu")["state"], "skip")
        self.assertEqual(self.ob.rec["skipped"]["imu"]["fp"], O.mount_fp("lsm6dsv16x", CALIB["imu_mount"]["quat"]))
        self.assertTrue(any("bound fingerprint" in e["msg"] for e in self.log.recent(30)))
        self.assertTrue(self.ob.save())
        self.assertEqual(O.Onboarding(self.tmp.name, self.log).rec["skipped"]["imu"]["ack"], ["unk", "fp"])   # ack item, item

    def test_imu_native_unverified_needs_two_clicks(self):
        """item IMU item (hard): item, item, item, item confirmed item."""
        bus = copy.deepcopy(BUS)
        bus["native"]["imu_mount_verified"] = False
        self.rd.bus.set(bus)
        r = row(self.tick(), "imu")
        self.assertEqual(r["state"], "no")
        self.assertTrue(r["mark"]["skip"]["confirm"])
        self.assertEqual(r["mark"]["skip"]["ask"], "Confirm skip? robotd has not verified IMU orientation: even if Skipped, the duck cannot stand")
        self.assertEqual(r["mark"]["done"]["ask"], "Confirm mark as done? robotd has not verified IMU orientation: even if Marked done, the duck cannot stand")
        ok, why = self.ob.skip("imu", self.rd, self.pad, by="PC")
        self.assertFalse(ok)
        self.assertIn("even if Skipped, the duck cannot stand", why)
        self.assertNotIn("imu", self.ob.rec.get("skipped") or {})
        # Skip (): 
        self.ob.rec["skipped"] = {"imu": {"at": 1.7e9, "by": "x", "fp": O.mount_fp("lsm6dsv16x", CALIB["imu_mount"]["quat"])}}
        r = row(self.tick(), "imu")
        self.assertEqual(r["state"], "no")
        self.assertIn("skip has no effect", r["note"])
        # :  (ack  hard),  note
        self.ob.rec["skipped"] = {}
        self.assertTrue(self.ob.skip("imu", self.rd, self.pad, by="PC", confirmed=True)[0])
        self.assertIn("hard", self.ob.rec["skipped"]["imu"]["ack"])
        r = row(self.tick(), "imu")
        self.assertEqual(r["state"], "skip")
        self.assertIn("unverified", r["note"])
        bus["native"]["imu_mount_verified"] = True                             # item: itemSkipitem
        self.rd.bus.set(bus)
        self.assertEqual(row(self.tick(), "imu")["state"], "skip")

    def test_fingerprint_change_voids_skip_but_none_does_not(self):
        self.assertTrue(self.ob.skip("servo", self.rd, self.pad)[0])
        self.assertTrue(self.ob.skip("estop", self.rd, self.pad, confirmed=True)[0])
        #  ( None): 
        self.rd.calib_valid_after = self.rd.calib.at + 1
        self.pad.view = pad_view(known=False)
        v = self.tick()
        self.assertEqual(row(v, "servo")["state"], "skip")
        self.assertEqual(row(v, "estop")["state"], "skip")
        # ,  (,  2048)/ : ,  ●, 
        self.rd.calib.set(calib_with(dirs={"left_knee": 1}))
        self.rd.calib_valid_after = self.rd.calib.at - 0.001
        other = dict(XPAD, mac="98:B6:E9:28:06:09", name="Pro Controller")
        self.pad.view = pad_view(pads=[other])
        v = self.tick()
        r = row(v, "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Servo zeroing changed after being skipped", r["text"])
        r = row(v, "estop")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Gamepad (or robotd build) changed after being skipped", r["text"])
        self.assertTrue(r["mark"]["skip"]["confirm"])                         # item (item), item
        self.assertTrue(self.ob.skip("estop", self.rd, self.pad, confirmed=True)[0])
        self.assertEqual(row(self.tick(), "estop")["state"], "skip")

    def test_fingerprint_unknown_can_still_be_marked(self):
        """item, item, item (item None): item, item."""
        two = [dict(XPAD, connected=False), dict(XPAD, mac="98:B6:E9:28:06:09", connected=False)]
        self.pad.view = pad_view(pads=two)
        r = row(self.tick(), "estop")
        self.assertIn("connect one first", r["text"])                     # item D11: item
        self.assertTrue(r["mark"]["skip"]["confirm"])
        self.assertIn("fp", r["mark"]["ack"])
        self.assertFalse(self.ob.skip("estop", self.rd, self.pad)[0])
        self.assertTrue(self.ob.skip("estop", self.rd, self.pad, confirmed=True)[0])
        self.assertIsNone(self.ob.rec["skipped"]["estop"]["fp"])
        self.pad.view = pad_view(pads=[dict(XPAD, connected=True), two[1]])
        self.assertEqual(row(self.tick(), "estop")["state"], "skip")
        self.assertEqual(self.ob.rec["skipped"]["estop"]["fp"], O._hash([XBOX, BUILD]))

    def test_web_only_estop_can_be_skipped(self):
        """item (item): item'item……itemSkipitem', item, Skipitem."""
        self.pad.view = pad_view()
        r = row(self.tick(), "estop")
        self.assertIn("Space / top E-Stop button works directly", r["text"])
        self.assertTrue(self.ob.skip("estop", self.rd, self.pad, confirmed=True)[0])
        self.assertEqual(row(self.tick(), "estop")["state"], "skip")
        self.pad.view = pad_view(pads=[XPAD])
        self.assertEqual(row(self.tick(), "estop")["state"], "todo")

    def test_skip_counts_as_done_and_next_moves(self):
        self.ob.rec["confirm_servo"] = {"at": 1.7e9, "fp": O.joints_fp(CALIB)}
        v = self.tick()
        self.assertEqual(v["next"], "imu")
        self.assertTrue(self.ob.skip("imu", self.rd, self.pad)[0])
        v = self.tick()
        self.assertEqual(v["next"], "estop")                                   # item ✓ (XPAD item): item
        self.assertEqual(v["skipped_n"], 1)
        self.assertTrue(self.ob.skip("estop", self.rd, self.pad, confirmed=True)[0])
        self.ob.rec["walked"] = {"at": 1.7e9, "fp": O.full_fp(BUS, CALIB)}
        v = self.tick()
        self.assertTrue(v["all_done"])
        self.assertIsNone(v["next"])
        self.assertEqual(v["skipped_n"], 2)
        json.dumps(v)

    def test_bad_skipped_records_are_dropped(self):
        Path(self.tmp.name, "onboarding.json").write_text(json.dumps({"v": 1, "skipped": {
            "imu": {"at": 1.7e9, "by": "x", "fp": "abc"}, "servo": {"at": "item"}, "nope": {"at": 1}, "pad": [1]}}), encoding="utf-8")
        ob = O.Onboarding(self.tmp.name, self.log)
        self.assertEqual(list(ob.rec["skipped"]), ["imu"])
        self.assertEqual(sum(1 for e in self.log.recent(30) if "skipped." in e["msg"]), 3)
        Path(self.tmp.name, "onboarding.json").write_text(json.dumps({"v": 1, "skipped": "item"}), encoding="utf-8")
        self.assertNotIn("skipped", O.Onboarding(self.tmp.name, self.log).rec)
        ob.tick(self.rd, self.pad)                                             # item
        self.assertEqual(len(ob.view()["rows"]), 6)

    def test_hard_block_servo_needs_two_clicks(self):
        c = copy.deepcopy(CALIB)
        c["uncalibrated_joints"] = ["left_knee"]
        self.rd.calib.set(c)
        r = row(self.tick(), "servo")
        self.assertEqual(r["mark"]["skip"]["ask"], "Confirm skip? robotd reports servo calibration incomplete: even if Skipped, the duck cannot stand")
        self.assertFalse(self.ob.skip("servo", self.rd, self.pad)[0])
        self.assertTrue(self.ob.skip("servo", self.rd, self.pad, confirmed=True)[0])
        r = row(self.tick(), "servo")
        self.assertEqual(r["state"], "skip")
        self.assertIn("1 uncalibrated servos", r["note"])


class DoneTest(unittest.TestCase):
    """'item'= item (item 10-20: 'item'): done.<item>, ✓'item', itemSkipitem."""
    setUp, tearDown, tick = SkipTest.setUp, SkipTest.tearDown, SkipTest.tick

    def test_done_marks_ok_and_cancel(self):
        before = row(self.tick(), "servo")
        self.assertEqual(before["state"], "todo")
        ok, why = self.ob.mark_done("servo", self.rd, self.pad, by="PC 1.2.3.4")
        self.assertTrue(ok, why)
        self.assertEqual(self.ob.rec["done"]["servo"]["fp"], O.joints_fp(CALIB))
        r = row(self.tick(), "servo")
        self.assertEqual((r["state"], r["marked"], r["text"]), ("ok", "done", "Marked done"))
        self.assertEqual(r["note"], before["text"])                          # item, item'item: …'
        self.assertIsNotNone(r["when"])
        self.assertEqual(self.ob.view()["marked_n"], 1)
        self.assertTrue(self.ob.mark_done("servo", self.rd, self.pad)[0])     # item: item
        ok, why = self.ob.skip("servo", self.rd, self.pad)
        self.assertFalse(ok)                                                  # item: item
        self.assertIn("click Undo first", why)
        self.assertTrue(self.ob.unmark("servo", "done", by="PC 1.2.3.4")[0])
        self.assertNotIn("servo", self.ob.rec["done"])
        self.assertEqual(row(self.tick(), "servo")["state"], "todo")
        msgs = [e["msg"] for e in self.log.recent(30)]
        self.assertIn("Onboarding: Servo Zeroing marked done (PC 1.2.3.4)", msgs)
        self.assertIn("Onboarding: Servo Zeroing unmarked done (PC 1.2.3.4)", msgs)

    def test_done_wins_over_skip_and_marking_removes_the_other(self):
        fp = O.joints_fp(CALIB)
        self.ob.rec.update({"skipped": {"servo": {"at": 1.0, "by": "x", "fp": fp}}, "done": {"servo": {"at": 2.0, "by": "x", "fp": fp}}})
        self.assertEqual(row(self.tick(), "servo")["marked"], "done")
        self.ob.rec["done"] = {}
        self.assertTrue(self.ob.unskip("servo")[0])
        self.ob.rec["skipped"] = {"servo": {"at": 1.0, "by": "x", "fp": "old"}}    # itemSkip (item)item
        self.assertEqual(row(self.tick(), "servo")["state"], "todo")
        self.assertTrue(self.ob.mark_done("servo", self.rd, self.pad)[0])
        self.assertNotIn("servo", self.ob.rec["skipped"])                       # item: itemSkipitem

    def test_done_voided_by_fingerprint_change(self):
        self.assertTrue(self.ob.mark_done("servo", self.rd, self.pad)[0])
        self.rd.calib.set(calib_with(dirs={"left_knee": 1}))
        r = row(self.tick(), "servo")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Servo zeroing changed after being marked done", r["text"])
        self.assertIn("servo", self.ob.rec["done"])                             # item (item)

    def test_found_problem_with_and_without_ack(self):
        """IMU item 8.5° (found): item'item'item, item."""
        src, mount = "lsm6dsv16x", CALIB["imu_mount"]
        mfp = O.mount_fp(src, mount["quat"])
        self.ob.rec["imu_check"] = {"at": 1.7e9, "source": src, "mount_fp": mfp, "diff_deg": 8.5, "tilt_deg": 1.0, "ok": False}
        r = row(self.tick(), "imu")
        self.assertEqual(r["mark"]["done"]["ask"],
                         "Confirm mark as done? Standing check differed by 8.5 deg: Marked done may still drift forward when walking")
        ok, why = self.ob.mark_done("imu", self.rd, self.pad)
        self.assertFalse(ok)
        self.assertIn("differed by 8.5 deg", why)
        self.assertTrue(self.ob.mark_done("imu", self.rd, self.pad, confirmed=True)[0])
        self.assertIn("found", self.ob.rec["done"]["imu"]["ack"])
        for _ in range(2):
            self.assertEqual(row(self.tick(), "imu")["state"], "ok")
        self.assertIn("imu", self.ob.rec["done"])
        self.ob.rec["done"] = {"imu": {"at": 1.7e9, "by": "x", "fp": mfp}}          # item
        r = row(self.tick(), "imu")
        self.assertEqual(r["state"], "todo")
        self.assertIn("mark was cleared", r["note"])
        self.assertEqual(self.ob.rec["done"], {})
        self.assertTrue(any("invalidated" in e["msg"] for e in self.log.recent(30)))

    def test_frozen_during_servo_session_needs_two_clicks(self):
        """item (item D11): itemSkip, item, item (item)."""
        self.assertTrue(self.ob.skip("imu", self.rd, self.pad)[0])
        self.tick()

        class Sess:
            state = "open"
        self.ob.tick(self.rd, self.pad, Sess())
        v = self.ob.view()
        self.assertTrue(v["frozen"])
        self.assertIn("paused until you hand control back to robotd", v["frozen_ask"]["skip"])
        self.assertIn("Confirm undo?", v["frozen_ask"]["undo"])
        ok, why = self.ob.skip("walk", self.rd, self.pad, Sess())
        self.assertFalse(ok)
        self.assertIn("Confirm skip? Servo tuning is active", why)
        self.assertTrue(self.ob.skip("walk", self.rd, self.pad, Sess(), confirmed=True)[0])
        ok, why = self.ob.unskip("imu", servo=Sess())
        self.assertFalse(ok)
        self.assertIn("handing control back to robotd", why)
        self.assertIn("imu", self.ob.rec["skipped"])
        self.assertFalse(self.ob.unskip("imu")[0])        # item: item (item)——item (item R6)
        self.assertTrue(self.ob.unskip("imu", confirmed=True)[0])
        self.ob.tick(self.rd, self.pad, Sess())
        self.assertEqual([r["state"] for r in self.ob.view()["rows"]], [r["state"] for r in v["rows"]])   # item: item
        Sess.state = "closed"                             # item: item
        self.assertEqual(row(self.tick(), "walk")["state"], "skip")
        self.assertEqual(row(self.tick(), "imu")["state"], "todo")

    def test_records_validation(self):
        good, bad = O.clean_records({"done": {
            "servo": {"at": 1.7e9, "by": "x", "fp": None, "ack": ["hard", "unk"]}, "imu": {"at": 1.7e9, "fp": "x", "ack": [1]},
            "walk": {"at": None, "fp": "x"}, "pad": {"at": 1.7e9, "fp": None}}})
        self.assertEqual(sorted(good["done"]), ["pad", "servo"])
        self.assertEqual(sorted(k for k, _ in bad), ["done.imu", "done.walk"])


class Batch3SkipTest(unittest.TestCase):
    """item D5, D10, D11 (item repro_onboard.py item R1–R7), item 10-20 item (item, item)item."""
    setUp, tearDown, tick = SkipTest.setUp, SkipTest.tearDown, SkipTest.tick

    def test_found_problems_need_two_clicks_and_old_skips_are_voided(self):
        """D5: IMU item 8.5°, item, item——itemSkipitem (item)item, item."""
        src, mount = "lsm6dsv16x", CALIB["imu_mount"]
        mfp = O.mount_fp(src, mount["quat"])
        efp = O._hash([XBOX, BUILD])
        self.ob.rec.update({
            "imu_check": {"at": 1.7e9, "source": src, "mount_fp": mfp, "diff_deg": 8.5, "tilt_deg": 1.0, "ok": False},
            "estop": {"at": 1.7e9, "ms": 180, "mac": XBOX, "name": "Xbox", "build": BUILD},
            "skipped": {"imu": {"at": 1.7e9, "by": "x", "fp": mfp}, "estop": {"at": 1.7e9, "by": "x", "fp": efp},
                        "servo": {"at": 1.7e9, "by": "x", "fp": O.joints_fp(calib_with(left_knee=1990))}}})
        self.rd.calib.set(calib_with(left_knee=1990))
        self.rd.calib_valid_after = self.rd.calib.at - 0.001
        self.pad.view = pad_view(pads=[XPAD], estop="bad", checks=[{"label": "padd item", "ok": False, "why": "padd item"}])
        v = self.tick()
        for key, word in (("imu", "differed by 8.5 deg"), ("estop", "E-Stop is currently failing"), ("servo", "existing servo zero positions")):
            with self.subTest(step=key):
                r = row(v, key)
                self.assertEqual(r["state"], "todo", r)                         # item, item'Skipitem'
                self.assertTrue(r["mark"]["skip"]["confirm"])
                self.assertIn(word, r["mark"]["skip"]["ask"])
                self.assertNotIn("{v}", r["mark"]["skip"]["ask"])
                self.assertIn("skip was cleared", r["note"])
                self.assertTrue(r["found"])
                ok, why = self.ob.skip(key, self.rd, self.pad)
                self.assertFalse(ok)
                self.assertIn(word, why)
        self.assertEqual(self.ob.rec["skipped"], {})                           # item
        self.assertTrue(any("invalidated" in e["msg"] for e in self.log.recent(50)))
        self.assertTrue(self.ob.skip("servo", self.rd, self.pad, confirmed=True)[0])   # item: item, item
        self.assertEqual(row(self.tick(), "servo")["state"], "skip")
        self.assertIn("servo", self.ob.rec["skipped"])                         # item

    def test_skip_records_need_number_at(self):
        """D10: at item → item warn.fp item null (item, tick item: item C2), item null item."""
        good, bad = O.clean_records({"skipped": {
            "imu": {}, "servo": {"x": 1}, "walk": {"at": None, "fp": "abc"}, "estop": {"at": 1.7e9, "fp": None},
            "pad": {"at": 1.7e9, "by": "x", "fp": None}, "servo2": {"at": 1.7e9, "fp": "a"}, "commit": {"at": 1.7e9, "fp": 3}}})
        self.assertEqual(sorted(good["skipped"]), ["estop", "pad"])
        self.assertEqual(sorted(k for k, _ in bad), ["skipped.commit", "skipped.imu", "skipped.servo", "skipped.servo2", "skipped.walk"])
        p = Path(self.tmp.name) / O.ONBOARD_FILE
        p.write_text(json.dumps({"skipped": {"servo": {"at": 1.7e9, "by": "x", "fp": None}}}), encoding="utf-8")
        ob = O.Onboarding(self.tmp.name, self.log)
        self.assertEqual(ob.rec["skipped"]["servo"]["fp"], None)
        ob.tick(self.rd, self.pad)                                             # item: item
        self.assertEqual(ob.rec["skipped"]["servo"]["fp"], O.joints_fp(CALIB))

    def test_estop_ask_text_by_situation(self):
        """D11: item: item / item / item / item / item'item'item."""
        self.pad.view = pad_view(pads=[XPAD])
        m = row(self.tick(), "estop")["mark"]
        self.assertIn("Confirm skip? Untested", m["skip"]["ask"])
        self.assertIn("Confirm mark as done? Untested", m["done"]["ask"])
        self.pad.view = pad_view(pads=[XPAD], estop="bad", checks=[{"label": "padd item", "ok": False, "why": "padd item"}])
        r = row(self.tick(), "estop")
        self.assertIn("Confirm skip? E-Stop is currently not working (padd item: padd item); Skipped will not fix it", r["mark"]["skip"]["ask"])
        self.assertNotIn("found", r["mark"]["ack"])                            # item: item
        self.pad.view = pad_view()
        self.assertIn("Web-only", row(self.tick(), "estop")["mark"]["skip"]["ask"])
        self.ob.rec["estop"] = {"at": 1.7e9, "ms": 180, "mac": "98:B6:E9:28:06:09", "name": "Pro", "build": BUILD}
        self.pad.view = pad_view(pads=[XPAD])
        r = row(self.tick(), "estop")
        self.assertIn("Gamepad changed", r["mark"]["skip"]["ask"])
        self.ob.rec["estop"] = {"at": 1.7e9, "ms": 180, "mac": XBOX, "name": "Xbox", "build": "old-build"}
        self.assertIn("robotd updated", row(self.tick(), "estop")["mark"]["skip"]["ask"])


class StartingTooLongTest(unittest.TestCase):
    """item 16: item 30 item (Studio item): item, item, item'item 10 item'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(Path(self.tmp.name) / "logs", echo=False, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summarize_todo_and_header(self):
        boot = {"build": BUILD, "mode": "commissioning", "phase": "configuring_bus", "port": "/dev/ttyS2"}
        rd = FakeRd(bus=boot)
        now = time.monotonic()
        rd.starting_since = now - 5
        s = summarize(rd, {}, now=now)
        self.assertEqual((s["posture"], s["starting_s"], s["starting_long"]), ("starting", 5, False))
        self.assertEqual(next(t for t in s["todo"] if t["key"] == "starting")["hint"], "Usually finishes within 10s")
        rd.starting_since = now - 31
        s = summarize(rd, {}, now=now)
        self.assertTrue(s["starting_long"])
        self.assertEqual(s["starting_s"], 31)
        t = next(t for t in s["todo"] if t["key"] == "starting")
        self.assertEqual(t["msg"], "robotd stuck in starting state")
        self.assertIn("Elapsed 31s", t["hint"])
        self.assertIn("stale status", t["hint"])
        self.assertNotIn("10 item", t["hint"])
        ob = O.Onboarding(self.tmp.name, self.log)
        ob.tick(rd)
        self.assertIn("stuck starting", ob.view()["header"]["text"])
        rd.bus.set(dict(boot, phase="ready"))                                 # item (item): item
        s = summarize(rd, {}, now=now)
        self.assertEqual((s["posture"], s["starting_s"], s["starting_long"]), ("commissioning", None, False))


class BatteryWordingTest(unittest.TestCase):
    """item: itemSit Downitem, item robotd item [safety] battery_empty_shutdown item (item false)."""

    def test_read_setting_and_wording(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            d = Path(tmp.name)
            fork, official = d / "fork.toml", d / "official.toml"
            self.assertIsNone(O.read_battery_shutdown([str(fork), str(official)]))          # item: item
            official.write_text("[safety]\nbattery_empty_shutdown = true\n", encoding="utf-8")
            self.assertIs(O.read_battery_shutdown([str(fork), str(official)]), True)
            fork.write_text("[safety]\nlimp_fall = false\nbattery_empty_shutdown = false\n", encoding="utf-8")
            self.assertIs(O.read_battery_shutdown([str(fork), str(official)]), False)        # fork item
            fork.write_text("[safety]\nlimp_fall = false\n", encoding="utf-8")
            self.assertIsNone(O.read_battery_shutdown([str(fork)]))                         # item: item
            fork.write_text("item[", encoding="utf-8")
            self.assertIsNone(O.read_battery_shutdown([str(fork)]))
        finally:
            tmp.cleanup()
        low = {"pct": 9}

        def hint(v):
            return next(i for i in todo(HEALTH, BUS, {"stale": []}, "ok", {}, {}, low, "holding", battery_shutdown=v)
                        if i["key"] == "battery")["hint"]
        self.assertEqual(hint(True), "Charge soon; At 6.6 V it will sit down and shut off automatically")
        self.assertEqual(hint(False), "Charge soon; It will not sit down automatically when empty; stop, sit it down, and charge")
        self.assertEqual(hint(None), "Charge soon; It will not sit down automatically when empty; stop, sit it down, and charge")


# ----------------  3 '' ( 11) ----------------

def factory_calib():
    c = copy.deepcopy(CALIB)
    cal = c["calibration"]
    cal["motion_enabled"] = False
    cal["imu_mount_verified"] = False
    for j in cal["joints"]:
        j["calibrated"] = False
        j.pop("min_rad", None)
        j.pop("max_rad", None)
    return c


def commit_view(ready=False):
    return {"ready": ready, "items": [
        {"key": "servo", "label": "item", "ok": False, "text": "item 15 item Studio item",
         "action": {"do": "servos", "label": "item"}},
        {"key": "directions", "label": "item", "ok": True, "text": "15 item (−1)"},
        {"key": "imu", "label": "IMU Orientation", "ok": True, "text": "IMU item"},
        {"key": "bus", "label": "item", "ok": True, "text": "item"},
        {"key": "robotd", "label": "item", "ok": True, "text": "itemOnline"},
    ], "job": None}


class CommitStepTest(unittest.TestCase):
    def test_order_title_and_number(self):
        v = O.evaluate(inp(), {})
        self.assertEqual([r["key"] for r in v["rows"]], ["servo", "imu", "commit", "pad", "estop", "walk"])
        r = row(v, "commit")
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["title"], "Write to robotd")
        self.assertEqual(r["desc"], O.COMMIT_TEXT)

    def test_committed_calibration_is_done(self):
        r = row(O.evaluate(inp(), {}), "commit")
        self.assertEqual(r["state"], "ok")
        self.assertIn("Written to robotd", r["text"])

    def test_factory_shows_readiness_and_action(self):
        cv = commit_view()
        r = row(O.evaluate(inp(calib=factory_calib(), commit=cv), {}), "commit")
        self.assertEqual(r["state"], "todo")
        self.assertIn("Missing", r["text"])
        self.assertEqual(r["commit"], cv)
        self.assertEqual(r["actions"][0]["do"], "commit")

    def test_unreadable_is_unknown(self):
        r = row(O.evaluate(inp(calib=None), {}), "commit")
        self.assertEqual(r["state"], "unk")

    def test_calib_read_fallback_is_not_unknown(self):
        """item waiting_for_bus: calibration-status item, item calib-read item."""
        cv = commit_view()
        cv.update({"preview_ok": True, "from_file": True,
                   "note": "item, item"})
        bus = {"build": BUILD, "mode": "motion", "phase": "waiting_for_bus",
               "error": "torque readback at register 40: Feetech Sync Read timeout; missing IDs [21]"}
        r = row(O.evaluate(inp(bus=bus, calib=None, commit=cv), {}), "commit")
        self.assertEqual(r["state"], "todo")
        self.assertIn("item", r["desc"])

    def test_skip_is_bound_to_calibration_fingerprint(self):
        bound, fp = O.step_fp("commit", inp())
        self.assertTrue(bound)
        self.assertEqual(fp, O.full_fp(BUS, CALIB))

    def test_marked_steps_and_commit_readiness(self):
        """item 16: item 1 item (item: item)→ item 3 item'item'item
        item 17: item 2 item, item, item →'IMU item'item, item 3 item."""
        from studio.commit import CommitService
        tmp = tempfile.TemporaryDirectory()
        try:
            log = Log(Path(tmp.name) / "logs", echo=False, background=False)
            fc = factory_calib()
            fc["uncalibrated_joints"] = [j["name"] for j in fc["calibration"]["joints"]]
            fc["calibration"]["imu_mounts"]["lsm6dsv16x"]["verified"] = False
            fc["imu_mount"]["verified"] = False
            bus = {"build": BUILD, "mode": "commissioning", "phase": "ready", "port": "/dev/ttyS2", "calibrated": False,
                   "imu_mount_verified": False, "last_sample": {"servos": [{"id": i} for i in O.SERVO_IDS], "imu": {}}}
            rd = FakeRd(bus=bus, calib=fc)
            ob = O.Onboarding(tmp.name, log)
            svc = CommitService(rd, log, tmp.name)
            ob.commit_service, svc.onboard = svc, ob
            ob.tick(rd)

            def item(key):
                return next(i for i in svc.view(rd)["items"] if i["key"] == key)
            self.assertFalse(item("servo")["ok"])
            r = row(ob.view(), "servo")
            self.assertEqual(r["state"], "no")
            self.assertTrue(r["mark"]["done"]["confirm"])                         # item (hard): item
            self.assertFalse(ob.mark_done("servo", rd)[0])
            self.assertTrue(ob.mark_done("servo", rd, confirmed=True)[0])
            ob.tick(rd)
            self.assertEqual(row(ob.view(), "servo")["state"], "ok")
            s = item("servo")
            self.assertTrue(s["ok"])
            self.assertEqual(s["text"], "Step 1 marked done: keeping existing servo zero offsets and directions in robotd")
            self.assertIsNone(s["action"])
            self.assertFalse(item("imu")["ok"])
            ob.tick(rd)
            self.assertIn("IMU Orientation", row(ob.view(), "commit")["text"])           # item 2 item: item 3 item IMU item
            self.assertFalse(ob.mark_done("imu", rd)[0])                          # item (hard): item
            self.assertTrue(ob.mark_done("imu", rd, confirmed=True)[0])
            ob.tick(rd)
            m = item("imu")
            self.assertTrue(m["ok"], m)
            self.assertIn("Step 2 marked done: writing default mount for LSM6DSV16X (I2C) (Replica mount", m["text"])
            self.assertEqual(svc.view(rd)["imu_default"]["quat"], [0.7071067811865476, 0.0, 0.7071067811865476, 0.0])
            ob.tick(rd)
            text = row(ob.view(), "commit")["text"]
            self.assertNotIn("IMU Orientation", text)
            self.assertNotIn("item", text)
            self.assertIn("Ready:", text)                                        # Skipitem 1, 2 item
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
