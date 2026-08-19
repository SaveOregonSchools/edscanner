from __future__ import annotations

import email.utils
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
import truststore
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError
from urllib3.util import create_urllib3_context

from common import (
    BOARD_ALLOW_INSECURE_SSL_FALLBACK,
    BOARD_ALLOW_PRIVATE_NETWORKS,
    BOARD_HTTP_CACHE_MAX_BYTES,
    BOARD_HTTP_CACHE_MAX_ENTRIES,
    BOARD_HTTP_MAX_REDIRECTS,
    BOARD_INSECURE_SSL_FALLBACK_HOSTS,
    BOARD_IPV4_ONLY,
    BOARD_MAX_DOCUMENT_SIZE_BYTES,
    BOARD_PER_HOST_WORKERS,
    BOARD_REQUEST_DELAY_SECONDS,
    BOARD_WORKERS,
    MAX_HTML_SIZE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    RESPECT_ROBOTS,
    USER_AGENT,
    VERIFY_SSL,
)


LOGGER = logging.getLogger(__name__)
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "cookie", "host", "proxy-authorization"}
)

# Cross-host redirects are allowed only within one of these explicitly known
# public board-platform families. Suffix matching uses a label boundary.
KNOWN_BOARD_VENDOR_FAMILIES = (
    ("boardbook.org", "boardbookpremier.com"),
    ("boarddocs.com",),
    ("eboardsolutions.com", "simbli.com"),
    (
        "diligentoneplatform.com",
        "diligent.community",
        "community.diligent.com",
        "community.highbond.com",
        "civicweb.net",
    ),
    ("civicclerk.com",),
)
NATIVE_TRUST_HOSTS = frozenset({"meetings.boardbook.org"})


def _windows_server_auth_root_snapshot() -> tuple[bytes, ...]:
    """Copy the Windows ROOT store entries trusted for TLS server auth.

    ``truststore`` delegates chain construction to the operating system and may
    perform platform-specific intermediate retrieval while a connection is in
    progress. Ordinary board requests instead load this bounded snapshot into
    OpenSSL before any network I/O, so their trust anchors cannot change during
    a run and certificate verification remains local and deterministic.
    """

    enum_certificates = getattr(ssl, "enum_certificates", None)
    if enum_certificates is None:
        return ()
    try:
        entries = enum_certificates("ROOT")
    except OSError:
        LOGGER.warning("Unable to enumerate the Windows ROOT certificate store")
        return ()

    server_auth_oid = ssl.Purpose.SERVER_AUTH.oid
    certificates: list[bytes] = []
    seen: set[bytes] = set()
    for certificate, encoding, trust in entries:
        if encoding != "x509_asn":
            continue
        if trust is not True and server_auth_oid not in trust:
            continue
        certificate_bytes = bytes(certificate)
        if certificate_bytes in seen:
            continue
        seen.add(certificate_bytes)
        certificates.append(certificate_bytes)
    return tuple(certificates)


def _create_pinned_openssl_context() -> ssl.SSLContext:
    """Build the verified OpenSSL context used by ordinary pinned requests."""

    context = create_urllib3_context()
    # urllib3 enables OpenSSL's strict RFC 5280 mode.  Windows' public trust
    # store still contains valid legacy chains whose CA Basic Constraints were
    # not encoded as critical, so strict mode rejects sites that Windows and
    # ordinary Requests verification accept.  Match the platform verifier for
    # that compatibility detail while retaining CERT_REQUIRED, hostname
    # verification, and all of the client's DNS-pinning/public-IP controls.
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    # Requests normally supplies certifi after selecting a connection pool.
    # Load it up front, then augment it with the static Windows ROOT snapshot so
    # the shared context is complete before concurrent requests begin.
    context.load_verify_locations(cafile=requests.certs.where())
    for certificate in _windows_server_auth_root_snapshot():
        try:
            context.load_verify_locations(cadata=certificate)
        except ssl.SSLError as exc:
            # A malformed store entry must not disable verification or prevent
            # all other trusted roots from loading.
            LOGGER.warning("Skipped an invalid Windows ROOT certificate: %s", exc)
    return context


class BoardHTTPError(RuntimeError):
    pass


class InvalidPublicURL(BoardHTTPError):
    pass


class RobotsDenied(BoardHTTPError):
    pass


class ResponseTooLarge(BoardHTTPError):
    pass


class RedirectDenied(BoardHTTPError):
    pass


class TooManyRedirects(BoardHTTPError):
    pass


