"""JSON-RPC 2.0 NDJSON client for robotd / configd."""
import asyncio
import itertools
import json
import os

LINE_LIMIT = 1 << 20      # 1 MiB ceiling (busStatus can exceed 64 KiB)


class RpcError(Exception):
    """Remote JSON-RPC error response."""

    def __init__(self, method, code, message, data=None):
        super().__init__(f"{method}: {message} (code {code})")
        self.method, self.code, self.message, self.data = method, code, message, data


class Unreachable(Exception):
    """Socket missing, connection refused, or remote closed."""


class Timeout(Exception):
    """Connected, but no reply within timeout."""


def parse_addr(addr):
    if addr.startswith("unix:"):
        return ("unix", addr[5:])
    if addr.startswith("tcp:"):
        host, _, port = addr[4:].rpartition(":")
        return ("tcp", host or "127.0.0.1", int(port))
    raise ValueError(f"Address must be unix:/path or tcp:host:port; got {addr!r}")


async def open_conn(addr):
    kind, *rest = parse_addr(addr)
    try:
        if kind == "unix":
            path = rest[0]
            if not hasattr(asyncio, "open_unix_connection"):
                raise Unreachable("Unix sockets are unsupported in this Python build; use tcp:")
            if not os.path.exists(path):
                raise Unreachable(f"{path} missing (controller daemon not running)")
            return await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
        host, port = rest
        return await asyncio.open_connection(host, port, limit=LINE_LIMIT)
    except Unreachable:
        raise
    except (ConnectionRefusedError, FileNotFoundError) as e:
        raise Unreachable(f"Cannot connect to {addr}: connection refused") from e
    except OSError as e:
        raise Unreachable(f"Cannot connect to {addr}: {type(e).__name__}: {e}") from e


def encode(rid, method, params):
    return (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                       ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def read_message(reader, timeout):
    """Read and parse one NDJSON line. Returns None on malformed JSON."""
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout)
    except asyncio.TimeoutError:
        raise
    except (ValueError, asyncio.LimitOverrunError) as e:
        raise Unreachable(f"Incoming RPC line exceeded {LINE_LIMIT} bytes") from e
    except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
        raise Unreachable(f"Connection closed: {type(e).__name__}: {e}") from e
    if not raw:
        raise Unreachable("Remote closed connection")
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class Conn:
    """Serialized JSON-RPC connection; closes on timeout/error and reconnects on next call."""

    def __init__(self, addr, name="rpc", log=None):
        self.addr, self.name, self.log = addr, name, log
        self._r = self._w = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self.connected = False
        self.reconnects = 0
        self.bad_lines = 0

    @property
    def busy(self):
        return self._lock.locked()

    async def call(self, method, params=None, timeout=2.0):
        """Send one JSON-RPC request and await matching response within timeout."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            if self._w is None:
                try:
                    await asyncio.wait_for(self._connect(), timeout)
                except asyncio.TimeoutError:
                    self._close()
                    raise Timeout(f"{method}: connect timed out after {timeout:g}s") from None
            rid = next(self._ids)
            try:
                self._w.write(encode(rid, method, params))
                await self._w.drain()
                while True:
                    left = deadline - loop.time()
                    if left <= 0:
                        raise asyncio.TimeoutError
                    msg = await read_message(self._r, left)
                    if msg is None:
                        self.bad_lines += 1
                        continue
                    if msg.get("id") == rid:
                        break
            except asyncio.TimeoutError:
                self._close()
                raise Timeout(f"{method} timed out after {timeout:g}s") from None
            except Unreachable:
                self._close()
                raise
            except (ConnectionError, OSError) as e:
                self._close()
                raise Unreachable(f"Connection closed: {type(e).__name__}: {e}") from e
            except asyncio.CancelledError:
                self._close()
                raise
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            raise RpcError(method, err.get("code"), err.get("message", ""), err.get("data"))
        return msg.get("result")

    async def _connect(self):
        self._r, self._w = await open_conn(self.addr)
        if self.connected is False and self.reconnects:
            self._note("info", f"Restored {self.name} connection to controller", raw=f"reconnect #{self.reconnects}")
        self.connected = True

    def _close(self):
        if self._w is not None:
            try:
                self._w.close()
            except Exception:  # noqa: BLE001
                pass
        self._r = self._w = None
        if self.connected:
            self.reconnects += 1
        self.connected = False

    def close(self):
        self._close()

    def _note(self, level, msg, raw=None):
        if self.log:
            self.log.write(msg, cat="robotd", level=level, raw=raw)


async def call_once(addr, method, params=None, timeout=2.0, name="once", log=None):
    """Open a fresh short-lived connection, execute one call, and close it."""
    c = Conn(addr, name, log)
    try:
        return await c.call(method, params, timeout)
    finally:
        c.close()
