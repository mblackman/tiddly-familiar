"""
Tiny in-process TCP proxy.

TiddlyPWA's sync uses WebCrypto + service workers, which browsers only enable in
a **secure context**. A wiki served over plain HTTP on a LAN IP is not secure, so
sync silently fails. But http://127.0.0.1 and http://localhost are *always* treated
as secure contexts — so we forward a local port to the real wiki host and point the
headless browser at the localhost URL.
"""

import asyncio
from urllib.parse import urlparse


class TCPProxy:
    def __init__(self, dest_host: str, dest_port: int, listen_host: str = "127.0.0.1",
                 listen_port: int = 0):
        self.dest_host = dest_host
        self.dest_port = dest_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        # Actual bound port (useful when listen_port=0 picks a free one)
        if self._server is None:
            return self.listen_port
        return self._server.sockets[0].getsockname()[1]

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )
        await self._server.start_serving()
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle(self, client_reader, client_writer):
        try:
            server_reader, server_writer = await asyncio.open_connection(
                self.dest_host, self.dest_port
            )
        except Exception:
            client_writer.close()
            return
        await asyncio.gather(
            self._pipe(client_reader, server_writer),
            self._pipe(server_reader, client_writer),
        )

    @staticmethod
    async def _pipe(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


def split_app_url(app_url: str) -> tuple[str, int, str]:
    """Return (host, port, path) for a wiki app_url."""
    u = urlparse(app_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return u.hostname, port, path


def local_url(local_port: int, path: str) -> str:
    """Build the secure-context localhost URL the browser should load."""
    return f"http://127.0.0.1:{local_port}{path}"