@dataclass(slots=True)
class BoardHTTPSettings:
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    delay_seconds: float = BOARD_REQUEST_DELAY_SECONDS
    max_html_size_bytes: int = MAX_HTML_SIZE_BYTES
    max_document_size_bytes: int = BOARD_MAX_DOCUMENT_SIZE_BYTES
    user_agent: str = USER_AGENT
    verify_ssl: bool = VERIFY_SSL
    respect_robots: bool = RESPECT_ROBOTS
    max_retries: int = 2
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    per_host_concurrency: int = BOARD_PER_HOST_WORKERS
    global_concurrency: int = BOARD_WORKERS
    max_redirects: int = BOARD_HTTP_MAX_REDIRECTS
    cache_max_entries: int = BOARD_HTTP_CACHE_MAX_ENTRIES
    cache_max_bytes: int = BOARD_HTTP_CACHE_MAX_BYTES
    allow_private_networks: bool = BOARD_ALLOW_PRIVATE_NETWORKS
    ipv4_only: bool = BOARD_IPV4_ONLY
    allow_insecure_ssl_fallback: bool = BOARD_ALLOW_INSECURE_SSL_FALLBACK
    insecure_ssl_fallback_hosts: tuple[str, ...] | str = BOARD_INSECURE_SSL_FALLBACK_HOSTS
    # Windows' native certificate verifier may retrieve missing intermediate
    # certificates while building a chain. Keep that network-capable behavior
    # confined to exact, reviewed vendor hosts; arbitrary district and manually
    # entered hosts use certifi plus a static Windows ROOT snapshot in OpenSSL.
    native_trust_hosts: tuple[str, ...] | str = tuple(NATIVE_TRUST_HOSTS)

    def __post_init__(self) -> None:
        self.timeout_seconds = max(1.0, float(self.timeout_seconds))
        self.delay_seconds = max(0.0, float(self.delay_seconds))
        self.max_html_size_bytes = max(1, int(self.max_html_size_bytes))
        self.max_document_size_bytes = max(1, int(self.max_document_size_bytes))
        self.max_retries = max(0, min(int(self.max_retries), 5))
        self.backoff_base_seconds = max(0.0, float(self.backoff_base_seconds))
        self.backoff_max_seconds = max(self.backoff_base_seconds, float(self.backoff_max_seconds))
        self.per_host_concurrency = max(1, min(int(self.per_host_concurrency), 8))
        self.global_concurrency = max(1, min(int(self.global_concurrency), 32))
        self.max_redirects = max(0, min(int(self.max_redirects), 10))
        self.cache_max_entries = max(0, min(int(self.cache_max_entries), 10_000))
        self.cache_max_bytes = max(0, int(self.cache_max_bytes))
        hosts = self.insecure_ssl_fallback_hosts
        if hosts is None:
            hosts = ()
        if isinstance(hosts, str):
            hosts = tuple(hosts.split(","))
        self.insecure_ssl_fallback_hosts = tuple(
            sorted(
                {
                    str(host).strip().casefold().lstrip("*.").rstrip(".")
                    for host in hosts
                    if str(host).strip().lstrip("*.").rstrip(".")
                }
            )
        )
        native_hosts = self.native_trust_hosts
        if native_hosts is None:
            native_hosts = ()
        if isinstance(native_hosts, str):
            native_hosts = tuple(native_hosts.split(","))
        self.native_trust_hosts = tuple(
            sorted(
                {
                    str(host).strip().casefold().rstrip(".")
                    for host in native_hosts
                    if str(host).strip().casefold().rstrip(".") in NATIVE_TRUST_HOSTS
                }
            )
        )


@dataclass(slots=True)
class HTTPResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    from_cache: bool = False
    elapsed_seconds: float = 0.0
    redirect_chain: tuple[str, ...] = ()
    website_migration_accepted: bool = False
    insecure_tls: bool = False
    tls_mode: str = "verified"
    insecure_tls_hosts: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return self.final_url

    def header(self, name: str, default: str | None = None) -> str | None:
        """Return a response header without relying on its original casing."""

        folded = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == folded:
                return value
        return default

    @property
    def content_type(self) -> str:
        return self.header("Content-Type", "") or ""

    @property
    def etag(self) -> str | None:
        return self.header("ETag")

    @property
    def last_modified(self) -> str | None:
        return self.header("Last-Modified")

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    @property
    def tls_verification_disabled(self) -> bool:
        """Whether any HTTPS hop was fetched without certificate verification."""

        return self.insecure_tls

    @property
    def text(self) -> str:
        headers = requests.structures.CaseInsensitiveDict(self.headers)
        encoding = requests.utils.get_encoding_from_headers(headers) or "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise requests.HTTPError(
                f"{self.status_code} response for {self.final_url}",
                response=_ResultResponseProxy(self),
            )


class _ResultResponseProxy:
    """Small response-shaped object attached to errors from cached results."""

    def __init__(self, result: HTTPResult):
        self.status_code = result.status_code
        self.url = result.final_url
        self.headers = result.headers
        self.content = result.content


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: HTTPResult | None = None
    error: BaseException | None = None


@dataclass
class _HostState:
    capacity: int
    semaphore: threading.BoundedSemaphore = field(init=False)
    throttle_lock: threading.Lock = field(default_factory=threading.Lock)
    next_request_at: float = 0.0

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(self.capacity)


