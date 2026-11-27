"""item (item: item)item: item robotd + item Studio + item Edge.

tests/vpad_e2e.mjs item, item, item, Phoneitem, item robotd item
item'item → robotd item' (pad-vpad-plan.md §8 / item pad-vpad-review.md item V1–V7, V16, V17).

itemPCitem Node (≥ 22, item WebSocket)item Edgeitem (item)itemSkip.item STUDIO_E2E=0 itemSkip.
item Studio 18090, item robotd 17021 / configd 17022, Edge item 9343, item (item).
VPAD_E2E_DIR=item: item, Edge item profile, item robotd item (item, item)itemVPAD_SHOTS=item: item.
item: 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_vpad_e2e'])"
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NODE = shutil.which("node")
EDGE = next((p for p in (os.environ.get("STUDIO_EDGE"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", shutil.which("msedge"),
                         shutil.which("microsoft-edge")) if p and os.path.exists(p)), None)
HEAD0 = {"neck_pitch": 0, "head_pitch": 0, "head_yaw": 0, "head_roll": 0}


def _node_ok():
    if not NODE:
        return False
    try:
        v = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        return int(v.split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _port(want):
    """item, item (item Studio / Edge)item."""
    for p in (want, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("item")


def _wait_http(url, what, timeout=20):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # item: item
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with opener.open(url, timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"{timeout} item {what} ({url})")


def _kill(p):
    if p is None or p.poll() is not None:
        return
    if sys.platform == "win32":     # Edge item: item
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def moves(recs):
    return [r["params"] for r in recs if r["method"] == "robot.move"]


def nonzero(p):
    return any(abs(v) > 1e-6 for v in p.values() if isinstance(v, (int, float)))


@unittest.skipUnless(_node_ok() and EDGE and os.environ.get("STUDIO_E2E", "1") != "0",
                     "item Node ≥ 22 item Edge (item STUDIO_E2E=0): Skipitem")
class VpadE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("VPAD_E2E_DIR")
        cls.dir = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="vpad-e2e-"))
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.procs = []
        rec = cls.dir / "robotd-recv.jsonl"
        rec.unlink(missing_ok=True)
        p_rd, p_cfg, p_web, p_cdp = _port(17021), _port(17022), _port(18090), _port(9343)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = lambda name: open(cls.dir / name, "w", encoding="utf-8")   # noqa: E731
        try:
            cls.procs.append(subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "dev" / "fake_robotd.py"), "--fixture", str(HERE / "fixtures" / "now-20261018.jsonl"),
                 "--robotd-port", str(p_rd), "--configd-port", str(p_cfg), "--posture", "policy", "--record", str(rec)],
                cwd=ROOT, env=env, stdout=out("fake_robotd.out"), stderr=subprocess.STDOUT))
            cls.procs.append(subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "run.py"), "--listen", f"127.0.0.1:{p_web}", "--robotd", f"tcp:127.0.0.1:{p_rd}",
                 "--configd", f"tcp:127.0.0.1:{p_cfg}", "--duck-label", "② item", "--dev-autologin", "--servo-sim",
                 "--state-dir", str(cls.dir / "state"), "--log-dir", str(cls.dir / "logs")],
                cwd=ROOT, env=env, stdout=out("studio.out"), stderr=subprocess.STDOUT))
            _wait_http(f"http://127.0.0.1:{p_web}/healthz", "Studio")
            cls.procs.append(subprocess.Popen(
                [EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={p_cdp}", f"--user-data-dir={cls.dir / 'edge'}",
                 "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=out("edge.out"), stderr=subprocess.STDOUT))
            _wait_http(f"http://127.0.0.1:{p_cdp}/json/version", "item Edge item")
            args = [NODE, str(HERE / "vpad_e2e.mjs"), "--url", f"http://127.0.0.1:{p_web}/#/pad", "--cdp", str(p_cdp),
                    "--record", str(rec), "--fake", str(p_cfg)]
            if os.environ.get("VPAD_SHOTS"):
                args += ["--shots", os.environ["VPAD_SHOTS"]]
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=300, cwd=ROOT)
            (cls.dir / "vpad_e2e.err").write_text(r.stderr or "", encoding="utf-8")
            if r.returncode != 0:
                raise AssertionError(f"Node item (item {cls.dir}): {(r.stderr or '')[-3000:]}")
            cls.out = json.loads(r.stdout)
            (cls.dir / "vpad_e2e.json").write_text(r.stdout, encoding="utf-8")
        finally:
            for p in reversed(cls.procs):
                _kill(p)
        if not keep:
            shutil.rmtree(cls.dir, ignore_errors=True)

    def recs(self, step):
        return self.out["steps"][step]["recs"]

    def test_page_has_no_errors(self):
        self.assertEqual(self.out["errors"], [])

    def test_sticks_are_relative_and_release_stops(self):
        self.assertFalse(any(nonzero(p) for p in moves(self.recs("stick_press_only"))), "item (item, V2)")
        mv = moves(self.recs("stick_drag"))
        self.assertGreaterEqual(sum(1 for p in mv if p["vx"] > 0.15), 3)
        self.assertTrue(all(r["notify"] for r in self.recs("stick_drag")))
        self.assertEqual(mv[-1], {"vx": 0, "vy": 0, "vyaw": 0})
        self.assertTrue(all(p["vy"] == 0 and p["vyaw"] == 0 for p in mv))

    def test_a_is_one_skill_request(self):
        self.assertEqual(self.recs("key_a"), [{"method": "robot.do", "params": {"skill": "ground_pick"}, "notify": False}])

    def test_x_hold_rolls_again_and_again(self):
        """item C (V4): item, item 40 ms item."""
        rs = self.recs("x_hold")
        self.assertEqual(rs[0], {"method": "robot.do", "params": {"skill": "roulade"}, "notify": False})
        self.assertGreaterEqual(sum(1 for r in rs[1:] if r["method"] == "robot.do" and r["notify"]), 8)
        self.assertTrue(all(r["params"] == {"skill": "roulade"} for r in rs))

    def test_lt_wheee_and_mouth_close(self):
        rs = self.recs("lt_hold")
        mouths = [r["params"] for r in rs if r["method"] == "robot.mouth"]
        sounds = [r["params"] for r in rs if r["method"] == "robot.sound"]
        self.assertGreaterEqual(mouths.count({"open": 1}), 5)
        self.assertGreaterEqual(sounds.count({"tag": "wheee", "hold": True}), 5)
        self.assertEqual(mouths[-1], {"open": 0})                       # item
        self.assertEqual(sounds[-1], {"tag": "wheee", "hold": False})   # item hold:false
        self.assertTrue(all(r["notify"] for r in rs))

    def test_rt_chirps_once(self):
        rs = self.recs("rt_tap")
        self.assertEqual([r["params"] for r in rs if r["method"] == "robot.sound"], [{"tag": "chirp"}])
        self.assertEqual([r["params"] for r in rs if r["method"] == "robot.mouth"][-1], {"open": 0})

    def test_head_mode_moves_head_not_body(self):
        rs = self.recs("head")
        heads = [r["params"] for r in rs if r["method"] == "robot.head"]
        self.assertGreaterEqual(sum(1 for h in heads if h["neck_pitch"] > 1.0), 3)   # item = item (item 2)
        self.assertEqual(heads[-1], HEAD0)                                            # item / item: item
        self.assertFalse(any(nonzero(p) for p in moves(rs)), "item")
        self.assertNotIn("robot.pose", [r["method"] for r in rs])

    def test_body_mode_crouch_then_exit_with_active_false(self):
        poses = [r["params"] for r in self.recs("body") if r["method"] == "robot.pose"]
        self.assertTrue(any(p["z"] < -0.01 and p["active"] is True for p in poses if "z" in p))   # item = item
        self.assertIn({"z": 0, "roll": 0, "pitch": 0, "active": True}, poses)                     # item
        self.assertEqual(poses[-1], {"active": False})                                            # item: item active:false
        self.assertTrue(all("active" in p for p in poses))

    def test_keyboard_only_when_focused_and_no_arrows(self):
        self.assertEqual(moves(self.recs("keyboard_unfocused")), [])
        st = self.out["steps"]["keyboard"]
        self.assertTrue(st["focused"])
        w = moves(st["w_recs"])
        self.assertGreaterEqual(sum(1 for p in w if p["vx"] == 0.3), 2)
        self.assertEqual(w[-1], {"vx": 0, "vy": 0, "vyaw": 0})
        self.assertFalse(any(nonzero(p) for p in moves(self.recs("keyboard"))), "item")

    def test_blur_stops_even_while_still_pressed(self):
        st = self.out["steps"]["blur"]
        self.assertTrue(any(nonzero(p) for p in moves(st["before"])))
        self.assertIn({"vx": 0, "vy": 0, "vyaw": 0}, moves(st["atBlur"]))
        self.assertFalse(any(nonzero(p) for p in moves(st["after"])), "item")

    def test_start_follows_posture(self):
        self.assertIn("Stop", self.out["start0"])
        self.assertEqual([(r["method"], r["params"]) for r in self.recs("start_stop") if not r["notify"]],
                         [("robot.stop", {}), ("robot.enable", {"on": False, "toggle": False})])
        self.assertEqual([(r["method"], r["params"]) for r in self.recs("start_walk") if not r["notify"]],
                         [("robot.enable", {"on": True, "toggle": False})])

    def test_select_relaxes_on_press(self):
        """V3: item Select itemE-Stop (item robotd item)item."""
        self.assertIn("robot.relax", [r["method"] for r in self.out["steps"]["select"]["whilePressed"]])
        self.assertIn("robot.relax", [r["method"] for r in self.recs("mirror_select")])

    def test_dpad_up_needs_a_3s_hold_like_official(self):
        """item: item 3 item 3 item robot.setMode (item), item."""
        def modes(step):
            return [r["params"] for r in self.recs(step) if r["method"] == "robot.setMode"]
        self.assertEqual(modes("dpad_up_short"), [])
        self.assertIn("item 3 item", self.out["du_short"])
        self.assertEqual(modes("dpad_up_refused"), [{"mode": "roller"}])
        self.assertEqual(modes("dpad_up_hold"), [{"mode": "roller"}])
        self.assertEqual(modes("dpad_up_back"), [{"mode": "walk"}])

    def test_dpad_right_reboots_with_one_press(self):
        """item: item robot.rebootMotors (item = item), item."""
        self.assertEqual([(r["method"], r["params"]) for r in self.recs("dpad_right") if not r["notify"]],
                         [("robot.rebootMotors", {"ids": []})])

    def test_leaving_page_exits_body_mode_and_coming_back_works(self):
        poses = [r["params"] for r in self.recs("leave_page") if r["method"] == "robot.pose"]
        self.assertEqual(poses[-1], {"active": False})
        self.assertTrue(any(p["vx"] > 0.15 for p in moves(self.recs("back_again"))), "item (V5 open)")

    def test_mirror_sends_nothing(self):
        """V16: item, item, item, item."""
        self.assertEqual(self.recs("mirror"), [])
        self.assertIn("item", self.out["steps"]["mirror"]["bar"])

    def test_phone_two_fingers_and_no_side_scroll(self):
        both = [p for p in moves(self.recs("two_fingers")) if p["vx"] > 0.1 and p["vyaw"] < -0.3]
        self.assertGreaterEqual(len(both), 2, "item: item")
        self.assertEqual(moves(self.recs("two_fingers"))[-1], {"vx": 0, "vy": 0, "vyaw": 0})
        for k in ("phone", "phone_mirror"):
            self.assertLessEqual(self.out[k]["sw"], self.out[k]["cw"], f"{k}: Phoneitem")


if __name__ == "__main__":
    unittest.main()
