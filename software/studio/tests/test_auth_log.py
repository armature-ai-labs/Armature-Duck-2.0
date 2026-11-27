"""Tests for auth and log modules."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import auth as auth_mod  # noqa: E402
from studio.auth import Auth, OriginGuard  # noqa: E402
from studio.log import Log, log_cmd  # noqa: E402


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Auth(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_password_refuses_login(self):
        token, why, _ = self.auth.login("whatever", "1.2.3.4")
        self.assertIsNone(token)
        self.assertIn("No admin password", why)

    def test_set_and_login(self):
        self.auth.set_password("duck-123456")
        self.assertTrue(self.auth.check_password("duck-123456"))
        self.assertFalse(self.auth.check_password("duck-12345"))
        token, why, _ = self.auth.login("duck-123456", "1.2.3.4", remember=True)
        self.assertIsNotNone(token, why)
        sess = self.auth.session(token)
        self.assertTrue(sess["remember"])
        self.assertTrue(Auth.recent_auth(sess))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            self.auth.set_password("123")

    def test_password_file_is_private(self):
        self.auth.set_password("duck-123456")
        if os.name == "posix":
            self.assertEqual(os.stat(self.auth.pw_path).st_mode & 0o777, 0o600)
        data = json.loads(self.auth.pw_path.read_text(encoding="utf-8"))
        self.assertNotIn("duck-123456", json.dumps(data))

    def test_backoff_after_five_failures_and_only_for_that_ip(self):
        self.auth.set_password("duck-123456")
        waits = [self.auth.login("bad", "9.9.9.9")[2] for _ in range(5)]
        self.assertEqual(waits[:4], [0, 0, 0, 0])
        self.assertEqual(waits[4], 30)
        token, why, wait = self.auth.login("duck-123456", "9.9.9.9")
        self.assertIsNone(token)
        self.assertGreater(wait, 0)
        self.assertIn("retry in", why)
        token, _, _ = self.auth.login("duck-123456", "8.8.8.8")
        self.assertIsNotNone(token)

    def test_sessions_survive_restart_and_expire(self):
        self.auth.set_password("duck-123456")
        keep, _, _ = self.auth.login("duck-123456", "1.1.1.1", remember=True)
        short, _, _ = self.auth.login("duck-123456", "1.1.1.1", remember=False)
        again = Auth(self.tmp.name)
        self.assertIsNotNone(again.session(keep))
        self.assertIsNotNone(again.session(short))
        later = time.time() + auth_mod.IDLE_S + 10
        with mock.patch("studio.auth.time.time", return_value=later):
            self.assertIsNone(again.session(short))
            self.assertIsNotNone(again.session(keep))

    def test_changing_password_revokes_sessions(self):
        self.auth.set_password("duck-123456")
        token, _, _ = self.auth.login("duck-123456", "1.1.1.1")
        self.auth.set_password("duck-654321")
        self.assertIsNone(self.auth.session(token))

    def test_changing_password_can_keep_the_current_browser(self):
        self.auth.set_password("duck-123456")
        token, _, _ = self.auth.login("duck-123456", "1.1.1.1")
        other, _, _ = self.auth.login("duck-123456", "2.2.2.2")
        self.auth.set_password("duck-654321", keep_token=token)
        self.assertIsNotNone(self.auth.session(token))
        self.assertIsNone(self.auth.session(other))
        self.assertTrue(self.auth.check_password("duck-654321"))

    def test_clear_password_removes_file_and_sessions(self):
        self.auth.set_password("duck-123456")
        token, _, _ = self.auth.login("duck-123456", "1.1.1.1")
        self.auth.clear_password()
        self.assertFalse(self.auth.has_password())
        self.assertFalse(self.auth.pw_path.exists())
        self.assertIsNone(self.auth.session(token))

    def test_reauth(self):
        self.auth.set_password("duck-123456")
        token, _, _ = self.auth.login("duck-123456", "1.1.1.1")
        with mock.patch("studio.auth.time.time", return_value=time.time() + 700):
            self.assertFalse(Auth.recent_auth(self.auth.session(token)))
            self.assertFalse(self.auth.reauth(token, "wrong"))
            self.assertTrue(self.auth.reauth(token, "duck-123456"))
            self.assertTrue(Auth.recent_auth(self.auth.session(token)))


class OriginGuardTest(unittest.TestCase):
    def setUp(self):
        self.g = OriginGuard(names=["microduck-2", "microduck-2.local"])

    def test_host(self):
        self.assertTrue(self.g.host_ok("microduck-2.local"))
        self.assertTrue(self.g.host_ok("microduck-2:80"))
        self.assertTrue(self.g.host_ok("localhost:8000"))
        self.assertTrue(self.g.host_ok("127.0.0.1:8000"))
        self.assertTrue(self.g.host_ok("192.168.0.103", local_ip="192.168.0.103"))
        self.assertFalse(self.g.host_ok("192.168.0.103", local_ip="192.168.0.105"))
        self.assertFalse(self.g.host_ok("evil.example.com"))
        self.assertFalse(self.g.host_ok(""))

    def test_origin(self):
        self.assertTrue(self.g.origin_ok("http://microduck-2.local", "microduck-2.local"))
        self.assertTrue(self.g.origin_ok("http://192.168.0.103:8000", "192.168.0.103:8000"))
        self.assertFalse(self.g.origin_ok("http://192.168.0.103:8080", "192.168.0.103:8000"))
        self.assertFalse(self.g.origin_ok("http://evil.example.com", "microduck-2.local"))
        self.assertFalse(self.g.origin_ok(None, "microduck-2.local"))
        self.assertFalse(self.g.origin_ok("file://", "microduck-2.local"))


class SetPasswordCommandTest(unittest.TestCase):
    def test_refuses_root_before_prompting(self):
        from studio import __main__ as main_mod
        with mock.patch.object(main_mod.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(main_mod.getpass, "getpass") as ask:
            with self.assertRaises(SystemExit) as e:
                main_mod.main(["set-password"])
        self.assertIn("Do not run with sudo", str(e.exception))
        self.assertIn("radxa", str(e.exception))
        ask.assert_not_called()


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Log(self.tmp.name, echo=False, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_error_line_names_step_raw_and_hint(self):
        self.log.error("Stand failed", cat="robotd", step="robot.init", raw="waiting_for_bus", hint="Wait 10s")
        text = self.log.files()[-1].read_text(encoding="utf-8")
        self.assertIn("[robotd][ERROR] Stand failed", text)
        self.assertIn("at: robot.init", text)
        self.assertIn("raw: waiting_for_bus", text)
        self.assertIn("hint: Wait 10s", text)
        e = self.log.recent()[-1]
        self.assertEqual((e["level"], e["step"]), ("error", "robot.init"))

    def test_success_line_does_not_say_stuck(self):
        self.log.info("<- Relax ok", cat="Command", step="robot.relax")
        text = self.log.files()[-1].read_text(encoding="utf-8")
        self.assertIn("step: robot.relax", text)
        self.assertNotIn("at:", text)

    def test_debug_not_on_page_or_disk_by_default(self):
        self.log.debug("frame 123")
        self.assertEqual(self.log.recent(min_level="debug"), [])
        self.assertEqual(self.log.files(), [])

    def test_filter(self):
        self.log.info("connected", cat="Connection")
        self.log.warn("high temp", cat="System")
        self.assertEqual([e["msg"] for e in self.log.recent(cats=["Connection"])], ["connected"])
        self.assertEqual([e["msg"] for e in self.log.recent(min_level="warn")], ["high temp"])

    def test_prune_keeps_recent_days(self):
        d = Path(self.tmp.name)
        for i in range(20):
            (d / f"studio-2026-09-{i + 1:02d}.log").write_text("x", encoding="utf-8")
        self.log.info("today")
        left = sorted(p.name for p in d.glob("studio-*.log"))
        self.assertEqual(len(left), 14)
        self.assertIn("studio-" + time.strftime("%Y-%m-%d") + ".log", left)   # item: item 1970

    def test_prune_by_size_when_today_sorts_first(self):
        #  1970 , , 
        from studio import log as log_mod
        d = Path(self.tmp.name)
        for i in range(3):
            (d / f"studio-2099-01-0{i + 1}.log").write_text("x" * 50, encoding="utf-8")
        with mock.patch.object(log_mod, "MAX_TOTAL_BYTES", 120):
            self.log.info("today")
        left = sorted(p.name for p in d.glob("studio-*.log"))
        self.assertIn("studio-" + time.strftime("%Y-%m-%d") + ".log", left)
        self.assertLess(len([n for n in left if "2099" in n]), 3)

    def test_subscribe_receives_and_drops_oldest(self):
        import asyncio

        async def run():
            q = self.log.subscribe(maxsize=2)
            for i in range(3):
                self.log.info(f"item {i}")
            await asyncio.sleep(0)
            got = [q.get_nowait()["msg"] for _ in range(q.qsize())]
            self.log.unsubscribe(q)
            return got

        self.assertEqual(asyncio.run(run()), ["item 1", "item 2"])


    def test_secrets_are_hidden(self):
        self.log.hide("duck-123456")
        self.log.warn("Login failed", raw="password=duck-123456")
        text = self.log.files()[-1].read_text(encoding="utf-8")
        self.assertNotIn("duck-123456", text)
        self.assertIn("[REDACTED]", text)

    def test_secret_keys_are_redacted_even_without_hide(self):
        """Redact sensitive keys like psk and password in log context."""
        with log_cmd(self.log, "Connect WiFi", "configd net.connect",
                     args={"ssid": "Home", "psk": "Unique-PSK-2026-xyz"}) as t:
            t.ok({"outcome": "connected", "ssid": "Home"})
        self.log.warn("ctx", ctx={"ssid": "Home", "psk": "Unique-PSK-2026-xyz", "nested": [{"password": "p2-unique"}]})
        text = self.log.files()[-1].read_text(encoding="utf-8")
        self.assertNotIn("Unique-PSK-2026-xyz", text)
        self.assertNotIn("p2-unique", text)
        self.assertIn("[REDACTED]", text)

    def test_repeats_within_a_minute_are_merged(self):
        for _ in range(5):
            self.log.warn("TOF disconnected", cat="Connection")
        self.assertEqual(len(self.log.recent()), 1)
        with mock.patch("studio.log.time.time", return_value=time.time() + 61):
            self.log.warn("TOF disconnected", cat="Connection")
        msgs = [e["msg"] for e in self.log.recent()]
        self.assertIn("repeated 4 times within 1m", msgs[1])
        self.assertEqual(msgs[2], "TOF disconnected")

    def test_command_timer_logs_request_reply_and_failure(self):
        with log_cmd(self.log, "Stand", "robot.init", args={"x": 1}) as t:
            t.fail("Bus not ready", raw={"error": "waiting_for_bus"}, hint="Wait 10s")
        with log_cmd(self.log, "Relax", "robot.relax"):
            pass
        msgs = [e["msg"] for e in self.log.recent()]
        self.assertTrue(msgs[0].startswith("→ Stand"))
        self.assertIn("failed: Bus not ready", msgs[1])
        self.assertTrue(msgs[3].startswith("← Relax ok"))
        self.assertEqual(self.log.recent()[1]["level"], "error")

    def test_timestamp_has_milliseconds(self):
        self.log.info("x")
        line = self.log.files()[-1].read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}")


if __name__ == "__main__":
    unittest.main()
