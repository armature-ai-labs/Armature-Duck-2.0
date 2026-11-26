"""Wi-Fi networking: reads netcheck state, calls configd net.status / scan / connect / forget, and toggles hotspot via helper.

microduck-netcheck starts a hotspot (NM connection Microduck-Hotspot, 10.42.0.1) on boot if Wi-Fi fails to connect,
and writes its current state to /run/microduck/net.json. Studio only reads it:

    {"mode": "wifi" | "hotspot" | "none",
     "hotspot_ssid": "Microduck-c51b", "hotspot_ip": "10.42.0.1", "hotspot_psk": "microduck",
     "hostname": "duck-c51b", "since": 1790000000.0,
     "scan": [{"ssid": "Home", "signal": 72, "security": "wpa_psk"}], "scan_at": 1790000000.0}

If the file is missing (desktop dev, older images), Wi-Fi UI is hidden while everything else works normally.
In hotspot mode, wlan0 cannot scan (AP mode), so if configd net.scan is empty/fails, Studio falls back to the pre-hotspot scan marked cached.

Passwords: home Wi-Fi passwords are only passed in memory from browser -> configd and never written to logs, state, last-try, or diagnostic bundles.
hotspot.psk in state is the duck's own public hotspot password (microduck), shown in the UI.
"""
import asyncio
import json
import os
import re
import socket
import time
from pathlib import Path

from . import helper
from .log import log_cmd
from .rpc import Conn, RpcError, Timeout, Unreachable, call_once

NET_STATE = "/run/microduck/net.json"
LAST_TRY_NAME = "net-last-try.json"
READ_EVERY_S = 2.0
STATUS_EVERY_S = 5.0
STATUS_TIMEOUT_S = 4.0
CONNECT_DELAY_S = 2.0          # Wait for UI to receive 'connecting' before tearing down hotspot
CONNECT_TIMEOUT_S = 60.0
SCAN_TIMEOUT_S = 15.0
FORGET_TIMEOUT_S = 10.0
STATUS_MAX_AGE_S = 15.0        # 5s interval; 3 consecutive failures before treating as unknown
PORT = 8000
HOTSPOT_IP = "10.42.0.1"       # Default NetworkManager shared hotspot IP
OWN_PREFIX = "Microduck-"      # Duck hotspot SSIDs start with Microduck-
PSK_HEX = re.compile(r"^[0-9A-Fa-f]{64}$")
PERMISSION = "Studio lacks permission to modify Wi-Fi (configd did not allow radxa)"

REASON_TEXT = {
    "bad_key": "Incorrect password",
    "not_found": "Network not found (check SSID, move closer to router; hidden SSIDs are not supported)",
    "timeout": "Connected but failed to obtain an IP address; try again",
    "studio_timeout": "Network daemon did not respond within 60s",
    "unsupported": "Unsupported Wi-Fi security (enterprise auth or missing password)",
    "other": "Connection failed",
}


class Refused(Exception):
    """Network operation refused or invalid parameters: message is user-facing."""


