from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from common import (
    BRAVE_SEARCH_API_KEY_ENV,
    STATE_NAME_TO_ABBR,
    get_local_setting,
    normalize_state,
)

from .adapters import detect_platform
from .http import BoardHTTPClient
from .provider_directories import normalized_organization_name


BRAVE_BOARD_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_BOARD_SEARCH_RESULT_LIMIT = 10
BRAVE_BOARD_EVIDENCE_QUERY = (
    '("board meetings" OR BoardBook OR BoardDocs OR Diligent OR Simbli OR CivicClerk)'
)

_STATE_ABBR_TO_SEARCH_NAME = {
    abbreviation: name.title()
    for name, abbreviation in STATE_NAME_TO_ABBR.items()
}
_STATE_ABBR_TO_SEARCH_NAME.update(
    {
        "AA": "Armed Forces Americas",
        "AE": "Armed Forces Europe",
        "AP": "Armed Forces Pacific",
        "AS": "American Samoa",
        "FM": "Federated States of Micronesia",
        "GU": "Guam",
        "MH": "Marshall Islands",
        "MP": "Northern Mariana Islands",
        "PR": "Puerto Rico",
        "PW": "Palau",
        "UM": "United States Minor Outlying Islands",
        "VI": "United States Virgin Islands",
    }
)


@dataclass(frozen=True, slots=True)
class BoardSearchResult:
    url: str
    title: str
    snippet: str
    rank: int
    platform: str
    canonical_source_url: str
    identity_overlap: float


def _plain_text(value: Any) -> str:
    if not value:
        return ""
    return " ".join(
        BeautifulSoup(str(value), "html.parser").get_text(" ").split()
    )


def _quoted_query_phrase(value: Any) -> str:
    """Return one inert Brave phrase without query-control characters."""

    text = " ".join(str(value or "").replace("\\", " ").replace('"', " ").split())
    return f'"{text}"' if text else ""


def _meaningful_district_query(value: Any) -> str:
    """Return safe unquoted identity terms, excluding Boolean controls."""

    return " ".join(
        token
        for token in normalized_organization_name(value).split()
        if not token.isdigit() and token not in {"and", "not", "or"}
    )


def _identity_overlap(district_name: str, evidence_text: str) -> float:
    district_tokens = {
        token
        for token in normalized_organization_name(district_name).split()
        if not token.isdigit()
    }
    evidence_tokens = {
        token
        for token in normalized_organization_name(evidence_text).split()
        if not token.isdigit()
    }
    if not district_tokens or not evidence_tokens:
        return 0.0
    return len(district_tokens & evidence_tokens) / len(district_tokens)


