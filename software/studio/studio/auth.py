"""Studio authentication, sessions, and Host/Origin guard."""
import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

COOKIE = "studio_session"
REMEMBER_S = 30 * 24 * 3600
IDLE_S = 12 * 3600
RECENT_AUTH_S = 600
FAIL_FREE = 5
FAIL_BASE_S = 30
FAIL_MAX_S = 300
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
FLUSH_EVERY_S = 300


def _hash(password, salt, n, r, p, dklen):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=64 * 1024 * 1024)


def _write_private(path, data):
    """Atomic write with 0600 permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Auth:
    def __init__(self, state_dir, log=None):
        self.dir = Path(state_dir)
        self.pw_path = self.dir / "password.json"
        self.sess_path = self.dir / "sessions.json"
        self.log = log
        self._lock = threading.Lock()
        self._sessions = {}
        self._fails = {}
        self._dirty_at = None
        self._load_sessions()

    # ---- Password ----

    def has_password(self):
        return self.pw_path.is_file()

    def set_password(self, password, keep_token=None):
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters")
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt, **SCRYPT)
        _write_private(self.pw_path, {"scrypt": {**SCRYPT, "salt": salt.hex(), "hash": digest.hex()},
                                      "set_at": int(time.time())})
        kept = False
        with self._lock:
            if keep_token and keep_token in self._sessions:
                self._sessions = {keep_token: self._sessions[keep_token]}
                kept = True
                self._flush_locked()
            else:
                self._sessions.clear()
                self._flush_locked()
        self._log("info", "Admin password updated; other sessions revoked" if kept else "Admin password updated; all old sessions revoked")

    def clear_password(self):
        """Remove admin password and return Studio to open LAN mode."""
        try:
            self.pw_path.unlink()
        except FileNotFoundError:
            pass
        self.revoke_all()
        self._log("info", "Admin password cleared; Studio returned to open access mode")

    def check_password(self, password):
        try:
            rec = json.loads(self.pw_path.read_text(encoding="utf-8"))["scrypt"]
            want = bytes.fromhex(rec["hash"])
            got = _hash(password, bytes.fromhex(rec["salt"]), rec["n"], rec["r"], rec["p"], rec["dklen"])
        except (OSError, ValueError, KeyError):
            return False
        return hmac.compare_digest(want, got)

    # ---- Login ----

    def login(self, password, ip, remember=False):
        """Return (token, None, 0) on success, or (None, reason, retry_wait_s) on failure."""
        now = time.time()
        with self._lock:
            f = self._fails.get(ip)
            if f and f["until"] > now:
                wait = int(f["until"] - now) + 1
                return None, f"Too many failed attempts; retry in {wait}s", wait
        if not self.has_password():
            return None, "No admin password is set; run set-password on the board or set it in Settings", 0
        with self._lock:
            if len(self._fails) > 1000:
                self._fails = {k: v for k, v in self._fails.items() if v["until"] > now or now - v["at"] < 3600}
            f = self._fails.setdefault(ip, {"n": 0, "until": 0, "at": now})
            f["n"] += 1
            f["at"] = now
            wait = 0
            if f["n"] >= FAIL_FREE:
                wait = min(FAIL_BASE_S * 2 ** (f["n"] - FAIL_FREE), FAIL_MAX_S)
                f["until"] = now + wait
            n = f["n"]
        if not self.check_password(password):
            self._log("warn", "Login failed", cat="Connection", raw=f"from {ip}, attempt #{n}")
            return None, "Incorrect password" + (f"; retry in {wait}s" if wait else ""), wait
        token = secrets.token_urlsafe(32)
        sess = {"created": now, "last": now, "remember": bool(remember), "ip": ip,
                "csrf": secrets.token_urlsafe(24), "auth_at": now}
        with self._lock:
            self._fails.pop(ip, None)
            self._sessions = {t: v for t, v in self._sessions.items() if not self._expired(v, now)}
            self._sessions[token] = sess
            self._flush_locked()
        self._log("info", f"Signed in (from {ip}{', remembered 30d' if remember else ''})", cat="Connection")
        return token, None, 0

    def session(self, token, touch=True):
        """Return session dict if valid, else None."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if self._expired(s, now):
                self._sessions.pop(token, None)
                self._flush_locked()
                return None
            if not touch:
                return dict(s)
            s["last"] = now
            if self._dirty_at is None:
                self._dirty_at = now
            elif now - self._dirty_at > FLUSH_EVERY_S:
                self._flush_locked()
            return dict(s)

    def reauth(self, token, password):
        """Verify password again before sensitive operations."""
        if not self.check_password(password):
            return False
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return False
            s["auth_at"] = time.time()
            self._flush_locked()
        return True

    @staticmethod
    def recent_auth(sess):
        return bool(sess) and time.time() - sess.get("auth_at", 0) < RECENT_AUTH_S

    def logout(self, token):
        with self._lock:
            if self._sessions.pop(token, None) is not None:
                self._flush_locked()

    def revoke_all(self):
        with self._lock:
            self._sessions.clear()
            self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    # ---- Internal ----

    @staticmethod
    def _expired(s, now):
        created = s.get("created") or 0
        if s.get("remember"):
            return now - created > REMEMBER_S
        return now - (s.get("last") or created) > IDLE_S

    def _load_sessions(self):
        try:
            data = json.loads(self.sess_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        if not isinstance(data, dict):
            return
        self._sessions = {t: s for t, s in data.items()
                          if isinstance(s, dict) and isinstance(s.get("created"), (int, float))
                          and isinstance(s.get("csrf"), str) and not self._expired(s, now)}

    def _flush_locked(self):
        try:
            _write_private(self.sess_path, self._sessions)
        except OSError as e:
            self._log("warn", "Failed to persist sessions; login required after Studio restart", cat="System", raw=str(e))
        self._dirty_at = None

    def _log(self, level, msg, cat="Connection", **kw):
        if self.log:
            self.log.write(msg, cat=cat, level=level, **kw)


# ---- Origin Guard ----

def _split_host(host):
    """'microduck-2.local:80' -> ('microduck-2.local', '80'); '[fe80::1]:80' -> ('fe80::1', '80')."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end], host[end + 2:] if end != -1 and host[end + 1:end + 2] == ":" else ""
    if host.count(":") == 1:
        name, port = host.split(":")
        return name, port
    return host, ""


class OriginGuard:
    def __init__(self, names=None):
        hn = socket.gethostname().lower()
        self.names = {"localhost", hn, f"{hn}.local"} | {n.lower() for n in (names or [])}

    def host_ok(self, host_header, local_ip=None):
        name, _ = _split_host(host_header)
        if not name:
            return False
        if name in self.names:
            return True
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            return False
        if ip.is_loopback:
            return True
        return local_ip is not None and name == str(local_ip).lower()

    def origin_ok(self, origin_header, host_header):
        """Require same-origin Host match for mutating HTTP requests and WebSocket handshakes."""
        if not origin_header:
            return False
        try:
            u = urlsplit(origin_header)
        except ValueError:
            return False
        if u.scheme not in ("http", "https"):
            return False
        o_name, o_port = _split_host(u.netloc)
        h_name, h_port = _split_host(host_header)
        default = "443" if u.scheme == "https" else "80"
        return o_name == h_name and (o_port or default) == (h_port or default)


def main():
    ap = argparse.ArgumentParser(description="Studio admin password")
    ap.add_argument("cmd", choices=["set-password"])
    ap.add_argument("--state-dir", required=True)
    a = ap.parse_args()
    pw = getpass.getpass("New password (at least 6 characters): ")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match; unchanged")
    Auth(a.state_dir).set_password(pw)
    print("Password updated; previous sessions revoked.")


if __name__ == "__main__":
    main()
