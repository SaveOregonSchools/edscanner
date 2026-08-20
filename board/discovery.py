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

from common import normalize_state, normalize_website, prefer_https_url
from search_engine import RunDebugLogger, canonical_url, debug_log, same_organization_url

from .adapters import build_adapters, detect_platform
from .adapters.base import (
    ChallengeAssessment,
    RenderedPage,
    RenderedPageRejected,
    assess_challenge,
)
from .http import BoardHTTPClient, RedirectDenied, RobotsDenied
from .provider_directories import (
    BoardBookDirectoryCatalog,
    BoardBookDirectoryMatch,
    district_location_values,
    normalized_organization_name,
    organization_name_score,
)
from .search_fallback import BRAVE_BOARD_SEARCH_ENDPOINT, search_known_board_sources


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
ADMIN_CANDIDATE_SEGMENTS = {
    "a-z",
    "about-us",
    "contact",
    "contacts",
    "directory",
    "district-report-cards",
    "handbook",
    "handbooks",
    "public-records",
    "report-cards",
    "site-map",
    "sitemap",
    "staff",
}
TRANSPORT_BROWSER_FALLBACK_LIMIT = 2
SOURCE_VALIDATION_LIMIT = 5
DISCOVERY_MAX_PAGES = 8
SEARCH_FALLBACK_EVIDENCE = "Brave Search API known-provider result"


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
    # District CMS paths frequently encode labels as ``school-board`` or
    # ``Agendas--Minutes``. Treat punctuation as word boundaries so those
    # durable board phrases outrank unrelated pages that merely live below a
    # broad ``/Board/`` site prefix.
    haystack = _collapse_ws(
        re.sub(r"[^a-z0-9]+", " ", unquote(f"{text_fold} {url_fold}"))
    )
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
    normalized_path = _collapse_ws(
        re.sub(r"[^a-z0-9]+", " ", unquote(urlparse(url).path.casefold()))
    )
    path_has_specific_board_evidence = any(
        phrase in normalized_path
        for phrase in (
            "school board",
            "board meeting",
            "board agenda",
            "board minutes",
            "board packet",
        )
    )
    if path_segments & ADMIN_CANDIDATE_SEGMENTS and not path_has_specific_board_evidence:
        score -= 20
        evidence.append("administrative/site-map path penalty")
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


def _https_host_variant(url: str) -> str:
    """Return the narrowly related HTTPS apex/www variant, if one exists."""

    parsed = urlparse(canonical_url(url))
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or "." not in host:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    if all(part.isdigit() for part in host.split(".")):
        return ""
    variant_host = host[4:] if host.startswith("www.") else f"www.{host}"
    if not variant_host or "." not in variant_host:
        return ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return canonical_url(parsed._replace(netloc=f"{variant_host}{port}").geturl())


def _redirect_denied_target(error: BaseException) -> str:
    """Extract the already validated HTTPS target named by RedirectDenied."""

    match = re.search(r"\s->\s(https://\S+)\s*$", str(error), flags=re.IGNORECASE)
    return canonical_url(match.group(1)) if match else ""


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


def _document_resolution_base(
    soup: BeautifulSoup,
    page_url: str,
    district_base_url: str,
) -> str:
    """Return a safe HTML ``base`` URL for resolving document links.

    Some district CMSs publish root-relative navigation without a leading
    slash and rely on ``<base href="https://district.example/">``. Ignoring
    that standard browser behavior turns links such as ``About-Us/index.html``
    into malformed descendants of the current board page. Honor only the first
    public, same-organization base and never allow it to downgrade an HTTPS
    page or redirect relative links to another organization.
    """

    node = soup.find("base", href=True)
    raw_href = str(node.get("href") or "").strip() if node is not None else ""
    if not raw_href:
        return page_url
    try:
        candidate = urljoin(page_url, raw_href)
        parsed = urlparse(candidate)
        page_parsed = urlparse(page_url)
        # Accessing ``port`` also rejects malformed/non-numeric port values.
        parsed.port
    except (TypeError, ValueError):
        return page_url
    if parsed.scheme.casefold() not in {"http", "https"}:
        return page_url
    if page_parsed.scheme.casefold() == "https" and parsed.scheme.casefold() != "https":
        return page_url
    if parsed.username is not None or parsed.password is not None:
        return page_url
    if not same_organization_url(candidate, page_url):
        return page_url
    if not same_organization_url(candidate, district_base_url):
        return page_url
    return candidate


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
    resolution_base = _document_resolution_base(
        soup,
        page_url,
        district_base_url,
    )
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
        url = canonical_url(urljoin(resolution_base, href))
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


