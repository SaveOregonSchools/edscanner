from __future__ import annotations

import os
import socket
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from requests.exceptions import SSLError
from requests.adapters import HTTPAdapter

from board.adapters.base import (
    _BrowserNavigationPolicy,
    _browser_navigation_scope,
    _main_frame_response_details,
    _validate_browser_route,
)
from board.browser_proxy import pinned_browser_proxy
from board.http import (
    BoardHTTPClient,
    BoardHTTPSettings,
    HTTPResult,
    InvalidPublicURL,
    RedirectDenied,
    RobotsDenied,
    _PinnedHTTPAdapter,
    _create_pinned_openssl_context,
    _windows_server_auth_root_snapshot,
    canonical_public_url,
    redirect_target_allowed,
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


class FakeNavigationRequest:
    def __init__(
        self,
        frame,
        *,
        navigation: bool = True,
        redirected_from=None,
        url: str = "",
    ):
        self.frame = frame
        self._navigation = navigation
        self.redirected_from = redirected_from
        self.url = url

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeNavigationResponse:
    def __init__(self, status: int, url: str, request):
        self.status = status
        self.url = url
        self.request = request


class FakeFrame:
    def __init__(self, page):
        self.page = page


class PopupFirstNavigationRequest:
    def is_navigation_request(self) -> bool:
        return True

    @property
    def frame(self):
        raise RuntimeError("frame is unavailable for a popup's first request")


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

    def test_http_input_is_upgraded_to_https_before_dns_or_fetch(self):
        session = SequenceSession(
            [FakeResponse("https://district.example/board", content=b"board")]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER) as resolver:
            result = client.get("http://district.example:80/board", force=True)

        self.assertEqual(result.requested_url, "https://district.example/board")
        self.assertEqual(session.calls[0]["url"], "https://district.example/board")
        self.assertTrue(session.calls[0]["verify"])
        resolver.assert_called_with(
            "district.example",
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        self.assertEqual(
            canonical_public_url("http://district.example:8080/board"),
            "https://district.example:8080/board",
        )

    def test_browser_route_checks_main_frame_redirect_before_fetch(self):
        client = Mock()
        client.validate_target_url.side_effect = lambda url: url
        client.can_fetch.return_value = True
        client.redirect_allowed.side_effect = (
            lambda _source, target: target.startswith("https://district.example/")
        )

        self.assertEqual(
            _validate_browser_route(
                client,
                "https://district.example/board",
                "https://district.example/meetings",
                main_frame_navigation=True,
            ),
            "https://district.example/meetings",
        )
        # Public cross-host assets remain subject to public-address validation,
        # but they are not top-level redirects.
        self.assertEqual(
            _validate_browser_route(
                client,
                "https://district.example/board",
                "https://cdn.example.net/app.js",
                main_frame_navigation=False,
            ),
            "https://cdn.example.net/app.js",
        )
        with self.assertRaises(InvalidPublicURL):
            _validate_browser_route(
                client,
                "https://district.example/board",
                "https://unrelated.example.net/login",
                main_frame_navigation=True,
            )

    def test_browser_main_frame_redirect_rechecks_robots_policy(self):
        client = Mock()
        client.validate_target_url.side_effect = lambda url: url
        client.redirect_allowed.return_value = True
        client.can_fetch.side_effect = (
            lambda url: not url.endswith("/private-board-archive")
        )

        with self.assertRaises(RobotsDenied):
            _validate_browser_route(
                client,
                "https://district.example/board",
                "https://district.example/private-board-archive",
                main_frame_navigation=True,
            )

        # Subresources remain under the public-network boundary but are not
        # treated as independent crawler navigation targets.
        self.assertEqual(
            _validate_browser_route(
                client,
                "https://district.example/board",
                "https://district.example/private-board-archive",
                main_frame_navigation=False,
            ),
            "https://district.example/private-board-archive",
        )

    def test_initial_district_homepage_policy_accepts_one_public_https_move(self):
        client = Mock()
        client.validate_target_url.side_effect = lambda url: url
        client.can_fetch.return_value = True
        client.redirect_allowed.side_effect = lambda source, target: (
            source.split("/", 3)[2].removeprefix("www.")
            == target.split("/", 3)[2].removeprefix("www.")
        )
        old_url = "https://www.estacada.k12.or.us/"
        moved_url = "https://www.estacadaschools.org/"
        policy = _BrowserNavigationPolicy(
            client,
            old_url,
            allow_district_website_move=True,
        )

        self.assertEqual(
            policy.validate(old_url, main_frame_navigation=True),
            old_url,
        )
        self.assertEqual(
            policy.validate(
                moved_url,
                main_frame_navigation=True,
                redirected_from_url=old_url,
            ),
            moved_url,
        )
        policy.close_initial_navigation()

        self.assertTrue(policy.website_migration_accepted)
        self.assertEqual(policy.active_base_url, moved_url)
        self.assertEqual(policy.redirect_chain, [moved_url])

    def test_initial_district_homepage_policy_rejects_https_downgrade(self):
        client = Mock()
        client.validate_target_url.side_effect = lambda url: url
        client.can_fetch.return_value = True
        client.redirect_allowed.return_value = False
        old_url = "https://www.estacada.k12.or.us/"
        policy = _BrowserNavigationPolicy(
            client,
            old_url,
            allow_district_website_move=True,
        )

        with self.assertRaises(InvalidPublicURL):
            policy.validate(
                "http://www.estacadaschools.org/",
                main_frame_navigation=True,
                redirected_from_url=old_url,
            )
        self.assertFalse(policy.website_migration_accepted)

    def test_district_move_does_not_relax_later_candidate_or_subresource_policy(self):
        client = Mock()
        client.validate_target_url.side_effect = lambda url: url
        client.can_fetch.return_value = True
        client.redirect_allowed.side_effect = lambda source, target: (
            source.split("/", 3)[2].removeprefix("www.")
            == target.split("/", 3)[2].removeprefix("www.")
        )
        old_url = "https://old-district.example/"
        moved_url = "https://new-district.example/"
        unrelated_url = "https://unrelated.example/board"
        policy = _BrowserNavigationPolicy(
            client,
            old_url,
            allow_district_website_move=True,
        )
        policy.validate(
            moved_url,
            main_frame_navigation=True,
            redirected_from_url=old_url,
        )
        policy.close_initial_navigation()

        with self.assertRaises(InvalidPublicURL):
            policy.validate(
                unrelated_url,
                main_frame_navigation=True,
                redirected_from_url=moved_url,
            )
        # Public subresources are validated but cannot establish another main
        # document/crawl boundary.
        self.assertEqual(
            policy.validate(
                unrelated_url,
                main_frame_navigation=False,
            ),
            unrelated_url,
        )

        candidate_policy = _BrowserNavigationPolicy(client, moved_url)
        with self.assertRaises(InvalidPublicURL):
            candidate_policy.validate(
                unrelated_url,
                main_frame_navigation=True,
                redirected_from_url=moved_url,
            )

    def test_browser_context_navigation_scope_blocks_popup_first_request(self):
        page = type("FakePage", (), {})()
        page.main_frame = FakeFrame(page)
        child_frame = FakeFrame(page)
        other_page = object()
        popup_frame = FakeFrame(other_page)

        self.assertEqual(
            _browser_navigation_scope(FakeNavigationRequest(page.main_frame), page),
            "main",
        )
        self.assertEqual(
            _browser_navigation_scope(FakeNavigationRequest(child_frame), page),
            "child",
        )
        self.assertEqual(
            _browser_navigation_scope(FakeNavigationRequest(popup_frame), page),
            "popup",
        )
        self.assertEqual(
            _browser_navigation_scope(PopupFirstNavigationRequest(), page),
            "popup",
        )
        self.assertEqual(
            _browser_navigation_scope(
                FakeNavigationRequest(page.main_frame, navigation=False),
                page,
            ),
            "subresource",
        )

    def test_browser_uses_latest_main_document_response_after_challenge_reload(self):
        main_frame = object()
        page = type("FakePage", (), {"main_frame": main_frame})()
        initial = FakeNavigationResponse(
            403,
            "https://district.example/challenge",
            FakeNavigationRequest(main_frame),
        )
        solved = FakeNavigationResponse(
            200,
            "https://district.example/board",
            FakeNavigationRequest(main_frame),
        )
        subresource = FakeNavigationResponse(
            404,
            "https://district.example/missing.js",
            FakeNavigationRequest(main_frame, navigation=False),
        )

        latest = None
        for response in (initial, solved, subresource):
            details = _main_frame_response_details(page, response)
            if details is not None:
                latest = details

        self.assertEqual(latest, (200, "https://district.example/board"))

    def test_redirect_to_private_target_is_blocked_before_second_request(self):
        session = SequenceSession(
            [
                FakeResponse(
                    "https://district.example/start",
                    status=302,
                    headers={"Location": "https://127.0.0.1/private"},
                    content=b"",
                )
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            with self.assertRaises(InvalidPublicURL):
                client.get("https://district.example/start")
        self.assertEqual(len(session.calls), 1)

    def test_redirect_to_http_is_rejected_without_a_second_request(self):
        session = SequenceSession(
            [
                FakeResponse(
                    "https://district.example/start",
                    status=302,
                    headers={"Location": "http://district.example/board"},
                    content=b"",
                )
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            with self.assertRaisesRegex(RedirectDenied, "insecure HTTP"):
                client.get(
                    "https://district.example/start",
                    allow_district_website_move=True,
                )
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(
            redirect_target_allowed(
                "https://district.example/start",
                "http://district.example/board",
            )
        )

    def test_initial_district_homepage_http_move_is_explicit_and_cache_isolated(self):
        old_url = "https://www.estacada.k12.or.us/"
        moved_url = "https://www.estacadaschools.org/"
        session = SequenceSession(
            [
                FakeResponse(
                    old_url,
                    status=302,
                    headers={"Location": moved_url},
                    content=b"",
                ),
                FakeResponse(moved_url, content=b"district home"),
                FakeResponse(
                    old_url,
                    status=302,
                    headers={"Location": moved_url},
                    content=b"",
                ),
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            result = client.get(
                old_url,
                allow_district_website_move=True,
            )
            cached = client.get(
                old_url,
                allow_district_website_move=True,
            )
            with self.assertRaises(RedirectDenied):
                client.get(old_url)

        self.assertEqual(result.requested_url, old_url)
        self.assertEqual(result.final_url, moved_url)
        self.assertEqual(result.redirect_chain, (moved_url,))
        self.assertTrue(result.website_migration_accepted)
        self.assertTrue(cached.from_cache)
        self.assertTrue(cached.website_migration_accepted)
        self.assertEqual(len(session.calls), 3)

    def test_initial_district_homepage_http_move_allows_only_one_boundary_change(self):
        old_url = "https://old-district.example/"
        moved_url = "https://new-district.example/"
        unrelated_url = "https://unrelated.example/board"
        session = SequenceSession(
            [
                FakeResponse(
                    old_url,
                    status=302,
                    headers={"Location": moved_url},
                    content=b"",
                ),
                FakeResponse(
                    moved_url,
                    status=302,
                    headers={"Location": unrelated_url},
                    content=b"",
                ),
            ]
        )
        client = BoardHTTPClient(self.settings(), session=session)
        with patch("board.http.socket.getaddrinfo", return_value=PUBLIC_DNS_ANSWER):
            with self.assertRaises(RedirectDenied):
                client.get(
                    old_url,
                    allow_district_website_move=True,
                )

        self.assertEqual([call["url"] for call in session.calls], [old_url, moved_url])

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

        adapter.pin("http://district.example/board", "93.184.216.34")
        with self.assertRaisesRegex(InvalidPublicURL, "requires HTTPS"):
            adapter._pool_for_url("http://district.example/board", {})

    def test_native_trust_context_is_used_only_for_verified_https(self):
        verified_context = object()
        native_context = object()
        adapter = _PinnedHTTPAdapter(
            verified_ssl_context=verified_context,
            native_ssl_context=native_context,
        )
        url = "https://meetings.boardbook.org/Public"
        adapter.pin(url, "93.184.216.34")
        request = type("PreparedRequest", (), {"url": url})()
        connection = object()

        with patch.object(
            adapter.poolmanager,
            "connection_from_host",
            return_value=connection,
        ) as connection_from_host:
            self.assertIs(
                adapter.get_connection_with_tls_context(request, True),
                connection,
            )
            verified_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
            self.assertIs(verified_kwargs["ssl_context"], native_context)
            self.assertEqual(verified_kwargs["cert_reqs"], "CERT_REQUIRED")
            self.assertEqual(verified_kwargs["server_hostname"], "meetings.boardbook.org")

            self.assertIs(
                adapter.get_connection_with_tls_context(request, False),
                connection,
            )
            insecure_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
            self.assertNotIn("ssl_context", insecure_kwargs)
            self.assertEqual(insecure_kwargs["cert_reqs"], "CERT_NONE")

            unrelated_url = "https://district.example/board"
            adapter.pin(unrelated_url, "93.184.216.34")
            unrelated_request = type("PreparedRequest", (), {"url": unrelated_url})()
            self.assertIs(
                adapter.get_connection_with_tls_context(unrelated_request, True),
                connection,
            )
            unrelated_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
            self.assertIs(unrelated_kwargs["ssl_context"], verified_context)
            self.assertEqual(unrelated_kwargs["cert_reqs"], "CERT_REQUIRED")

            subdomain_url = "https://public.meetings.boardbook.org/board"
            adapter.pin(subdomain_url, "93.184.216.34")
            subdomain_request = type("PreparedRequest", (), {"url": subdomain_url})()
            self.assertIs(
                adapter.get_connection_with_tls_context(subdomain_request, True),
                connection,
            )
            subdomain_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
            self.assertIs(subdomain_kwargs["ssl_context"], verified_context)

    def test_windows_root_snapshot_filters_by_server_auth_and_deduplicates(self):
        server_auth_oid = ssl.Purpose.SERVER_AUTH.oid
        entries = [
            (b"all-purpose", "x509_asn", True),
            (b"server", "x509_asn", {server_auth_oid}),
            (b"client-only", "x509_asn", {ssl.Purpose.CLIENT_AUTH.oid}),
            (b"container", "pkcs_7_asn", True),
            (b"all-purpose", "x509_asn", True),
        ]
        with patch.object(ssl, "enum_certificates", return_value=entries, create=True) as enum:
            self.assertEqual(
                _windows_server_auth_root_snapshot(),
                (b"all-purpose", b"server"),
            )
        enum.assert_called_once_with("ROOT")

    @unittest.skipUnless(
        os.name == "nt" and hasattr(ssl, "enum_certificates"),
        "Windows certificate store is required",
    )
    def test_pinned_openssl_context_contains_windows_root_snapshot_without_web(self):
        roots = set(_windows_server_auth_root_snapshot())
        self.assertTrue(roots)

        context = _create_pinned_openssl_context()
        loaded = set(context.get_ca_certs(binary_form=True))
        self.assertTrue(roots.intersection(loaded))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.assertFalse(context.verify_flags & strict_flag)

    def test_robots_fetch_never_enables_district_website_migration(self):
        client = BoardHTTPClient(self.settings(respect_robots=True))
        robots_result = HTTPResult(
            requested_url="https://district.example/robots.txt",
            final_url="https://district.example/robots.txt",
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content=b"User-agent: *\nAllow: /\n",
        )
        with (
            patch.object(client, "_validate_target", side_effect=lambda value: value),
            patch.object(client, "_request", return_value=robots_result) as request,
        ):
            self.assertTrue(client.can_fetch("https://district.example/board"))

        self.assertFalse(request.call_args.kwargs["allow_district_website_move"])

    def test_native_trust_allowlist_is_exact_and_custom_ca_takes_precedence(self):
        settings = self.settings(
            native_trust_hosts=(
                "Meetings.BoardBook.org.",
                "district.example",
                "*.district.example",
                "https://invalid.example",
            )
        )
        self.assertEqual(settings.native_trust_hosts, ("meetings.boardbook.org",))

        native_context = object()
        adapter = _PinnedHTTPAdapter(
            native_ssl_context=native_context,
            native_trust_hosts=settings.native_trust_hosts,
        )
        url = "https://meetings.boardbook.org/Public"
        adapter.pin(url, "93.184.216.34")
        request = type("PreparedRequest", (), {"url": url})()
        connection = object()
        with patch.object(
            adapter.poolmanager,
            "connection_from_host",
            return_value=connection,
        ) as connection_from_host:
            self.assertIs(
                adapter.get_connection_with_tls_context(
                    request,
                    "C:/fixture/custom-ca.pem",
                ),
                connection,
            )

        pool_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
        self.assertNotIn("ssl_context", pool_kwargs)
        self.assertEqual(pool_kwargs["ca_certs"], "C:/fixture/custom-ca.pem")
        self.assertEqual(pool_kwargs["cert_reqs"], "CERT_REQUIRED")

    def test_requests_231_path_preserves_unverified_pool_separation(self):
        native_context = object()
        adapter = _PinnedHTTPAdapter(native_ssl_context=native_context)
        url = "https://meetings.boardbook.org/Public"
        adapter.pin(url, "93.184.216.34")
        request = type("PreparedRequest", (), {"url": url})()
        connection = object()

        with (
            patch.object(
                adapter.poolmanager,
                "connection_from_host",
                return_value=connection,
            ) as connection_from_host,
            patch.object(
                HTTPAdapter,
                "send",
                side_effect=lambda *_args, **_kwargs: adapter.get_connection(url),
            ),
        ):
            self.assertIs(adapter.send(request, verify=False), connection)

        pool_kwargs = connection_from_host.call_args.kwargs["pool_kwargs"]
        self.assertNotIn("ssl_context", pool_kwargs)
        self.assertEqual(pool_kwargs["cert_reqs"], "CERT_NONE")
        self.assertFalse(hasattr(adapter._active_verify, "value"))

    def test_mixed_dns_answers_pin_only_ipv4_by_default(self):
        answers = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:4860::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ]
        client = BoardHTTPClient(
            self.settings(max_retries=0, backoff_base_seconds=0, backoff_max_seconds=0),
            session=SequenceSession([]),
        )
        response = FakeResponse("https://district.example/", content=b"ipv4")
        with (
            patch("board.http.socket.getaddrinfo", return_value=answers) as resolver,
            patch.object(
                client,
                "_perform_get",
                return_value=(response, "verified"),
            ) as perform_get,
        ):
            result = client.get("https://district.example/", force=True)
        self.assertEqual(result.content, b"ipv4")
        self.assertEqual(
            [call.args[2] for call in perform_get.call_args_list],
            ["93.184.216.34"],
        )
        self.assertTrue(resolver.call_args_list)
        self.assertTrue(
            all(call.args[2] == socket.AF_INET for call in resolver.call_args_list)
        )

    def test_ipv6_only_dns_answer_is_rejected_by_default(self):
        answers = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:4860::1", 443, 0, 0),
            )
        ]
        client = BoardHTTPClient(self.settings())

        with patch("board.http.socket.getaddrinfo", return_value=answers) as resolver:
            with self.assertRaisesRegex(InvalidPublicURL, "no usable IPv4 addresses"):
                client.validated_connection_target("https://district.example/")

        resolver.assert_called_once_with(
            "district.example",
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

    def test_ipv6_may_be_enabled_explicitly(self):
        answers = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:4860::1", 443, 0, 0),
            )
        ]
        client = BoardHTTPClient(self.settings(ipv4_only=False))

        with patch("board.http.socket.getaddrinfo", return_value=answers) as resolver:
            _canonical, addresses = client.validated_connection_target(
                "https://district.example/"
            )

        self.assertEqual(addresses, ("2001:4860::1",))
        resolver.assert_called_once_with(
            "district.example",
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
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

    def test_browser_proxy_rejects_ipv6_destination_when_ipv4_only(self):
        client = BoardHTTPClient(
            self.settings(allow_private_networks=True, ipv4_only=True)
        )
        try:
            with pinned_browser_proxy(client) as proxy_url:
                proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                proxy.settimeout(2)
                proxy.connect(("127.0.0.1", int(proxy_url.rsplit(":", 1)[1])))
                with proxy:
                    proxy.sendall(bytes((5, 1, 0)))
                    self.assertEqual(recv_exact(proxy, 2), bytes((5, 0)))
                    proxy.sendall(
                        bytes((5, 1, 0, 4))
                        + socket.inet_pton(socket.AF_INET6, "2001:4860::1")
                        + (443).to_bytes(2, "big")
                    )
                    self.assertEqual(recv_exact(proxy, 2), bytes((5, 2)))
                    recv_exact(proxy, 8)
        finally:
            client.close()

    def test_browser_proxy_rejects_plain_http_port(self):
        client = BoardHTTPClient(self.settings(allow_private_networks=True))
        try:
            with pinned_browser_proxy(client) as proxy_url:
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
                        + (80).to_bytes(2, "big")
                    )
                    self.assertEqual(recv_exact(proxy, 2), bytes((5, 2)))
                    recv_exact(proxy, 8)
        finally:
            client.close()

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
