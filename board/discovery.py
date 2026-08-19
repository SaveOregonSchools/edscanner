from __future__ import annotations

import heapq
import math
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import normalize_website, prefer_https_url
from search_engine import RunDebugLogger, canonical_url, debug_log, same_organization_url

from .adapters import build_adapters, detect_platform
from .adapters.base import (
    ChallengeAssessment,
    RenderedPage,
    RenderedPageRejected,
    assess_challenge,
)
from .http import BoardHTTPClient, RedirectDenied, RobotsDenied
from .provider_directories import BoardBookDirectoryCatalog, BoardBookDirectoryMatch


BOARD_LINK_TERMS = (
    "school board",
    "board of education",
    "board of directors",
    "board meetings",
    "board meeting",
    "board agendas",
    "meeting agendas",
    "board minutes",
    "meeting minutes",
    "meeting packets",
    "board packet",
    "governance",
    "boarddocs",
    "boardbook",
    "simbli",
    "eboardsolutions",
    "diligent",
    "civicclerk",
)

DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")
MEDIA_SUFFIXES = (
    ".avif",
    ".bmp",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".png",
    ".svg",
    ".webm",
    ".webp",
    ".wmv",
    ".zip",
)
PRUNED_CANDIDATE_SEGMENTS = {
    "board-member",
    "board-members",
    "image",
    "images",
    "media",
    "member",
    "members",
    "policies",
    "policy",
    "resource-manager",
}
EPHEMERAL_CANDIDATE_SEGMENTS = {
    "article",
    "articles",
    "event",
    "events",
    "news",
}
TRANSPORT_BROWSER_FALLBACK_LIMIT = 2
SOURCE_VALIDATION_LIMIT = 5
DISCOVERY_MAX_PAGES = 8


@dataclass(frozen=True)
class BoardSourceCandidate:
    url: str
    text: str
    discovered_from_url: str
    score: int
    known_platform: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class DiscoveryOutcome:
    status: str
    platform: str
    source_url: str
    confidence: float = 0.0
    organization_external_id: str = ""
    platform_tenant: str = ""
    discovered_from_url: str = ""
    requires_javascript: bool = False
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    source: Any | None = None


