"""Manage authorized SSH public keys for the radxa user."""
import base64
import binascii
import hashlib
import os
import struct

KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
MAX_LINE = 16384
MAX_COMMENT = 200


class BadKey(ValueError):
    """Invalid SSH public key input; message is shown directly in the UI."""


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def parse(line):
    """Parse one authorized_keys line into {"type", "b64", "comment", "fp"}."""
    if not isinstance(line, str):
        raise BadKey("Paste a single public key line")
    text = line.strip()
    if not text:
        raise BadKey("Public key is empty")
    if "\n" in text or "\r" in text:
        raise BadKey("Paste a single line only (e.g. ssh-ed25519 AAAA... comment)")
    if len(text) > MAX_LINE:
        raise BadKey("Key line is too long")
    parts = text.split(None, 2)
    kind = parts[0]
    if kind not in KEY_TYPES:
        if "PRIVATE KEY" in text:
            raise BadKey("That is a private key! Never share private keys; paste your .pub file contents")
        raise BadKey("Key must start with ssh-ed25519, ssh-rsa, or ecdsa-sha2-... (from your .pub file, no options prefix)")
    if len(parts) < 2:
        raise BadKey("Missing key payload; paste the complete .pub line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise BadKey("Key payload is not valid base64; ensure the entire line was copied") from None
    if len(blob) < 4:
        raise BadKey("Key payload is truncated; ensure the entire line was copied")
    (n,) = struct.unpack(">I", blob[:4])
    if blob[4:4 + n] != kind.encode("ascii"):
        raise BadKey("Key payload does not match declared key type")
    comment = parts[2].strip() if len(parts) > 2 else ""
    comment = "".join(c for c in comment if ord(c) >= 32 and ord(c) != 127)[:MAX_COMMENT]
    return {"type": kind, "b64": parts[1], "comment": comment, "fp": fingerprint(blob)}


def _key_in_line(line):
    """Parse an authorized_keys line (with optional key options) or return None."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    tokens = text.split()
    for i, t in enumerate(tokens):
        if t in KEY_TYPES and i + 1 < len(tokens):
            try:
                k = parse(" ".join(tokens[i:]))
            except BadKey:
                return None
            k["options"] = i > 0
            return k
    return None


class SshKeys:
    def __init__(self, home=None):
        self.home = home or os.path.expanduser("~")

    @property
    def dir(self):
        return os.path.join(self.home, ".ssh")

    @property
    def path(self):
        return os.path.join(self.dir, "authorized_keys")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def list(self):
        out = []
        for line in self._read().splitlines():
            k = _key_in_line(line)
            if k is not None:
                out.append({"type": k["type"], "comment": k["comment"], "fp": k["fp"], "options": k["options"]})
        return out

    def _write(self, text):
        if os.path.islink(self.dir):
            raise BadKey("Refusing to write through ~/.ssh symlink")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        if os.path.islink(self.path):
            raise BadKey("Refusing to modify authorized_keys symlink")
        tmp = os.path.join(self.dir, f".authorized_keys.studio.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, line):
        """Add key if absent. Returns (added, key_dict)."""
        k = parse(line)
        if any(x["fp"] == k["fp"] for x in self.list()):
            return False, k
        text = self._read()
        if text and not text.endswith("\n"):
            text += "\n"
        text += " ".join(p for p in (k["type"], k["b64"], k["comment"]) if p) + "\n"
        self._write(text)
        return True, k

    def remove(self, fp):
        """Remove all entries matching fingerprint `fp`. Returns count removed."""
        if not isinstance(fp, str) or not fp.startswith("SHA256:"):
            raise BadKey("Invalid key fingerprint")
        kept, gone = [], 0
        for line in self._read().splitlines(keepends=True):
            k = _key_in_line(line)
            if k is not None and k["fp"] == fp:
                gone += 1
                continue
            kept.append(line)
        if gone:
            self._write("".join(kept))
        return gone
