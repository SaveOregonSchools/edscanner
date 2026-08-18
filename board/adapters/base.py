from __future__ import annotations

import html as html_lib
import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from board.http import (
    BoardHTTPClient,
    HTTPResult,
    InvalidPublicURL,
    ResponseTooLarge,
    RobotsDenied,
    canonical_public_url,
)
from board.models import (
    BoardSource,
    BoardSourceResult,
    DetectionResult,
    DocumentRef,
    DownloadedDocument,
    MeetingRef,
    NormalizedMeeting,
    meeting_ref_from_mapping,
    source_from_mapping,
)
from common import utc_now_iso


LOGGER = logging.getLogger(__name__)
Content = bytes | str
SourceLike = BoardSource | Mapping[str, Any]
MeetingLike = MeetingRef | Mapping[str, Any]

_MONTH_DATE_FORMATS = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
)
_DATE_SEARCH = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_TIME_SEARCH = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))\b", re.IGNORECASE)
_DOTNET_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", re.IGNORECASE)


def collapse_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()


def content_bytes(value: Content | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def content_text(value: Content | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def html_soup(value: Content) -> BeautifulSoup:
    return BeautifulSoup(value, "lxml")


def public_absolute_url(base_url: str, candidate: str | None) -> str | None:
    if not candidate:
        return None
    try:
        return canonical_public_url(urljoin(base_url, candidate.strip()))
    except (InvalidPublicURL, TypeError, ValueError):
        return None


def origin_for(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def core_host(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    value = str(host or "").casefold().strip(".")
    return value[4:] if value.startswith("www.") else value


def same_host_or_subdomain(url: str, base_url: str) -> bool:
    host = core_host(url)
    base_host = core_host(base_url)
    return bool(host and base_host and (host == base_host or host.endswith(f".{base_host}")))


def query_value(url: str, *names: str) -> str | None:
    values = parse_qs(urlsplit(url).query)
    folded = {key.casefold(): items for key, items in values.items()}
    for name in names:
        found = folded.get(name.casefold())
        if found and found[0] != "":
            return str(found[0])
    return None


def parse_date_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = collapse_ws(value)
    dotnet = _DOTNET_DATE.search(text)
    if dotnet:
        return parse_date_value(int(dotnet.group(1)))
    if re.fullmatch(r"-?\d{10}(?:\d{3})?", text):
        return parse_date_value(int(text))
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date().isoformat()
    except ValueError:
        pass
    match = _DATE_SEARCH.search(text)
    if not match:
        return None
    candidate = re.sub(r"(?<=\d),(?=\s*\d{4})", "", match.group(0))
    for fmt in _MONTH_DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time_value(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0).isoformat()
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).time().replace(microsecond=0).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = collapse_ws(value)
    if not text:
        return None
    dotnet = _DOTNET_DATE.search(text)
    if dotnet:
        return parse_time_value(int(dotnet.group(1)))
    if re.fullmatch(r"-?\d{10}(?:\d{3})?", text):
        return parse_time_value(int(text))
    if re.search(r"\d{4}-\d{2}-\d{2}[T ]", text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.time().replace(microsecond=0).isoformat()
        except ValueError:
            pass
    match = _TIME_SEARCH.search(text)
    candidate = match.group(1) if match else text
    candidate = candidate.replace(".", "").upper().strip()
    for fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(candidate, fmt).time().replace(microsecond=0).isoformat()
        except ValueError:
            continue
    return None


def parse_meeting_datetime_text(value: Any) -> tuple[str | None, str | None]:
    text = collapse_ws(value)
    return parse_date_value(text), parse_time_value(text)


def since_date(value: date | datetime | str | None) -> str | None:
    return parse_date_value(value)


def meeting_is_on_or_after(meeting: MeetingRef, since: date | datetime | str | None) -> bool:
    cutoff = since_date(since)
    return not cutoff or not meeting.meeting_date or meeting.meeting_date >= cutoff


def document_type_from_text(title: str, url: str = "") -> str:
    text = f"{title} {url}".casefold()
    if "minute" in text:
        return "minutes"
    if "public notice" in text or "meeting notice" in text:
        return "notice"
    if "packet" in text:
        return "packet"
    if "agenda" in text:
        return "agenda"
    if "policy" in text:
        return "policy"
    if any(word in text for word in ("video", "recording", "livestream", "live stream")):
        return "video"
    return "attachment"


def content_type_from_url(url: str) -> str | None:
    suffix = PurePosixPath(urlsplit(url).path).suffix.casefold()
    return {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(suffix)


def dedupe_documents(documents: Iterable[DocumentRef]) -> list[DocumentRef]:
    out: list[DocumentRef] = []
    seen: set[tuple[str, str, str]] = set()
    for document in documents:
        key = document.identity_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(document)
    return out


def dedupe_meetings(meetings: Iterable[MeetingRef]) -> list[MeetingRef]:
    out: list[MeetingRef] = []
    seen: set[tuple[str, str]] = set()
    for meeting in meetings:
        key = (meeting.external_meeting_id, meeting.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(meeting)
    return out


def mapping_value(value: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    by_name = {str(key).casefold(): item for key, item in value.items()}
    for name in names:
        if name.casefold() in by_name:
            return by_name[name.casefold()]
    return default


def parse_bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = collapse_ws(value).casefold()
    if text in {"true", "1", "yes", "y", "on", "published"}:
        return True
    if text in {"false", "0", "no", "n", "off", "unpublished", "not published", ""}:
        return False
    return default


def nested_values(value: Any, keys: Sequence[str]) -> list[Any]:
    wanted = {key.casefold() for key in keys}
    found: list[Any] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in wanted:
                    found.append(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def looks_like_blocked_page(content: Content) -> bool:
    text = content_text(content).casefold()
    markers = (
        "captcha",
        "cf-chl-",
        "cloudflare ray id",
        "request unsuccessful",
        "_incapsula_resource",
        "access denied",
        "verify you are human",
    )
    return any(marker in text for marker in markers)


class BoardPlatformAdapter(ABC):
    platform_name = "unknown"
    requires_javascript = False

    def __init__(
        self,
        client: BoardHTTPClient | None = None,
        *,
        allow_browser_fallback: bool = False,
    ) -> None:
        self.client = client or BoardHTTPClient()
        self.allow_browser_fallback = bool(allow_browser_fallback)

    @abstractmethod
    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        raise NotImplementedError

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, Any] | None = None,
    ) -> BoardSource:
        detection = self.detect(url, content)
        metadata = dict(detection.metadata)
        organization_name = metadata.pop("organization_name", None)
        external_source_id = metadata.pop("external_source_id", None)
        if district:
            organization_name = organization_name or district.get("agency_name") or district.get("name")
        return BoardSource(
            platform=self.platform_name,
            public_url=detection.canonical_url or canonical_public_url(url),
            external_source_id=str(external_source_id) if external_source_id is not None else None,
            district_id=(int(district["id"]) if district and district.get("id") is not None else None),
            organization_name=organization_name,
            status="working" if detection.matched else "manual_review",
            requires_javascript=detection.requires_javascript,
            metadata=metadata,
        )

    def discover_source(
        self,
        district: Mapping[str, Any],
        candidate_url: str,
        html: Content | None = None,
    ) -> BoardSourceResult:
        final_url = candidate_url
        content = html
        try:
            if content is None:
                try:
                    response = self.fetch_url(candidate_url, raise_for_status=True)
                    final_url = response.final_url
                    content = response.content
                except Exception:
                    if not (self.allow_browser_fallback and self.requires_javascript):
                        raise
                    content = self.render_page(candidate_url)
            if (
                looks_like_blocked_page(content or b"")
                and self.allow_browser_fallback
                and self.requires_javascript
            ):
                content = self.render_page(final_url)
            detection = self.detect(final_url, content)
            if not detection.matched:
                return BoardSourceResult(
                    detection=detection,
                    source=None,
                    status="manual_review",
                    candidate_url=candidate_url,
                    error=detection.reason or "Candidate did not match this platform.",
                )
            if looks_like_blocked_page(content or b""):
                return BoardSourceResult(
                    detection=detection,
                    source=None,
                    status="manual_review",
                    candidate_url=candidate_url,
                    error="Public page returned a challenge or access-denied response.",
                )
            source = self.parse_source(content or b"", final_url, district)
            return BoardSourceResult(
                detection=detection,
                source=source,
                status=source.status,
                candidate_url=candidate_url,
            )
        except RobotsDenied as exc:
            detection = self.detect(candidate_url, html)
            return BoardSourceResult(
                detection=detection,
                source=None,
                status="blocked_by_robots",
                candidate_url=candidate_url,
                error=str(exc),
            )
        except Exception as exc:
            detection = self.detect(candidate_url, html)
            return BoardSourceResult(
                detection=detection,
                source=None,
                status="error",
                candidate_url=candidate_url,
                error=str(exc),
            )

    def meeting_listing_url(
        self,
        source: BoardSource,
        since: date | datetime | str | None = None,
    ) -> str:
        return source.public_url

    @abstractmethod
    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        raise NotImplementedError

    @abstractmethod
    def parse_meeting_detail(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        meeting_ref: MeetingLike | None = None,
    ) -> NormalizedMeeting:
        raise NotImplementedError

    def list_meetings(
        self,
        source: SourceLike,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        normalized_source = source_from_mapping(source)
        listing_url = self.meeting_listing_url(normalized_source, since)
        response = self.fetch_url(listing_url)
        meetings = self.parse_meeting_list(response.content, response.final_url, normalized_source, since)
        if (
            not meetings
            and self.allow_browser_fallback
            and (self.requires_javascript or normalized_source.requires_javascript)
        ):
            rendered = self.render_page(response.final_url)
            meetings = self.parse_meeting_list(rendered, response.final_url, normalized_source, since)
        return dedupe_meetings(meetings)

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        detail_url = normalized_ref.agenda_url or normalized_ref.url
        response = self.fetch_url(detail_url)
        meeting = self.parse_meeting_detail(
            response.content,
            response.final_url,
            normalized_source,
            normalized_ref,
        )
        if (
            not meeting.agenda_items
            and self.allow_browser_fallback
            and (self.requires_javascript or normalized_source.requires_javascript)
        ):
            rendered = self.render_page(response.final_url)
            meeting = self.parse_meeting_detail(rendered, response.final_url, normalized_source, normalized_ref)
        return meeting

    def fetch_url(self, url: str, **kwargs: Any) -> HTTPResult:
        return self.client.get(url, **kwargs)

    def fetch_document(self, document_ref: DocumentRef) -> DownloadedDocument:
        response = self.fetch_url(
            document_ref.url,
            max_bytes=self.client.settings.max_document_size_bytes,
        )
        return DownloadedDocument(
            document_ref=document_ref,
            content=response.content,
            final_url=response.final_url,
            status_code=response.status_code,
            content_type=response.content_type or document_ref.content_type,
            etag=response.etag,
            last_modified=response.last_modified,
            fetched_at=utc_now_iso(),
            metadata={
                "from_cache": response.from_cache,
                "redirect_chain": list(response.redirect_chain),
                "insecure_tls": response.insecure_tls,
                "tls_mode": response.tls_mode,
                "insecure_tls_hosts": list(response.insecure_tls_hosts),
            },
        )

    def _prepare_rendered_page(self, page: Any, url: str, timeout_ms: int) -> None:
        """Vendor hook for a minimal public-page interaction after navigation."""

    def render_page(self, url: str) -> bytes:
        if not self.allow_browser_fallback:
            raise RuntimeError("Browser fallback is disabled for this adapter.")
        if not self.client.can_fetch(url):
            raise RuntimeError(f"robots.txt disallows browser rendering for {url}")
        try:
            from board.browser_proxy import pinned_browser_proxy
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for this public page; install Chromium with "
                "'python -m playwright install chromium'."
            ) from exc

        timeout_ms = max(1000, int(self.client.settings.timeout_seconds * 1000))
        requested_url = self.client.validate_target_url(url)
        with (
            self.client.browser_slot(requested_url),
            pinned_browser_proxy(self.client) as proxy_server,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(
                headless=True,
                proxy={"server": proxy_server},
                args=[
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=self.client.settings.user_agent,
                    ignore_https_errors=not self.client.settings.verify_ssl,
                    accept_downloads=False,
                    service_workers="block",
                )

                def guard_public_route(route: Any) -> None:
                    request_url = str(route.request.url or "")
                    request_scheme = urlsplit(request_url).scheme.casefold()
                    if request_scheme in {"blob", "data"} or request_url in {
                        "about:blank",
                        "about:srcdoc",
                    }:
                        route.continue_()
                        return
                    if request_scheme not in {"http", "https"}:
                        LOGGER.warning(
                            "Blocked unsupported browser network scheme %s for %s",
                            request_scheme or "(missing)",
                            request_url,
                        )
                        route.abort("blockedbyclient")
                        return
                    try:
                        self.client.validate_target_url(request_url)
                    except InvalidPublicURL as exc:
                        LOGGER.warning(
                            "Blocked non-public browser subrequest %s: %s",
                            request_url,
                            exc,
                        )
                        route.abort("blockedbyclient")
                        return
                    route.continue_()

                context.route("**/*", guard_public_route)
                if not hasattr(context, "route_web_socket"):
                    raise RuntimeError(
                        "Secure browser rendering requires Playwright 1.48 or newer."
                    )
                # Public board parsing needs navigation/XHR, not a separate
                # WebSocket path. Fail closed even though the proxy also pins it.
                context.route_web_socket(
                    "**/*",
                    lambda socket_route: socket_route.close(
                        code=1008,
                        reason="WebSockets disabled during board rendering",
                    ),
                )
                page = context.new_page()
                page.goto(requested_url, wait_until="domcontentloaded", timeout=timeout_ms)
                final_url = self.client.validate_target_url(page.url)
                if not self.client.redirect_allowed(requested_url, final_url):
                    raise InvalidPublicURL(
                        f"Browser navigation left the allowed public boundary: "
                        f"{requested_url} -> {final_url}"
                    )
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
                except PlaywrightTimeoutError:
                    pass
                self._prepare_rendered_page(page, url, timeout_ms)
                final_url = self.client.validate_target_url(page.url)
                if not self.client.redirect_allowed(requested_url, final_url):
                    raise InvalidPublicURL(
                        f"Browser interaction left the allowed public boundary: "
                        f"{requested_url} -> {final_url}"
                    )
                content = page.content().encode("utf-8")
            finally:
                browser.close()
        if len(content) > self.client.settings.max_html_size_bytes:
            raise ResponseTooLarge(
                f"Rendered page exceeded {self.client.settings.max_html_size_bytes} bytes: {url}"
            )
        if looks_like_blocked_page(content):
            raise RuntimeError("Browser received an anti-bot challenge or access-denied page.")
        return content


__all__ = [
    "BoardPlatformAdapter",
    "Content",
    "MeetingLike",
    "SourceLike",
    "collapse_ws",
    "content_bytes",
    "content_text",
    "content_type_from_url",
    "core_host",
    "dedupe_documents",
    "dedupe_meetings",
    "document_type_from_text",
    "html_soup",
    "looks_like_blocked_page",
    "mapping_value",
    "meeting_is_on_or_after",
    "nested_values",
    "origin_for",
    "parse_bool_value",
    "parse_date_value",
    "parse_meeting_datetime_text",
    "parse_time_value",
    "public_absolute_url",
    "query_value",
    "same_host_or_subdomain",
    "since_date",
]