def validate_ssid(value):
    """SSID 1-32 bytes (UTF-8). Do not strip: leading/trailing spaces may be part of the SSID."""
    if not isinstance(value, str) or value == "":
        raise Refused("Wi-Fi name must be 1-32 bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as e:
        raise Refused(f"Wi-Fi name contains invalid characters: {e}") from None
    if size > 32:
        raise Refused("Wi-Fi name must be at most 32 bytes (UTF-8)")
    if "\x00" in value:
        raise Refused("Wi-Fi name cannot contain NUL")
    return value


def validate_psk(value):
    """Empty = open network; otherwise 8-63 characters or 64 hex digits (WPA formats)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise Refused("Wi-Fi password must be a string")
    if PSK_HEX.fullmatch(value):
        return value
    if 8 <= len(value) <= 63:
        return value
    raise Refused("Wi-Fi password must be 8-63 characters or 64 hex digits")


def validate_connect(ssid, psk):
    return validate_ssid(ssid), validate_psk(psk)


def addresses(host, ip, port=PORT, scheme="http"):
    """Available addresses for this duck, with platform notes (Windows with system proxy bypasses bare hostname via <local>).
    Factory HTTPS cert covers hostname, hotspot IP, and loopback only."""
    out = []
    if ip and (scheme != "https" or ip in (HOTSPOT_IP, "127.0.0.1")):
        out.append({"kind": "ip", "url": f"{scheme}://{ip}:{port}/", "note": "IP: works on any device (may change across router/DHCP renewals)"})
    out.append({"kind": "host", "url": f"{scheme}://{host}:{port}/", "note": "Recommended for Windows (works even with system proxy enabled)"})
    out.append({"kind": "local", "url": f"{scheme}://{host}.local:{port}/", "note": "Recommended for iOS and macOS"})
    return out


def own_hotspot(n, hotspot_ssid):
    """Whether a scan entry is the duck's own hotspot (avoid showing it as a saved Wi-Fi network to join)."""
    ssid = n.get("ssid") if isinstance(n, dict) else None
    if not isinstance(ssid, str):
        return False
    if hotspot_ssid:
        return ssid == hotspot_ssid
    return ssid.startswith(OWN_PREFIX) and bool(n.get("saved"))


def explain(reason, detail=None):
    """Map configd failure reason to a user-facing sentence."""
    if detail and str(detail).strip() == PERMISSION:
        return PERMISSION
    base = REASON_TEXT.get(reason, REASON_TEXT["other"])
    if reason == "other" and detail:
        return f"{base} ({detail})"
    return base


def _scrub(text, psk=None):
    """Redact home Wi-Fi password before writing to logs or last-try."""
    if text is None:
        return None
    out = str(text)
    if psk:
        out = out.replace(psk, "[REDACTED]")
    return out[:600]


def _rpc_error(e, what):
    if isinstance(e, RpcError):
        if e.code == 14 or re.search(r"permission|not authori[sz]ed|not allowed|access denied", e.message or "", re.I):
            return PERMISSION
        return f"{what}: {e.message}"
    if isinstance(e, Timeout):
        return f"{what} timed out ({e})"
    return f"Cannot connect to configd: {e}"


class _SchemeView:
    """Adapter for summarize(): only exposes view()."""

    def __init__(self, net, scheme):
        self.net, self.scheme = net, scheme

    def view(self, now=None):
        return self.net.view(now, scheme=self.scheme)


class Net:
    """Network state and operations. start() launches two loops: read net.json every 2s and poll configd net.status every 5s."""

    def __init__(self, configd_addr, state_file=NET_STATE, state_dir=None, log=None, *, dev_mode=False,
                 hostname=None, port=PORT, scheme="http"):
        self.configd_addr = configd_addr
        self.state_file = Path(state_file)
        self.hostname = hostname
        self.port = int(port)
        self.scheme = "https" if scheme == "https" else "http"
        self.log = log
        self.dev_mode = bool(dev_mode)
        self.last_try_path = Path(state_dir) / LAST_TRY_NAME if state_dir else None
        self.cfg = Conn(configd_addr, "configd-net", log) if configd_addr and configd_addr != "none" else None
        self.info = None
        self.info_at = None
        self.status = None
        self.status_at = None
        self.busy = None
        self.last_try = self._load_last_try()
        self._bad_state_logged = False
        self._status_error_logged = False
        self._tasks = []
        self.connect_task = None       # Background Wi-Fi connect task held by app.py so client disconnect does not cancel it

    # ---- Lifecycle ----

    def start(self):
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._read_loop(), name="net-state"),
                       loop.create_task(self._status_loop(), name="net-status")]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        self._tasks = []
        if self.cfg is not None:
            self.cfg.close()

    async def _read_loop(self):
        while True:
            try:
                await asyncio.to_thread(self._read_once)
            except Exception as e:  # noqa: BLE001 — reading net state must not crash Studio
                if self.log:
                    self.log.warn("Error reading network state file", cat="Network", step=str(self.state_file),
                                  raw=f"{type(e).__name__}: {e}", dedup=True)
            await asyncio.sleep(READ_EVERY_S)

    def _read_once(self):
        try:
            raw = self.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            had = self.info is not None
            self.info, self.info_at = None, None
            self.status, self.status_at = None, None
            self._bad_state_logged = False
            if had and self.log:
                self.log.info("Network state file missing; hiding Wi-Fi controls", cat="Network")
            return
        except OSError as e:
            self._state_bad(f"Cannot read {self.state_file}: {type(e).__name__}: {e}")
            return
        try:
            data = json.loads(raw)
        except ValueError as e:
            self._state_bad(f"Invalid JSON: {e}")
            return
        if not isinstance(data, dict) or data.get("mode") not in ("wifi", "hotspot", "none"):
            self._state_bad("Not a JSON object or mode is not wifi / hotspot / none")
            return
        was_bad = self._bad_state_logged
        self.info, self.info_at = data, time.monotonic()
        self._bad_state_logged = False
        if was_bad and self.log:
            self.log.info("Network state file readable again", cat="Network")

    def _state_bad(self, why):
        self.info, self.info_at = None, None
        self.status, self.status_at = None, None
        if self._bad_state_logged or self.log is None:
            return
        self._bad_state_logged = True
        self.log.warn("Cannot parse network state file; hiding Wi-Fi controls", cat="Network", step=str(self.state_file),
                      raw=why, hint="Written by netcheck; it will rewrite on next cycle. Other features are unaffected")

    async def _status_loop(self):
        while True:
            if self.cfg is not None and self.info is not None:
                try:
                    r = await self.cfg.call("net.status", {}, timeout=STATUS_TIMEOUT_S)
                    if not isinstance(r, dict):
                        raise RpcError("net.status", None, f"Non-object response ({type(r).__name__})")
                    self.status, self.status_at = r, time.monotonic()
                    if self._status_error_logged and self.log:
                        self.log.info("Wi-Fi status recovered", cat="Network")
                    self._status_error_logged = False
                except (Unreachable, Timeout, RpcError) as e:
                    self.status, self.status_at = None, None
                    if not self._status_error_logged and self.log:
                        self._status_error_logged = True
                        self.log.warn("Cannot read Wi-Fi status", cat="Network", step="configd net.status", raw=str(e),
                                      hint="configd may not be running; standing/walking is unaffected, only Wi-Fi status is unknown")
            else:
                self.status, self.status_at = None, None
                self._status_error_logged = False
            await asyncio.sleep(STATUS_EVERY_S)

    # ---- UI Views ----

    def mode(self):
        return self.info.get("mode") if isinstance(self.info, dict) else None

    def for_scheme(self, scheme):
        """Per-connection view matching the browser's http/https scheme to avoid mixed-content blocks."""
        return _SchemeView(self, scheme)

    def view(self, now=None, scheme=None):
        """Network payload pushed to the UI. Missing/corrupt file -> {"known": false}."""
        scheme = scheme if scheme in ("http", "https") else self.scheme
        if not isinstance(self.info, dict):
            return {"known": False}
        now = time.monotonic() if now is None else now
        st = self.status if (isinstance(self.status, dict) and self.status_at is not None
                             and now - self.status_at <= STATUS_MAX_AGE_S) else None
        connected = bool(st and st.get("state") == "connected")
        host = self.info.get("hostname") or self.hostname or socket.gethostname()
        last = None
        if isinstance(self.last_try, dict):
            last = {k: self.last_try.get(k) for k in ("ssid", "at", "ok", "reason", "detail", "ip4")}
            last["reason_text"] = explain(last.get("reason"), last.get("detail"))
        mode = self.info.get("mode")
        ip4 = st.get("ip4") if connected else None
        # Active IP: hotspot IP in hotspot mode, otherwise configd net.status ip4
        ip_now = (self.info.get("hotspot_ip") or HOTSPOT_IP) if mode == "hotspot" else ip4
        hotspot_ip = self.info.get("hotspot_ip") or HOTSPOT_IP
        return {
            "known": True,
            "mode": mode,
            "ssid": st.get("ssid") if connected else None,
            "signal": st.get("signal") if connected else None,
            "ip4": ip4,
            "ip_now": ip_now,
            "hostname": host,
            "url_local": f"{scheme}://{host}.local:{self.port}/",
            "url_host": f"{scheme}://{host}:{self.port}/",
            "url_hotspot": f"{scheme}://{hotspot_ip}:{self.port}/m",
            "urls": addresses(host, ip_now, self.port, scheme),
            "hotspot": {"ssid": self.info.get("hotspot_ssid"), "ip": hotspot_ip,
                        "psk": self.info.get("hotspot_psk")},
            "last_try": last,
            "busy": self.busy,
        }

    def begin_connect(self):
        """Claim 'connecting' slot; returns False if busy. Raises Refused if network state is unknown."""
        if not isinstance(self.info, dict):
            raise Refused("Duck network state unknown; Wi-Fi management unavailable")
        if self.busy is not None:
            return False
        self.busy = "connecting"
        return True

    def begin_hotspot(self):
        """Claim 'hotspot' slot; returns False if busy. Separate from begin_connect()."""
        if not isinstance(self.info, dict):
            raise Refused("Duck network state unknown; Wi-Fi management unavailable")
        if self.busy is not None:
            return False
        self.busy = "hotspot"
        return True

    # ---- Scan ----

    async def scan(self):
        """Call configd net.scan; in hotspot mode fall back to pre-hotspot cached scan if empty/failed, filtering own_hotspot."""
        if not isinstance(self.info, dict):
            raise Refused("Duck network state unknown; Wi-Fi management unavailable")
        own = self.info.get("hotspot_ssid")
        result, error = None, None
        with log_cmd(self.log, "Scan Wi-Fi", "configd net.scan") as t:
            try:
                r = await call_once(self.configd_addr, "net.scan", {}, timeout=SCAN_TIMEOUT_S)
                nets = r.get("networks") if isinstance(r, dict) else None
                if not isinstance(nets, list):
                    raise Refused("Unexpected Wi-Fi list format from configd")
                result = [n for n in nets if isinstance(n, dict) and n.get("ssid") and not own_hotspot(n, own)]
                t.ok({"networks": len(result)})
            except (Unreachable, Timeout, RpcError, Refused) as e:
                error = _rpc_error(e, "Scan Wi-Fi") if not isinstance(e, Refused) else str(e)
                result = None
                t.fail(error, hint="Falling back to pre-hotspot scan results")
        if self.mode() == "hotspot" and not result:
            cached = self.info.get("scan")
            if isinstance(cached, list):
                if self.log:
                    self.log.info("Using pre-hotspot Wi-Fi scan in hotspot mode", cat="Network", raw=error or "configd returned empty list",
                                  dedup=True)
                return {"networks": [n for n in cached if isinstance(n, dict) and not own_hotspot(n, own)], "cached": True,
                        "scan_at": self.info.get("scan_at")}
        if result is None:
            raise Refused(error or "Cannot read Wi-Fi list")
        return {"networks": result, "cached": False, "scan_at": None}

    # ---- Connect ----

    async def connect(self, ssid, psk, by):
        """Stop hotspot -> configd net.connect -> write last-try. Call begin_connect() first."""
        ssid, psk = validate_connect(ssid, psk)
        if self.busy is None:
            self.busy = "connecting"
        if psk and self.log:
            self.log.hide(psk)                    # Fallback: redact password if any other log call captures it
        outcome = {"ok": False, "reason": "other", "detail": None, "ip4": None}
        hotspot_stopped = False
        hotspot_down_tried = False
        try:
            await asyncio.sleep(CONNECT_DELAY_S)
            if self.mode() == "hotspot":
                if self.dev_mode:
                    if self.log:
                        self.log.info("Desktop dev (--dev-autologin): skipping hotspot teardown, connecting Wi-Fi directly", cat="Network",
                                      raw=f"by={by}", dedup=False)
                else:
                    if not helper.available(refresh=True):
                        if self.log:
                            self.log.warn("Helper not installed before Wi-Fi connect", cat="Network", step="wifi-hotspot-down",
                                          hint="Run deploy/install.sh on the duck first")
                        raise Refused("Helper not installed: run deploy/install.sh on the duck before Studio can stop the hotspot")
                    hotspot_down_tried = True
                    with log_cmd(self.log, "Stop hotspot", "helper wifi-hotspot-down", who=by) as th:
                        try:
                            ok_h, result_h, detail_h = await asyncio.to_thread(helper.wifi_hotspot_down, by, ssid)
                        except Exception as e:  # noqa: BLE001
                            th.fail(f"{type(e).__name__}: {e}", hint="Check whether helper is installed and up to date")
                            raise Refused(f"Helper failed: {type(e).__name__}: {e}") from None
                        if result_h == "bad_verb":
                            th.fail("Helper is too old and lacks wifi-hotspot-down", hint="Re-run deploy/install.sh")
                            raise Refused("Helper is too old; re-run deploy/install.sh")
                        if not ok_h:
                            why = helper.friendly(result_h, detail_h, "wifi-hotspot-down")
                            th.fail(why, raw=str(detail_h), hint="Check if nmcli shows Microduck-Hotspot")
                            raise Refused(f"Failed to stop hotspot: {why}")
                        th.ok({"result": result_h})
                    hotspot_stopped = True
            params = {"ssid": ssid}
            if psk:
                params["psk"] = psk
            with log_cmd(self.log, f"Connect Wi-Fi '{ssid}'", "configd net.connect", who=by,
                         args={"ssid": ssid, "auth": "password" if psk else "open"}) as t:
                try:
                    r = await call_once(self.configd_addr, "net.connect", params, timeout=CONNECT_TIMEOUT_S)
                except (Unreachable, Timeout, RpcError) as e:
                    if isinstance(e, Timeout):
                        outcome = {"ok": False, "reason": "studio_timeout",
                                   "detail": f"Duck network daemon did not respond within {CONNECT_TIMEOUT_S:.0f}s", "ip4": None}
                    else:
                        outcome = {"ok": False, "reason": "other", "detail": _scrub(_rpc_error(e, "Connect Wi-Fi"), psk),
                                   "ip4": None}
                    t.fail(explain(outcome["reason"], outcome["detail"]), raw=_scrub(str(e), psk))
                else:
                    outcome = self._read_connect(r, psk)
                    if outcome["ok"]:
                        note = f"IP {outcome['ip4']}" if outcome.get("ip4") else ""
                        t.ok({"outcome": "connected", "ssid": r.get("ssid"), "ip4": r.get("ip4")}, note=note)
                    else:
                        t.fail(explain(outcome["reason"], outcome["detail"]),
                               raw={"outcome": r.get("outcome"), "reason": r.get("reason")} if isinstance(r, dict) else None,
                               hint="Hotspot will return shortly; reconnect to the duck hotspot to see the error")
        except Refused as e:
            outcome = {"ok": False, "reason": "other", "detail": _scrub(str(e), psk), "ip4": None}
            if self.log:
                self.log.warn("Wi-Fi connect aborted", cat="Network", step="wifi-hotspot-down", raw=_scrub(str(e), psk),
                              hint="Check whether helper is installed and up to date")
        except Exception as e:  # noqa: BLE001 — write last-try even on Studio internal errors
            outcome = {"ok": False, "reason": "other", "detail": _scrub(f"{type(e).__name__}: {e}", psk), "ip4": None}
            if self.log:
                self.log.error("Studio error while connecting Wi-Fi", cat="Network", raw=_scrub(f"{type(e).__name__}: {e}", psk),
                               exc=True, hint="This is a Studio bug; export a diagnostic bundle")
        finally:
            rec = {"ssid": ssid, "at": time.time(), "ok": bool(outcome["ok"]),
                   "reason": outcome["reason"], "detail": _scrub(outcome["detail"], psk),
                   "ip4": outcome.get("ip4")}
            self.last_try = rec
            self._save_last_try(rec)
            if outcome["ok"]:
                self._set_connected(ssid, outcome.get("ip4"))
            elif hotspot_down_tried and not outcome["ok"]:
                if hotspot_stopped and outcome["reason"] == "bad_key":
                    await self._forget_bad_key(ssid, by)
                await self._hotspot_up(by)
            self.busy = None
        return rec

    def _read_connect(self, r, psk):
        if not isinstance(r, dict):
            return {"ok": False, "reason": "other", "detail": "configd returned a non-object response", "ip4": None}
        if r.get("outcome") == "connected":
            return {"ok": True, "reason": None, "detail": None, "ip4": r.get("ip4")}
        if r.get("outcome") == "failed":
            reason = r.get("reason") if r.get("reason") in REASON_TEXT else "other"
            detail = _scrub(r.get("detail"), psk) if r.get("detail") else None
            return {"ok": False, "reason": reason, "detail": detail, "ip4": None}
        return {"ok": False, "reason": "other", "detail": _scrub(f"Unexpected reply: {r}", psk), "ip4": None}

    async def _hotspot_up(self, by):
        """Start hotspot; returns (ok, user-facing reason). Shared by fallback recovery and Outdoor Mode."""
        with log_cmd(self.log, "Start hotspot", "helper wifi-hotspot-up", who=by) as t:
            try:
                ok, result, detail = await asyncio.to_thread(helper.wifi_hotspot_up, by)
            except Exception as e:  # noqa: BLE001
                why = f"Helper failed: {type(e).__name__}: {e}"
                t.fail(why, hint="netcheck will also start the hotspot after the connection window")
                return False, why
            if result == "bad_verb":
                why = "Helper is too old and lacks wifi-hotspot-up; re-run deploy/install.sh"
                t.fail(why, hint="netcheck will also start the hotspot after the connection window")
                return False, why
            if ok:
                t.ok({"result": result})
                return True, None
            why = helper.friendly(result, detail, "wifi-hotspot-up")
            t.fail(why, raw=str(detail), hint="netcheck will also start the hotspot after the connection window")
            return False, why

    async def hotspot_up(self, by):
        """Outdoor mode: start hotspot immediately without waiting for netcheck. Call begin_hotspot() first."""
        try:
            if not helper.available(refresh=True):
                raise Refused("Helper not installed: run deploy/install.sh on the duck before Studio can start the hotspot")
            return await self._hotspot_up(by)
        finally:
            self.busy = None

    async def hotspot_down(self, by, ssid=None):
        """Outdoor mode: stop hotspot so NetworkManager reconnects to saved home Wi-Fi. Call begin_hotspot() first."""
        try:
            if not helper.available(refresh=True):
                raise Refused("Helper not installed: run deploy/install.sh on the duck before Studio can stop the hotspot")
            with log_cmd(self.log, "Stop hotspot", "helper wifi-hotspot-down", who=by) as t:
                try:
                    ok, result, detail = await asyncio.to_thread(helper.wifi_hotspot_down, by, ssid)
                except Exception as e:  # noqa: BLE001
                    why = f"Helper failed: {type(e).__name__}: {e}"
                    t.fail(why, hint="Check whether helper is installed and up to date")
                    return False, why
                if result == "bad_verb":
                    why = "Helper is too old and lacks wifi-hotspot-down; re-run deploy/install.sh"
                    t.fail(why, hint="Re-run deploy/install.sh")
                    return False, why
                if ok:
                    t.ok({"result": result})
                    return True, None
                why = helper.friendly(result, detail, "wifi-hotspot-down")
                t.fail(why, raw=str(detail), hint="Check if nmcli shows Microduck-Hotspot")
                return False, why
        finally:
            self.busy = None

    async def _forget_bad_key(self, ssid, by):
        """Wrong password: net.connect already overwrote the saved profile with the bad key, so forget it before netcheck retries."""
        with log_cmd(self.log, f"Forget Wi-Fi with bad password '{ssid}'", "configd net.forget", who=by) as t:
            try:
                r = await call_once(self.configd_addr, "net.forget", {"ssid": ssid}, timeout=FORGET_TIMEOUT_S)
            except (Unreachable, Timeout, RpcError) as e:
                t.fail(_rpc_error(e, "Forget Wi-Fi"), raw=str(e), hint="Hotspot will still restart; profile may remain saved")
                return
            if not isinstance(r, dict):
                t.fail("configd returned a non-object response", raw=repr(r)[:200])
                return
            t.ok({"removed": bool(r.get("removed"))})

    def _set_connected(self, ssid, ip4):
        self.status = {"state": "connected", "ssid": ssid, "ip4": ip4}
        self.status_at = time.monotonic()

    # ---- Forget ----

    async def forget(self, ssid):
        ssid = validate_ssid(ssid)
        if not isinstance(self.info, dict):
            raise Refused("Duck network state unknown; Wi-Fi management unavailable")
        with log_cmd(self.log, f"Forget Wi-Fi '{ssid}'", "configd net.forget") as t:
            try:
                r = await call_once(self.configd_addr, "net.forget", {"ssid": ssid}, timeout=FORGET_TIMEOUT_S)
            except (Unreachable, Timeout, RpcError) as e:
                text = _rpc_error(e, "Forget Wi-Fi")
                t.fail(text, raw=str(e))
                raise Refused(text) from None
            if not isinstance(r, dict):
                t.fail("configd returned a non-object response", raw=repr(r)[:200])
                raise Refused("configd returned a non-object response")
            removed = bool(r.get("removed"))
            t.ok({"removed": removed})
        if isinstance(self.status, dict) and self.status.get("ssid") == ssid:
            self.status, self.status_at = None, None
        return {"removed": removed}

    # ---- last-try file ----

    def _load_last_try(self):
        if self.last_try_path is None:
            return None
        try:
            data = json.loads(self.last_try_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return {k: data.get(k) for k in ("ssid", "at", "ok", "reason", "detail", "ip4")}

    def _save_last_try(self, rec):
        if self.last_try_path is None:
            return
        try:
            self.last_try_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.last_try_path.with_name(self.last_try_path.name + ".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp, 0o644)
            except OSError:
                pass
            os.replace(tmp, self.last_try_path)
        except OSError as e:
            if self.log:
                self.log.warn("Failed to persist last Wi-Fi attempt", cat="Network", step=str(self.last_try_path),
                              raw=f"{type(e).__name__}: {e}",
                              hint="Last failure reason will not appear after reconnect; check state directory space and permissions")
