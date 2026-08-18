from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import normalize_website, prefer_https_url
from search_engine import RunDebugLogger, canonical_url, debug_log, same_organization_url

from .adapters import build_adapters, detect_platform
from .adapters.base import ChallengeAssessment, assess_challenge
from .http import BoardHTTPClient, RobotsDenied
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
        confidence=_outcome_confidence(result, source, candidate),
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
    max_pages: int = 8,
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

        pending: list[tuple[int, int, str]] = [(-100, 0, canonical_url(base_url))]
        queued = {canonical_url(base_url)}
        visited: set[str] = set()
        candidates: dict[str, BoardSourceCandidate] = {}
        candidate_content: dict[tuple[str, str, str, str], bytes] = {}
        browser_attempted_urls: set[tuple[str, str, str, str]] = set()
        challenges: list[dict[str, Any]] = []
        challenge_recoveries: list[dict[str, Any]] = []
        fetch_errors: list[dict[str, Any]] = []
        robots_denials: list[dict[str, Any]] = []

        while pending and len(visited) < max(1, max_pages):
            if cancel_requested and cancel_requested():
                return DiscoveryOutcome(
                    status="cancelled",
                    platform="unknown",
                    source_url=base_url,
                    error_message="Cancellation requested.",
                    raw={
                        "visited": sorted(visited),
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
            response_content = response.content
            effective_status_code = response.status_code
            effective_final_url = response.final_url or url
            challenge = assess_challenge(
                response.status_code,
                response_content,
                response.final_url,
            )
            if challenge.is_challenge:
                debug_log(
                    debug_logger,
                    "challenge_detected",
                    district=district.get("agency_name"),
                    url=url,
                    status_code=response.status_code,
                    category=challenge.category,
                    marker=challenge.marker,
                )
                browser_attempted = False
                browser_error = ""
                browser_robots_denied = False
                recovered_content: bytes | None = None
                browser_key = _crawl_url_key(response.final_url or url)
                if (
                    allow_browser_fallback
                    and challenge.browser_retry_allowed
                    and browser_key not in browser_attempted_urls
                ):
                    renderer = _browser_renderer_for_url(response.final_url or url, adapters)
                    if renderer is not None:
                        browser_attempted = True
                        browser_attempted_urls.add(browser_key)
                        try:
                            render_with_metadata = getattr(
                                renderer,
                                "render_page_with_metadata",
                                None,
                            )
                            if callable(render_with_metadata):
                                rendered_page = render_with_metadata(response.final_url or url)
                                rendered = rendered_page.content
                                rendered_status = rendered_page.status_code
                                rendered_final_url = rendered_page.final_url
                            else:
                                rendered = renderer.render_page(response.final_url or url)
                                rendered_status = 200
                                rendered_final_url = response.final_url or url
                            rendered_challenge = assess_challenge(
                                rendered_status,
                                rendered,
                                rendered_final_url,
                            )
                            if rendered_challenge.is_challenge:
                                browser_error = (
                                    "Rendered page remained challenged "
                                    f"({rendered_challenge.marker or rendered_challenge.category})."
                                )
                            else:
                                recovered_content = rendered
                                effective_final_url = rendered_final_url
                                effective_status_code = rendered_status
                        except RobotsDenied as exc:
                            browser_error = str(exc)
                            browser_robots_denied = True
                            robots_denials.append({"url": url, "error": str(exc)})
                        except Exception as exc:
                            browser_error = str(exc)
                record = _challenge_record(
                    challenge,
                    url=response.final_url or url,
                    status_code=response.status_code,
                    browser_fallback_attempted=browser_attempted,
                    browser_fallback_error=browser_error,
                )
                if recovered_content is None:
                    if browser_robots_denied:
                        continue
                    challenges.append(record)
                    score, evidence = score_board_link("", url)
                    challenged_url = canonical_url(response.final_url or url)
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
                    status_code=response.status_code,
                )
            if effective_status_code >= 400:
                fetch_errors.append({"url": url, "status_code": response.status_code})
                continue

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

            for candidate in extract_board_candidates(response_content, final_url, base_url):
                existing = candidates.get(candidate.url)
                if existing is None or candidate.score > existing.score:
                    candidates[candidate.url] = candidate
                if (
                    depth < 2
                    and same_organization_url(candidate.url, base_url)
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
                candidate_url = canonical_url(candidate.url)
                candidate_key = _crawl_url_key(candidate_url)
                # A challenged page gets at most one browser attempt. Recovered
                # HTML is validated directly instead of refetching the cached
                # challenge and launching a second browser.
                if (
                    candidate_key in browser_attempted_urls
                    and candidate_key not in candidate_content
                ):
                    continue
                result = adapter.discover_source(
                    district,
                    candidate.url,
                    html=candidate_content.get(candidate_key),
                )
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
                            "challenge_recoveries": challenge_recoveries,
                            "robots_denials": robots_denials,
                        }
                    )
                    if provider_directory_raw:
                        outcome.raw["provider_directory"] = provider_directory_raw
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
                    "challenge_recoveries": challenge_recoveries,
                    "robots_denials": robots_denials,
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
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
                raw={
                    "visited_urls": sorted(visited),
                    "fetch_errors": fetch_errors,
                    "challenges": challenges,
                    "challenge_recoveries": challenge_recoveries,
                    **(
                        {"provider_directory": provider_directory_raw}
                        if provider_directory_raw
                        else {}
                    ),
                },
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
                raw={
                    "visited_urls": sorted(visited),
                    "candidates": [item.__dict__ for item in ordered[:25]],
                    "fetch_errors": fetch_errors,
                    "challenge_recoveries": challenge_recoveries,
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
            source_url=base_url,
            discovered_from_url=base_url,
            error_message="No public school-board meeting source was found on the inspected district pages.",
            raw={
                "visited_urls": sorted(visited),
                "fetch_errors": fetch_errors,
                "challenge_recoveries": challenge_recoveries,
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
