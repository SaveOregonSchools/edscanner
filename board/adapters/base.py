from __future__ import annotations

import html as html_lib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from board.http import (
    BoardHTTPClient,
    BoardHTTPError,
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


@dataclass(frozen=True, slots=True)
class ChallengeAssessment:
    """Explain whether a response is an access challenge and how to recover.

    The recovery flag deliberately excludes rate limiting. A terminal HTTP 429
    should not cause EdScanner to add more traffic by starting a browser.
    """

    is_challenge: bool
    category: str = ""
    marker: str = ""
    browser_retry_allowed: bool = False


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """A bounded browser result with the final document provenance intact."""

    content: bytes
    final_url: str
    status_code: int
    browser_rendered: bool = False


_TITLE_PATTERN = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CHALLENGE_TITLE_MARKERS = (
    "access denied",
    "attention required",
    "checking your browser",
    "just a moment",
    "request unsuccessful",
    "verify you are human",
)


def assess_challenge(
    status_code: int,
    content: Content | None,
    url: str = "",
) -> ChallengeAssessment:
    """Classify an HTTP/browser response without matching incidental page text.

    A 200 page is considered challenged only when its title or a combination of
    provider-specific challenge markers is present. This avoids treating a
    normal news page, navigation item, or embedded script containing a phrase
    such as ``access denied`` as a block page.
    """

    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 0
    if status == 429:
        return ChallengeAssessment(True, "rate_limited", "http_429", False)
    if status in {401, 403}:
        return ChallengeAssessment(True, "http_access_denied", f"http_{status}", True)

    text = content_text(content)[:100_000].casefold()
    title_match = _TITLE_PATTERN.search(text)
    title = collapse_ws(title_match.group(1)) if title_match else ""
    for marker in _CHALLENGE_TITLE_MARKERS:
        if marker in title:
            return ChallengeAssessment(True, "challenge_title", marker, True)

    provider_checks = (
        (
            "cloudflare",
            "cf-chl-",
            ("challenge-platform", "checking your browser", "verify you are human"),
        ),
        (
            "cloudflare",
            "cloudflare ray id",
            ("attention required", "sorry, you have been blocked", "access denied"),
        ),
        (
            "incapsula",
            "_incapsula_resource",
            ("incident id", "request unsuccessful", "access denied"),
        ),
        (
            "human_verification",
            "verify you are human",
            ("captcha", "challenge", "turnstile"),
        ),
    )
    for category, primary, companions in provider_checks:
        if primary in text and any(companion in text for companion in companions):
            return ChallengeAssessment(True, category, primary, True)

    soup = BeautifulSoup(text, "lxml")
    heading = soup.find(["h1", "h2"])
    heading_text = collapse_ws(heading.get_text(" ", strip=True) if heading else "")
    if (
        heading_text in {
            "access denied",
            "request unsuccessful",
            "verify you are human",
            "checking your browser",
        }
        and len(collapse_ws(soup.get_text(" ", strip=True))) <= 2_000
    ):
        return ChallengeAssessment(True, "challenge_heading", heading_text, True)

    # A known vendor hostname alone is not enough to label a successful page
    # blocked. The URL remains part of this public API for future diagnostics.
    return ChallengeAssessment(False)


def looks_like_blocked_page(content: Content) -> bool:
    """Backward-compatible Boolean wrapper for rendered-page callers."""

    return assess_challenge(200, content).is_challenge


def _validate_browser_route(
    client: BoardHTTPClient,
    requested_url: str,
    request_url: str,
    *,
    main_frame_navigation: bool,
) -> str:
    """Validate a browser request before Playwright sends it.

    All browser traffic must remain on public addresses. Main-frame navigation
    additionally follows the ordinary HTTP client's organization/vendor redirect
    boundary; public CDN and API subrequests retain the existing public-address
    validation without being mistaken for top-level redirects.
    """

    target_url = client.validate_target_url(request_url)
    if main_frame_navigation and not client.redirect_allowed(requested_url, target_url):
        raise InvalidPublicURL(
            "Browser navigation left the allowed public boundary: "
            f"{requested_url} -> {target_url}"
        )
    if main_frame_navigation and not client.can_fetch(target_url):
        raise RobotsDenied(f"robots.txt disallows browser rendering for {target_url}")
    return target_url


def _browser_navigation_scope(request: Any, page: Any) -> str:
    """Classify a browser request without allowing a popup's first request.

    Playwright deliberately raises when ``request.frame`` is read for a popup's
    initial navigation. Browser-context routing sees that request early enough
    to abort it, whereas page-level routing does not. Child-frame documents stay
    subject to public-address validation but are not treated as top-level
    organization redirects.
    """

    if not request.is_navigation_request():
        return "subresource"
    try:
        frame = request.frame
    except Exception:
        return "popup"
    if frame == page.main_frame:
        return "main"
    try:
        owning_page = frame.page
    except Exception:
        return "popup"
    return "child" if owning_page is page else "popup"


def _main_frame_response_details(page: Any, response: Any) -> tuple[int, str] | None:
    """Return status/URL only for a response that committed the main document."""

    try:
        request = response.request
        if not request.is_navigation_request() or request.frame != page.main_frame:
            return None
        return int(response.status), str(response.url or "")
    except (AttributeError, TypeError, ValueError):
        return None


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
                    response = self.fetch_url(candidate_url, raise_for_status=False)
                    final_url = response.final_url
                    content = response.content
                    challenge = assess_challenge(
                        response.status_code,
                        content,
                        final_url,
                    )
                    if challenge.is_challenge:
                        if not (
                            self.allow_browser_fallback
                            and challenge.browser_retry_allowed
                        ):
                            return BoardSourceResult(
                                detection=self.detect(final_url, content),
                                source=None,
                                status="blocked_by_challenge",
                                candidate_url=candidate_url,
                                error=(
                                    "Public page returned an access challenge "
                                    f"({challenge.marker or challenge.category})."
                                ),
                            )
                        try:
                            rendered = self.render_page_with_metadata(final_url)
                            content = rendered.content
                            final_url = rendered.final_url
                        except RobotsDenied:
                            raise
                        except Exception as exc:
                            return BoardSourceResult(
                                detection=self.detect(final_url, content),
                                source=None,
                                status="blocked_by_challenge",
                                candidate_url=candidate_url,
                                error=(
                                    "Public page returned an access challenge and "
                                    f"browser recovery failed: {exc}"
                                ),
                            )
                    elif response.status_code >= 400:
                        response.raise_for_status()
                except RobotsDenied:
                    raise
                except Exception:
                    if not (self.allow_browser_fallback and self.requires_javascript):
                        raise
                    rendered = self.render_page_with_metadata(candidate_url)
                    content = rendered.content
                    final_url = rendered.final_url
            if (
                looks_like_blocked_page(content or b"")
                and self.allow_browser_fallback
            ):
                rendered = self.render_page_with_metadata(final_url)
                content = rendered.content
                final_url = rendered.final_url
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
                    status="blocked_by_challenge",
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
        page = self.fetch_page_with_browser_recovery(listing_url)
        meetings = self.parse_meeting_list(page.content, page.final_url, normalized_source, since)
        if (
            not meetings
            and self.allow_browser_fallback
            and (self.requires_javascript or normalized_source.requires_javascript)
            and not page.browser_rendered
        ):
            rendered = self.render_page_with_metadata(page.final_url)
            meetings = self.parse_meeting_list(
                rendered.content,
                rendered.final_url,
                normalized_source,
                since,
            )
        return dedupe_meetings(meetings)

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        detail_url = normalized_ref.agenda_url or normalized_ref.url
        page = self.fetch_page_with_browser_recovery(detail_url)
        meeting = self.parse_meeting_detail(
            page.content,
            page.final_url,
            normalized_source,
            normalized_ref,
        )
        if (
            not meeting.agenda_items
            and self.allow_browser_fallback
            and (self.requires_javascript or normalized_source.requires_javascript)
            and not page.browser_rendered
        ):
            rendered = self.render_page_with_metadata(page.final_url)
            meeting = self.parse_meeting_detail(
                rendered.content,
                rendered.final_url,
                normalized_source,
                normalized_ref,
            )
        return meeting

    def fetch_url(self, url: str, **kwargs: Any) -> HTTPResult:
        return self.client.get(url, **kwargs)

    def fetch_page_with_browser_recovery(self, url: str) -> RenderedPage:
        """Fetch one HTML/JSON page and recover one real access challenge.

        This is shared by discovery, meeting listings, and meeting details so a
        source that required Chromium during discovery remains usable during
        manual and scheduled syncs. Rate limits and robots denials never trigger
        the browser path.
        """

        response = self.fetch_url(url, raise_for_status=False)
        challenge = assess_challenge(
            response.status_code,
            response.content,
            response.final_url,
        )
        if challenge.is_challenge:
            if self.allow_browser_fallback and challenge.browser_retry_allowed:
                return self.render_page_with_metadata(response.final_url)
            if response.status_code >= 400:
                response.raise_for_status()
            raise BoardHTTPError(
                "Public page returned an access challenge "
                f"({challenge.marker or challenge.category}): {response.final_url}"
            )
        if response.status_code >= 400:
            response.raise_for_status()
        return RenderedPage(
            content=response.content,
            final_url=response.final_url,
            status_code=response.status_code,
            browser_rendered=False,
        )

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

    def render_page_with_metadata(self, url: str) -> RenderedPage:
        """Render a page while retaining its final URL and document status.

        Tests and specialized adapters may replace ``render_page`` with a
        lightweight implementation. Preserve that extension point and attach
        conservative metadata instead of bypassing the override.
        """

        render_method = self.render_page
        if getattr(render_method, "__func__", None) is not BoardPlatformAdapter.render_page:
            return RenderedPage(
                content=content_bytes(render_method(url)),
                final_url=canonical_public_url(url),
                status_code=200,
                browser_rendered=True,
            )
        return self._render_page_result(url)

    def render_page(self, url: str) -> bytes:
        """Backward-compatible bytes-only wrapper for rendered pages."""

        return self._render_page_result(url).content

    def _render_page_result(self, url: str) -> RenderedPage:
        if not self.allow_browser_fallback:
            raise RuntimeError("Browser fallback is disabled for this adapter.")
        if not self.client.can_fetch(url):
            raise RobotsDenied(f"robots.txt disallows browser rendering for {url}")
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
        navigation_status = 200
        navigation_seen = False
        navigation_policy_error: BoardHTTPError | None = None
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

                def track_main_frame_response(response: Any) -> None:
                    nonlocal navigation_seen, navigation_status
                    details = _main_frame_response_details(page, response)
                    if details is not None:
                        navigation_status = details[0]
                        navigation_seen = True

                def guard_public_route(route: Any) -> None:
                    nonlocal navigation_policy_error
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
                    navigation_scope = _browser_navigation_scope(route.request, page)
                    if navigation_scope == "popup":
                        LOGGER.info(
                            "Blocked popup navigation during board rendering: %s",
                            request_url,
                        )
                        route.abort("blockedbyclient")
                        return
                    try:
                        _validate_browser_route(
                            self.client,
                            requested_url,
                            request_url,
                            main_frame_navigation=navigation_scope == "main",
                        )
                    except RobotsDenied as exc:
                        if navigation_scope == "main":
                            navigation_policy_error = exc
                        LOGGER.info("Blocked browser navigation by robots policy: %s", exc)
                        route.abort("blockedbyclient")
                        return
                    except InvalidPublicURL as exc:
                        if navigation_scope == "main":
                            navigation_policy_error = exc
                        LOGGER.warning(
                            "Blocked browser request outside its public boundary %s: %s",
                            request_url,
                            exc,
                        )
                        route.abort("blockedbyclient")
                        return
                    route.continue_()

                page.on("response", track_main_frame_response)
                # Browser-context routing, unlike page routing, covers the
                # first request of a popup. It also lets us apply policy to
                # every matching main-document request in a redirect chain.
                context.route("**/*", guard_public_route)
                try:
                    navigation_response = page.goto(
                        requested_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except Exception as exc:
                    if navigation_policy_error is not None:
                        raise navigation_policy_error from exc
                    raise
                if navigation_policy_error is not None:
                    raise navigation_policy_error
                details = _main_frame_response_details(page, navigation_response)
                if details is not None and not navigation_seen:
                    navigation_status = details[0]
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
                if navigation_policy_error is not None:
                    raise navigation_policy_error
                try:
                    self._prepare_rendered_page(page, url, timeout_ms)
                except Exception as exc:
                    if navigation_policy_error is not None:
                        raise navigation_policy_error from exc
                    raise
                if navigation_policy_error is not None:
                    raise navigation_policy_error
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
        browser_challenge = assess_challenge(navigation_status, content, final_url)
        if browser_challenge.is_challenge:
            raise RuntimeError(
                "Browser received an anti-bot challenge or access-denied page "
                f"({browser_challenge.marker or browser_challenge.category})."
            )
        if navigation_status >= 400:
            raise RuntimeError(
                f"Browser received HTTP {navigation_status} for {final_url}."
            )
        return RenderedPage(
            content=content,
            final_url=final_url,
            status_code=navigation_status,
            browser_rendered=True,
        )


__all__ = [
    "BoardPlatformAdapter",
    "ChallengeAssessment",
    "Content",
    "MeetingLike",
    "RenderedPage",
    "SourceLike",
    "_browser_navigation_scope",
    "_main_frame_response_details",
    "assess_challenge",
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