def _collapse_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def _platform_name(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _adapter_confidence_percent(value: Any) -> float | None:
    """Return an adapter confidence on the persistence layer's 0..100 scale.

    Adapter ``DetectionResult`` instances conventionally report a probability in
    the 0..1 range, while link-candidate scores are already percentage-like.  Keep
    the conversion at this boundary so a candidate score of ``1`` is not
    accidentally interpreted as 100 percent.
    """

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or confidence < 0:
        return None
    if confidence <= 1:
        confidence *= 100
    return min(confidence, 100)


def _outcome_confidence(result: Any, source: Any, candidate: BoardSourceCandidate) -> float:
    for value in (
        getattr(result, "confidence", None),
        getattr(source, "confidence", None),
    ):
        normalized = _adapter_confidence_percent(value)
        if normalized is not None and normalized > 0:
            return normalized
    # Candidate scores are already expressed on the storage layer's 0..100 scale.
    return float(candidate.score)


def is_known_board_host(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").casefold().strip(".")
    if not host:
        return False
    return (
        host == "meetings.boardbook.org"
        or host == "go.boarddocs.com"
        or host == "simbli.eboardsolutions.com"
        or host.endswith(".community.diligentoneplatform.com")
        or host.endswith(".community.highbond.com")
        or host.endswith(".diligent.community")
        or host.endswith(".civicweb.net")
        or host.endswith(".portal.civicclerk.com")
        or host.endswith(".civicclerk.com")
    )


def _crawl_url_key(url: str) -> tuple[str, str, str, str]:
    """Return a stable crawl identity without lowercasing arbitrary site paths."""

    canonical = canonical_url(url)
    parsed = urlparse(canonical)
    path = parsed.path.casefold() if is_known_board_host(canonical) else parsed.path
    return (parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query)


def board_url_allowed(url: str, district_base_url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    return same_organization_url(url, district_base_url) or is_known_board_host(url)


def _looks_like_challenge(status_code: int, content: bytes | str, url: str) -> bool:
    """Compatibility wrapper around the structured challenge classifier."""

    return assess_challenge(status_code, content, url).is_challenge


def _challenge_record(
    assessment: ChallengeAssessment,
    *,
    url: str,
    status_code: int,
    browser_fallback_attempted: bool = False,
    browser_fallback_error: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "url": url,
        "status_code": int(status_code),
        "category": assessment.category,
        "marker": assessment.marker,
        "browser_retry_allowed": assessment.browser_retry_allowed,
        "browser_fallback_attempted": bool(browser_fallback_attempted),
    }
    if browser_fallback_error:
        record["browser_fallback_error"] = browser_fallback_error
    return record


def _browser_renderer_for_url(url: str, adapters: list[Any]) -> Any | None:
    detection = detect_platform(url, adapters=adapters)
    platform = (
        _platform_name(getattr(detection, "platform", ""))
        if getattr(detection, "matched", False)
        else ""
    )
    if platform:
        renderer = next(
            (
                adapter
                for adapter in adapters
                if _platform_name(getattr(adapter, "platform_name", "")) == platform
            ),
            None,
        )
        if renderer is not None:
            return renderer
    return next(
        (
            adapter
            for adapter in adapters
            if _platform_name(getattr(adapter, "platform_name", "")) == "generic"
        ),
        adapters[0] if adapters else None,
    )


@dataclass(frozen=True)
class _BrowserRenderAttempt:
    attempted: bool = False
    page: RenderedPage | None = None
    challenge: ChallengeAssessment | None = None
    error: str = ""
    error_type: str = ""
    robots_denied: bool = False

    @property
    def recovered(self) -> bool:
        return self.page is not None and not self.error


def _attempt_browser_render(
    url: str,
    adapters: list[Any],
    attempted_urls: set[tuple[str, str, str, str]],
    *,
    enabled: bool,
    challenge_error_prefix: str,
    http_error_prefix: str = "",
    allow_district_website_move: bool = False,
) -> _BrowserRenderAttempt:
    """Render one URL through an adapter's policy gates, at most once per URL."""

    key = _crawl_url_key(url)
    if not enabled or key in attempted_urls:
        return _BrowserRenderAttempt()
    renderer = _browser_renderer_for_url(url, adapters)
    if renderer is None:
        return _BrowserRenderAttempt()

    attempted_urls.add(key)
    try:
        render_with_metadata = getattr(renderer, "render_page_with_metadata", None)
        if not callable(render_with_metadata):
            raise RuntimeError(
                "Browser renderer does not expose policy-gated page metadata."
            )
        render_district_website = getattr(
            renderer,
            "render_district_website_with_metadata",
            None,
        )
        if allow_district_website_move and callable(render_district_website):
            page = render_district_website(url)
        else:
            page = render_with_metadata(url)
        if not isinstance(page, RenderedPage):
            raise TypeError("Browser renderer returned an invalid page result.")
    except RobotsDenied as exc:
        return _BrowserRenderAttempt(
            attempted=True,
            error=str(exc),
            error_type=type(exc).__name__,
            robots_denied=True,
        )
    except RenderedPageRejected as exc:
        return _BrowserRenderAttempt(
            attempted=True,
            page=exc.page,
            challenge=exc.challenge,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    except Exception as exc:
        return _BrowserRenderAttempt(
            attempted=True,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    challenge = assess_challenge(page.status_code, page.content, page.final_url)
    if challenge.is_challenge:
        error = (
            f"{challenge_error_prefix} "
            f"({challenge.marker or challenge.category})."
        )
    elif http_error_prefix and page.status_code >= 400:
        error = f"{http_error_prefix} {page.status_code}."
    else:
        error = ""
    return _BrowserRenderAttempt(
        attempted=True,
        page=page,
        challenge=challenge,
        error=error,
    )


def score_board_link(text: str, url: str) -> tuple[int, list[str]]:
    text_fold = _collapse_ws(text).casefold()
    url_fold = str(url or "").casefold()
    haystack = f"{text_fold} {url_fold}"
    evidence: list[str] = []
    score = 0
    for term in BOARD_LINK_TERMS:
        if term in haystack:
            weight = 20 if term in {"boardbook", "boarddocs", "simbli", "eboardsolutions", "diligent", "civicclerk"} else 8
            score += weight
            evidence.append(term)
    if re.search(r"/(board|boe|governance|meetings?|agendas?|minutes?)(?:[/_.?=&-]|$)", url_fold):
        score += 5
        evidence.append("board-like URL")
    if is_known_board_host(url):
        score += 50
        evidence.append("known public board host")
    if urlparse(url).path.casefold().endswith(DOCUMENT_SUFFIXES):
        score -= 15
    if any(term in haystack for term in ("login", "sign in", "admin", "employee portal")):
        score -= 60
    path_segments = {
        segment for segment in urlparse(url).path.casefold().split("/") if segment
    }
    if path_segments & EPHEMERAL_CANDIDATE_SEGMENTS:
        # A single article/event is usually evidence, not a durable source, but
        # a strongly labeled archive hub under /events or /news can still earn
        # validation instead of being discarded categorically.
        score -= 12
        evidence.append("news/article/event path penalty")
    if any(segment in {"feed", "feeds", "rss"} for segment in path_segments):
        score -= 12
        evidence.append("feed path penalty")
    if any(segment.isdigit() and len(segment) >= 4 for segment in path_segments):
        score -= 5
        evidence.append("opaque numeric path penalty")
    return score, evidence


def _anchor_scoring_text(anchor: Any) -> str:
    """Keep useful local context without borrowing labels from sibling links."""

    text = _collapse_ws(anchor.get_text(" ", strip=True))
    parent = anchor.parent
    if parent is None or parent.name in {"nav", "ul", "ol", "header", "footer"}:
        return text
    if len(parent.find_all("a", href=True)) > 1:
        return text
    parent_text = _collapse_ws(parent.get_text(" ", strip=True))[:500]
    return f"{text} {parent_text}" if parent_text else text


def _pruned_candidate_url(url: str, known_platform: str = "") -> bool:
    if known_platform and known_platform != "generic":
        return False
    path = urlparse(url).path.casefold()
    if path.endswith(MEDIA_SUFFIXES):
        return True
    segments = {segment for segment in path.split("/") if segment}
    if segments & PRUNED_CANDIDATE_SEGMENTS:
        return True
    return any(
        segment.startswith(("board-member-", "member-", "members-", "policy-", "policies-"))
        for segment in segments
    )


def _decoded_embedded_text(content: bytes | str) -> str:
    text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content or "")
    text = unescape(text)
    for _ in range(4):
        decoded = unquote(text)
        decoded = re.sub(r"\\u003a", ":", decoded, flags=re.IGNORECASE)
        decoded = re.sub(r"\\u002f", "/", decoded, flags=re.IGNORECASE)
        decoded = re.sub(r"\\u0026", "&", decoded, flags=re.IGNORECASE)
        decoded = decoded.replace("\\/", "/")
        if decoded == text:
            break
        text = decoded
    return text


def _embedded_provider_urls(content: bytes | str) -> list[str]:
    text = _decoded_embedded_text(content)
    found: list[str] = []
    for match in re.finditer(
        r"(?i)(?:https?:)?//[a-z0-9][a-z0-9.-]*(?::\d+)?(?:/[^\s<>\"']*)?",
        text,
    ):
        # Escaped JSON/JavaScript strings commonly leave the backslash from a
        # closing ``\"`` in the regex match.  A backslash is never valid in a
        # canonical public URL, so discard it with the surrounding punctuation
        # before platform detection and robots checks.
        value = match.group(0).rstrip(".,;:!?)]}\\")
        if value.startswith("//"):
            value = f"https:{value}"
        url = canonical_url(value)
        if is_known_board_host(url) and url not in found:
            found.append(url)
    return found


def extract_board_candidates(
    content: bytes | str,
    page_url: str,
    district_base_url: str,
    *,
    adapters: list[Any] | None = None,
) -> list[BoardSourceCandidate]:
    soup = BeautifulSoup(content, "lxml")
    found: dict[str, BoardSourceCandidate] = {}

    def add_candidate(
        href: str,
        text: str,
        scoring_text: str,
        *,
        embedded: bool = False,
    ) -> None:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        url = canonical_url(urljoin(page_url, href))
        if not board_url_allowed(url, district_base_url):
            return
        score, evidence = score_board_link(scoring_text, url)
        if score < 5:
            return
        detection = detect_platform(url, adapters=adapters)
        platform = _platform_name(getattr(detection, "platform", "")) if getattr(detection, "matched", False) else ""
        if is_known_board_host(url) and platform in {"", "generic"}:
            return
        if _pruned_candidate_url(url, platform):
            return
        if embedded:
            evidence = [*evidence, "embedded public provider URL"]
        candidate = BoardSourceCandidate(
            url=url,
            text=text,
            discovered_from_url=page_url,
            score=score,
            known_platform=platform,
            evidence=evidence,
        )
        existing = found.get(url)
        if existing is None or candidate.score > existing.score:
            found[url] = candidate

    for node in soup.find_all(["a", "iframe"]):
        attribute = "href" if node.name == "a" else "src"
        href = str(node.get(attribute) or "").strip()
        text = _collapse_ws(node.get_text(" ", strip=True))
        scoring_text = _anchor_scoring_text(node) if node.name == "a" else f"{text} board meetings"
        add_candidate(href, text, scoring_text)

    for url in _embedded_provider_urls(content):
        add_candidate(
            url,
            "Embedded board meeting provider",
            "board meetings",
            embedded=True,
        )
    return sorted(found.values(), key=lambda item: (-item.score, item.url))


def _outcome_from_adapter_result(result: Any, candidate: BoardSourceCandidate) -> DiscoveryOutcome:
    source = getattr(result, "source", None)
    detection = getattr(result, "detection", None)
    status = str(getattr(result, "status", "") or getattr(source, "status", "") or "manual_review")
    platform = _platform_name(
        getattr(result, "platform", "") or getattr(source, "platform", "") or candidate.known_platform or "generic"
    )
    source_url = str(
        getattr(result, "source_url", "")
        or getattr(source, "public_url", "")
        or candidate.url
    )
    detection_metadata = dict(getattr(detection, "metadata", {}) or {})
    metadata = {**detection_metadata, **dict(getattr(source, "metadata", {}) or {})}
    raw = dict(getattr(result, "metadata", {}) or {})
    if detection is not None:
        raw.setdefault(
            "detection",
            {
                "matched": bool(getattr(detection, "matched", False)),
                "platform": _platform_name(getattr(detection, "platform", "")),
                "confidence": _adapter_confidence_percent(
                    getattr(detection, "confidence", 0)
                ),
                "reason": str(getattr(detection, "reason", "") or ""),
                "canonical_url": str(
                    getattr(detection, "canonical_url", "") or ""
                ),
                "metadata": detection_metadata,
            },
        )
    raw.setdefault("candidate_text", candidate.text)
    raw.setdefault("candidate_score", candidate.score)
    raw.setdefault("candidate_evidence", candidate.evidence)
    return DiscoveryOutcome(
        status=status,
        platform=platform,
        source_url=source_url,
        confidence=_outcome_confidence(result, source, candidate),
        organization_external_id=str(
            getattr(result, "organization_external_id", "")
            or getattr(source, "external_source_id", "")
            or metadata.get("external_source_id")
            or metadata.get("organization_external_id")
            or ""
        ),
        platform_tenant=str(getattr(result, "platform_tenant", "") or metadata.get("platform_tenant") or metadata.get("tenant") or ""),
        discovered_from_url=candidate.discovered_from_url,
        requires_javascript=bool(
            getattr(result, "requires_javascript", False) or getattr(source, "requires_javascript", False)
        ),
        error_message=str(getattr(result, "error_message", "") or getattr(result, "error", "") or ""),
        raw=raw,
        source=source,
    )


def _source_validation_candidates(
    ordered: list[BoardSourceCandidate],
    limit: int = SOURCE_VALIDATION_LIMIT,
) -> list[BoardSourceCandidate]:
    """Reserve the bounded validation budget for canonical providers first."""

    bounded = max(0, int(limit))
    known = [
        item
        for item in ordered
        if item.known_platform not in {"", "generic", "unknown"}
    ]
    other = [item for item in ordered if item not in known]
    return [*known, *other][:bounded]


def _provider_directory_diagnostics(
    catalog: BoardBookDirectoryCatalog,
    match: BoardBookDirectoryMatch,
) -> dict[str, Any]:
    verified_id = (
        match.verified.entry.external_id
        if match.verified is not None
        else None
    )
    return {
        "provider": "boardbook",
        "catalog_url": catalog.source_url,
        "status": match.status,
        "reason": match.reason,
        "verified_external_id": verified_id,
        "candidates": [
            {
                "external_id": item.entry.external_id,
                "organization_name": item.entry.organization_name,
                "public_url": item.entry.public_url,
                "name_score": item.name_score,
            }
            for item in match.candidates
        ],
        "state_evidence": [
            {
                "external_id": item.entry.external_id,
                "organization_name": item.organization_name,
                "states": sorted(item.states),
                "cities": sorted(item.cities),
                "meeting_count": item.meeting_count,
                "homepage_urls": list(item.homepage_urls),
            }
            for item in match.evidence
        ],
        "errors": list(match.errors),
    }


def discover_board_source(
    district: Mapping[str, Any],
    *,
    client: BoardHTTPClient | None = None,
    provider_directory: BoardBookDirectoryCatalog | None = None,
    max_pages: int = DISCOVERY_MAX_PAGES,
    allow_browser_fallback: bool = True,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> DiscoveryOutcome:
    district_id = int(district.get("id") or 0)
    base_url = prefer_https_url(
        district.get("website_normalized") or normalize_website(district.get("website"))[0]
    )
    if not district_id:
        return DiscoveryOutcome(
            status="error",
            platform="unknown",
            source_url=base_url,
            error_message="District has no local identifier.",
            raw={"district_id": district_id},
        )
    if not base_url and provider_directory is None:
        return DiscoveryOutcome(
            status="error",
            platform="unknown",
            source_url="",
            error_message="District has no normalized public website.",
            raw={"district_id": district_id},
        )

    owns_client = client is None
    client = client or BoardHTTPClient()
    try:
        adapters = build_adapters(client, allow_browser_fallback=allow_browser_fallback)
        provider_directory_raw: dict[str, Any] = {}
        if cancel_requested and cancel_requested():
            return DiscoveryOutcome(
                status="cancelled",
                platform="unknown",
                source_url=base_url,
                error_message="Cancellation requested.",
                raw={"district_id": district_id},
            )
        if provider_directory is not None:
            try:
                provider_match = provider_directory.match_and_verify(district, client)
                provider_directory_raw = _provider_directory_diagnostics(
                    provider_directory,
                    provider_match,
                )
                if provider_match.is_verified:
                    verified = provider_match.verified
                    provider_candidate = BoardSourceCandidate(
                        url=verified.entry.public_url,
                        text=verified.entry.organization_name,
                        discovered_from_url=provider_directory.source_url,
                        score=max(1, min(100, int(round(verified.name_score * 100)))),
                        known_platform="boardbook",
                        evidence=[
                            "verified BoardBook public-directory match",
                            provider_match.reason,
                        ],
                    )
                    boardbook_adapter = next(
                        (
                            item
                            for item in adapters
                            if _platform_name(getattr(item, "platform_name", "")) == "boardbook"
                        ),
                        None,
                    )
                    if boardbook_adapter is not None:
                        try:
                            provider_result = boardbook_adapter.discover_source(
                                district,
                                provider_candidate.url,
                            )
                            provider_outcome = _outcome_from_adapter_result(
                                provider_result,
                                provider_candidate,
                            )
                            provider_directory_raw["adapter_status"] = provider_outcome.status
                            if provider_outcome.status == "working":
                                provider_outcome.raw.update(
                                    {
                                        "provider_directory": provider_directory_raw,
                                        "visited_urls": [],
                                        "candidate_count": 1,
                                        "fetch_errors": [],
                                        "challenges": [],
                                        "challenge_recoveries": [],
                                        "robots_denials": [],
                                    }
                                )
                                debug_log(
                                    debug_logger,
                                    "board_provider_directory_verified",
                                    district=district.get("agency_name"),
                                    platform="boardbook",
                                    url=provider_outcome.source_url,
                                    external_id=verified.entry.external_id,
                                )
                                return provider_outcome
                            provider_directory_raw["adapter_error"] = provider_outcome.error_message
                        except Exception as exc:
                            provider_directory_raw["adapter_status"] = "error"
                            provider_directory_raw["adapter_error"] = str(exc)
            except Exception as exc:
                provider_directory_raw = {
                    "provider": "boardbook",
                    "catalog_url": provider_directory.source_url,
                    "status": "error",
                    "reason": "Provider-directory matching failed; district-site discovery continued.",
                    "errors": [str(exc)],
                }

        if not base_url:
            candidates = provider_directory_raw.get("candidates", [])
            first = candidates[0] if candidates else {}
            return DiscoveryOutcome(
                status="manual_review" if candidates else "error",
                platform="boardbook" if candidates else "unknown",
                source_url=str(first.get("public_url") or ""),
                confidence=float(first.get("name_score") or 0) * 100,
                discovered_from_url=(provider_directory.source_url if provider_directory else ""),
                error_message=(
                    "Provider-directory candidates require manual review and the district has no website fallback."
                    if candidates
                    else "District has no normalized public website and no verified provider-directory source."
                ),
                raw={"district_id": district_id, "provider_directory": provider_directory_raw},
            )

        configured_website_url = canonical_url(base_url)
        crawl_base_url = configured_website_url
        configured_website_key = _crawl_url_key(configured_website_url)
        website_migration: dict[str, Any] = {}
        pending: list[tuple[int, int, str]] = [(-100, 0, configured_website_url)]
        queued = {configured_website_url}
        visited: set[str] = set()
        candidates: dict[str, BoardSourceCandidate] = {}
        candidate_content: dict[tuple[str, str, str, str], bytes] = {}
        browser_attempted_urls: set[tuple[str, str, str, str]] = set()
        challenges: list[dict[str, Any]] = []
        challenge_recoveries: list[dict[str, Any]] = []
        transport_recoveries: list[dict[str, Any]] = []
        fetch_errors: list[dict[str, Any]] = []
        browser_errors: list[dict[str, Any]] = []
        robots_denials: list[dict[str, Any]] = []
        successful_page_fetches = 0
        transport_browser_fallback_count = 0

        def record_website_migration(
            *,
            final_url: str,
            redirect_chain: list[str] | tuple[str, ...],
            transport: str,
            accepted: bool,
        ) -> bool:
            """Validate and retain one initial configured-site migration."""

            nonlocal crawl_base_url, website_migration
            if not accepted:
                return False
            final_canonical = canonical_url(final_url)
            if (
                urlparse(configured_website_url).scheme.casefold() != "https"
                or urlparse(final_canonical).scheme.casefold() != "https"
            ):
                return False
            validator = getattr(client, "validate_target_url", None)
            if callable(validator):
                final_canonical = canonical_url(validator(final_canonical))
            chain: list[str] = []
            for item in redirect_chain:
                if not str(item).strip():
                    continue
                if urlparse(str(item)).scheme.casefold() != "https":
                    return False
                canonical_item = canonical_url(item)
                if callable(validator):
                    canonical_item = canonical_url(validator(canonical_item))
                chain.append(canonical_item)
            if not chain or chain[-1] != final_canonical:
                chain.append(final_canonical)
            website_migration = {
                "status": "accepted",
                "evidence": "initial_configured_website_https_redirect",
                "original_url": configured_website_url,
                "redirect_chain": chain,
                "final_url": final_canonical,
                "canonical_website_url": final_canonical,
                "transport": transport,
            }
            crawl_base_url = final_canonical
            debug_log(
                debug_logger,
                "board_district_website_migration_accepted",
                district=district.get("agency_name"),
                status=website_migration["status"],
                evidence=website_migration["evidence"],
                original_url=configured_website_url,
                redirect_chain=chain,
                final_url=final_canonical,
                canonical_website_url=final_canonical,
                transport=transport,
            )
            return True

        def migration_diagnostics() -> dict[str, Any]:
            return (
                {"website_migration": dict(website_migration)}
                if website_migration
                else {}
            )

        def record_fetch_error(error_url: str, **details: Any) -> dict[str, Any]:
            record = {"url": error_url, **details}
            fetch_errors.append(record)
            debug_log(
                debug_logger,
                "board_source_fetch_error",
                district=district.get("agency_name"),
                url=error_url,
                **{
                    key: value
                    for key, value in details.items()
                    if key != "browser_fallback_attempted"
                },
            )
            return record

        def record_browser_error(
            error_url: str,
            error: str,
            *,
            trigger: str,
            **details: Any,
        ) -> None:
            browser_errors.append(
                {"url": error_url, "error": error, "trigger": trigger, **details}
            )
            debug_log(
                debug_logger,
                "board_browser_fallback_error",
                district=district.get("agency_name"),
                url=error_url,
                error=error,
                trigger=trigger,
                **details,
            )

        while pending and len(visited) < max(1, max_pages):
            if cancel_requested and cancel_requested():
                return DiscoveryOutcome(
                    status="cancelled",
                    platform="unknown",
                    source_url=crawl_base_url,
                    error_message="Cancellation requested.",
                    raw={
                        "visited": sorted(visited),
                        **migration_diagnostics(),
                        **(
                            {"provider_directory": provider_directory_raw}
                            if provider_directory_raw
                            else {}
                        ),
                    },
                )
            negative_score, depth, url = heapq.heappop(pending)
            if url in visited:
                continue
            visited.add(url)
            is_initial_district_navigation = bool(
                depth == 0 and _crawl_url_key(url) == configured_website_key
            )
            debug_log(debug_logger, "board_source_candidate", district=district.get("agency_name"), url=url, depth=depth)
            try:
                response = client.get(
                    url,
                    check_robots=True,
                    raise_for_status=False,
                    allow_district_website_move=is_initial_district_navigation,
                )
                response_content = response.content
                effective_status_code = response.status_code
                effective_final_url = response.final_url or url
                if is_initial_district_navigation and bool(
                    getattr(response, "website_migration_accepted", False)
                ):
                    record_website_migration(
                        final_url=effective_final_url,
                        redirect_chain=getattr(response, "redirect_chain", ()),
                        transport="http",
                        accepted=True,
                    )
            except RobotsDenied as exc:
                robots_denials.append({"url": url, "error": str(exc)})
                debug_log(
                    debug_logger,
                    "blocked_by_robots",
                    district=district.get("agency_name"),
                    url=url,
                    error=str(exc),
                )
                continue
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                RedirectDenied,
            ) as exc:
                fetch_error = record_fetch_error(
                    url,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    kind=("redirect" if isinstance(exc, RedirectDenied) else "transport"),
                    browser_fallback_attempted=False,
                )
                exception_status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                recovery = _attempt_browser_render(
                    url,
                    adapters,
                    browser_attempted_urls,
                    enabled=(
                        allow_browser_fallback
                        and exception_status != 429
                        and transport_browser_fallback_count
                        < TRANSPORT_BROWSER_FALLBACK_LIMIT
                        and (
                            not isinstance(exc, RedirectDenied)
                            or is_initial_district_navigation
                        )
                    ),
                    challenge_error_prefix=(
                        "Transport recovery browser remained challenged"
                    ),
                    http_error_prefix="Transport recovery browser returned HTTP",
                    allow_district_website_move=is_initial_district_navigation,
                )
                if recovery.attempted:
                    transport_browser_fallback_count += 1
                    fetch_error["browser_fallback_attempted"] = True
                if recovery.attempted and not recovery.recovered:
                    fetch_error["browser_fallback_error"] = recovery.error
                    error_details = (
                        {
                            "final_url": recovery.page.final_url,
                            "status_code": recovery.page.status_code,
                        }
                        if recovery.page is not None
                        else {"error_type": recovery.error_type}
                    )
                    record_browser_error(
                        url,
                        recovery.error,
                        trigger="transport",
                        **error_details,
                    )
                if recovery.robots_denied:
                    robots_denials.append({"url": url, "error": recovery.error})
                if recovery.challenge and recovery.challenge.is_challenge:
                    assert recovery.page is not None
                    challenges.append(
                        _challenge_record(
                            recovery.challenge,
                            url=recovery.page.final_url,
                            status_code=recovery.page.status_code,
                            browser_fallback_attempted=True,
                            browser_fallback_error=recovery.error,
                        )
                    )
                if not recovery.recovered:
                    continue

                assert recovery.page is not None
                if recovery.page.website_migration_accepted and not record_website_migration(
                    final_url=recovery.page.final_url,
                    redirect_chain=recovery.page.redirect_chain,
                    transport="browser",
                    accepted=is_initial_district_navigation,
                ):
                    record_browser_error(
                        url,
                        "Browser reported a district website move outside the initial "
                        "configured-site navigation.",
                        trigger="transport",
                        error_type="InvalidPublicURL",
                    )
                    continue
                response_content = recovery.page.content
                effective_status_code = recovery.page.status_code
                effective_final_url = recovery.page.final_url
                browser_attempted_urls.add(_crawl_url_key(recovery.page.final_url))
                transport_recovery = {
                    "url": url,
                    "final_url": recovery.page.final_url,
                    "status_code": recovery.page.status_code,
                    "browser_rendered": bool(recovery.page.browser_rendered),
                    "transport_error": str(exc),
                    "transport_error_type": type(exc).__name__,
                }
                if recovery.page.redirect_chain:
                    transport_recovery["redirect_chain"] = list(
                        recovery.page.redirect_chain
                    )
                transport_recoveries.append(transport_recovery)
                debug_log(
                    debug_logger,
                    "board_transport_recovered_with_browser",
                    district=district.get("agency_name"),
                    url=url,
                    final_url=recovery.page.final_url,
                    status_code=recovery.page.status_code,
                    website_migration=bool(
                        recovery.page.website_migration_accepted
                    ),
                )
            except Exception as exc:
                record_fetch_error(
                    url,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    kind="fetch",
                )
                continue
            challenge_status_code = effective_status_code
            challenge_url = effective_final_url
            challenge = assess_challenge(
                challenge_status_code,
                response_content,
                challenge_url,
            )
            if challenge.is_challenge:
                debug_log(
                    debug_logger,
                    "challenge_detected",
                    district=district.get("agency_name"),
                    url=url,
                    status_code=challenge_status_code,
                    category=challenge.category,
                    marker=challenge.marker,
                )
                recovery = _attempt_browser_render(
                    challenge_url,
                    adapters,
                    browser_attempted_urls,
                    enabled=(
                        allow_browser_fallback and challenge.browser_retry_allowed
                    ),
                    challenge_error_prefix="Rendered page remained challenged",
                    allow_district_website_move=is_initial_district_navigation,
                )
                browser_attempted = recovery.attempted
                browser_error = recovery.error
                browser_robots_denied = recovery.robots_denied
                recovered_content = (
                    recovery.page.content if recovery.recovered else None
                )
                if browser_robots_denied:
                    robots_denials.append({"url": url, "error": browser_error})
                if recovery.recovered:
                    assert recovery.page is not None
                    if (
                        recovery.page.website_migration_accepted
                        and not record_website_migration(
                            final_url=recovery.page.final_url,
                            redirect_chain=recovery.page.redirect_chain,
                            transport="browser",
                            accepted=is_initial_district_navigation,
                        )
                    ):
                        record_browser_error(
                            challenge_url,
                            "Browser reported a district website move outside the "
                            "initial configured-site navigation.",
                            trigger="challenge",
                            error_type="InvalidPublicURL",
                        )
                        continue
                    effective_final_url = recovery.page.final_url
                    effective_status_code = recovery.page.status_code
                if browser_error:
                    record_browser_error(
                        challenge_url,
                        browser_error,
                        trigger="challenge",
                    )
                record = _challenge_record(
                    challenge,
                    url=challenge_url,
                    status_code=challenge_status_code,
                    browser_fallback_attempted=browser_attempted,
                    browser_fallback_error=browser_error,
                )
                if recovered_content is None:
                    if browser_robots_denied:
                        continue
                    challenges.append(record)
                    score, evidence = score_board_link("", url)
                    challenged_url = canonical_url(challenge_url)
                    if is_known_board_host(challenged_url):
                        candidates[challenged_url] = BoardSourceCandidate(
                            url=challenged_url,
                            text="",
                            discovered_from_url=url,
                            score=max(score, 50),
                            known_platform=_platform_name(
                                getattr(
                                    detect_platform(challenged_url, adapters=adapters),
                                    "platform",
                                    "",
                                )
                            ),
                            evidence=[*evidence, "challenge encountered"],
                        )
                    continue
                record["recovered"] = True
                challenge_recoveries.append(record)
                response_content = recovered_content
                debug_log(
                    debug_logger,
                    "challenge_recovered_with_browser",
                    district=district.get("agency_name"),
                    url=url,
                    status_code=challenge_status_code,
                    rendered_status_code=effective_status_code,
                )
            if effective_status_code >= 400:
                record_fetch_error(
                    url,
                    status_code=effective_status_code,
                    kind="http_status",
                )
                continue

            successful_page_fetches += 1
            final_url = canonical_url(effective_final_url)
            candidate_content[_crawl_url_key(final_url)] = response_content
            detection = detect_platform(final_url, response_content, adapters=adapters)
            if getattr(detection, "matched", False) and _platform_name(getattr(detection, "platform", "")) != "generic":
                score, evidence = score_board_link("", final_url)
                candidates[final_url] = BoardSourceCandidate(
                    url=final_url,
                    text="",
                    discovered_from_url=url,
                    score=max(
                        score,
                        int(round(_adapter_confidence_percent(getattr(detection, "confidence", 0)) or 0)),
                    ),
                    known_platform=_platform_name(getattr(detection, "platform", "")),
                    evidence=[*evidence, *list(getattr(detection, "evidence", []) or [])],
                )

            for candidate in extract_board_candidates(
                response_content,
                final_url,
                crawl_base_url,
                adapters=adapters,
            ):
                existing = candidates.get(candidate.url)
                if existing is None or candidate.score > existing.score:
                    candidates[candidate.url] = candidate
                if (
                    depth < 2
                    and same_organization_url(candidate.url, crawl_base_url)
                    and not urlparse(candidate.url).path.casefold().endswith(DOCUMENT_SUFFIXES)
                    and candidate.url not in queued
                    and all(
                        _crawl_url_key(candidate.url) != _crawl_url_key(item)
                        for item in visited
                    )
                ):
                    queued.add(candidate.url)
                    heapq.heappush(pending, (-candidate.score, depth + 1, candidate.url))

        ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.url))
        validation_candidates = _source_validation_candidates(ordered)
        candidate_validations: list[dict[str, Any]] = []
        review_outcomes: list[tuple[BoardSourceCandidate, DiscoveryOutcome]] = []
        adapter_error_outcomes: list[tuple[BoardSourceCandidate, DiscoveryOutcome]] = []

        def enrich_outcome_raw(outcome: DiscoveryOutcome) -> None:
            outcome.raw.update(
                {
                    "visited_urls": sorted(visited),
                    "candidate_count": len(ordered),
                    "candidate_validation_limit": SOURCE_VALIDATION_LIMIT,
                    "candidate_validation_count": len(candidate_validations),
                    "candidate_validations": candidate_validations,
                    "successful_page_fetches": successful_page_fetches,
                    "fetch_errors": fetch_errors,
                    "browser_errors": browser_errors,
                    "challenges": challenges,
                    "challenge_recoveries": challenge_recoveries,
                    "transport_recoveries": transport_recoveries,
                    "robots_denials": robots_denials,
                }
            )
            if provider_directory_raw:
                outcome.raw["provider_directory"] = provider_directory_raw
            outcome.raw.update(migration_diagnostics())

        for validation_index, candidate in enumerate(validation_candidates, start=1):
            if cancel_requested and cancel_requested():
                break
            adapter = next(
                (
                    item
                    for item in adapters
                    if _platform_name(getattr(item, "platform_name", "")) == candidate.known_platform
                ),
                None,
            )
            if adapter is None:
                detection = detect_platform(candidate.url, adapters=adapters)
                adapter = getattr(detection, "adapter", None)
            if adapter is None:
                adapter = next((item for item in adapters if _platform_name(getattr(item, "platform_name", "")) == "generic"), None)
            if adapter is None:
                continue
            validation_started = time.monotonic()
            validation_record: dict[str, Any] = {
                "index": validation_index,
                "url": candidate.url,
                "score": candidate.score,
                "evidence": list(candidate.evidence),
                "expected_platform": candidate.known_platform or "generic",
            }
            candidate_validations.append(validation_record)
            debug_log(
                debug_logger,
                "board_candidate_validation_started",
                district=district.get("agency_name"),
                **validation_record,
            )
            try:
                candidate_url = canonical_url(candidate.url)
                candidate_key = _crawl_url_key(candidate_url)
                # A browser-rendered page gets at most one attempt per URL.
                # Recovered HTML is validated directly instead of refetching it
                # and launching a second browser.
                if (
                    candidate_key in browser_attempted_urls
                    and candidate_key not in candidate_content
                ):
                    validation_record.update(
                        {
                            "status": "skipped",
                            "error": "A prior browser attempt did not produce usable content.",
                            "elapsed_seconds": round(time.monotonic() - validation_started, 3),
                        }
                    )
                    debug_log(
                        debug_logger,
                        "board_candidate_validation_result",
                        district=district.get("agency_name"),
                        **validation_record,
                    )
                    continue
                result = adapter.discover_source(
                    district,
                    candidate.url,
                    html=candidate_content.get(candidate_key),
                )
                outcome = _outcome_from_adapter_result(result, candidate)
                validation_record.update(
                    {
                        "status": outcome.status,
                        "platform": outcome.platform,
                        "source_url": outcome.source_url,
                        "organization_external_id": (
                            outcome.organization_external_id or None
                        ),
                        "error": outcome.error_message or None,
                        "elapsed_seconds": round(time.monotonic() - validation_started, 3),
                    }
                )
                debug_log(
                    debug_logger,
                    "board_candidate_validation_result",
                    district=district.get("agency_name"),
                    **validation_record,
                )
                if outcome.status == "blocked_by_robots":
                    robots_denials.append(
                        {"url": candidate.url, "error": outcome.error_message}
                    )
                    # Retain the adapter's canonical URL and provider identity
                    # as review evidence even though robots policy prevents
                    # verification.  This never marks the source working or
                    # bypasses the denied request.
                    review_outcomes.append((candidate, outcome))
                    continue
                if outcome.status == "working":
                    enrich_outcome_raw(outcome)
                    debug_log(
                        debug_logger,
                        "board_platform_detected",
                        district=district.get("agency_name"),
                        platform=outcome.platform,
                        url=outcome.source_url,
                        status=outcome.status,
                    )
                    return outcome
                if outcome.status == "error":
                    record_fetch_error(
                        candidate.url,
                        platform=outcome.platform or candidate.known_platform,
                        error=outcome.error_message or "Adapter validation failed.",
                        error_type="AdapterResultError",
                        kind="adapter",
                    )
                    adapter_error_outcomes.append((candidate, outcome))
                    continue
                if outcome.status in {
                    "requires_javascript",
                    "manual_review",
                    "blocked_by_challenge",
                }:
                    review_outcomes.append((candidate, outcome))
            except Exception as exc:
                validation_record.update(
                    {
                        "status": "error",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "elapsed_seconds": round(time.monotonic() - validation_started, 3),
                    }
                )
                debug_log(
                    debug_logger,
                    "board_candidate_validation_result",
                    district=district.get("agency_name"),
                    **validation_record,
                )
                record_fetch_error(
                    candidate.url,
                    platform=candidate.known_platform,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    kind="adapter",
                )

        if cancel_requested and cancel_requested():
            cancelled_outcome = DiscoveryOutcome(
                status="cancelled",
                platform="unknown",
                source_url=crawl_base_url,
                discovered_from_url=base_url,
                error_message="Cancellation requested during source validation.",
            )
            enrich_outcome_raw(cancelled_outcome)
            return cancelled_outcome

        if review_outcomes:
            status_priority = {
                "manual_review": 1,
                "blocked_by_challenge": 2,
                "blocked_by_robots": 2,
                "requires_javascript": 3,
            }
            selected_candidate, selected_outcome = max(
                review_outcomes,
                key=lambda pair: (
                    pair[1].platform not in {"", "generic", "unknown"},
                    status_priority.get(pair[1].status, 0),
                    pair[0].score,
                ),
            )
            enrich_outcome_raw(selected_outcome)
            selected_outcome.raw["selected_candidate"] = selected_candidate.__dict__
            debug_log(
                debug_logger,
                "board_platform_detected",
                district=district.get("agency_name"),
                platform=selected_outcome.platform,
                url=selected_outcome.source_url,
                status=selected_outcome.status,
            )
            return selected_outcome

        validation_diagnostics = {
            "candidate_count": len(ordered),
            "candidate_validation_limit": SOURCE_VALIDATION_LIMIT,
            "candidate_validation_count": len(candidate_validations),
            "candidate_validations": candidate_validations,
            "successful_page_fetches": successful_page_fetches,
        }

        if robots_denials and not challenges:
            candidate = (
                ordered[0]
                if ordered
                else BoardSourceCandidate(crawl_base_url, "", base_url, 0)
            )
            return DiscoveryOutcome(
                status="blocked_by_robots",
                platform=candidate.known_platform or "unknown",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                error_message="robots.txt disallowed the public board-source request.",
                raw={
                    "visited_urls": sorted(visited),
                    **validation_diagnostics,
                    "fetch_errors": fetch_errors,
                    "browser_errors": browser_errors,
                    "challenge_recoveries": challenge_recoveries,
                    "transport_recoveries": transport_recoveries,
                    "robots_denials": robots_denials,
                    **migration_diagnostics(),
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
                },
            )
        if challenges:
            candidate = (
                ordered[0]
                if ordered
                else BoardSourceCandidate(crawl_base_url, "", base_url, 0)
            )
            return DiscoveryOutcome(
                status="requires_javascript" if candidate.known_platform in {"boarddocs", "simbli"} else "blocked_by_challenge",
                platform=candidate.known_platform or "unknown",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                requires_javascript=candidate.known_platform in {"boarddocs", "simbli"},
                error_message="The public source returned an anti-bot challenge; browser or manual review is required.",
                raw={
                    "visited_urls": sorted(visited),
                    **validation_diagnostics,
                    "fetch_errors": fetch_errors,
                    "browser_errors": browser_errors,
                    "challenges": challenges,
                    "challenge_recoveries": challenge_recoveries,
                    "transport_recoveries": transport_recoveries,
                    **migration_diagnostics(),
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
                },
            )
        if adapter_error_outcomes:
            selected_candidate, selected_outcome = max(
                adapter_error_outcomes,
                key=lambda pair: (
                    pair[1].platform not in {"", "generic", "unknown"},
                    pair[0].score,
                ),
            )
            enrich_outcome_raw(selected_outcome)
            selected_outcome.raw["selected_candidate"] = (
                selected_candidate.__dict__
            )
            return selected_outcome
        if ordered:
            candidate = ordered[0]
            return DiscoveryOutcome(
                status="manual_review",
                platform=candidate.known_platform or "generic",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                error_message="Board-related public links were found but no adapter could verify a meeting source.",
                raw={
                    "visited_urls": sorted(visited),
                    "candidates": [item.__dict__ for item in ordered[:25]],
                    **validation_diagnostics,
                    "fetch_errors": fetch_errors,
                    "browser_errors": browser_errors,
                    "challenge_recoveries": challenge_recoveries,
                    "transport_recoveries": transport_recoveries,
                    **migration_diagnostics(),
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
                },
            )
        if successful_page_fetches == 0 and fetch_errors:
            transport_failed = any(
                error.get("kind") == "transport" for error in fetch_errors
            )
            return DiscoveryOutcome(
                status="error",
                platform="unknown",
                source_url=crawl_base_url,
                discovered_from_url=base_url,
                error_message=(
                    "No district page could be inspected because every transport "
                    "attempt failed. See the fetch and browser diagnostics."
                    if transport_failed
                    else "No district page could be inspected because every fetch failed. "
                    "See the fetch diagnostics."
                ),
                raw={
                    "visited_urls": sorted(visited),
                    **validation_diagnostics,
                    "fetch_errors": fetch_errors,
                    "browser_errors": browser_errors,
                    "challenges": challenges,
                    "challenge_recoveries": challenge_recoveries,
                    "transport_recoveries": transport_recoveries,
                    "robots_denials": robots_denials,
                    **migration_diagnostics(),
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
                },
            )
        return DiscoveryOutcome(
            status="not_found",
            platform="unknown",
            source_url=crawl_base_url,
            discovered_from_url=base_url,
            error_message="No public school-board meeting source was found on the inspected district pages.",
            raw={
                "visited_urls": sorted(visited),
                **validation_diagnostics,
                "fetch_errors": fetch_errors,
                "browser_errors": browser_errors,
                "challenge_recoveries": challenge_recoveries,
                "transport_recoveries": transport_recoveries,
                **migration_diagnostics(),
                **(
                    {"provider_directory": provider_directory_raw}
                    if provider_directory_raw
                    else {}
                ),
            },
        )
    finally:
        if owns_client:
            client.close()
