from __future__ import annotations

import codecs
import hashlib
import io
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader


DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 48_000
DEFAULT_MAX_REDIRECTS = 4
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 12.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"text/plain"})
_PDF_TYPES = frozenset({"application/pdf"})
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
        "metadata.azure.internal",
    }
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home",
    ".lan",
)


class ExampleContentError(RuntimeError):
    """Base error for bounded example-URL retrieval."""


class UnsafeExampleURLError(ExampleContentError):
    """The URL or one of its resolved destinations is not publicly routable."""


class ExampleContentTooLarge(ExampleContentError):
    """The response exceeded the configured byte or extracted-text limit."""


class UnsupportedExampleContent(ExampleContentError):
    """The response does not have a supported, useful content type."""


class ExampleHTTPError(ExampleContentError):
    """The public endpoint returned an unsuccessful response."""


@dataclass(frozen=True, slots=True)
class ExampleContent:
    original_url: str
    final_url: str
    content_type: str
    text: str
    bytes_read: int
    redirect_chain: tuple[str, ...]
    content_sha256: str


class ExampleHTTPResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterable[bytes]: ...

    def close(self) -> None: ...


class ExampleHTTPTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        resolved_addresses: Sequence[str],
        headers: Mapping[str, str],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> ExampleHTTPResponse: ...


Resolver = Callable[[str, int], Sequence[str]]


class _PinnedResponse:
    def __init__(self, response: urllib3.response.BaseHTTPResponse, pool: object) -> None:
        self._response = response
        self._pool = pool
        self.status = int(response.status)
        self.headers = response.headers

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        yield from self._response.stream(max(1, int(chunk_size)), decode_content=True)

    def close(self) -> None:
        try:
            self._response.release_conn()
            self._response.close()
        finally:
            close = getattr(self._pool, "close", None)
            if callable(close):
                close()


