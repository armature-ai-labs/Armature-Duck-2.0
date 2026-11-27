"""Studio'item WiFi'item: item configd item hotspot item net.json + item Studio + item Edge (390 item).

tests/wifi_e2e.mjs item, item Home, item, item, item'item', item last-try item'item'.
item 17: item (item WebSocket item)→'item'item, item probe.svg (CDP Fetch item), itemStop, itemSkipitem.
itemPCitem Node (>= 22)item Edgeitem (item)itemSkip.item STUDIO_E2E=0 itemSkip.
item Studio 18091, item robotd 17031 / configd 17032, Edge item 9344, item.
WIFI_E2E_DIR=item: item, Edge profile, net.json item (item, item)itemWIFI_SHOTS=item: item.
item: 
    py -3.13 -I -c "import sys;sys.path[:0]=['deps','.','dev'];import unittest;unittest.main(module=None,argv=['x','-v','tests.test_wifi_e2e'])"
"""
import json
import os
import re
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
                     "item Node >= 22 item Edge (item STUDIO_E2E=0): Skip WiFi item")
class WifiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("WIFI_E2E_DIR")
        cls.dir = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="wifi-e2e-"))
        cls.dir.mkdir(parents=True, exist_ok=True)
        state = cls.dir / "state"
        if state.exists():
            shutil.rmtree(state)
        net_state = cls.dir / "net.json"
        rec = cls.dir / "robotd-recv.jsonl"
        rec.unlink(missing_ok=True)
        p_rd, p_cfg, p_web, p_cdp = _port(17031), _port(17032), _port(18091), _port(9344)
        cls.port = p_web
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = lambda name: open(cls.dir / name, "w", encoding="utf-8")   # noqa: E731
        procs = {}
        try:
            procs["fake"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "dev" / "fake_robotd.py"), "--fixture", str(FIXTURE),
                 "--robotd-port", str(p_rd), "--configd-port", str(p_cfg), "--posture", "holding",
                 "--record", str(rec), "--net-mode", "hotspot", "--net-state-file", str(net_state)],
                cwd=ROOT, env=env, stdout=out("fake_robotd.out"), stderr=subprocess.STDOUT)
            procs["studio"] = subprocess.Popen(
                [sys.executable, "-I", str(ROOT / "run.py"), "--listen", f"127.0.0.1:{p_web}",
                 "--robotd", f"tcp:127.0.0.1:{p_rd}", "--configd", f"tcp:127.0.0.1:{p_cfg}",
                 "--net-state", str(net_state), "--dev-autologin", "--servo-sim",
                 "--state-dir", str(state), "--log-dir", str(cls.dir / "logs")],
                cwd=ROOT, env=env, stdout=out("studio.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_web}/healthz", "Studio")
            procs["edge"] = subprocess.Popen(
                [EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={p_cdp}",
                 f"--user-data-dir={cls.dir / 'edge'}", "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=out("edge.out"), stderr=subprocess.STDOUT)
            _wait_http(f"http://127.0.0.1:{p_cdp}/json/version", "item Edge item")
            args = [NODE, str(HERE / "wifi_e2e.mjs"), "--url", f"http://127.0.0.1:{p_web}/", "--cdp", str(p_cdp)]
            if os.environ.get("WIFI_SHOTS"):
                args += ["--shots", os.environ["WIFI_SHOTS"]]
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=240, cwd=ROOT)
            (cls.dir / "wifi_e2e.err").write_text(r.stderr or "", encoding="utf-8")
            if r.returncode != 0:
                raise AssertionError(f"Node item (item {cls.dir}): {(r.stderr or '')[-3000:]}")
            (cls.dir / "wifi_e2e.json").write_text(r.stdout, encoding="utf-8")
            cls.out = json.loads(r.stdout)
        finally:
            for p in reversed(list(procs.values())):
                _kill(p)
        if not keep:
            shutil.rmtree(cls.dir, ignore_errors=True)

    def test_page_has_no_errors(self):
        self.assertEqual(self.out["errors"], [])

    def test_card_is_before_onboard_and_fits_phone(self):
        c = self.out["card"]
        self.assertTrue(c["beforeOnboard"])
        self.assertIn("item (Microduck-fake)", c["text"])
        self.assertIn(f"duck-fake.local:{self.port}", c["text"])
        self.assertIn("item", c["text"])
        self.assertLessEqual(c["sw"], c["cw"])

    def test_card_addresses_by_device_and_own_hotspot_filtered(self):
        """item 16: item Clash item Windows item .local——item IP."""
        c = self.out["card"]
        self.assertIn(f"Windows PC: http://duck-fake:{self.port}/ (item)", c["text"])
        self.assertIn(f"itemPhone, Mac: http://duck-fake.local:{self.port}/", c["text"])
        self.assertIn("item duck-fake", c["text"])
        self.assertIn("Clash item TUN item", c["text"])
        self.assertIn("Home", c["ssids"])
        self.assertNotIn("Microduck-fake", c["ssids"])
        self.assertIn("IP 10.42.0.1", c["nameTitle"])

    def test_duck_addresses_in_more_page(self):
        a = self.out["addr"]
        self.assertEqual([(r["kind"], r["url"]) for r in a["rows"]],
                         [("ip", f"http://10.42.0.1:{self.port}/"), ("host", f"http://duck-fake:{self.port}/"),
                          ("local", f"http://duck-fake.local:{self.port}/")])
        self.assertTrue(all(r["copy"] == "item" for r in a["rows"]))
        self.assertIn(a["copied"], ("item", "item, item"))
        self.assertIn("item", a["text"])
        self.assertLessEqual(a["sw"], a["cw"])

    def test_home_expands_for_password_and_saved_network_is_marked(self):
        self.assertEqual(self.out["expanded"]["pskType"], "password")
        self.assertEqual(self.out["expanded"]["saved"], "item")
        self.assertIn("item (item)", self.out["expanded"]["savedNote"])

    def test_confirm_and_switching_text(self):
        c = self.out["confirm"]
        self.assertIn("item, item'Home'", c)
        self.assertIn("itemPhoneitem", c)
        self.assertIn("item", c)
        self.assertIn("Microduck-fake", c)
        s = self.out["switching"]
        self.assertIn("item……Phoneitem", s)
        # :  ( 16)
        self.assertIn(f"Windows PC: http://duck-fake:{self.port}/ (item)", s)
        self.assertIn(f"itemPhone, Mac: http://duck-fake.local:{self.port}/", s)
        self.assertIn("itemPhone, item: item duck-fake, item IP item", s)
        self.assertIn("item Clash item (192.168.0.0/16)item", s)

    def test_wrong_password_is_red_last_try_and_fits_phone(self):
        lt = self.out["lastTry"]
        self.assertIn("item'Home'item: item", lt["text"])
        self.assertNotEqual(lt["color"], "rgb(0, 0, 0)")
        self.assertLessEqual(lt["sw"], lt["cw"])

    def test_phone_can_open_logs_and_diag_from_more(self):
        m = self.out["mobile"]
        self.assertTrue(m["navHidden"])
        self.assertTrue(m["hasLogs"])
        self.assertTrue(m["hasDiag"])
        self.assertIn("item", m["logs"])
        self.assertEqual(m["diagClicked"], "/api/diag")

    def test_finding_card_probes_and_stops_when_hotspot_returns(self):
        """item 17: item, item →'item'item (item), item 2 item duck-fake / duck-fake.local item probe.svgitem
        item (item)→ item, item, item."""
        f = self.out["findA"]
        self.assertEqual(f["phase"], "finding")
        self.assertIn("item'Ghost'……", f["text"])
        self.assertIn("item'Ghost' (PCitem), item", f["text"])
        self.assertIn("item: item duck-fake item IP", f["text"])
        self.assertEqual(f["links"], [f"http://duck-fake:{self.port}/", f"http://duck-fake.local:{self.port}/"])
        self.assertLessEqual(f["sw"], f["cw"])
        probed = {re.sub(r"\?t=\d+$", "", u) for u in f["probes"]}
        self.assertEqual(probed, {f"http://duck-fake:{self.port}/probe.svg", f"http://duck-fake.local:{self.port}/probe.svg"})
        self.assertGreaterEqual(len(f["probes"]), 4, f["probes"])               # 4.5 item, item
        b = self.out["findBack"]
        self.assertFalse(b["finding"])
        self.assertIn("item'Ghost'item: item WiFi", b["lastTry"])
        self.assertEqual(b["probesLater"], b["probesAtBack"])                   # item: item

    def test_found_duck_opens_new_address(self):
        """item 17: item duck-fake.local item: itemSkipitem."""
        self.assertEqual(self.out["found"]["href"], f"http://duck-fake.local:{self.port}/")
        self.assertEqual(self.out["found"]["title"], "found")

    def test_hotspot_password_warning_only_while_no_password(self):
        self.assertTrue(self.out["hotspotPasswordHint"])
        self.assertFalse(self.out["hotspotPasswordHintAfterSet"])
        self.assertEqual(self.out["passwordSet"], 200)          # item


if __name__ == "__main__":
    unittest.main()
