from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError

from board.browser_proxy import pinned_browser_proxy
from board.http import (
    BoardHTTPClient,
    BoardHTTPSettings,
    InvalidPublicURL,
    _PinnedHTTPAdapter,
    validate_public_url,
)


PUBLIC_DNS_ANSWER = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
]


def recv_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise AssertionError("SOCKS proxy closed before returning its response")
        data.extend(chunk)
    return bytes(data)


class LocalHandler(BaseHTTPRequestHandler):
    seen_hosts: list[str] = []

    def do_GET(self):
        self.seen_hosts.append(str(self.headers.get("Host", "")))
        body = b"pinned browser response"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        del args


class FakeResponse:
    def __init__(self, url: str, *, status: int = 200, headers=None, content: bytes = b"ok"):
        self.url = url
        self.status_code = status
        self.headers = dict(headers or {"Content-Type": "text/plain"})
        self._content = content
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        yield self._content

    def close(self) -> None:
        self.closed = True


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        return None


class BoardHTTPPolicyTests(unittest.TestCase):
    def settings(self, **overrides) -> BoardHTTPSettings:
        values = {
            "delay_seconds": 0,
            "max_retries": 0,
            "cache_max_entries": 8,
            "cache_max_bytes": 4096,
        }
        values.update(overrides)
        return BoardHTTPSettings(**values)

    def test_private_literal_and_private_dns_answer_are_rejected(self):
        for url in (
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
        ):
            with self.subTest(url=url), self.assertRaises(InvalidPublicURL):
                validate_public_url(url)

        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.1.2.3", 443))
        ]
        with patch("board.http.socket.getaddrinfo", return_value=private_answer):
            with self.assertRaises(InvalidPublicURL):
                validate_public_url("https://district.example/")

    def test_redirect_to_private_target_is_blocked_before_second_request(self):
        session = SequenceSession(
            [
                FakeResponse(
                    "https://district.example/start",
                    status=302,
                    headers={"Location": "http://127.0.0.1/private"},
                    content=b"",
                )
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            with self.assertRaises(InvalidPublicURL):
                client.get("https://district.example/start")
        self.assertEqual(len(session.calls), 1)

    def test_request_connects_to_the_validated_dns_answer(self):
        session = SequenceSession(
            [FakeResponse("https://district.example/board", content=b"board")]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with (
            patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER),
            patch.object(client, "_perform_get", wraps=client._perform_get) as perform_get,
        ):
            client.get("https://district.example/board")

        self.assertEqual(perform_get.call_args.args[2], "93.184.216.34")
        self.assertEqual(session.calls[0]["headers"]["Host"], "district.example")

        adapter = _PinnedHTTPAdapter()
        adapter.pin("https://district.example/board", "93.184.216.34")
        connection = object()
        with patch.object(
            adapter.poolmanager,
            "connection_from_host",
            return_value=connection,
        ) as connection_from_host:
            self.assertIs(
                adapter._pool_for_url("https://district.example/board", {}),
                connection,
            )
        pool_call = connection_from_host.call_args.kwargs
        self.assertEqual(pool_call["host"], "93.184.216.34")
        self.assertEqual(pool_call["pool_kwargs"]["server_hostname"], "district.example")
        self.assertEqual(pool_call["pool_kwargs"]["assert_hostname"], "district.example")

    def test_real_pinned_http_transport_preserves_the_logical_host(self):
        LocalHandler.seen_hosts.clear()
        origin = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        client = BoardHTTPClient(
            self.settings(allow_private_networks=True, respect_robots=False)
        )
        try:
            local_answer = [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", origin.server_port),
                )
            ]
            with patch("board.http.socket.getaddrinfo", return_value=local_answer):
                result = client.get(
                    f"http://fixture.example:{origin.server_port}/",
                    force=True,
                )
            self.assertEqual(result.content, b"pinned browser response")
            self.assertEqual(
                LocalHandler.seen_hosts,
                [f"fixture.example:{origin.server_port}"],
            )
        finally:
            client.close()
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)

    def test_each_validated_address_is_tried_even_when_retries_are_disabled(self):
        answers = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:4860::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ]
        client = BoardHTTPClient(
            self.settings(max_retries=0, backoff_base_seconds=0, backoff_max_seconds=0),
            session=SequenceSession([]),
        )
        response = FakeResponse("https://district.example/", content=b"fallback")
        with (
            patch("board.http.socket.getaddrinfo", return_value=answers),
            patch.object(
                client,
                "_perform_get",
                side_effect=[RequestsConnectionError("IPv6 unavailable"), (response, "verified")],
            ) as perform_get,
        ):
            result = client.get("https://district.example/", force=True)
        self.assertEqual(result.content, b"fallback")
        self.assertEqual(
            [call.args[2] for call in perform_get.call_args_list],
            ["2001:4860::1", "93.184.216.34"],
        )

    def test_browser_proxy_connects_to_the_validated_address(self):
        origin = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        client = BoardHTTPClient(
            self.settings(allow_private_networks=True, per_host_concurrency=1)
        )
        try:
            with (
                patch(
                    "board.http.socket.getaddrinfo",
                    return_value=[
                        (
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                            socket.IPPROTO_TCP,
                            "",
                            ("127.0.0.1", origin.server_port),
                        )
                    ],
                ),
                pinned_browser_proxy(client) as proxy_url,
            ):
                proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                proxy.settimeout(2)
                proxy.connect(("127.0.0.1", int(proxy_url.rsplit(":", 1)[1])))
                with proxy:
                    proxy.sendall(bytes((5, 1, 0)))
                    self.assertEqual(recv_exact(proxy, 2), bytes((5, 0)))
                    host = b"fixture.example"
                    proxy.sendall(
                        bytes((5, 1, 0, 3, len(host)))
                        + host
                        + int(origin.server_port).to_bytes(2, "big")
                    )
                    self.assertEqual(recv_exact(proxy, 2), bytes((5, 0)))
                    recv_exact(proxy, 8)
                    proxy.sendall(
                        b"GET / HTTP/1.1\r\nHost: fixture.example\r\nConnection: close\r\n\r\n"
                    )
                    response = bytearray()
                    while True:
                        chunk = proxy.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                self.assertIn(b"pinned browser response", response)
        finally:
            client.close()
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)

    def test_allowed_vendor_redirect_retains_provenance(self):
        session = SequenceSession(
            [
                FakeResponse(
                    "https://meetings.boardbook.org/start",
                    status=302,
                    headers={"Location": "https://cdn.boardbook.org/final"},
                    content=b"",
                ),
                FakeResponse("https://cdn.boardbook.org/final", content=b"meeting data"),
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            result = client.get("https://meetings.boardbook.org/start")
        self.assertEqual(result.final_url, "https://cdn.boardbook.org/final")
        self.assertEqual(result.redirect_chain, ("https://cdn.boardbook.org/final",))
        self.assertEqual(result.content, b"meeting data")
        self.assertEqual(len(session.calls), 2)

    def test_lru_cache_is_entry_bounded(self):
        session = SequenceSession(
            [
                FakeResponse("https://district.example/one", content=b"one"),
                FakeResponse("https://district.example/two", content=b"two"),
                FakeResponse("https://district.example/one", content=b"one-again"),
            ]
        )
        client = BoardHTTPClient(self.settings(cache_max_entries=1), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            client.get("https://district.example/one")
            client.get("https://district.example/two")
            result = client.get("https://district.example/one")
        self.assertEqual(result.content, b"one-again")
        self.assertEqual(client.cache_info()["entries"], 1)
        self.assertEqual(len(session.calls), 3)

    def test_insecure_tls_fallback_requires_an_allowlisted_host(self):
        allowed_session = SequenceSession(
            [
                SSLError("fixture certificate error"),
                FakeResponse("https://meetings.boardbook.org/public", content=b"public"),
            ]
        )
        allowed = BoardHTTPClient(
            self.settings(
                allow_insecure_ssl_fallback=True,
                insecure_ssl_fallback_hosts=("meetings.boardbook.org",),
            ),
            session=allowed_session,
        )
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            result = allowed.get("https://meetings.boardbook.org/public")
        self.assertTrue(result.insecure_tls)
        self.assertEqual(result.tls_mode, "explicit_fallback")
        self.assertEqual(result.insecure_tls_hosts, ("meetings.boardbook.org",))
        self.assertEqual([call["verify"] for call in allowed_session.calls], [True, False])

        denied_session = SequenceSession([SSLError("fixture certificate error")])
        denied = BoardHTTPClient(
            self.settings(
                allow_insecure_ssl_fallback=True,
                insecure_ssl_fallback_hosts=("other.example",),
            ),
            session=denied_session,
        )
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            with self.assertRaises(SSLError):
                denied.get("https://meetings.boardbook.org/public")
        self.assertEqual(len(denied_session.calls), 1)


if __name__ == "__main__":
    unittest.main()
