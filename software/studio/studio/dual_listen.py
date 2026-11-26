"""Serve HTTP and HTTPS simultaneously on the same TCP port using MSG_PEEK."""
import asyncio
import socket
import ssl
import sys

import uvicorn
from uvicorn.config import SSL_PROTOCOL_VERSION, create_ssl_context

TLS_HANDSHAKE = 0x16      # TLS Handshake record type: ClientHello first byte
PEEK_TIMEOUT_S = 10.0     # Close silent pre-connections after timeout
PEEK_FIRST_S = 0.002
PEEK_MAX_S = 0.05


def load_tls(certfile, keyfile):
    """Create SSL context matching uvicorn defaults; raises OSError or ssl.SSLError on invalid certs."""
    return create_ssl_context(certfile=certfile, keyfile=keyfile, password=None, ssl_version=SSL_PROTOCOL_VERSION,
                              cert_reqs=ssl.CERT_NONE, ca_certs=None, ciphers="TLSv1")


class DualServer(uvicorn.Server):
    def __init__(self, config, tls, peek_timeout=PEEK_TIMEOUT_S):
        super().__init__(config)
        self.tls = tls
        self.peek_timeout = peek_timeout
        self.port = None            # Bound port (useful when --listen uses port 0)
        self._listener = None
        self._accepting = None
        self._waiting = set()

    async def startup(self, sockets=None):
        await self.lifespan.startup()
        if self.lifespan.should_exit:
            self.should_exit = True
            return
        config = self.config
        host = config.host or "0.0.0.0"
        try:
            self._listener = socket.create_server((host, config.port), backlog=config.backlog,
                                                  family=socket.AF_INET6 if ":" in host else socket.AF_INET)
        except OSError as exc:
            uvicorn.server.logger.error(exc)
            await self.lifespan.shutdown()
            sys.exit(1)
        self._listener.setblocking(False)
        self.port = self._listener.getsockname()[1]
        self.servers = []           # Base class iterates on shutdown; we close _listener ourselves
        self._accepting = asyncio.get_running_loop().create_task(self._accept())
        self.started = True

    def _protocol(self):
        return self.config.http_protocol_class(config=self.config, server_state=self.server_state,
                                               app_state=self.lifespan.state)

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _ = await loop.sock_accept(self._listener)
            except OSError:
                await asyncio.sleep(0.2)
                continue
            task = loop.create_task(self._hand_over(conn))
            self._waiting.add(task)
            task.add_done_callback(self._waiting.discard)

    async def _first_byte(self, conn):
        loop = asyncio.get_running_loop()
        end, delay = loop.time() + self.peek_timeout, PEEK_FIRST_S
        while True:
            try:
                data = conn.recv(1, socket.MSG_PEEK)
            except (BlockingIOError, InterruptedError):
                data = None
            if data is not None:
                return data[0] if data else None
            if loop.time() >= end:
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, PEEK_MAX_S)

    async def _hand_over(self, conn):
        try:
            conn.setblocking(False)
            first = await self._first_byte(conn)
            if first is None:
                conn.close()
                return
            await asyncio.get_running_loop().connect_accepted_socket(
                self._protocol, conn, ssl=self.tls if first == TLS_HANDSHAKE else None)
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception:
            conn.close()

    async def shutdown(self, sockets=None):
        if self._accepting is not None:
            self._accepting.cancel()
        for task in list(self._waiting):
            task.cancel()
        await asyncio.gather(*filter(None, [self._accepting, *self._waiting]), return_exceptions=True)
        if self._listener is not None:
            self._listener.close()
        await super().shutdown(sockets=sockets)
