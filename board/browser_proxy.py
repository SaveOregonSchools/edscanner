from __future__ import annotations

import ipaddress
import logging
import select
import socket
import socketserver
import threading
from contextlib import contextmanager
from typing import Iterator

from .http import BoardHTTPClient, BoardHTTPError


LOGGER = logging.getLogger(__name__)


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError("SOCKS client closed the connection early")
        data.extend(chunk)
    return bytes(data)


def _connect_address(address: str, port: int, timeout: float) -> socket.socket:
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    upstream = socket.socket(family, socket.SOCK_STREAM)
    upstream.settimeout(timeout)
    try:
        target = (str(parsed), port, 0, 0) if parsed.version == 6 else (str(parsed), port)
        upstream.connect(target)
        upstream.settimeout(None)
        return upstream
    except Exception:
        upstream.close()
        raise


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = (left, right)
    while True:
        readable, _writable, exceptional = select.select(sockets, (), sockets, 1.0)
        if exceptional:
            return
        if not readable:
            continue
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class _PinnedSOCKSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], client: BoardHTTPClient) -> None:
        self.board_client = client
        super().__init__(address, _PinnedSOCKSHandler)


class _PinnedSOCKSHandler(socketserver.BaseRequestHandler):
    server: _PinnedSOCKSServer

    def _reply(self, status: int) -> None:
        # A zero IPv4 bind address is valid for both IPv4 and IPv6 upstreams.
        self.request.sendall(bytes((5, status, 0, 1, 0, 0, 0, 0, 0, 0)))

    def _read_target(self) -> tuple[str, int]:
        version, command, _reserved, address_type = _recv_exact(self.request, 4)
        if version != 5 or command != 1:
            raise BoardHTTPError("Only SOCKS5 CONNECT is supported")
        if address_type == 1:
            host = socket.inet_ntop(socket.AF_INET, _recv_exact(self.request, 4))
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(self.request, 16))
        elif address_type == 3:
            host_length = _recv_exact(self.request, 1)[0]
            host = _recv_exact(self.request, host_length).decode("ascii", errors="strict")
        else:
            raise BoardHTTPError("Unsupported SOCKS5 address type")
        port = int.from_bytes(_recv_exact(self.request, 2), "big")
        if port not in {80, 443} and not self.server.board_client.settings.allow_private_networks:
            raise BoardHTTPError(f"Browser proxy blocked non-web destination port {port}")
        return host, port

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            version, method_count = _recv_exact(self.request, 2)
            methods = _recv_exact(self.request, method_count)
            if version != 5 or 0 not in methods:
                self.request.sendall(bytes((5, 255)))
                return
            self.request.sendall(bytes((5, 0)))
            host, port = self._read_target()
            host_for_url = f"[{host}]" if ":" in host else host
            scheme = "https" if port == 443 else "http"
            target_url = f"{scheme}://{host_for_url}:{port}/"
            # Hold the shared host gate through connection establishment, then
            # relay the browser's end-to-end TLS/HTTP stream untouched.
            with self.server.board_client.host_slot(target_url):
                _canonical, addresses = self.server.board_client.validated_connection_target(
                    target_url
                )
                last_error: OSError | None = None
                for address in addresses:
                    try:
                        upstream = _connect_address(
                            address,
                            port,
                            self.server.board_client.settings.timeout_seconds,
                        )
                        break
                    except OSError as exc:
                        last_error = exc
                if upstream is None:
                    raise last_error or ConnectionError("No validated address was reachable")
            self._reply(0)
            _relay(self.request, upstream)
        except (BoardHTTPError, ConnectionError, OSError, UnicodeError, ValueError) as exc:
            LOGGER.info("Pinned browser proxy rejected a connection: %s", exc)
            try:
                self._reply(2)
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()


@contextmanager
def pinned_browser_proxy(client: BoardHTTPClient) -> Iterator[str]:
    """Run a loopback SOCKS5 proxy that pins every browser connection.

    Chromium sends the original hostname to this proxy. Each tunnel is opened
    to an address returned by the same public-network validator used by the
    ordinary HTTP client, eliminating browser-side DNS validation/use races.
    TLS stays end-to-end between Chromium and the origin, so normal SNI and
    certificate verification remain intact.
    """

    server = _PinnedSOCKSServer(("127.0.0.1", 0), client)
    thread = threading.Thread(
        target=server.serve_forever,
        name="board-browser-proxy",
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"socks5://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = ["pinned_browser_proxy"]
