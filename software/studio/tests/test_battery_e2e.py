"""item (battery-plan.md v2)item: item robotd (item)+ item Studio (item 20 item)+ item Edge.
tests/battery_e2e.mjs item (item, item, item, item, item, item, item, Phoneitem), 
item (item), 'item', item.

itemPCitem Node (≥ 22)item EdgeitemSkip.item STUDIO_E2E=0 itemSkip.
item Studio 18103, item robotd 17055 / configd 17056, Edge item 9353 (item), item.
BATTERY_E2E_DIR=item: item, Edge item profile item (item, item)itemBATTERY_SHOTS=item: item.
item: 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.','dev'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_battery_e2e'])"
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "deps"), str(ROOT / "dev")]

from tests.test_vpad_e2e import EDGE, NODE, _kill, _node_ok, _port, _wait_http  # noqa: E402

FIXTURE = HERE / "fixtures" / "now-20261018.jsonl"


@unittest.skipUnless(_node_ok() and EDGE and os.environ.get("STUDIO_E2E", "1") != "0",
                     "item Node ≥ 22 item Edge (item STUDIO_E2E=0): Skipitem")
class BatteryE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("BATTERY_E2E_DIR")
        cls.dir = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="battery-e2e-"))
        cls.dir.mkdir(parents=True, exist_ok=True)
        state = cls.dir / "state"
        if state.exists():
            shutil.rmtree(state)
        p_rd, p_cfg, p_web, p_cdp = _port(17055), _port(17056), _port(18103), _port(9353)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = lambda name: open(cls.dir / name, "w", encoding="utf-8")   # noqa: E731
        procs = {}
        try:
            #  (E-Stop): , ''E-Stop
            procs["fake"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "dev" / "fake_robotd.py"), "--fixture", str(FIXTURE),
                 "--robotd-port", str(p_rd), "--configd-port", str(p_cfg), "--posture", "limp"],
                cwd=ROOT, env=env, stdout=out("fake_robotd.out"), stderr=subprocess.STDOUT)
            procs["studio"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "run.py"), "--listen", f"127.0.0.1:{p_web}", "--robotd", f"tcp:127.0.0.1:{p_rd}",
                 "--configd", f"tcp:127.0.0.1:{p_cfg}", "--duck-label", "② item", "--dev-autologin",
                 "--state-dir", str(state), "--log-dir", str(cls.dir / "logs")],
                cwd=ROOT, env=dict(env, STUDIO_BATTERY_SPEED="20"), stdout=out("studio.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_web}/healthz", "Studio")
            procs["edge"] = subprocess.Popen(
                [EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={p_cdp}", f"--user-data-dir={cls.dir / 'edge'}",
                 "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=out("edge.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_cdp}/json/version", "item Edge item")
            args = [NODE, str(HERE / "battery_e2e.mjs"), "--url", f"http://127.0.0.1:{p_web}/", "--cdp", str(p_cdp),
                    "--fake", str(p_cfg)]
            if os.environ.get("BATTERY_SHOTS"):
                args += ["--shots", os.environ["BATTERY_SHOTS"]]
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=300, cwd=ROOT)
            (cls.dir / "battery_e2e.err").write_text(r.stderr or "", encoding="utf-8")
            if r.returncode != 0:
                raise AssertionError(f"Node item (item {cls.dir}): {(r.stderr or '')[-3000:]}")
            (cls.dir / "battery_e2e.json").write_text(r.stdout, encoding="utf-8")
            cls.out = json.loads(r.stdout)
            cls.settings_file = json.loads((state / "battery-settings.json").read_text(encoding="utf-8"))
        finally:
            for p in reversed(list(procs.values())):
                _kill(p)
        if not keep:
            shutil.rmtree(cls.dir, ignore_errors=True)

    def step(self, name):
        return self.out["steps"][name]

    def test_no_page_errors(self):
        self.assertEqual(self.out["errors"], [])

    def test_overview_states(self):
        calm = self.step("calm")
        self.assertRegex(calm["text"], r"item (4[5-9]|50) % 7\.6\d V · item")       # item: 7.62 V → item 48%
        self.assertNotIn("warn", calm["cls"])
        self.assertIn("item", self.step("nodata")["text"])
        self.assertIn("item", self.step("estimating")["text"])
        self.assertNotIn("item", self.step("estimating")["todo"])                    # item (item B-R2)
        warn = self.step("warn")
        self.assertRegex(warn["text"], r"item (2\d|30) %")                          # 7.40 V item: 7.42 V → 30%
        self.assertRegex(warn["todo"], r"item (2\d|30)%")
        red = self.step("red")
        self.assertRegex(red["todo"], r"item \d+%")
        self.assertIn("item", red["todo"])                                       # item
        low = self.step("lowvolt")
        self.assertIn("item", low["todo"])
        self.assertTrue(low["badge"])
        self.assertLess(low["todo"].index("item"), len(low["todo"]))

    def test_detail_explains(self):
        d = self.step("detail")
        self.assertEqual(d["expanded"], "true")
        text = "\n".join(d["lines"])
        for s in ("item 5 item (E-Stop", "15 item 3 item", "item 3.81 V", "±15%",
                  "item", "item, item", "item", "item (item 7.4–7.6 V)"):
            self.assertIn(s, text)

    def test_mobile_no_horizontal_scroll(self):
        m = self.step("mobile")
        self.assertLessEqual(m["sw"], m["cw"])
        self.assertLessEqual(m["popRight"], m["cw"])

    def test_settings(self):
        s = self.step("settings")["text"]
        for x in ("2S item (NP-F550 item)", "item (item)", "item", "item", "item", "item", "item",
                  "item, item (item)", "item",
                  "item (item 7.4–7.6 V)", "item"):
            self.assertIn(x, s)
        self.assertIn("item", self.step("bad")["msg"])
        self.assertIn("item 5%", self.step("bad")["msg"])
        self.assertIn("item", self.step("saved")["msg"])
        self.assertTrue(any("item 15% → 20%" in m for m in self.step("saved")["logs"]))
        self.assertIn("item +0.18 V", self.step("calc")["msg"])
        self.assertIn("item +0.18 V", self.step("calibrated")["now"])
        #  2S , 
        self.assertEqual((self.settings_file["preset"], self.settings_file["red_pct"], self.settings_file["cal_offset_v"]),
                         ("2s_liion", 20, 0.18))

    def test_bench(self):
        self.assertIn("item, item, item", self.step("benchForm")["text"])
        self.assertNotIn("item (item)", self.step("benchForm")["text"])
        b = self.step("bench")
        self.assertRegex(b["text"], r"item 7\.\d\d V item, item")
        self.assertNotIn("%", b["text"])
        self.assertNotIn("item", b["todo"])
        text = "\n".join(b["lines"])
        self.assertIn("item", text)
        self.assertNotIn("item", text)
        self.assertNotIn("item", text)


if __name__ == "__main__":
    unittest.main()
