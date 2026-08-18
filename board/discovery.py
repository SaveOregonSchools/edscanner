from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import normalize_website, prefer_https_url
from search_engine import RunDebugLogger, canonical_url, debug_log, same_organization_url

from .adapters import build_adapters, detect_platform
from .http import BoardHTTPClient, RobotsDenied


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
        or host.endswith(".portal.civicclerk.com")
        or host.endswith(".civicclerk.com")
    )


def board_url_allowed(url: str, district_base_url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    return same_organization_url(url, district_base_url) or is_known_board_host(url)


def _looks_like_challenge(status_code: int, content: bytes | str, url: str) -> bool:
    sample = (content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content or ""))[:100000].casefold()
    return status_code in {401, 403, 429} or any(
        marker in sample
        for marker in (
            "captcha",
            "cf-chl-",
            "cloudflare ray id",
            "_incapsula_resource",
            "request unsuccessful",
            "access denied",
            "verify you are human",
        )
    ) or ("go.boarddocs.com" in url.casefold() and status_code == 403)


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
    return score, evidence


def extract_board_candidates(content: bytes | str, page_url: str, district_base_url: str) -> list[BoardSourceCandidate]:
    soup = BeautifulSoup(content, "lxml")
    found: dict[str, BoardSourceCandidate] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = canonical_url(urljoin(page_url, href))
        if not board_url_allowed(url, district_base_url):
            continue
        text = _collapse_ws(anchor.get_text(" ", strip=True))
        parent_text = _collapse_ws(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")[:500]
        score, evidence = score_board_link(f"{text} {parent_text}", url)
        if score < 5:
            continue
        detection = detect_platform(url)
        platform = _platform_name(getattr(detection, "platform", "")) if getattr(detection, "matched", False) else ""
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
    return sorted(found.values(), key=lambda item: (-item.score, item.url))


def _outcome_from_adapter_result(result: Any, candidate: BoardSourceCandidate) -> DiscoveryOutcome:
    source = getattr(result, "source", None)
    status = str(getattr(result, "status", "") or getattr(source, "status", "") or "manual_review")
    platform = _platform_name(
        getattr(result, "platform", "") or getattr(source, "platform", "") or candidate.known_platform or "generic"
    )
    source_url = str(
        getattr(result, "source_url", "")
        or getattr(source, "public_url", "")
        or candidate.url
    )
    metadata = dict(getattr(source, "metadata", {}) or {})
    raw = dict(getattr(result, "metadata", {}) or {})
    raw.setdefault("candidate_text", candidate.text)
    raw.setdefault("candidate_score", candidate.score)
    raw.setdefault("candidate_evidence", candidate.evidence)
    return DiscoveryOutcome(
        status=status,
        platform=platform,
        source_url=source_url,
        confidence=float(getattr(result, "confidence", 0) or getattr(source, "confidence", 0) or candidate.score),
        organization_external_id=str(
            getattr(result, "organization_external_id", "")
            or getattr(source, "external_source_id", "")
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


def discover_board_source(
    district: Mapping[str, Any],
    *,
    client: BoardHTTPClient | None = None,
    max_pages: int = 8,
    allow_browser_fallback: bool = True,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> DiscoveryOutcome:
    district_id = int(district.get("id") or 0)
    base_url = prefer_https_url(
        district.get("website_normalized") or normalize_website(district.get("website"))[0]
    )
    if not district_id or not base_url:
        return DiscoveryOutcome(
            status="error",
            platform="unknown",
            source_url=base_url,
            error_message="District has no normalized public website.",
            raw={"district_id": district_id},
        )

    owns_client = client is None
    client = client or BoardHTTPClient()
    adapters = build_adapters(client, allow_browser_fallback=allow_browser_fallback)
    pending: list[tuple[int, int, str]] = [(-100, 0, canonical_url(base_url))]
    queued = {canonical_url(base_url)}
    visited: set[str] = set()
    candidates: dict[str, BoardSourceCandidate] = {}
    challenges: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    robots_denials: list[dict[str, Any]] = []

    try:
        while pending and len(visited) < max(1, max_pages):
            if cancel_requested and cancel_requested():
                return DiscoveryOutcome(
                    status="cancelled",
                    platform="unknown",
                    source_url=base_url,
                    error_message="Cancellation requested.",
                    raw={"visited": sorted(visited)},
                )
            negative_score, depth, url = heapq.heappop(pending)
            if url in visited:
                continue
            visited.add(url)
            debug_log(debug_logger, "board_source_candidate", district=district.get("agency_name"), url=url, depth=depth)
            try:
                response = client.get(url, check_robots=True, raise_for_status=False)
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
            except Exception as exc:
                fetch_errors.append({"url": url, "error": str(exc)})
                continue
            if _looks_like_challenge(response.status_code, response.content, response.final_url):
                challenges.append({"url": url, "status_code": response.status_code})
                debug_log(debug_logger, "challenge_detected", district=district.get("agency_name"), url=url, status_code=response.status_code)
                score, evidence = score_board_link("", url)
                if is_known_board_host(url):
                    candidates[url] = BoardSourceCandidate(
                        url=response.final_url or url,
                        text="",
                        discovered_from_url=url,
                        score=max(score, 50),
                        known_platform=_platform_name(getattr(detect_platform(url), "platform", "")),
                        evidence=[*evidence, "challenge encountered"],
                    )
                continue
            if response.status_code >= 400:
                fetch_errors.append({"url": url, "status_code": response.status_code})
                continue

            final_url = canonical_url(response.final_url or url)
            detection = detect_platform(final_url, response.content, adapters=adapters)
            if getattr(detection, "matched", False) and _platform_name(getattr(detection, "platform", "")) != "generic":
                score, evidence = score_board_link("", final_url)
                candidates[final_url] = BoardSourceCandidate(
                    url=final_url,
                    text="",
                    discovered_from_url=url,
                    score=max(score, int(float(getattr(detection, "confidence", 0) or 0))),
                    known_platform=_platform_name(getattr(detection, "platform", "")),
                    evidence=[*evidence, *list(getattr(detection, "evidence", []) or [])],
                )

            for candidate in extract_board_candidates(response.content, final_url, base_url):
                existing = candidates.get(candidate.url)
                if existing is None or candidate.score > existing.score:
                    candidates[candidate.url] = candidate
                if (
                    depth < 2
                    and same_organization_url(candidate.url, base_url)
                    and not urlparse(candidate.url).path.casefold().endswith(DOCUMENT_SUFFIXES)
                    and candidate.url not in queued
                ):
                    queued.add(candidate.url)
                    heapq.heappush(pending, (-candidate.score, depth + 1, candidate.url))

        ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.url))
        for candidate in ordered:
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
            try:
                result = adapter.discover_source(district, candidate.url)
                outcome = _outcome_from_adapter_result(result, candidate)
                if outcome.status == "blocked_by_robots":
                    robots_denials.append(
                        {"url": candidate.url, "error": outcome.error_message}
                    )
                    continue
                if outcome.status in {"working", "requires_javascript", "manual_review", "blocked_by_challenge"}:
                    outcome.raw.update(
                        {
                            "visited_urls": sorted(visited),
                            "candidate_count": len(ordered),
                            "fetch_errors": fetch_errors,
                            "challenges": challenges,
                            "robots_denials": robots_denials,
                        }
                    )
                    debug_log(
                        debug_logger,
                        "board_platform_detected",
                        district=district.get("agency_name"),
                        platform=outcome.platform,
                        url=outcome.source_url,
                        status=outcome.status,
                    )
                    return outcome
            except Exception as exc:
                fetch_errors.append({"url": candidate.url, "platform": candidate.known_platform, "error": str(exc)})

        if robots_denials and not challenges:
            candidate = ordered[0] if ordered else BoardSourceCandidate(base_url, "", base_url, 0)
            return DiscoveryOutcome(
                status="blocked_by_robots",
                platform=candidate.known_platform or "unknown",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                error_message="robots.txt disallowed the public board-source request.",
                raw={
                    "visited_urls": sorted(visited),
                    "fetch_errors": fetch_errors,
                    "robots_denials": robots_denials,
                },
            )
        if challenges:
            candidate = ordered[0] if ordered else BoardSourceCandidate(base_url, "", base_url, 0)
            return DiscoveryOutcome(
                status="requires_javascript" if candidate.known_platform in {"boarddocs", "simbli"} else "blocked_by_challenge",
                platform=candidate.known_platform or "unknown",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                requires_javascript=candidate.known_platform in {"boarddocs", "simbli"},
                error_message="The public source returned an anti-bot challenge; browser or manual review is required.",
                raw={"visited_urls": sorted(visited), "fetch_errors": fetch_errors, "challenges": challenges},
            )
        if ordered:
            candidate = ordered[0]
            return DiscoveryOutcome(
                status="manual_review",
                platform=candidate.known_platform or "generic",
                source_url=candidate.url,
                confidence=float(candidate.score),
                discovered_from_url=candidate.discovered_from_url,
                error_message="Board-related public links were found but no adapter could verify a meeting source.",
                raw={"visited_urls": sorted(visited), "candidates": [item.__dict__ for item in ordered[:25]], "fetch_errors": fetch_errors},
            )
        return DiscoveryOutcome(
            status="not_found",
            platform="unknown",
            source_url=base_url,
            discovered_from_url=base_url,
            error_message="No public school-board meeting source was found on the inspected district pages.",
            raw={"visited_urls": sorted(visited), "fetch_errors": fetch_errors},
        )
    finally:
        if owns_client:
            client.close()