class PinnedUrllib3Transport:
    """Connect to a previously validated IP while preserving Host and TLS SNI.

    Resolving and pinning the connection target outside the HTTP library prevents a
    second, unvalidated DNS lookup between validation and connection.  Redirects are
    deliberately disabled here and are handled by :class:`ExampleContentFetcher`.
    """

    def get(
        self,
        url: str,
        *,
        resolved_addresses: Sequence[str],
        headers: Mapping[str, str],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> ExampleHTTPResponse:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        host_header = _host_header(hostname, port, parsed.scheme)
        request_headers = dict(headers)
        request_headers["Host"] = host_header
        timeout = urllib3.Timeout(
            connect=max(0.1, float(connect_timeout_seconds)),
            read=max(0.1, float(read_timeout_seconds)),
        )

        last_error: Exception | None = None
        for address in resolved_addresses:
            if parsed.scheme == "https":
                pool: object = urllib3.HTTPSConnectionPool(
                    address,
                    port=port,
                    timeout=timeout,
                    maxsize=1,
                    block=True,
                    retries=False,
                    cert_reqs="CERT_REQUIRED",
                    ca_certs=requests.certs.where(),
                    assert_hostname=hostname,
                    server_hostname=hostname,
                )
            else:
                pool = urllib3.HTTPConnectionPool(
                    address,
                    port=port,
                    timeout=timeout,
                    maxsize=1,
                    block=True,
                    retries=False,
                )
            try:
                response = pool.urlopen(  # type: ignore[attr-defined]
                    "GET",
                    request_target,
                    headers=request_headers,
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    decode_content=True,
                    timeout=timeout,
                )
                return _PinnedResponse(response, pool)
            except Exception as exc:
                last_error = exc
                close = getattr(pool, "close", None)
                if callable(close):
                    close()
        if last_error is not None:
            raise last_error
        raise ExampleContentError("The URL did not resolve to a usable public address.")


def _host_header(hostname: str, port: int, scheme: str) -> str:
    rendered = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return rendered if port == default_port else f"{rendered}:{port}"


def _system_resolver(hostname: str, port: int) -> Sequence[str]:
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeExampleURLError("The example URL host could not be resolved.") from exc
    addresses: list[str] = []
    for record in records:
        address = str(record[4][0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _canonical_public_url(value: str) -> tuple[str, str, int]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise UnsafeExampleURLError("A reasonably sized public HTTP or HTTPS URL is required.")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw):
        raise UnsafeExampleURLError("Whitespace and control characters are not allowed in URLs.")
    if "\\" in raw:
        raise UnsafeExampleURLError("Backslashes are not allowed in URLs.")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeExampleURLError("The example URL is malformed.") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise UnsafeExampleURLError("Only public HTTP and HTTPS example URLs are allowed.")
    if not parsed.netloc or not parsed.hostname:
        raise UnsafeExampleURLError("The example URL must include a public host.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsafeExampleURLError("Credentials embedded in example URLs are not allowed.")

    hostname = parsed.hostname.rstrip(".").casefold()
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeExampleURLError("The example URL host is invalid.") from exc
    if not hostname or hostname in _BLOCKED_HOSTS or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeExampleURLError("Local and metadata hosts are not allowed.")
    if "." not in hostname:
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise UnsafeExampleURLError("Single-label and local hostnames are not allowed.") from exc
    effective_port = port or (443 if scheme == "https" else 80)
    if effective_port < 1 or effective_port > 65535:
        raise UnsafeExampleURLError("The example URL port is invalid.")

    netloc = _host_header(hostname, effective_port, scheme)
    canonical = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
    return canonical, hostname, effective_port


def _validated_addresses(
    hostname: str,
    port: int,
    resolver: Resolver,
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        values = resolver(hostname, port)
    else:
        values = (str(literal),)
    addresses: list[str] = []
    for raw_address in values:
        try:
            address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeExampleURLError("DNS returned an invalid destination address.") from exc
        transition_address = isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
        )
        site_local = isinstance(address, ipaddress.IPv6Address) and address.is_site_local
        # `is_global` alone is insufficient: Python classifies multicast as
        # global. Accept only ordinary public unicast addresses and reject
        # transition forms that can conceal an embedded IPv4 destination.
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or site_local
            or transition_address
        ):
            raise UnsafeExampleURLError(
                "The example URL resolves to a non-public destination address."
            )
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise UnsafeExampleURLError("The example URL host did not resolve to a public address.")
    return tuple(addresses)


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value or "").strip()
    return ""


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


def _charset(content_type: str) -> str:
    match = re.search(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"']+)", content_type, re.I)
    candidate = match.group(1).strip() if match else "utf-8"
    try:
        return codecs.lookup(candidate).name
    except LookupError:
        return "utf-8"


def _bounded_body(response: ExampleHTTPResponse, maximum: int) -> bytes:
    length_header = _header(response.headers, "Content-Length")
    if length_header:
        try:
            announced = int(length_header)
        except ValueError:
            announced = -1
        if announced > maximum:
            raise ExampleContentTooLarge(
                f"Example content exceeds the {maximum}-byte response limit."
            )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(min(64 * 1024, maximum + 1)):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ExampleContentError("The example response yielded invalid byte content.")
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise ExampleContentTooLarge(
                f"Example content exceeds the {maximum}-byte response limit."
            )
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _extract_html(content: bytes, maximum_chars: int) -> str:
    soup = BeautifulSoup(content, "lxml")
    for node in soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    return _bounded_text(text, maximum_chars)


def _extract_pdf(content: bytes, maximum_chars: int, maximum_pages: int) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        parts: list[str] = []
        used = 0
        for page in reader.pages[:maximum_pages]:
            try:
                part = page.extract_text() or ""
            except Exception:
                continue
            remaining = maximum_chars - used
            if remaining <= 0:
                break
            parts.append(part[:remaining])
            used += len(part[:remaining])
        return _bounded_text("\n".join(parts), maximum_chars)
    except ExampleContentError:
        raise
    except Exception as exc:
        raise UnsupportedExampleContent("The PDF example could not be safely extracted.") from exc


def _bounded_text(text: str, maximum_chars: int) -> str:
    normalized = re.sub(r"[\t\x0b\x0c\r ]+", " ", str(text or ""))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) > maximum_chars:
        normalized = normalized[:maximum_chars].rstrip()
    if not normalized:
        raise UnsupportedExampleContent("The example did not contain extractable text.")
    return normalized


class ExampleContentFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: ExampleHTTPTransport | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_pdf_pages: int = 25,
    ) -> None:
        self._resolver = resolver or _system_resolver
        self._transport = transport or PinnedUrllib3Transport()
        self.max_response_bytes = max(1, min(int(max_response_bytes), 10 * 1024 * 1024))
        self.max_text_chars = max(1, min(int(max_text_chars), 100_000))
        self.max_redirects = max(0, min(int(max_redirects), 8))
        self.connect_timeout_seconds = max(0.1, min(float(connect_timeout_seconds), 30.0))
        self.read_timeout_seconds = max(0.1, min(float(read_timeout_seconds), 60.0))
        self.max_pdf_pages = max(1, min(int(max_pdf_pages), 50))

    def fetch(self, url: str) -> ExampleContent:
        original_url, _, _ = _canonical_public_url(url)
        current_url = original_url
        redirect_chain: list[str] = []
        seen: set[str] = set()

        for hop in range(self.max_redirects + 1):
            current_url, hostname, port = _canonical_public_url(current_url)
            if current_url in seen:
                raise UnsafeExampleURLError("The example URL entered a redirect loop.")
            seen.add(current_url)
            addresses = _validated_addresses(hostname, port, self._resolver)
            try:
                response = self._transport.get(
                    current_url,
                    resolved_addresses=addresses,
                    headers={
                        "Accept": "text/html, application/xhtml+xml, application/pdf, text/plain;q=0.8",
                        "Accept-Encoding": "gzip, deflate",
                        "User-Agent": "EdScanner-GuidedSearch/1.0",
                    },
                    connect_timeout_seconds=self.connect_timeout_seconds,
                    read_timeout_seconds=self.read_timeout_seconds,
                )
            except ExampleContentError:
                raise
            except Exception as exc:
                raise ExampleContentError("The public example URL could not be retrieved.") from exc

            try:
                status = int(response.status)
                if status in _REDIRECT_STATUSES:
                    location = _header(response.headers, "Location")
                    if not location:
                        raise ExampleHTTPError("The example endpoint returned an invalid redirect.")
                    if hop >= self.max_redirects:
                        raise UnsafeExampleURLError("The example URL exceeded the redirect limit.")
                    target = urljoin(current_url, location)
                    target, _, _ = _canonical_public_url(target)
                    redirect_chain.append(target)
                    current_url = target
                    continue
                if status < 200 or status >= 300:
                    raise ExampleHTTPError(f"The example endpoint returned HTTP {status}.")

                raw_content_type = _header(response.headers, "Content-Type")
                content_type = _media_type(raw_content_type)
                supported = _HTML_TYPES | _TEXT_TYPES | _PDF_TYPES
                if content_type not in supported:
                    raise UnsupportedExampleContent(
                        "Example URLs must return HTML, plain text, or PDF content."
                    )
                body = _bounded_body(response, self.max_response_bytes)
                if content_type in _HTML_TYPES:
                    text = _extract_html(body, self.max_text_chars)
                elif content_type in _PDF_TYPES:
                    text = _extract_pdf(body, self.max_text_chars, self.max_pdf_pages)
                else:
                    decoded = body.decode(_charset(raw_content_type), errors="replace")
                    text = _bounded_text(decoded, self.max_text_chars)
                return ExampleContent(
                    original_url=original_url,
                    final_url=current_url,
                    content_type=content_type,
                    text=text,
                    bytes_read=len(body),
                    redirect_chain=tuple(redirect_chain),
                    content_sha256=hashlib.sha256(body).hexdigest(),
                )
            finally:
                response.close()

        raise UnsafeExampleURLError("The example URL exceeded the redirect limit.")


def fetch_example_content(
    url: str,
    *,
    resolver: Resolver | None = None,
    transport: ExampleHTTPTransport | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> ExampleContent:
    return ExampleContentFetcher(
        resolver=resolver,
        transport=transport,
        max_response_bytes=max_response_bytes,
        max_text_chars=max_text_chars,
        max_redirects=max_redirects,
    ).fetch(url)


__all__ = [
    "ExampleContent",
    "ExampleContentError",
    "ExampleContentFetcher",
    "ExampleContentTooLarge",
    "ExampleHTTPError",
    "ExampleHTTPResponse",
    "ExampleHTTPTransport",
    "PinnedUrllib3Transport",
    "Resolver",
    "UnsafeExampleURLError",
    "UnsupportedExampleContent",
    "fetch_example_content",
]