def search_known_board_sources(
    district: Mapping[str, Any],
    client: BoardHTTPClient,
    adapters: Sequence[Any],
    *,
    limit: int = BRAVE_BOARD_SEARCH_RESULT_LIMIT,
) -> tuple[list[BoardSearchResult], dict[str, Any]]:
    """Query Brave's official API for canonical board-provider sources.

    This is optional independent discovery evidence alongside the district
    crawl. It never treats arbitrary search results as sources: only URLs
    recognized by a known provider adapter are returned, and the normal
    adapter fetch/identity checks still run afterwards.
    """

    api_key = get_local_setting(BRAVE_SEARCH_API_KEY_ENV).strip()
    district_name = " ".join(
        str(district.get("agency_name") or district.get("name") or "").split()
    )
    state = normalize_state(district.get("state"))
    diagnostics: dict[str, Any] = {
        "provider": "brave",
        "requested": True,
        "available": bool(api_key),
        "query": "",
        "results_returned": 0,
        "known_provider_candidates": 0,
    }
    if not api_key or not district_name:
        diagnostics["status"] = "unavailable"
        diagnostics["reason"] = (
            "Brave Search API key is not configured."
            if not api_key
            else "District name is unavailable."
        )
        return [], diagnostics

    name_query = _meaningful_district_query(district_name)
    if not name_query:
        diagnostics["status"] = "unavailable"
        diagnostics["reason"] = "District name has no safe identifying search terms."
        return [], diagnostics
    state_name = _STATE_ABBR_TO_SEARCH_NAME.get(state)
    state_query = (
        " ".join(state_name.split())
        if state_name
        else _quoted_query_phrase(state)
    )
    query = " ".join(
        part
        for part in (
            name_query,
            state_query,
            BRAVE_BOARD_EVIDENCE_QUERY,
        )
        if part
    )
    diagnostics["query"] = query
    count = max(1, min(int(limit), BRAVE_BOARD_SEARCH_RESULT_LIMIT))
    request_url = f"{BRAVE_BOARD_SEARCH_ENDPOINT}?{urlencode({'q': query, 'count': count, 'safesearch': 'off', 'search_lang': 'en'})}"
    try:
        _response, payload = client.get_json(
            request_url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            check_robots=False,
            max_bytes=2_000_000,
        )
    except Exception as exc:
        diagnostics["status"] = "error"
        diagnostics["error"] = str(exc)
        return [], diagnostics

    if not isinstance(payload, Mapping):
        diagnostics["status"] = "invalid_response"
        diagnostics["reason"] = "Brave Search returned a non-object JSON payload."
        return [], diagnostics
    web_payload = payload.get("web")
    if web_payload is None:
        raw_results: Any = []
    elif isinstance(web_payload, Mapping):
        raw_results = web_payload.get("results", [])
    else:
        raw_results = None
    if not isinstance(raw_results, list):
        diagnostics["status"] = "invalid_response"
        diagnostics["reason"] = (
            "Brave Search returned an unexpected web-results payload."
        )
        return [], diagnostics
    diagnostics["results_returned"] = len(raw_results)
    found: dict[str, BoardSearchResult] = {}
    skipped: list[dict[str, Any]] = []
    for rank, item in enumerate(raw_results[:count], start=1):
        if not isinstance(item, Mapping):
            continue
        raw_url = str(item.get("url") or "").strip()
        title = _plain_text(item.get("title"))
        snippet = _plain_text(item.get("description"))
        try:
            public_url = client.validate_target_url(raw_url)
        except Exception as exc:
            skipped.append(
                {"rank": rank, "url": raw_url, "reason": f"unsafe URL: {exc}"}
            )
            continue
        detection = detect_platform(public_url, adapters=list(adapters))
        platform = str(getattr(detection, "platform", "") or "").strip().casefold()
        if not getattr(detection, "matched", False) or platform in {
            "",
            "generic",
            "unknown",
        }:
            continue
        canonical = str(getattr(detection, "canonical_url", "") or public_url)
        try:
            canonical = client.validate_target_url(canonical)
        except Exception as exc:
            skipped.append(
                {
                    "rank": rank,
                    "url": raw_url,
                    "reason": f"unsafe canonical URL: {exc}",
                }
            )
            continue
        overlap = _identity_overlap(district_name, f"{title} {snippet}")
        if overlap < 0.5:
            skipped.append(
                {
                    "rank": rank,
                    "url": canonical,
                    "reason": "district-name evidence was too weak",
                    "identity_overlap": round(overlap, 4),
                }
            )
            continue
        result = BoardSearchResult(
            url=public_url,
            title=title,
            snippet=snippet,
            rank=rank,
            platform=platform,
            canonical_source_url=canonical,
            identity_overlap=round(overlap, 6),
        )
        existing = found.get(canonical)
        if existing is None or result.rank < existing.rank:
            found[canonical] = result

    results = sorted(found.values(), key=lambda item: item.rank)
    diagnostics["known_provider_candidates"] = len(results)
    diagnostics["status"] = "completed"
    diagnostics["skipped"] = skipped[:25]
    diagnostics["candidates"] = [
        {
            "rank": item.rank,
            "platform": item.platform,
            "url": item.canonical_source_url,
            "title": item.title,
            "identity_overlap": item.identity_overlap,
        }
        for item in results
    ]
    return results, diagnostics


__all__ = [
    "BRAVE_BOARD_SEARCH_ENDPOINT",
    "BRAVE_BOARD_SEARCH_RESULT_LIMIT",
    "BoardSearchResult",
    "search_known_board_sources",
]