def _public_ip_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def canonical_public_url(
    url: str,
    *,
    allow_private_networks: bool = False,
) -> str:
    clean, _fragment = urldefrag(str(url or "").strip())
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise InvalidPublicURL("Control characters are not supported in public URLs.")
    parsed = urlsplit(clean)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidPublicURL(f"Invalid public URL: {url!r}") from exc
    source_scheme = parsed.scheme.casefold()
    if source_scheme not in {"http", "https"} or not hostname:
        raise InvalidPublicURL(f"Only public HTTP(S) URLs are supported: {url!r}")
    if parsed.username or parsed.password:
        raise InvalidPublicURL("Credential-bearing URLs are not supported.")
    host = hostname.casefold().strip(".")
    if not host:
        raise InvalidPublicURL(f"Invalid public URL: {url!r}")
    literal = _literal_ip(host)
    if literal is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidPublicURL(f"Invalid internationalized hostname: {hostname!r}") from exc
    if not allow_private_networks:
        if host == "localhost" or host.endswith((".localhost", ".local", ".home.arpa")):
            raise InvalidPublicURL(f"Local network hostname is not allowed: {host}")
        if literal is not None and not _public_ip_address(literal):
            raise InvalidPublicURL(f"Non-public network address is not allowed: {host}")
    # HTTP is accepted only as legacy input and is upgraded before validation or
    # network I/O. An explicit legacy/default port must not pin the upgraded
    # request to port 80.
    default_port = port == 443 or (source_scheme == "http" and port == 80)
    host_for_netloc = f"[{host}]" if ":" in host else host
    netloc = host_for_netloc if port is None or default_port else f"{host_for_netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def _validated_public_target(
    url: str,
    *,
    allow_private_networks: bool = False,
    ipv4_only: bool = BOARD_IPV4_ONLY,
) -> tuple[str, tuple[str, ...]]:
    """Return a canonical URL and the exact DNS answers approved for connection.

    Callers that perform network I/O must connect to one of the returned
    addresses rather than resolving the hostname a second time. That closes
    the validation/use gap exploited by DNS rebinding while preserving the
    original hostname for HTTP Host and TLS SNI/certificate verification.
    """

    canonical = canonical_public_url(
        url,
        allow_private_networks=allow_private_networks,
    )
    parsed = urlsplit(canonical)
    host = parsed.hostname or ""
    literal = _literal_ip(host)
    if literal is not None:
        if ipv4_only and literal.version != 4:
            raise InvalidPublicURL("IPv6 board destinations are disabled by configuration.")
        return canonical, (str(literal),)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(
            host,
            port,
            socket.AF_INET if ipv4_only else socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError) as exc:
        raise InvalidPublicURL(f"Public hostname could not be resolved: {host}") from exc
    addresses: list[str] = []
    seen_addresses: set[str] = set()
    for answer in answers:
        try:
            address_text = str(answer[4][0]).split("%", 1)[0]
            address = ipaddress.ip_address(address_text)
        except (IndexError, TypeError, ValueError):
            continue
        if ipv4_only and (answer[0] != socket.AF_INET or address.version != 4):
            continue
        normalized_address = str(address)
        if normalized_address in seen_addresses:
            continue
        seen_addresses.add(normalized_address)
        addresses.append(normalized_address)
        if not allow_private_networks and not _public_ip_address(address):
            raise InvalidPublicURL(
                f"Hostname {host} resolved to non-public address {address}"
            )
    if not addresses:
        address_kind = "IPv4 " if ipv4_only else ""
        raise InvalidPublicURL(
            f"Public hostname produced no usable {address_kind}addresses: {host}"
        )
    return canonical, tuple(addresses)


def validate_public_url(
    url: str,
    *,
    allow_private_networks: bool = False,
    ipv4_only: bool = BOARD_IPV4_ONLY,
) -> str:
    """Canonicalize a URL and reject DNS answers that are not globally routable."""

    canonical, _addresses = _validated_public_target(
        url,
        allow_private_networks=allow_private_networks,
        ipv4_only=ipv4_only,
    )
    return canonical


def _host_matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _suffix_related_hosts(first: str, second: str) -> bool:
    if first == second:
        return True
    if _literal_ip(first) is not None or _literal_ip(second) is not None:
        return False
    shorter, longer = sorted((first, second), key=len)
    return "." in shorter and longer.endswith(f".{shorter}")


def _vendor_family(host: str) -> int | None:
    for index, suffixes in enumerate(KNOWN_BOARD_VENDOR_FAMILIES):
        if any(_host_matches_suffix(host, suffix) for suffix in suffixes):
            return index
    return None


def redirect_target_allowed(source_url: str, target_url: str) -> bool:
    """Return whether a redirect stays within an organization/vendor boundary."""

    # Check the response's actual target before canonicalization can upgrade a
    # legacy HTTP URL. Board requests never follow TLS-downgrade redirects.
    if urlsplit(str(target_url or "").strip()).scheme.casefold() != "https":
        return False
    source = urlsplit(
        canonical_public_url(source_url, allow_private_networks=True)
    )
    target = urlsplit(
        canonical_public_url(target_url, allow_private_networks=True)
    )
    if source.scheme == "https" and target.scheme != "https":
        return False
    source_host = source.hostname or ""
    target_host = target.hostname or ""
    if _suffix_related_hosts(source_host, target_host):
        return True
    source_family = _vendor_family(source_host)
    return source_family is not None and source_family == _vendor_family(target_host)