def _search_candidate_identity_verified(
    district: Mapping[str, Any],
    candidate: BoardSourceCandidate,
    outcome: DiscoveryOutcome,
    identity_catalog: BoardBookDirectoryCatalog | None,
) -> tuple[bool, str]:
    """Require provider-page identity evidence before search can activate it.

    A search result is useful independent evidence, but a title match alone is
    not enough to bind a provider tenant to an NCES district. BoardBook pages
    expose organization and meeting-location evidence, so those can be safely
    auto-verified. Other known providers remain visible for manual review until
    their adapters expose equivalent state/organization corroboration.
    """

    if SEARCH_FALLBACK_EVIDENCE not in candidate.evidence:
        return True, "district-site evidence"
    if outcome.platform != "boardbook":
        return False, (
            "Search found a known provider, but this adapter does not expose "
            "enough district identity evidence for automatic activation."
        )

    detection = outcome.raw.get("detection")
    metadata = (
        detection.get("metadata", {})
        if isinstance(detection, Mapping)
        else {}
    )
    if not isinstance(metadata, Mapping):
        metadata = {}
    organization_name = str(metadata.get("organization_name") or "").strip()
    expected_name = str(
        district.get("agency_name") or district.get("name") or ""
    ).strip()
    expected_state = normalize_state(district.get("state"))
    observed_states = {
        normalize_state(value)
        for value in metadata.get("states", [])
        if normalize_state(value)
    }
    name_score = organization_name_score(expected_name, organization_name)
    if not organization_name or name_score < 0.84:
        return False, (
            "The BoardBook organization name did not match the selected "
            f"district strongly enough (score {name_score:.2f})."
        )
    if not expected_state or expected_state not in observed_states:
        return False, (
            "The BoardBook page did not expose matching state evidence for "
            f"{expected_state or 'the selected district'}."
        )

    expected_tokens = normalized_organization_name(expected_name).split()
    observed_tokens = set(normalized_organization_name(organization_name).split())
    expected_identifiers = {
        token for token in expected_tokens if any(char.isdigit() for char in token)
    }
    identifier_match = bool(
        expected_identifiers and expected_identifiers.issubset(observed_tokens)
    )

    expected_host = (
        urlparse(
            str(
                district.get("website_normalized")
                or district.get("website")
                or ""
            )
        ).hostname
        or ""
    ).casefold().removeprefix("www.")
    homepage_match = any(
        (urlparse(str(url)).hostname or "").casefold().removeprefix("www.")
        == expected_host
        for url in metadata.get("homepage_urls", [])
        if expected_host
    )

    reciprocal_match = bool(
        identity_catalog
        and identity_catalog.reciprocal_identity_match(
            district,
            organization_name=organization_name,
            states=list(observed_states),
            cities=list(metadata.get("cities", [])),
            minimum_score=0.84,
        )
    )

    if not (identifier_match or homepage_match or reciprocal_match):
        return False, (
            "The search result matched name and state, but lacked a unique "
            "district identifier, matching district homepage, or reciprocal "
            "NCES city/name match. Manual confirmation is required."
        )
    identity_signal = (
        "district identifier"
        if identifier_match
        else "district homepage"
        if homepage_match
        else "unique reciprocal NCES city/name match"
    )
    return True, (
        f"BoardBook organization, {expected_state} meeting-location evidence, "
        f"and {identity_signal} matched the district (name score {name_score:.2f})."
    )


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
    search_fallback: bool = False,
    identity_catalog: BoardBookDirectoryCatalog | None = None,
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
    if not base_url and provider_directory is None and not search_fallback:
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
        search_fallback_raw: dict[str, Any] = {
            "requested": bool(search_fallback),
            "status": "not_needed" if search_fallback else "disabled",
        }
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

        if not base_url and not search_fallback:
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
        pending: list[tuple[int, int, str]] = (
            [(-100, 0, configured_website_url)] if configured_website_url else []
        )
        queued = {configured_website_url} if configured_website_url else set()
        visited: set[str] = set()
        candidates: dict[str, BoardSourceCandidate] = {}
        candidate_content: dict[tuple[str, str, str, str], bytes] = {}
        failed_candidate_statuses: dict[tuple[str, str, str, str], int] = {}
        browser_attempted_urls: set[tuple[str, str, str, str]] = set()
        challenges: list[dict[str, Any]] = []
        challenge_recoveries: list[dict[str, Any]] = []
        transport_recoveries: list[dict[str, Any]] = []
        initial_render_recoveries: list[dict[str, Any]] = []
        website_recovery_attempts: list[dict[str, Any]] = []
        provider_wrapper_redirects: list[dict[str, Any]] = []
        fetch_errors: list[dict[str, Any]] = []
        browser_errors: list[dict[str, Any]] = []
        robots_denials: list[dict[str, Any]] = []
        successful_page_fetches = 0
        transport_browser_fallback_count = 0
        initial_empty_homepage_browser_attempted = False
        initial_host_variant_url = ""
        moved_origin_retry_url = ""
        provider_wrapper_redirect_count = 0

        def record_website_migration(
            *,
            final_url: str,
            redirect_chain: list[str] | tuple[str, ...],
            transport: str,
            accepted: bool,
            evidence: str = "initial_configured_website_https_redirect",
            browser_navigation_evidence: str = "",
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
                if not chain or chain[-1] != canonical_item:
                    chain.append(canonical_item)
            if not chain or chain[-1] != final_canonical:
                chain.append(final_canonical)
            if website_migration:
                existing_final = canonical_url(
                    website_migration.get("final_url") or ""
                )
                # Re-observing the same accepted destination is idempotent;
                # never let a later transport/browser phase turn A -> B into
                # a second, unrelated B -> C website migration.
                return _crawl_url_key(existing_final) == _crawl_url_key(
                    final_canonical
                )
            website_migration = {
                "status": "accepted",
                "evidence": evidence,
                "original_url": configured_website_url,
                "redirect_chain": chain,
                "final_url": final_canonical,
                "canonical_website_url": final_canonical,
                "transport": transport,
            }
            if browser_navigation_evidence:
                website_migration["browser_navigation_evidence"] = (
                    browser_navigation_evidence
                )
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
                browser_navigation_evidence=browser_navigation_evidence,
            )
            return True

        def migration_diagnostics() -> dict[str, Any]:
            diagnostics: dict[str, Any] = {}
            if website_migration:
                diagnostics["website_migration"] = dict(website_migration)
            if initial_render_recoveries:
                diagnostics["initial_render_recoveries"] = list(
                    initial_render_recoveries
                )
            if website_recovery_attempts:
                diagnostics["website_recovery_attempts"] = list(
                    website_recovery_attempts
                )
            if provider_wrapper_redirects:
                diagnostics["provider_wrapper_redirects"] = list(
                    provider_wrapper_redirects
                )
            diagnostics["search_fallback"] = dict(search_fallback_raw)
            return diagnostics

        def schedule_initial_host_variant(reason: str) -> bool:
            """Queue one public apex/www sibling for the configured homepage."""

            nonlocal initial_host_variant_url
            if initial_host_variant_url:
                return False
            variant = _https_host_variant(configured_website_url)
            if not variant or _crawl_url_key(variant) == configured_website_key:
                return False
            connection_validator = getattr(
                client, "validated_connection_target", None
            )
            if not callable(connection_validator):
                return False
            try:
                variant, _addresses = connection_validator(variant)
                variant = canonical_url(variant)
            except Exception as exc:
                website_recovery_attempts.append(
                    {
                        "kind": "initial_host_variant",
                        "url": variant,
                        "status": "unavailable",
                        "reason": reason,
                        "error": str(exc),
                    }
                )
                return False
            if variant in queued or any(
                _crawl_url_key(variant) == _crawl_url_key(item) for item in visited
            ):
                return False
            initial_host_variant_url = variant
            queued.add(variant)
            heapq.heappush(pending, (-99, 0, variant))
            website_recovery_attempts.append(
                {
                    "kind": "initial_host_variant",
                    "url": variant,
                    "status": "queued",
                    "reason": reason,
                }
            )
            debug_log(
                debug_logger,
                "board_initial_host_variant_queued",
                district=district.get("agency_name"),
                original_url=configured_website_url,
                url=variant,
                reason=reason,
            )
            return True

        def recover_known_provider_wrapper(
            wrapper_url: str,
            error: RedirectDenied,
        ) -> bool:
            """Convert one denied wrapper hop into a validated provider candidate."""

            nonlocal provider_wrapper_redirect_count
            if provider_wrapper_redirect_count >= 1:
                return False
            target = _redirect_denied_target(error)
            if not target or not is_known_board_host(target):
                return False
            validator = getattr(client, "validate_target_url", None)
            if not callable(validator):
                return False
            try:
                target = canonical_url(validator(target))
            except Exception:
                return False
            detection = detect_platform(target, adapters=adapters)
            platform = (
                _platform_name(getattr(detection, "platform", ""))
                if getattr(detection, "matched", False)
                else ""
            )
            if platform in {"", "generic", "unknown"}:
                return False
            score, evidence = score_board_link("board meetings", target)
            candidates[target] = BoardSourceCandidate(
                url=target,
                text="Board meeting provider redirect",
                discovered_from_url=wrapper_url,
                score=max(75, score),
                known_platform=platform,
                evidence=[*evidence, "validated provider wrapper redirect"],
            )
            provider_wrapper_redirect_count += 1
            provider_wrapper_redirects.append(
                {
                    "wrapper_url": wrapper_url,
                    "target_url": target,
                    "platform": platform,
                    "status": "accepted_for_validation",
                }
            )
            debug_log(
                debug_logger,
                "board_provider_wrapper_redirect_accepted",
                district=district.get("agency_name"),
                wrapper_url=wrapper_url,
                target_url=target,
                platform=platform,
            )
            return True

        def schedule_moved_origin_after_404(
            failed_url: str,
            *,
            trigger: str,
        ) -> bool:
            """Queue one moved-host origin after an accepted stale path returns 404."""

            nonlocal moved_origin_retry_url
            if not website_migration or moved_origin_retry_url:
                return False
            parsed_failed = urlparse(canonical_url(failed_url))
            moved_origin = canonical_url(
                parsed_failed._replace(
                    path="/", params="", query="", fragment=""
                ).geturl()
            )
            if _crawl_url_key(moved_origin) == _crawl_url_key(failed_url):
                return False
            validator = getattr(client, "validate_target_url", None)
            if not callable(validator):
                return False
            try:
                moved_origin = canonical_url(validator(moved_origin))
            except Exception as exc:
                website_recovery_attempts.append(
                    {
                        "kind": "moved_origin_after_404",
                        "url": moved_origin,
                        "status": "unavailable",
                        "trigger": trigger,
                        "error": str(exc),
                    }
                )
                return False
            if moved_origin in queued or any(
                _crawl_url_key(moved_origin) == _crawl_url_key(item)
                for item in visited
            ):
                return False
            moved_origin_retry_url = moved_origin
            queued.add(moved_origin)
            heapq.heappush(pending, (-98, 0, moved_origin))
            website_recovery_attempts.append(
                {
                    "kind": "moved_origin_after_404",
                    "url": moved_origin,
                    "status": "queued",
                    "trigger": trigger,
                    "failed_url": canonical_url(failed_url),
                }
            )
            debug_log(
                debug_logger,
                "board_moved_origin_retry_queued",
                district=district.get("agency_name"),
                failed_url=canonical_url(failed_url),
                url=moved_origin,
                trigger=trigger,
            )
            return True

        def retain_rejected_browser_404_move(
            recovery: _BrowserRenderAttempt,
            *,
            trigger: str,
            accepted: bool,
        ) -> bool:
            """Retain an accepted browser move even when its stale path is 404."""

            page = recovery.page
            if (
                page is None
                or page.status_code != 404
                or not page.website_migration_accepted
                or not accepted
            ):
                return False
            evidence = {
                "transport": (
                    "initial_configured_website_browser_transport_recovery"
                ),
                "challenge": (
                    "initial_configured_website_browser_challenge_recovery"
                ),
                "empty_homepage": (
                    "initial_configured_website_browser_empty_homepage_render"
                ),
            }.get(trigger)
            if evidence is None:
                return False
            if not record_website_migration(
                final_url=page.final_url,
                redirect_chain=page.redirect_chain,
                transport="browser",
                accepted=True,
                evidence=evidence,
                browser_navigation_evidence=str(
                    getattr(page, "website_migration_evidence", "") or ""
                ),
            ):
                return False
            failed_url = canonical_url(page.final_url)
            failed_candidate_statuses[_crawl_url_key(failed_url)] = 404
            record_fetch_error(
                failed_url,
                status_code=404,
                kind="http_status",
                browser_rejected=True,
                trigger=trigger,
            )
            schedule_moved_origin_after_404(
                failed_url,
                trigger=f"browser_{trigger}",
            )
            return True

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
            is_configured_homepage_navigation = bool(
                depth == 0 and _crawl_url_key(url) == configured_website_key
            )
            is_initial_host_variant_navigation = bool(
                initial_host_variant_url
                and depth == 0
                and _crawl_url_key(url) == _crawl_url_key(initial_host_variant_url)
            )
            is_initial_district_navigation = bool(
                is_configured_homepage_navigation
                or is_initial_host_variant_navigation
            )
            page_was_browser_rendered = False
            debug_log(debug_logger, "board_source_candidate", district=district.get("agency_name"), url=url, depth=depth)
            try:
                response = client.get(
                    url,
                    check_robots=True,
                    raise_for_status=False,
                    allow_district_website_move=bool(
                        is_initial_district_navigation and not website_migration
                    ),
                )
                response_content = response.content
                effective_status_code = response.status_code
                effective_final_url = response.final_url or url
                if is_initial_district_navigation and bool(
                    getattr(response, "website_migration_accepted", False)
                ):
                    redirect_chain = list(getattr(response, "redirect_chain", ()))
                    if is_initial_host_variant_navigation:
                        redirect_chain.insert(0, url)
                    record_website_migration(
                        final_url=effective_final_url,
                        redirect_chain=redirect_chain,
                        transport="http",
                        accepted=True,
                        evidence=(
                            "initial_configured_website_host_variant"
                            if is_initial_host_variant_navigation
                            else "initial_configured_website_https_redirect"
                        ),
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
                if isinstance(exc, RedirectDenied) and not is_initial_district_navigation:
                    if recover_known_provider_wrapper(url, exc):
                        fetch_error["provider_wrapper_recovered"] = True
                        continue
                host_variant_queued = bool(
                    is_configured_homepage_navigation
                    and not isinstance(exc, RedirectDenied)
                    and schedule_initial_host_variant(type(exc).__name__)
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
                        and not host_variant_queued
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
                    allow_district_website_move=bool(
                        is_initial_district_navigation and not website_migration
                    ),
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
                retain_rejected_browser_404_move(
                    recovery,
                    trigger="transport",
                    accepted=bool(
                        is_initial_district_navigation and not website_migration
                    ),
                )
                if not recovery.recovered:
                    continue

                assert recovery.page is not None
                if recovery.page.website_migration_accepted and not record_website_migration(
                    final_url=recovery.page.final_url,
                    redirect_chain=recovery.page.redirect_chain,
                    transport="browser",
                    accepted=bool(
                        is_initial_district_navigation and not website_migration
                    ),
                    evidence=(
                        "initial_configured_website_browser_transport_recovery"
                    ),
                    browser_navigation_evidence=str(
                        getattr(
                            recovery.page,
                            "website_migration_evidence",
                            "",
                        )
                        or ""
                    ),
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
                page_was_browser_rendered = True
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
                if is_configured_homepage_navigation:
                    schedule_initial_host_variant(type(exc).__name__)
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
                    allow_district_website_move=bool(
                        is_initial_district_navigation and not website_migration
                    ),
                )
                browser_attempted = recovery.attempted
                browser_error = recovery.error
                browser_robots_denied = recovery.robots_denied
                recovered_content = (
                    recovery.page.content if recovery.recovered else None
                )
                retain_rejected_browser_404_move(
                    recovery,
                    trigger="challenge",
                    accepted=bool(
                        is_initial_district_navigation and not website_migration
                    ),
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
                            accepted=bool(
                                is_initial_district_navigation
                                and not website_migration
                            ),
                            evidence=(
                                "initial_configured_website_browser_challenge_recovery"
                            ),
                            browser_navigation_evidence=str(
                                getattr(
                                    recovery.page,
                                    "website_migration_evidence",
                                    "",
                                )
                                or ""
                            ),
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
                page_was_browser_rendered = True
                debug_log(
                    debug_logger,
                    "challenge_recovered_with_browser",
                    district=district.get("agency_name"),
                    url=url,
                    status_code=challenge_status_code,
                    rendered_status_code=effective_status_code,
                )
            if effective_status_code >= 400:
                failed_url = canonical_url(effective_final_url or url)
                failed_candidate_statuses[_crawl_url_key(url)] = int(
                    effective_status_code
                )
                failed_candidate_statuses[_crawl_url_key(failed_url)] = int(
                    effective_status_code
                )
                record_fetch_error(
                    url,
                    status_code=effective_status_code,
                    kind="http_status",
                    **(
                        {"final_url": failed_url}
                        if _crawl_url_key(failed_url) != _crawl_url_key(url)
                        else {}
                    ),
                )
                if is_initial_host_variant_navigation:
                    website_recovery_attempts.append(
                        {
                            "kind": "initial_host_variant",
                            "url": url,
                            "status": "failed",
                            "status_code": effective_status_code,
                        }
                    )
                if (
                    effective_status_code == 404
                    and is_configured_homepage_navigation
                    and not website_migration
                ):
                    schedule_initial_host_variant("HTTP 404")
                if (
                    effective_status_code == 404
                    and website_migration
                    and not moved_origin_retry_url
                    and is_initial_district_navigation
                ):
                    schedule_moved_origin_after_404(
                        failed_url,
                        trigger="http_redirect",
                    )
                continue

            final_url = canonical_url(effective_final_url)
            if is_initial_host_variant_navigation and not website_migration:
                record_website_migration(
                    final_url=final_url,
                    redirect_chain=[url, final_url],
                    transport="http",
                    accepted=True,
                    evidence="initial_configured_website_host_variant",
                )
            if is_initial_host_variant_navigation:
                website_recovery_attempts.append(
                    {
                        "kind": "initial_host_variant",
                        "url": url,
                        "final_url": final_url,
                        "status": "recovered",
                    }
                )
            if (
                moved_origin_retry_url
                and _crawl_url_key(url) == _crawl_url_key(moved_origin_retry_url)
            ):
                crawl_base_url = final_url
                if website_migration:
                    stale_path_url = str(website_migration.get("final_url") or "")
                    if stale_path_url and stale_path_url != final_url:
                        website_migration["stale_path_url"] = stale_path_url
                    website_migration["final_url"] = final_url
                    website_migration["canonical_website_url"] = final_url
                    website_migration["moved_origin_recovery"] = True
                    chain = list(website_migration.get("redirect_chain") or [])
                    if not chain or chain[-1] != final_url:
                        chain.append(final_url)
                    website_migration["redirect_chain"] = chain
                website_recovery_attempts.append(
                    {
                        "kind": "moved_origin_after_404",
                        "url": url,
                        "final_url": final_url,
                        "status": "recovered",
                    }
                )

            detection = detect_platform(final_url, response_content, adapters=adapters)
            page_candidates = extract_board_candidates(
                response_content,
                final_url,
                crawl_base_url,
                adapters=adapters,
            )
            non_generic_detection = bool(
                getattr(detection, "matched", False)
                and _platform_name(getattr(detection, "platform", ""))
                not in {"", "generic", "unknown"}
            )
            if (
                is_initial_district_navigation
                and not page_was_browser_rendered
                and not initial_empty_homepage_browser_attempted
                and not page_candidates
                and not non_generic_detection
                and allow_browser_fallback
            ):
                initial_empty_homepage_browser_attempted = True
                render_target = final_url
                recovery = _attempt_browser_render(
                    render_target,
                    adapters,
                    browser_attempted_urls,
                    enabled=True,
                    challenge_error_prefix=(
                        "Initial empty-homepage browser remained challenged"
                    ),
                    http_error_prefix=(
                        "Initial empty-homepage browser returned HTTP"
                    ),
                    allow_district_website_move=bool(
                        is_configured_homepage_navigation and not website_migration
                    ),
                )
                recovery_record: dict[str, Any] = {
                    "url": render_target,
                    "attempted": recovery.attempted,
                    "status": "recovered" if recovery.recovered else "failed",
                }
                retain_rejected_browser_404_move(
                    recovery,
                    trigger="empty_homepage",
                    accepted=bool(
                        is_initial_district_navigation and not website_migration
                    ),
                )
                if recovery.error:
                    recovery_record["error"] = recovery.error
                    recovery_record["error_type"] = recovery.error_type
                    record_browser_error(
                        render_target,
                        recovery.error,
                        trigger="empty_homepage",
                        error_type=recovery.error_type,
                    )
                if recovery.robots_denied:
                    robots_denials.append(
                        {"url": render_target, "error": recovery.error}
                    )
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
                if recovery.recovered:
                    assert recovery.page is not None
                    if recovery.page.website_migration_accepted:
                        record_website_migration(
                            final_url=recovery.page.final_url,
                            redirect_chain=recovery.page.redirect_chain,
                            transport="browser",
                            accepted=bool(
                                is_configured_homepage_navigation
                                and not website_migration
                            ),
                            evidence=(
                                "initial_configured_website_browser_empty_homepage_render"
                            ),
                            browser_navigation_evidence=str(
                                getattr(
                                    recovery.page,
                                    "website_migration_evidence",
                                    "",
                                )
                                or ""
                            ),
                        )
                    response_content = recovery.page.content
                    effective_status_code = recovery.page.status_code
                    final_url = canonical_url(recovery.page.final_url)
                    page_was_browser_rendered = True
                    browser_attempted_urls.add(_crawl_url_key(final_url))
                    detection = detect_platform(
                        final_url, response_content, adapters=adapters
                    )
                    page_candidates = extract_board_candidates(
                        response_content,
                        final_url,
                        crawl_base_url,
                        adapters=adapters,
                    )
                    recovery_record.update(
                        {
                            "final_url": final_url,
                            "status_code": effective_status_code,
                            "candidate_count": len(page_candidates),
                        }
                    )
                    debug_log(
                        debug_logger,
                        "board_empty_homepage_recovered_with_browser",
                        district=district.get("agency_name"),
                        url=render_target,
                        final_url=final_url,
                        candidate_count=len(page_candidates),
                    )
                initial_render_recoveries.append(recovery_record)

            successful_page_fetches += 1
            candidate_content[_crawl_url_key(final_url)] = response_content
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

            for candidate in page_candidates:
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

        search_fallback_needed = bool(search_fallback)
        if search_fallback_needed and not (
            cancel_requested and cancel_requested()
        ):
            search_results, search_fallback_raw = search_known_board_sources(
                district,
                client,
                adapters,
            )
            for result in search_results:
                candidate_url = canonical_url(result.canonical_source_url)
                score = max(80, 101 - int(result.rank))
                candidate = BoardSourceCandidate(
                    url=candidate_url,
                    text=_collapse_ws(f"{result.title} {result.snippet}")[:2000],
                    discovered_from_url=BRAVE_BOARD_SEARCH_ENDPOINT,
                    score=score,
                    known_platform=_platform_name(result.platform),
                    evidence=[
                        SEARCH_FALLBACK_EVIDENCE,
                        f"Brave result rank {result.rank}",
                        (
                            "district-name token overlap "
                            f"{result.identity_overlap:.2f}"
                        ),
                    ],
                )
                existing = candidates.get(candidate_url)
                if existing is None or candidate.score > existing.score:
                    candidates[candidate_url] = candidate
            debug_log(
                debug_logger,
                "board_search_fallback_finished",
                district=district.get("agency_name"),
                status=search_fallback_raw.get("status"),
                results_returned=search_fallback_raw.get("results_returned", 0),
                known_provider_candidates=search_fallback_raw.get(
                    "known_provider_candidates", 0
                ),
                error=search_fallback_raw.get("error"),
            )

        ordered_all = sorted(
            candidates.values(),
            key=lambda item: (
                -item.score,
                0 if _crawl_url_key(item.url) in candidate_content else 1,
                item.url,
            ),
        )
        skipped_http_candidates = [
            {
                "url": item.url,
                "status_code": failed_candidate_statuses[_crawl_url_key(item.url)],
            }
            for item in ordered_all
            if _crawl_url_key(item.url) in failed_candidate_statuses
        ]
        ordered = [
            item
            for item in ordered_all
            if _crawl_url_key(item.url) not in failed_candidate_statuses
        ]
        validation_candidates = _source_validation_candidates(ordered)
        candidate_validations: list[dict[str, Any]] = []
        review_outcomes: list[tuple[BoardSourceCandidate, DiscoveryOutcome]] = []
        adapter_error_outcomes: list[tuple[BoardSourceCandidate, DiscoveryOutcome]] = []

        def enrich_outcome_raw(outcome: DiscoveryOutcome) -> None:
            outcome.raw.update(
                {
                    "visited_urls": sorted(visited),
                    "candidate_count": len(ordered_all),
                    "actionable_candidate_count": len(ordered),
                    "skipped_http_candidates": skipped_http_candidates,
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
                if outcome.status == "working":
                    identity_verified, identity_reason = (
                        _search_candidate_identity_verified(
                            district,
                            candidate,
                            outcome,
                            identity_catalog,
                        )
                    )
                    if SEARCH_FALLBACK_EVIDENCE in candidate.evidence:
                        outcome.raw["search_identity_verification"] = {
                            "verified": identity_verified,
                            "reason": identity_reason,
                        }
                    if not identity_verified:
                        outcome.status = "manual_review"
                        outcome.error_message = identity_reason
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
            "candidate_count": len(ordered_all),
            "actionable_candidate_count": len(ordered),
            "skipped_http_candidates": skipped_http_candidates,
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
