"""item (overview-merge-plan.md §5, item overview-merge-review.md)item: item robotd + item Studio + item Edge.

tests/overview_e2e.mjs item, item, item, item (item / item, 
item, Phoneitem / item / item A item), item robotd item
item (item O1, O2, O5, O8, O13, O14, O17, O18).item: main (item Studio)itemdone (Studio item
'6 item'item onboarding.json item, item).

itemPCitem Node (≥ 22)item Edgeitem (item)itemSkip.item STUDIO_E2E=0 itemSkip.
item Studio 18090, item robotd 17021 / configd 17022, Edge item 9343, item (item).
OVERVIEW_E2E_DIR=item: item, Edge item profile, item robotd item (item, item)itemOVERVIEW_SHOTS=item: item.
item: 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.','dev'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_overview_e2e'])"
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "deps"), str(ROOT / "dev")]

from tests.test_vpad_e2e import EDGE, NODE, _kill, _node_ok, _port, _wait_http, moves, nonzero  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"
XBOX = "78:86:2E:BB:13:28"


def all_done_records():
    """'5 item'item: item robotd item (item Studio item onboard.py item)."""
    from fake_robotd import FakeRobotd
    from studio import onboard as O
    from studio.imu import mount_of
    f = FakeRobotd(str(FIXTURE), posture="policy")
    src, mount = mount_of(f.bus, f.calib)
    now = time.time()
    fp = O.full_fp(f.bus, f.calib)
    return {"v": 1, "confirm_servo": {"at": now - 3600, "fp": O.joints_fp(f.calib), "by": "e2e"},
            # IMU Skip ( 10-18: ', Skip'): ' (Skip 1 )'
            "skipped": {"imu": {"at": now - 3000, "by": "e2e", "fp": O.mount_fp(src, mount["quat"])}},
            "estop": {"at": now - 2400, "ms": 186, "mac": XBOX, "name": "Xbox Wireless Controller", "build": f.bus["build"]},
            "stood": {"at": now - 1800, "fp": fp}, "walked": {"at": now - 1700, "fp": fp}}


@unittest.skipUnless(_node_ok() and EDGE and os.environ.get("STUDIO_E2E", "1") != "0",
                     "item Node ≥ 22 item Edge (item STUDIO_E2E=0): Skipitem")
class OverviewE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("OVERVIEW_E2E_DIR")
        cls.dir = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="overview-e2e-"))
        cls.dir.mkdir(parents=True, exist_ok=True)
        state = cls.dir / "state"
        if state.exists():
            shutil.rmtree(state)
        rec = cls.dir / "robotd-recv.jsonl"
        rec.unlink(missing_ok=True)
        p_rd, p_cfg, p_web, p_cdp = _port(17021), _port(17022), _port(18090), _port(9343)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = lambda name: open(cls.dir / name, "w", encoding="utf-8")   # noqa: E731
        procs = {}

        def studio(tag):
            procs["studio"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "run.py"), "--listen", f"127.0.0.1:{p_web}", "--robotd", f"tcp:127.0.0.1:{p_rd}",
                 "--configd", f"tcp:127.0.0.1:{p_cfg}", "--duck-label", "② item", "--dev-autologin", "--servo-sim",
                 "--state-dir", str(state), "--log-dir", str(cls.dir / "logs")],
                cwd=ROOT, env=env, stdout=out(f"studio-{tag}.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_web}/healthz", "Studio")

        def node(phase):
            args = [NODE, str(HERE / "overview_e2e.mjs"), "--url", f"http://127.0.0.1:{p_web}/", "--cdp", str(p_cdp),
                    "--record", str(rec), "--fake", str(p_cfg), "--phase", phase]
            if os.environ.get("OVERVIEW_SHOTS"):
                args += ["--shots", os.environ["OVERVIEW_SHOTS"]]
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=400, cwd=ROOT)
            (cls.dir / f"overview_e2e-{phase}.err").write_text(r.stderr or "", encoding="utf-8")
            if r.returncode != 0:
                raise AssertionError(f"Node item ({phase})item (item {cls.dir}): {(r.stderr or '')[-3000:]}")
            (cls.dir / f"overview_e2e-{phase}.json").write_text(r.stdout, encoding="utf-8")
            return json.loads(r.stdout)

        try:
            # '': , ,  ('', )
            procs["fake"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "dev" / "fake_robotd.py"), "--fixture", str(FIXTURE),
                 "--robotd-port", str(p_rd), "--configd-port", str(p_cfg), "--posture", "policy", "--record", str(rec)],
                cwd=ROOT, env=env, stdout=out("fake_robotd.out"), stderr=subprocess.STDOUT)
            studio("main")
            procs["edge"] = subprocess.Popen(
                [EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={p_cdp}", f"--user-data-dir={cls.dir / 'edge'}",
                 "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=out("edge.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_cdp}/json/version", "item Edge item")
            cls.out = node("main")
            # : Studio '5 ' ()
            _kill(procs.pop("studio"))
            (state / "onboarding.json").write_text(json.dumps(all_done_records(), ensure_ascii=False), encoding="utf-8")
            import socket
            for _ in range(20):           # item robotd'item'item (0.3 item): item
                try:
                    with socket.create_connection(("127.0.0.1", p_cfg), 3) as s:
                        s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"fake.set","params":{"pad_connected":true}}\n')
                        if b"connected" in s.recv(4096):
                            break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise AssertionError("item configd item'item'")
            studio("done")
            cls.done = node("done")
        finally:
            for p in reversed(list(procs.values())):
                _kill(p)
        if not keep:
            shutil.rmtree(cls.dir, ignore_errors=True)

    def recs(self, step):
        return self.out["steps"][step]["recs"]

    def test_page_has_no_errors(self):
        self.assertEqual(self.out["errors"], [])
        self.assertEqual(self.done["errors"], [])

    def test_overview_layout(self):
        d = self.out["desk"]
        self.assertEqual(d["cells"], 7)                                  # itemToF, item
        self.assertIn("°C", d["cpu"])
        self.assertEqual(d["rows"], 6)                                   # item 6 item (item 11 item'item')
        self.assertFalse(d["padActive"])                                 # item: item
        self.assertEqual(d["nav"], "item")
        self.assertEqual(d["now"], "1 item")                         # item: item
        self.assertEqual(d["marks"][:3], ["●", "●", "✓"])
        tops = self.out["d1100"]["tops"]                                 # 1100 item: 4 + 3
        self.assertEqual(len(set(tops[:4])), 1)
        self.assertGreater(tops[4], tops[0])
        self.assertEqual(len(set(tops[4:])), 1)
        for k in ("d1100", "phone", "phone_pad"):
            self.assertLessEqual(self.out[k]["sw"], self.out[k]["cw"], f"{k}: item")

    def test_wheel_on_stage_scrolls_page(self):
        self.assertGreater(self.out["wheel"], 100, "item, item (item 3D item)")

    def test_pad_anchor(self):
        n = self.out["padNav"]
        self.assertTrue(n["active"])
        self.assertEqual(n["nav"], "item")
        self.assertEqual(n["topbar"], 0)                                 # item (E-Stopitem: O3)
        # ,  ( 10-19: ), , 
        self.assertEqual(n["band"][0], 56, n)
        self.assertEqual(n["navTop"], 0, n)
        self.assertTrue(n["band"][1] <= n["top"] <= n["band"][1] + 30, n)

    def assert_released(self, st):
        self.assertTrue(any(nonzero(p) for p in moves(st["before"])), "item (item)")
        self.assertIn({"vx": 0, "vy": 0, "vyaw": 0}, moves(st["atLeave" if "atLeave" in st else "atScroll"]))
        self.assertFalse(any(nonzero(p) for p in moves(st["after"])), "item")
        self.assertFalse(st["active"])
        self.assertTrue(st["offLogged"], "Studio item'item' (drive off)")

    def test_route_change_while_holding_stick_stops_and_drives_off(self):
        self.assert_released(self.out["steps"]["route_leave"])

    def test_scrolling_out_while_holding_stick_stops_and_drives_off(self):
        self.assert_released(self.out["steps"]["scroll_out"])

    def test_checklist_buttons_go_to_the_right_place(self):
        g = self.out["goto"]
        self.assertTrue(g["estop"]["inView"])
        self.assertIn(g["estop"]["focus"], ("pd-estop-btn", "pd-estop"))
        self.assertTrue(g["wizard"]["open"] and g["wizard"]["inView"])
        self.assertEqual(g["imu"], "#/imu")

    def test_swipes_on_phone_send_nothing(self):
        """O2: item, item, item A item, robotd item / item (item)."""
        for step in ("swipe_pad_blank", "swipe_action", "press_right_after_scroll", "swipe_svg_a"):
            with self.subTest(step=step):
                self.assertEqual([r for r in self.recs(step) if r["method"] in ("robot.move", "robot.head", "robot.do", "robot.mouth",
                                                                                  "robot.sound", "robot.pose")], [])
        self.assertTrue(any(r["method"] == "robot.head" for r in self.recs("hold_action")), "item'item'item, item")
        self.assertIn({"method": "robot.do", "params": {"skill": "ground_pick"}, "notify": False}, self.recs("tap_svg_a"))

    def test_board_temperature(self):
        c = self.out["cpu"]
        self.assertTrue(c["throttled"]["bad"])
        self.assertIn("item: 1008 / 1800 MHz", c["throttled"]["text"])
        self.assertIn("item", c["throttled"]["todo"])
        self.assertIn("item", c["hot"])
        self.assertIn("—", c["stale"])                                   # health item: item'—', item'Normal' (O5)
        self.assertNotIn("Normal", c["stale"])

    def test_unknown_shows_question_marks(self):
        self.assertEqual(self.out["unknown"][:4], ["?", "?", "?", "?"])   # item, IMU, item, item
        self.assertIn("item configd", self.out["unknownText"])

    def test_c3_keyboard_repeat_after_leaving_does_not_drive(self):
        """item C3: item W item (W item, item)item."""
        k = self.out["steps"]["kbd_repeat"]
        self.assertTrue(any(nonzero(p) for p in moves(k["held"])), "item W item (item)")
        self.assertIn({"vx": 0, "vy": 0, "vyaw": 0}, moves(k["out"]))
        self.assertTrue(k["activeBack"] and k["focus"])
        self.assertFalse(any(nonzero(p) for p in moves(k["back"])), "item")

    def test_c6_stage_canvas_keeps_pan_y_on_phone(self):
        self.assertEqual(self.out["phoneCanvas"]["computed"], "pan-y", self.out["phoneCanvas"])

    def test_c7_no_hold_after_fast_press_release_press(self):
        """item C7: item 300 ms, item, item, item: item 2 item action_hold item."""
        h = self.out["steps"]["hold_race"]
        self.assertGreater(h["holdsDuring"], 0, h)                     # item (item)
        self.assertEqual(h["holdsAfter"], 0, h)

    def test_servo_session_kept_with_bar_on_other_pages(self):
        """item 10-18: 'item': item, item, item, 
        item'item'item'item……sudo systemctl start robotd', item."""
        b = self.out["servoBar"]
        self.assertIn("item · 2 item · item (item, item, item)", b["text"])
        self.assertIn("item", b["text"])
        self.assertEqual(b["dialogs"], 0)
        self.assertTrue(b["afterReload"])
        self.assertIn("item: item 15 item, item——item", b["armed"])      # item D7
        self.assertIn("item (item Select item)", b["text"])                # item D6
        self.assertIn("item robotd, item'item'", b["text"])                               # item D9
        self.assertTrue(b["sticky"]["visible"], b["sticky"])                                         # item (item)
        self.assertTrue(b["visualFits"], "item, itemNormalitem")
        p = b["phone"]
        self.assertTrue(p["visible"], p)                                                             # Phoneitem
        self.assertEqual(p["badge"], "item")
        self.assertTrue(p["remote"])
        self.assertLessEqual(p["sw"], p["cw"])
        self.assertTrue(b["dbl"]["stillOpen"])                                                       # item (item D4)
        self.assertTrue(b["dbl"]["armed"])
        self.assertTrue(b["dbl"]["disarmed"])                                                        # 3 item
        self.assertTrue(b["earlyIgnored"])                                                           # 700 ms item
        self.assertTrue(b["stillThere"])                                                             # item, 'item'item
        self.assertTrue(b["stillOpen"])                                # item
        self.assertIn("sudo systemctl start robotd", b["after"])
        self.assertTrue(b["gone"])

    def test_pad_3d_left_of_sticks_and_pauses_out_of_view(self):
        """item 10-24: item3Ditem, item
        3D item'item' (item active item false).Phone: 3D item, item, item sticky, item."""
        d = self.out["pad3d"]
        self.assertGreaterEqual(d["visFrac"], 0.6, d)
        self.assertTrue(d["left"], d)
        self.assertEqual(d["sticky"], "static")
        self.assertEqual(d["stackSticky"], "sticky")
        self.assertTrue(d["cameraBelow"])
        self.assertEqual(d["touch"], "pan-y")
        self.assertTrue(d["drawsInView"])
        self.assertFalse(d["drawsOutOfView"])
        self.assertFalse(d["activeAtTop"])
        p = self.out["phone3d"]
        self.assertTrue(p["above"], p)
        self.assertLessEqual(p["h"], 240)
        self.assertEqual(p["sticky"], "static")

    def test_all_done_collapses_to_one_line(self):
        self.assertIn("item 6 item (Skip 1 item)", self.done["done"])

    def test_all_done_line_expands_and_remembers(self):
        e = self.done["expanded"]
        self.assertEqual(e["rows"], 6)
        self.assertEqual(e["redo"], 5)                                # 5 item ✓ item'item'
        self.assertEqual(e["skip"], 1)
        self.assertIn("item", e["head"])
        self.assertEqual(self.done["expandedAfterReload"], 6)        # item localStorage
        self.assertTrue(self.done["collapsedAfterReload"])

    def test_every_row_has_three_buttons(self):
        """item 10-20: item'item''item''Skip' (✓ item'item')."""
        rows = self.out["threeButtons"]
        self.assertGreaterEqual(len(rows), 3, rows)
        for r in rows:
            with self.subTest(step=r["step"]):
                self.assertEqual(len(r["labels"]), 3, r)
                self.assertEqual(r["labels"][1:], ["item", "Skip"], r)

    def test_skip_unskip_and_redo(self):
        """item'item'→ ✓'item (item)', item'item''item'itemIMU Skip → 'Skipitem' (item ↷), item 4 item
        itemSkipitem✓ item'item'item'item'item."""
        m = self.out["marked"]
        self.assertTrue(m["text"].startswith("item ("), m)
        self.assertEqual(m["buttons"], ["item", "item"])
        sk = self.out["skip"]
        self.assertEqual(sk["imu"], "onb-skip")
        self.assertEqual(sk["mark"], "↷")
        self.assertIn("Skipitem (item)", sk["imuText"])
        self.assertEqual(sk["now"], "pad")
        self.assertEqual(self.out["unskip"], {"imu": "onb-todo", "now": "imu"})
        self.assertEqual(self.out["redoHash"], "#/servos")
        self.assertEqual(self.out["undone"], "onb-todo")

    def test_estop_skip_needs_two_clicks_within_3s(self):
        self.assertIn("itemSkip", self.out["estopOnce"]["text"])
        self.assertEqual(self.out["estopOnce"]["skipStill"], ["Skip"])  # item (item D4), item
        self.assertEqual(self.out["estopOnce"]["state"], "onb-no")    # item
        self.assertEqual(self.out["estopDbl"], {"state": "onb-no", "armed": True},   # item: itemSkip, item
                         {k: self.out.get(k) for k in ("estopDblGap", "estopArmed300", "estopClicks", "estopTargets", "estopArms", "dblAtRects")})
        self.assertEqual(self.out["estopTimeout"], {"text": "Skip", "state": "onb-no"})   # 3 item: item
        self.assertEqual(self.out["estopTwice"], "onb-skip")

    def test_imu_unverified_skip_needs_two_clicks(self):
        """item IMU item: itemSkipitem, item, item."""
        b = self.out["imuBlocked"]
        self.assertFalse(b["disabled"])
        self.assertEqual(b["ask"], "itemSkipitem IMU item: Skipitem")
        self.assertEqual(b["state"], "onb-no")
        self.assertEqual(b["after"], "onb-no")


if __name__ == "__main__":
    unittest.main()
