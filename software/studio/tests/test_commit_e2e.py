"""item'item' (item 13)item: item robotd (--mode commissioning --calib factory)+ item Studio + item Edge.

item: tests/commit_e2e.mjs item 3 item IMU item (item, item, 
verify-imu item), item (--servo-sim), item 3 item.item motion + control, 
item 3 item, item.item'item': item, item.
IMU item'item / item' (item 17): item, item 3 item'Stand Up', item'E-Stop'item.

PCitem root item: item studio.helper.calib_commit item (item).
itemPCitem Node (≥ 22)item Edgeitem (item)itemSkip.item STUDIO_E2E=0 itemSkip.
item Studio 18091, item robotd 17031 / configd 17032, Edge item 9344, item.
COMMIT_E2E_DIR=item: item, Edge item profile item (item, item)itemCOMMIT_SHOTS=item: item.
item: 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.','dev'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_commit_e2e'])"
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "deps"), str(ROOT / "dev")]

from tests.test_vpad_e2e import EDGE, NODE, _kill, _node_ok, _port, _wait_http  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"
STUCK_CALIB = "motion requires all 15 calibrated joints, measured limits and verified IMU mounting for the lsm6dsv16x IMU"

BOOT = r'''
import copy, json, os, socket, sys, time
sys.path[:0] = [ROOT, os.path.join(ROOT, "deps"), os.path.join(ROOT, "dev")]
from studio import helper
from studio import commit as C

helper.available = lambda: True
# PC root :  bus-take,  bus-give-back"".
helper.flag_present = lambda: False
helper.bus_take = lambda by, allow_seated=False, timeout=30: (True, "taken", {})
helper.bus_give_back = lambda by, *, soft=True, marker=None, timeout=60: (True, "soft", {})
# '' SSH : ,  ()
helper.ssh_status = lambda timeout=10: (True, "ssh_status", {"exit": 0, "result": "ssh_status", "password_login": False,
                                                            "ssh_active": True, "keys": 0, "user": "radxa"})


def fake_rpc(method, obj):
    port = int(os.environ["E2E_FAKE_PORT"])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": obj}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            part = s.recv(4096)
            if not part:
                break
            buf += part
    return json.loads(buf.split(b"\n", 1)[0])


def fake_set(obj):
    return fake_rpc("fake.set", obj)


# Only replaces the systemd/filesystem boundary. Real ImuBackend capture, solver,
# runtime verify and exact calibration readback still run against fake robotd.
redo_state = {"active": False, "phase": "idle", "session_id": None, "source": None}
redo_backup = None


def fake_imu_state(*, root="/"):
    state = copy.deepcopy(redo_state)
    try:
        with open(os.environ["E2E_OUTCOME_FILE"], encoding="utf-8") as f:
            phase = json.load(f).get("redo_phase")
        if state["active"] and phase in ("entering", "finishing", "cancelling"):
            state["phase"] = phase
    except (OSError, ValueError):
        pass
    return state


def fake_imu_recalibrate(by, action, *, confirmed=None, session_id=None, source=None, quat=None, timeout=180):
    global redo_state, redo_backup
    if action == "enter":
        redo_backup = fake_rpc("robot.busCommand", {"action": "calibration-status"})["result"]
        redo_state = {"active": True, "phase": "commissioning", "session_id": "e2e-imu-session",
                      "source": redo_backup.get("imu_source") or "bus"}
        fake_set({"mode": "commissioning", "imu_pose": "upright"})
        fake_rpc("robot.relax", {})
        time.sleep(0.25)
        return True, "entered", dict(redo_state, note="item, item")
    if action == "status":
        return True, "status", dict(redo_state)
    if action not in ("finish", "cancel") or not redo_state["active"]:
        return False, "no_session", {"msg": "item"}
    current = fake_rpc("robot.busCommand", {"action": "calibration-status"})["result"]
    restored = copy.deepcopy(current if action == "finish" else redo_backup)
    restored["calibration"]["motion_enabled"] = redo_backup["calibration"]["motion_enabled"]
    fake_set({"calib_commit": {"calibration": restored, "outcome": "ok"}, "posture": "limp", "imu_pose": "upright"})
    redo_state = {"active": False, "phase": "idle", "session_id": None, "source": None}
    return True, "finished" if action == "finish" else "cancelled", dict(redo_state,
        mode="motion", readback_ok=True, note="item, itemE-Stop" if action == "finish" else "item")


helper.imu_redo_state = fake_imu_state
helper.imu_recalibrate = fake_imu_recalibrate


def outcome():
    path = os.environ.get("E2E_OUTCOME_FILE")
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("outcome") or "ok"
    except (OSError, ValueError):
        return "ok"


def fake_calib_commit(by, joints, imu, dry_run=False, timeout=180, on_line=None):
    cur = {"motion_enabled": False, "imu_mount_verified": False,
           "imu_mount_quat": list(C.DEFAULT_BUS_QUAT), "current_ma_per_count": 0.0, "units_verified": False,
           "imu_mounts": {}, "joints": [{"name": n, "id": C.JOINT_IDS[n], "zero_raw": 2048,
                                         "direction": -1, "calibrated": False} for n in C.JOINT_ORDER]}
    cal = C.build_calibration(cur, joints, imu)
    C.validate_calibration(cal, imu["source"])
    text = C.render_calibration(cal)
    if on_line:
        on_line({"step": "write", "msg": "item: item"})
    if dry_run:
        return True, "preview", {"exit": 0, "toml": text}

    want = outcome()
    if want == "reject":
        #  motion,  -> waiting_for_bus robotd , 
        fake_set({"calib_commit": {"calibration": cal, "outcome": "reject"}})
        fake_set({"restart": 1.5})
        if on_line:
            on_line({"step": "wait", "msg": "item: item"})
        time.sleep(1.7)
        # : , ,  ( robotd ).
        fake_set({"mode": "commissioning", "calib": "factory"})
        fake_set({"restart": 1.0})
        time.sleep(1.2)
        msg = "item: " + %r + "item, item"
        return False, "calib_rejected", {"exit": 5, "msg": msg, "rolled_back": True,
                                         "readback_ok": False, "mismatches": [],
                                         "bus_status": {"mode": "motion", "phase": "waiting_for_bus", "error": %r}}
    fake_set({"calib_commit": {"calibration": cal, "outcome": "ok"}})
    if on_line:
        on_line({"step": "done", "msg": "item: item, item"})
    return True, "committed", {"exit": 0, "toml": text, "mode": "motion", "phase": "control",
                               "readback_ok": True, "mismatches": [], "fake": True}


helper.calib_commit = fake_calib_commit
from studio.__main__ import main
main(sys.argv[1:])
''' % (STUCK_CALIB, STUCK_CALIB)


@unittest.skipUnless(_node_ok() and EDGE and os.environ.get("STUDIO_E2E", "1") != "0",
                     "item Node ≥ 22 item Edge (item STUDIO_E2E=0): Skip'item'item")
class CommitE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("COMMIT_E2E_DIR")
        cls.dir = (Path(keep) if keep else Path(tempfile.mkdtemp(prefix="commit-e2e-"))).resolve()
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.state = cls.dir / "state"
        cls.state.mkdir(exist_ok=True)
        cls.outcome_file = cls.dir / "commit-outcome.json"
        p_rd, p_cfg, p_web, p_cdp = _port(17031), _port(17032), _port(18091), _port(9344)
        cls.p_web, cls.p_cfg = p_web, p_cfg
        env = dict(os.environ, PYTHONIOENCODING="utf-8", E2E_FAKE_PORT=str(p_cfg),
                   E2E_OUTCOME_FILE=str(cls.outcome_file))
        out = lambda name: open(cls.dir / name, "w", encoding="utf-8")   # noqa: E731
        boot = cls.dir / "e2e_boot.py"
        boot.write_text(f"ROOT = {str(ROOT)!r}\n" + BOOT, encoding="utf-8")
        cls.procs = {
            "fake": subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "dev" / "fake_robotd.py"), "--fixture", str(FIXTURE),
                 "--robotd-port", str(p_rd), "--configd-port", str(p_cfg), "--mode", "commissioning",
                 "--posture", "commissioning", "--calib", "factory", "--record", str(cls.dir / "robotd-recv.jsonl")],
                cwd=ROOT, env=env, stdout=out("fake.out"), stderr=subprocess.STDOUT),
        }
        try:
            cls.procs["studio"] = subprocess.Popen(
                [sys.executable, "-I", str(boot), "--listen", f"127.0.0.1:{p_web}", "--robotd", f"tcp:127.0.0.1:{p_rd}",
                 "--configd", f"tcp:127.0.0.1:{p_cfg}", "--duck-label", "② item", "--dev-autologin", "--servo-sim",
                 "--state-dir", str(cls.state), "--log-dir", str(cls.dir / "logs")],
                cwd=ROOT, env=env, stdout=out("studio.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_web}/healthz", "Studio")
            edge_dir = cls.dir / "edge"
            shutil.rmtree(edge_dir, ignore_errors=True)      # item profile item Edge item
            edge_dir.mkdir(parents=True, exist_ok=True)         # Edge item (item Lock file item)
            cls.procs["edge"] = subprocess.Popen(
                [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-setuid-sandbox",
                 "--enable-unsafe-swiftshader", f"--remote-debugging-port={p_cdp}",
                 f"--user-data-dir={edge_dir}", "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=out("edge.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_cdp}/json/version", "Edge item", timeout=60)
        except Exception:
            for p in cls.procs.values():
                _kill(p)
            raise
        cls.p_cdp = p_cdp

    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "procs", {}).values():
            _kill(p)
        if os.environ.get("COMMIT_E2E_DIR"):
            return
        shutil.rmtree(cls.dir, ignore_errors=True)

    def node(self, phase):
        args = [NODE, str(HERE / "commit_e2e.mjs"), "--url", f"http://127.0.0.1:{self.p_web}/",
                "--cdp", str(self.p_cdp), "--fake-port", str(self.p_cfg), "--phase", phase,
                "--control-file", str(self.outcome_file)]
        if os.environ.get("COMMIT_SHOTS"):
            args += ["--shots", os.environ["COMMIT_SHOTS"]]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=220, cwd=ROOT)
        (self.dir / f"commit_e2e-{phase}.err").write_text(r.stderr or "", encoding="utf-8")
        if r.returncode != 0:
            raise AssertionError(f"Node item ({phase})item (item {self.dir}): {(r.stderr or '')[-4000:]}")
        (self.dir / f"commit_e2e-{phase}.json").write_text(r.stdout, encoding="utf-8")
        return json.loads(r.stdout)

    def fake_set(self, obj):
        with socket.create_connection(("127.0.0.1", self.p_cfg), timeout=5) as s:
            s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "fake.set", "params": obj}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                part = s.recv(4096)
                if not part:
                    break
                buf += part
        return json.loads(buf.split(b"\n", 1)[0])

    def test_01_factory_preview(self):
        first = self.node("factory")
        self.assertGreaterEqual(first["factory"]["bad"], 1, first)
        self.assertTrue(first["factory"]["writeDisabled"], first["factory"])
        blob = first["factory"]["desc"] + json.dumps(first["factory"]["checks"], ensure_ascii=False)
        self.assertIn("item", blob)
        self.assertEqual(first["preview"]["jointRows"], 15, first["preview"])
        self.assertTrue(first["preview"]["imu"], first["preview"])
        self.assertIn("motion_enabled = true", first["preview"]["toml"])

    def test_01b_mark_servo_step_done(self):
        """item 16: item 1 item'item' (item: item, item)→ ✓, item 3 item'item'item."""
        m = self.node("mark")
        self.assertEqual(m["before"]["check"], "bad", m)
        self.assertEqual(m["ask"], "item: item", m)
        self.assertNotEqual(m["afterOne"], "item", m)                          # item
        self.assertEqual(m["after"]["row"], "item", m)
        self.assertIn("item", m["after"]["text"])
        self.assertIn("item 1 item: 15 item 2048 item (item Studio item)", m["after"]["check"])
        self.assertEqual(m["errors"], [])

    def test_01d_ssh_card_on_the_more_page(self):
        """10-21'item SSH item, item': 'item'item SSH item, item, item
        item'item, 0 item' (item).item: item, item, item."""
        r = self.node("ssh")
        s = r["ssh"]
        self.assertIn("SSH item", s["text"])
        self.assertIn("item, item, item", s["text"])
        self.assertEqual(s["now"], "item: item (item), item")
        self.assertTrue(s["needWebPw"], s)
        self.assertEqual(s["inputs"], 0)
        self.assertTrue(s["onDisabled"])
        self.assertEqual(r["errors"], [])

    def test_01c_stance_buttons_in_commissioning(self):
        """item 10-21'item': item'item'item, item imu.op hold, 
        item (fork hold-home)item, item'item (item)'item'item (E-Stop)'item relax, item."""
        s = self.node("stance0")
        st = s["stance"]
        self.assertEqual(st["stand"], {"text": "item", "disabled": False}, st)
        self.assertEqual(st["relax"], {"text": "item (E-Stop)", "disabled": False}, st)
        self.assertIn("item: item (item)", st["now"])
        self.assertIsNone(st["why"], st)
        self.assertFalse(st["goCommit"], st)
        self.assertIn("item", st["text"])
        self.assertEqual(s["holdSent"], [{"name": "imu.op", "args": {"op": "hold"}}])
        self.assertEqual(len(s["dialogs"]), 1, s["dialogs"])
        self.assertIn("15 item 3 item", s["dialogs"][0])
        self.assertIn("item: item (item)", s["holding"]["now"])
        self.assertEqual(s["holding"]["stand"], {"text": "item", "disabled": True}, s["holding"])
        self.assertEqual(s["holding"]["why"], "item")
        self.assertEqual(s["relaxSent"], [{"name": "relax", "args": {}}])
        self.assertIn("item: item (item)", s["after"]["now"])
        recv = [json.loads(x) for x in (self.dir / "robotd-recv.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        holds = [r for r in recv if r.get("method") == "robot.busCommand" and (r.get("params") or {}).get("action") == "hold-home"]
        self.assertEqual(len(holds), 1, holds)
        dirs = holds[0]["params"]["directions"]
        self.assertEqual(len(dirs), 15)
        self.assertEqual(holds[0]["params"]["duration_ms"], 3000)
        self.assertEqual(s["errors"], [])

    def test_02_imu_coarse_verify(self):
        imu = self.node("imu")
        self.assertIn("item", imu["imu"], imu)

    def test_02b_imu_coarse_verify_while_holding(self):
        result = self.node("imu-hold")
        self.assertIn("item", result["imu"], result)
        self.assertIn("item'item'", result["start"])
        for label in ("holdBefore", "holdAfter"):
            self.assertEqual(result[label]["torque_state"], "all_enabled", result[label])
            self.assertEqual(result[label]["hold"]["stage"], "holding", result[label])
            self.assertIs(result[label]["policy_motion_enabled"], False)
        self.assertEqual(result["cleaned"]["torque_state"], "off")
        self.assertEqual(result["errors"], [])

    def test_03_servo_center_then_commit(self):
        from studio import commit as C
        servo = self.node("servo")
        self.assertEqual(servo["planRows"], 15, servo)
        # --servo-sim ,  servo-centered.json ().
        #  15 , '' test_servo_* .
        C.record_centered(str(self.state), list(C.JOINT_IDS.values()), "e2e item")
        time.sleep(1.6)                       # CommitService item 1 item state item

        second = self.node("commit")
        self.assertEqual(second["ready"]["bad"], 0, second["ready"])
        self.assertFalse(second["ready"]["writeDisabled"], second["ready"])
        self.assertIn("item, itemE-Stop, item", second["confirmText"])
        self.assertEqual(second["done"]["rowState"], "item", second["done"])
        self.assertIn("item", second["topbar"], second)
        self.assertEqual(second["errors"], [])

    def test_03b_stance_buttons_in_motion(self):
        """item 17: item'item'item, item'Stand Up'item (pad.op stand → robot.init)item
        'item (E-Stop)'item'E-Stop'item (relax)."""
        s = self.node("stance")
        self.assertEqual(s["before"]["stand"], {"text": "item", "disabled": False}, s["before"])
        self.assertFalse(s["before"]["relax"]["disabled"], s["before"])
        self.assertIn("item: item", s["before"]["now"])
        self.assertFalse(s["before"]["goCommit"])
        self.assertEqual(s["standSent"], [{"name": "pad.op", "args": {"op": "stand"}}])
        self.assertEqual(s["remoteSent"], s["standSent"])
        self.assertEqual(s["relaxSent"], [{"name": "relax", "args": {}}])
        recv = [json.loads(x) for x in (self.dir / "robotd-recv.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        methods = [r.get("method") for r in recv]
        self.assertGreaterEqual(methods.count("robot.init"), 2, methods[-40:])      # IMU item, item
        self.assertIn("robot.relax", methods)
        self.assertEqual(s["errors"], [])

    def test_04_failure_rollback_is_visible(self):
        self.outcome_file.write_text(json.dumps({"outcome": "reject"}), encoding="utf-8")
        self.fake_set({"mode": "commissioning"})          # item
        time.sleep(1.6)
        failed = self.node("fail")
        self.assertTrue(failed["offline"], failed.get("offline"))
        self.assertIn("item", failed["offline"]["text"])
        self.assertEqual(failed["failed"]["rowState"], "item", failed["failed"])
        self.assertTrue(any("item" in m for m in failed["failed"]["msgs"]), failed["failed"])
        self.assertEqual((failed.get("wsCommit") or {}).get("job", {}).get("phase"), "failed", failed.get("wsCommit"))
        self.outcome_file.write_text(json.dumps({"outcome": "ok"}), encoding="utf-8")

    def test_03c_verified_imu_can_be_recalibrated(self):
        """item, item."""
        self.fake_set({"mode": "motion", "calib": "seed", "posture": "limp", "imu_pose": "upright"})
        result = self.node("imu-redo")
        self.assertTrue(result["entryVisible"], result)
        self.assertEqual(result["beforeConfirm"], [], result)
        self.assertTrue(result["active"]["stand"]["disabled"], result)
        self.assertFalse(result["active"]["relax"]["disabled"], result)
        self.assertIn("item, item", result["verifyInstruction"])
        self.assertEqual(result["original"]["calibration"]["joints"], result["after"]["calibration"]["joints"])
        self.assertTrue(any(j["zero_raw"] != 2048 for j in result["after"]["calibration"]["joints"]))
        self.assertEqual(result["after"], result["cancelled"])
        self.assertIn("item", result["afterStance"]["now"])
        self.assertEqual(result["motionCommands"], [])
        self.assertEqual(result["commissioningCommands"], [])
        self.assertEqual(result["recoveredPhases"], ["entering", "finishing", "cancelling"])
        self.assertEqual(result["errors"], [], result)


if __name__ == "__main__":
    unittest.main()