class _PinnedHTTPAdapter(HTTPAdapter):
    """Requests transport that connects to a previously validated IP address."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._verified_ssl_context = kwargs.pop("verified_ssl_context", None)
        self._native_ssl_context = kwargs.pop("native_ssl_context", None)
        native_trust_hosts = kwargs.pop(
            "native_trust_hosts",
            tuple(NATIVE_TRUST_HOSTS),
        )
        if isinstance(native_trust_hosts, str):
            native_trust_hosts = tuple(native_trust_hosts.split(","))
        self._native_trust_hosts = frozenset(
            str(host).strip().casefold().rstrip(".")
            for host in (native_trust_hosts or ())
            if str(host).strip().casefold().rstrip(".") in NATIVE_TRUST_HOSTS
        )
        super().__init__(*args, **kwargs)
        self._active_pin = threading.local()
        self._active_verify = threading.local()
        self._active_cert = threading.local()

    def pin(self, url: str, address: str) -> None:
        parsed = urlsplit(url)
        self._active_pin.value = (
            parsed.scheme.casefold(),
            (parsed.hostname or "").casefold().rstrip("."),
            parsed.port or (443 if parsed.scheme.casefold() == "https" else 80),
            str(address).split("%", 1)[0],
        )

    def _pool_for_url(
        self,
        url: str,
        proxies: Mapping[str, str] | None = None,
        *,
        verify: Any = True,
        cert: Any = None,
    ) -> Any:
        if proxies:
            raise BoardHTTPError("Proxy routing is disabled for pinned board requests.")
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        if scheme != "https":
            raise InvalidPublicURL("Pinned board transport requires HTTPS.")
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80)
        active = getattr(self._active_pin, "value", None)
        if active is None or active[:3] != (scheme, host, port):
            raise BoardHTTPError(f"No validated address is pinned for {url}")
        address = active[3]
        pool_kwargs: dict[str, Any] = {}
        if scheme == "https":
            pool_kwargs.update(
                {
                    "assert_hostname": host,
                    "server_hostname": host,
                }
            )
            if verify is False or verify is None:
                pool_kwargs["cert_reqs"] = "CERT_NONE"
            else:
                pool_kwargs["cert_reqs"] = "CERT_REQUIRED"
                if (
                    verify is True
                    and self._native_ssl_context is not None
                    and host in self._native_trust_hosts
                ):
                    # Keep the logical hostname for SNI/hostname checks while
                    # delegating chain construction to the operating system.
                    # On Windows this uses CryptoAPI and can retrieve missing
                    # intermediate certificates securely.
                    pool_kwargs["ssl_context"] = self._native_ssl_context
                elif verify is True and self._verified_ssl_context is not None:
                    pool_kwargs["ssl_context"] = self._verified_ssl_context
                elif isinstance(verify, str):
                    if os.path.isdir(verify):
                        pool_kwargs["ca_cert_dir"] = verify
                    else:
                        pool_kwargs["ca_certs"] = verify
            if cert:
                if isinstance(cert, tuple) and len(cert) == 2:
                    pool_kwargs["cert_file"] = cert[0]
                    pool_kwargs["key_file"] = cert[1]
                else:
                    pool_kwargs["cert_file"] = cert
        return self.poolmanager.connection_from_host(
            scheme=scheme,
            host=address,
            port=port,
            pool_kwargs=pool_kwargs,
        )

    # Requests 2.32+ calls this method. Keeping get_connection below preserves
    # compatibility with the project's requests>=2.31 range.
    def get_connection_with_tls_context(
        self,
        request: Any,
        verify: Any,
        proxies: Mapping[str, str] | None = None,
        cert: Any = None,
    ) -> Any:
        return self._pool_for_url(
            request.url,
            proxies,
            verify=verify,
            cert=cert,
        )

    def get_connection(
        self,
        url: str,
        proxies: Mapping[str, str] | None = None,
    ) -> Any:
        # Requests 2.31 does not pass TLS settings to get_connection(). send()
        # records them in thread-local state so verified and explicitly
        # unverified requests still use separate connection pools.
        return self._pool_for_url(
            url,
            proxies,
            verify=getattr(self._active_verify, "value", True),
            cert=getattr(self._active_cert, "value", None),
        )

    def send(
        self,
        request: Any,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Mapping[str, str] | None = None,
    ) -> Any:
        self._active_verify.value = verify
        self._active_cert.value = cert
        try:
            return super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        finally:
            try:
                del self._active_verify.value
            except AttributeError:
                pass
            try:
                del self._active_cert.value
            except AttributeError:
                pass

    def cert_verify(
        self,
        conn: Any,
        url: str,
        verify: Any,
        cert: Any,
    ) -> None:
        """Keep verified pools on their already-complete static contexts.

        Requests otherwise attaches certifi to the pool after selection, which
        makes urllib3 call ``load_verify_locations`` on the shared context at
        connection time. Both verified contexts are complete before use: the
        ordinary OpenSSL context contains certifi plus Windows ROOTs, while the
        exact-host native context intentionally owns its platform trust policy.
        """

        super().cert_verify(conn, url, verify, cert)
        if urlsplit(url).scheme.casefold() == "https" and verify is True:
            conn.ca_certs = None
            conn.ca_cert_dir = None


class BoardHTTPClient:
    """Bounded, host-aware HTTP client scoped to one collection run.

    The response cache and URL de-duplication are per client instance. Host gates
    are process-wide so independent board runs cannot collectively hammer a
    shared vendor host.
    """

    _states_lock = threading.Lock()
    _host_states: dict[str, _HostState] = {}
    _global_gates: dict[int, threading.BoundedSemaphore] = {}
    _browser_gate = threading.BoundedSemaphore(1)

    def __init__(
        self,
        settings: BoardHTTPSettings | None = None,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or BoardHTTPSettings()
        self._verified_ssl_context = _create_pinned_openssl_context()
        self._native_ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._injected_session = session
        self._thread_local = threading.local()
        self._sessions: set[requests.Session] = set()
        self._sessions_lock = threading.Lock()
        self._cache: OrderedDict[tuple[Any, ...], HTTPResult] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[tuple[Any, ...], _PendingRequest] = {}
        self._cache_lock = threading.Lock()
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        if self.settings.allow_private_networks:
            LOGGER.warning(
                "Board HTTP private-network access is enabled; use only for controlled local tests"
            )
        if not self.settings.verify_ssl:
            LOGGER.warning("Board HTTP TLS certificate verification is disabled by configuration")
        elif (
            self.settings.allow_insecure_ssl_fallback
            and self.settings.insecure_ssl_fallback_hosts
        ):
            LOGGER.warning(
                "Board HTTP insecure TLS fallback is enabled only for %s; "
                "affected results are marked insecure_tls",
                ", ".join(self.settings.insecure_ssl_fallback_hosts),
            )
        elif self.settings.allow_insecure_ssl_fallback:
            LOGGER.warning(
                "Board HTTP insecure TLS fallback was enabled without a host allowlist; "
                "fallback remains disabled"
            )

    def _session(self) -> requests.Session:
        if self._injected_session is not None:
            if isinstance(self._injected_session, requests.Session):
                self._configure_pinned_session(self._injected_session)
            return self._injected_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._configure_pinned_session(session)
            session.headers.update(
                {
                    "User-Agent": self.settings.user_agent,
                    "Accept": (
                        "text/html,application/xhtml+xml,application/json,"
                        "application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5"
                    ),
                }
            )
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.add(session)
        return session

    def _configure_pinned_session(self, session: requests.Session) -> None:
        session.trust_env = False
        if isinstance(session.get_adapter("https://"), _PinnedHTTPAdapter):
            return
        # Environment proxies perform their own DNS resolution and therefore
        # cannot provide the pinning guarantee. Board collection is anonymous
        # direct HTTP(S), so explicitly disable proxy inheritance.
        adapter = _PinnedHTTPAdapter(
            pool_connections=max(4, self.settings.global_concurrency),
            pool_maxsize=max(4, self.settings.global_concurrency),
            max_retries=0,
            pool_block=True,
            verified_ssl_context=self._verified_ssl_context,
            native_ssl_context=self._native_ssl_context,
            native_trust_hosts=self.settings.native_trust_hosts,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    @classmethod
    def _host_state(cls, host: str, capacity: int) -> _HostState:
        with cls._states_lock:
            state = cls._host_states.get(host)
            if state is None:
                state = _HostState(capacity=capacity)
                cls._host_states[host] = state
            return state

    @classmethod
    def _global_gate(cls, capacity: int) -> threading.BoundedSemaphore:
        with cls._states_lock:
            return cls._global_gates.setdefault(capacity, threading.BoundedSemaphore(capacity))

    def _wait_for_host(self, state: _HostState) -> None:
        if self.settings.delay_seconds <= 0:
            return
        with state.throttle_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, state.next_request_at - now)
            if wait_seconds:
                time.sleep(wait_seconds)
                now = time.monotonic()
            state.next_request_at = now + self.settings.delay_seconds

    def validate_target_url(self, url: str) -> str:
        """Return a canonical target only after applying the client's network policy.

        Browser-backed adapters can use this before allowing Playwright routes.
        """

        return validate_public_url(
            url,
            allow_private_networks=self.settings.allow_private_networks,
            ipv4_only=self.settings.ipv4_only,
        )

    def validated_connection_target(self, url: str) -> tuple[str, tuple[str, ...]]:
        """Return the canonical URL and exact addresses approved for a socket."""

        return self._validated_target(url)

    def _validated_target(self, url: str) -> tuple[str, tuple[str, ...]]:
        return _validated_public_target(
            url,
            allow_private_networks=self.settings.allow_private_networks,
            ipv4_only=self.settings.ipv4_only,
        )

    def _validate_target(self, url: str) -> str:
        return self.validate_target_url(url)

    def redirect_allowed(self, source_url: str, target_url: str) -> bool:
        source = self.validate_target_url(source_url)
        target = self.validate_target_url(target_url)
        return redirect_target_allowed(source, target)

    def _insecure_fallback_host_allowed(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        for allowed in self.settings.insecure_ssl_fallback_hosts:
            if host == allowed:
                return True
            if _literal_ip(allowed) is None and "." in allowed and host.endswith(f".{allowed}"):
                return True
        return False

    def _perform_get(
        self,
        session: requests.Session,
        url: str,
        resolved_address: str,
        **kwargs: Any,
    ) -> tuple[requests.Response, str]:
        if urlsplit(url).scheme.casefold() != "https":
            raise InvalidPublicURL("Board HTTP requests require HTTPS.")
        request_headers = dict(kwargs.pop("headers", {}) or {})
        request_headers.setdefault("Host", urlsplit(url).netloc)
        kwargs["headers"] = request_headers
        if isinstance(session, requests.Session):
            adapter = session.get_adapter(url)
            if not isinstance(adapter, _PinnedHTTPAdapter):
                raise BoardHTTPError("Board HTTP session is missing its pinned transport.")
            adapter.pin(url, resolved_address)
        try:
            response = session.get(url, verify=self.settings.verify_ssl, **kwargs)
            if urlsplit(url).scheme != "https":
                return response, "not_applicable"
            return response, "verified" if self.settings.verify_ssl else "disabled_by_config"
        except SSLError as exc:
            if (
                not self.settings.verify_ssl
                or not self.settings.allow_insecure_ssl_fallback
                or not self._insecure_fallback_host_allowed(url)
            ):
                raise
            LOGGER.warning(
                "SSL verification failed for %s; explicit insecure fallback is enabled (%s)",
                url,
                type(exc).__name__,
            )
            canonical, addresses = self._validated_target(url)
            if canonical != url or resolved_address not in addresses:
                raise InvalidPublicURL("TLS fallback target no longer matches its validated pin.")
            if isinstance(session, requests.Session):
                adapter = session.get_adapter(url)
                if not isinstance(adapter, _PinnedHTTPAdapter):
                    raise BoardHTTPError("Board HTTP session is missing its pinned transport.")
                adapter.pin(url, resolved_address)
            return session.get(url, verify=False, **kwargs), "explicit_fallback"

    @staticmethod
    def _redirect_headers(
        headers: Mapping[str, str],
        source_url: str,
        target_url: str,
    ) -> dict[str, str]:
        if (urlsplit(source_url).hostname or "") == (urlsplit(target_url).hostname or ""):
            return dict(headers)
        return {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).casefold() not in SENSITIVE_REDIRECT_HEADERS
            and str(key).casefold() not in {"if-none-match", "if-modified-since"}
        }

    @staticmethod
    def _cache_result_size(result: HTTPResult) -> int:
        metadata = (
            result.requested_url,
            result.final_url,
            result.tls_mode,
            *result.redirect_chain,
            *result.insecure_tls_hosts,
            *(f"{key}:{value}" for key, value in result.headers.items()),
        )
        return len(result.content) + sum(
            len(str(value).encode("utf-8", errors="replace")) for value in metadata
        )

    def _cached_result_locked(self, cache_key: tuple[Any, ...]) -> HTTPResult | None:
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
        return cached

    def _cache_result_locked(
        self,
        cache_key: tuple[Any, ...],
        result: HTTPResult,
    ) -> None:
        result = self._copy_result(result, from_cache=False)
        existing = self._cache.pop(cache_key, None)
        if existing is not None:
            self._cache_bytes -= self._cache_result_size(existing)
        size = self._cache_result_size(result)
        if (
            self.settings.cache_max_entries <= 0
            or self.settings.cache_max_bytes <= 0
            or size > self.settings.cache_max_bytes
        ):
            return
        while self._cache and (
            len(self._cache) >= self.settings.cache_max_entries
            or self._cache_bytes + size > self.settings.cache_max_bytes
        ):
            _old_key, old_result = self._cache.popitem(last=False)
            self._cache_bytes -= self._cache_result_size(old_result)
        self._cache[cache_key] = result
        self._cache_bytes += size

    def _response_limit(self, url: str, response: requests.Response, max_bytes: int | None) -> int:
        if max_bytes is not None:
            return max(1, int(max_bytes))
        content_type = response.headers.get("Content-Type", "").casefold()
        path = urlsplit(response.url or url).path.casefold()
        is_document = any(
            marker in content_type for marker in ("pdf", "word", "officedocument", "octet-stream")
        ) or path.endswith((".pdf", ".doc", ".docx", ".rtf"))
        return self.settings.max_document_size_bytes if is_document else self.settings.max_html_size_bytes

    def _read_limited(
        self,
        response: requests.Response,
        url: str,
        max_bytes: int | None,
    ) -> bytes:
        limit = self._response_limit(url, response, max_bytes)
        content_length = response.headers.get("Content-Length", "").strip()
        if content_length.isdigit() and int(content_length) > limit:
            response.close()
            raise ResponseTooLarge(
                f"Response declared {content_length} bytes, exceeding the {limit}-byte limit: {url}"
            )
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > limit:
                response.close()
                raise ResponseTooLarge(f"Response exceeded the {limit}-byte limit: {url}")
        return bytes(content)

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                return min(float(retry_after), self.settings.backoff_max_seconds)
            if retry_after:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    return min(
                        max(0.0, (when - datetime.now(timezone.utc)).total_seconds()),
                        self.settings.backoff_max_seconds,
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(
            self.settings.backoff_base_seconds * (2**attempt),
            self.settings.backoff_max_seconds,
        )

    def _robots_parser(self, url: str) -> robotparser.RobotFileParser | None:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        robots_url = f"{origin}/robots.txt"
        parser: robotparser.RobotFileParser | None = None
        try:
            result = self._request(
                robots_url,
                headers=None,
                max_bytes=min(self.settings.max_html_size_bytes, 1024 * 1024),
                check_robots=False,
                raise_for_status=False,
                allow_district_website_move=False,
            )
            if result.status_code < 400:
                parser = robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(result.text.splitlines())
        except (requests.RequestException, BoardHTTPError):
            LOGGER.info("robots.txt fetch failed for %s", origin, exc_info=True)
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        url = self._validate_target(url)
        if not self.settings.respect_robots:
            return True
        parser = self._robots_parser(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.settings.user_agent, url)
        except Exception:
            return True

    @contextmanager
    def host_slot(self, url: str) -> Iterator[None]:
        """Apply the same global/per-host gates to a non-requests fetch.

        This is used only for the bounded Playwright fallback.
        """

        canonical = self._validate_target(url)
        host = urlsplit(canonical).hostname or ""
        state = self._host_state(host, self.settings.per_host_concurrency)
        global_gate = self._global_gate(self.settings.global_concurrency)
        with global_gate, state.semaphore:
            self._wait_for_host(state)
            yield

    @contextmanager
    def browser_slot(self, url: str) -> Iterator[None]:
        """Serialize heavyweight Chromium fallbacks without blocking HTTP gates."""

        self._validate_target(url)
        with self._browser_gate:
            yield

    def _request(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        max_bytes: int | None,
        check_robots: bool,
        raise_for_status: bool,
        allow_district_website_move: bool = False,
    ) -> HTTPResult:
        requested_url, _requested_addresses = self._validated_target(url)
        current_url = requested_url
        request_headers = dict(headers or {})
        redirect_chain: list[str] = []
        visited = {requested_url}
        website_migration_accepted = False
        insecure_tls = False
        tls_modes: set[str] = set()
        insecure_tls_hosts: list[str] = []
        started = time.monotonic()
        last_exception: requests.RequestException | None = None
        while True:
            # Resolve again at every hop. This both validates newly redirected
            # hosts and narrows the DNS-rebinding window for retries.
            current_url, resolved_addresses = self._validated_target(current_url)
            if check_robots and not self.can_fetch(current_url):
                raise RobotsDenied(f"robots.txt disallows {current_url}")
            host = urlsplit(current_url).hostname or ""
            host_state = self._host_state(host, self.settings.per_host_concurrency)
            global_gate = self._global_gate(self.settings.global_concurrency)
            redirect_target: str | None = None

            attempt_limit = max(
                self.settings.max_retries,
                len(resolved_addresses) - 1,
            )
            for attempt in range(attempt_limit + 1):
                response: requests.Response | None = None
                try:
                    current_url, resolved_addresses = self._validated_target(current_url)
                    resolved_address = resolved_addresses[attempt % len(resolved_addresses)]
                    with global_gate, host_state.semaphore:
                        self._wait_for_host(host_state)
                        response, hop_tls_mode = self._perform_get(
                            self._session(),
                            current_url,
                            resolved_address,
                            headers=dict(request_headers),
                            timeout=self.settings.timeout_seconds,
                            allow_redirects=False,
                            stream=True,
                        )
                        retry_response = (
                            response.status_code in TRANSIENT_STATUS_CODES
                            and attempt < self.settings.max_retries
                        )
                        location = response.headers.get("Location", "").strip()
                        redirect_response = (
                            response.status_code in REDIRECT_STATUS_CODES and bool(location)
                        )
                        content = (
                            b""
                            if retry_response or redirect_response
                            else self._read_limited(response, current_url, max_bytes)
                        )
                    tls_modes.add(hop_tls_mode)
                    if hop_tls_mode in {"disabled_by_config", "explicit_fallback"}:
                        insecure_tls = True
                        insecure_host = urlsplit(current_url).hostname or ""
                        if insecure_host and insecure_host not in insecure_tls_hosts:
                            insecure_tls_hosts.append(insecure_host)
                    if retry_response:
                        delay = self._retry_delay(response, attempt)
                        response.close()
                        if delay:
                            time.sleep(delay)
                        continue

                    if redirect_response:
                        response.close()
                        if len(redirect_chain) >= self.settings.max_redirects:
                            raise TooManyRedirects(
                                f"Response exceeded {self.settings.max_redirects} redirects: "
                                f"{requested_url}"
                            )
                        raw_redirect_target = urljoin(current_url, location)
                        if urlsplit(raw_redirect_target).scheme.casefold() != "https":
                            raise RedirectDenied(
                                "Board HTTP does not follow redirects to insecure HTTP: "
                                f"{current_url} -> {raw_redirect_target}"
                            )
                        redirect_target = self._validate_target(raw_redirect_target)
                        if not self.redirect_allowed(current_url, redirect_target):
                            # This exception is intentionally narrower than the
                            # ordinary redirect policy. Discovery enables it only
                            # for the configured district homepage. The source
                            # must still be inside that original boundary, and a
                            # second boundary change is never accepted.
                            may_accept_district_website_move = (
                                allow_district_website_move
                                and not website_migration_accepted
                                and self.redirect_allowed(requested_url, current_url)
                            )
                            if not may_accept_district_website_move:
                                raise RedirectDenied(
                                    f"Redirect left the allowed organization/vendor boundary: "
                                    f"{current_url} -> {redirect_target}"
                                )
                            website_migration_accepted = True
                        if redirect_target in visited:
                            raise TooManyRedirects(
                                f"Redirect loop detected: {current_url} -> {redirect_target}"
                            )
                        request_headers = self._redirect_headers(
                            request_headers,
                            current_url,
                            redirect_target,
                        )
                        visited.add(redirect_target)
                        redirect_chain.append(redirect_target)
                        break
                    result = HTTPResult(
                        requested_url=requested_url,
                        final_url=current_url,
                        status_code=int(response.status_code),
                        headers={
                            str(key): str(value) for key, value in response.headers.items()
                        },
                        content=content,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                        redirect_chain=tuple(redirect_chain),
                        website_migration_accepted=website_migration_accepted,
                        insecure_tls=insecure_tls,
                        tls_mode=(
                            "explicit_fallback"
                            if "explicit_fallback" in tls_modes
                            else "disabled_by_config"
                            if "disabled_by_config" in tls_modes
                            else "verified"
                            if "verified" in tls_modes
                            else "not_applicable"
                        ),
                        insecure_tls_hosts=tuple(insecure_tls_hosts),
                    )
                    response.close()
                    if raise_for_status:
                        result.raise_for_status()
                    return result
                except (
                    requests.ConnectionError,
                    requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                ) as exc:
                    last_exception = exc
                    if response is not None:
                        response.close()
                    if attempt >= attempt_limit:
                        raise
                    delay = self._retry_delay(response, attempt)
                    if delay:
                        time.sleep(delay)
                except Exception:
                    if response is not None:
                        response.close()
                    raise
            if redirect_target is not None:
                current_url = redirect_target
                continue
            break
        if last_exception is not None:
            raise last_exception
        raise BoardHTTPError(f"Unable to fetch {requested_url}")

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        force: bool = False,
        check_robots: bool = True,
        raise_for_status: bool = True,
        allow_district_website_move: bool = False,
    ) -> HTTPResult:
        request_headers = dict(headers or {})
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        canonical = self._validate_target(url)
        cache_key = (
            canonical,
            tuple(
                sorted((str(key).casefold(), str(value)) for key, value in request_headers.items())
            ),
            max_bytes,
            check_robots,
            raise_for_status,
            allow_district_website_move,
        )
        pending: _PendingRequest | None = None
        owns_request = True
        if not force:
            with self._cache_lock:
                cached = self._cached_result_locked(cache_key)
                if cached is not None:
                    return self._copy_result(cached, from_cache=True)
                pending = self._inflight.get(cache_key)
                if pending is None:
                    pending = _PendingRequest()
                    self._inflight[cache_key] = pending
                else:
                    owns_request = False
            if not owns_request:
                pending.event.wait()
                if pending.error is not None:
                    raise pending.error
                if pending.result is None:
                    raise BoardHTTPError(f"Concurrent fetch did not produce a result for {canonical}")
                return self._copy_result(pending.result, from_cache=True)

        try:
            result = self._request(
                canonical,
                headers=request_headers,
                max_bytes=max_bytes,
                check_robots=check_robots,
                raise_for_status=raise_for_status,
                allow_district_website_move=allow_district_website_move,
            )
            if result.status_code < 400 or result.status_code == 304:
                with self._cache_lock:
                    self._cache_result_locked(cache_key, result)
            if pending is not None:
                pending.result = result
            return result
        except BaseException as exc:
            if pending is not None:
                pending.error = exc
            raise
        finally:
            if pending is not None:
                with self._cache_lock:
                    if self._inflight.get(cache_key) is pending:
                        self._inflight.pop(cache_key, None)
                pending.event.set()

    @staticmethod
    def _copy_result(result: HTTPResult, *, from_cache: bool) -> HTTPResult:
        return HTTPResult(
            requested_url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            headers=dict(result.headers),
            content=result.content,
            from_cache=from_cache,
            elapsed_seconds=result.elapsed_seconds,
            redirect_chain=result.redirect_chain,
            website_migration_accepted=result.website_migration_accepted,
            insecure_tls=result.insecure_tls,
            tls_mode=result.tls_mode,
            insecure_tls_hosts=result.insecure_tls_hosts,
        )

    def get_json(self, url: str, **kwargs: Any) -> tuple[HTTPResult, Any]:
        result = self.get(url, **kwargs)
        return result, result.json()

    def clear_run_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0

    def cache_info(self) -> dict[str, int]:
        with self._cache_lock:
            return {
                "entries": len(self._cache),
                "bytes": self._cache_bytes,
                "max_entries": self.settings.cache_max_entries,
                "max_bytes": self.settings.cache_max_bytes,
            }

    def close(self) -> None:
        if self._injected_session is not None:
            self._injected_session.close()
            return
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    def __enter__(self) -> "BoardHTTPClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = [
    "BoardHTTPClient",
    "BoardHTTPError",
    "BoardHTTPSettings",
    "HTTPResult",
    "InvalidPublicURL",
    "RedirectDenied",
    "ResponseTooLarge",
    "RobotsDenied",
    "TooManyRedirects",
    "canonical_public_url",
    "redirect_target_allowed",
    "validate_public_url",
]
