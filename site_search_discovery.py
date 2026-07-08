from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from common import connect_db, init_db, json_dumps, normalize_website, prefer_https_url, utc_now_iso
from search_engine import (
    RunDebugLogger,
    SearchSettings,
    can_fetch,
    canonical_url,
    collapse_ws,
    debug_log,
    fetch_limited,
    filename_title,
    load_robots,
    make_session,
    parse_html,
    parse_pdf,
    score_match,
    same_organization_url,
)


LOGGER = logging.getLogger(__name__)

QUERY_PARAM_CANDIDATES = {
    "q",
    "query",
    "search",
    "searchterm",
    "searchTerm",
    "keyword",
    "keywords",
    "terms",
    "text",
    "s",
    "SearchString",
    "searchString",
    "search_query",
}

COMMON_URL_TEMPLATES = [
    "/search?q={query}",
    "/search/?q={query}",
    "/search?query={query}",
    "/search/?query={query}",
    "/search?search={query}",
    "/search-results?q={query}",
    "/search-results?query={query}",
    "/site-search?q={query}",
    "/site-search?query={query}",
    "/apps/search/?q={query}",
    "/apps/search?term={query}",
    "/Search?search={query}",
    "/Search?query={query}",
    "/search/default.aspx?search={query}",
    "/site/default.aspx?PageType=6&SearchString={query}",
    "/?s={query}",
]

SEARCH_WORDS = ("search", "site search", "search our site", "find")
EXCLUDED_SEARCH_CONTEXT = (
    "login",
    "staff",
    "portal",
    "student",
    "parent",
    "payment",
    "calendar event search",
    "directory search",
    "employee search",
    "nutrition search",
    "athletics search",
    "registration",
)
NON_CONTENT_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".css",
    ".js",
    ".ico",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
)
SEARCH_LOOP_MARKERS = ("/search", "search?", "query=", "SearchString=", "searchTerm=", "searchterm=")
EDLIO_SEARCH_HOST = "search.edlio.com"


def _row_to_dict(row: Any | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def build_search_url(template: str, query_text: str) -> str:
    return str(template or "").replace("{query}", quote_plus(str(query_text or "").strip()))


def _contains_search_word(text: str) -> bool:
    text_fold = collapse_ws(text).casefold()
    return any(word in text_fold for word in SEARCH_WORDS)


def _is_excluded_context(text: str) -> bool:
    text_fold = collapse_ws(text).casefold()
    return any(word in text_fold for word in EXCLUDED_SEARCH_CONTEXT)


def _template_from_url(url: str, query_param: str, extra_params: dict[str, str] | None = None) -> str:
    parsed = urlparse(url)
    pairs = OrderedDict((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in (extra_params or {}).items():
        pairs.setdefault(key, value)
    pairs[query_param] = "{query}"
    query = urlencode(list(pairs.items()), doseq=True).replace("%7Bquery%7D", "{query}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", query, ""))


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (candidate.get("search_method") or "GET", candidate.get("search_url_template") or "")


def _candidate_from_form(form: Tag, page_url: str, base_url: str) -> dict[str, Any] | None:
    method = str(form.get("method") or "GET").strip().upper()
    action = str(form.get("action") or page_url).strip() or page_url
    action_url = urljoin(page_url, action)
    form_text = " ".join(
        [
            str(form.get("role") or ""),
            str(form.get("class") or ""),
            str(form.get("id") or ""),
            str(form.get("aria-label") or ""),
            form.get_text(" ", strip=True),
        ]
    )
    if _is_excluded_context(form_text):
        return None
    if not same_organization_url(action_url, base_url):
        return None

    text_inputs: list[Tag] = []
    hidden_params: dict[str, str] = {}
    for input_node in form.find_all("input"):
        input_type = str(input_node.get("type") or "text").strip().casefold()
        name = str(input_node.get("name") or "").strip()
        if input_type == "hidden" and name:
            hidden_params[name] = str(input_node.get("value") or "")
        elif input_type in {"text", "search", ""}:
            text_inputs.append(input_node)

    scored_inputs: list[tuple[int, Tag]] = []
    for input_node in text_inputs:
        name = str(input_node.get("name") or "").strip()
        descriptor = " ".join(
            [
                name,
                str(input_node.get("id") or ""),
                str(input_node.get("placeholder") or ""),
                str(input_node.get("aria-label") or ""),
            ]
        )
        score = 0
        if name in QUERY_PARAM_CANDIDATES or name.casefold() in {item.casefold() for item in QUERY_PARAM_CANDIDATES}:
            score += 30
        if _contains_search_word(descriptor):
            score += 20
        if str(input_node.get("type") or "").casefold() == "search":
            score += 10
        if _contains_search_word(form_text):
            score += 10
        if score:
            scored_inputs.append((score, input_node))

    if not scored_inputs:
        return None
    scored_inputs.sort(key=lambda item: item[0], reverse=True)
    query_input = scored_inputs[0][1]
    query_param = str(query_input.get("name") or "").strip() or "q"
    profile_type = "html_get_form" if method == "GET" else "html_post_form"
    return {
        "profile_type": profile_type,
        "search_method": method,
        "query_param": query_param,
        "search_url_template": _template_from_url(action_url, query_param, hidden_params),
        "extra_params": hidden_params,
        "source_url": page_url,
        "score_hint": 30 if method == "GET" else 5,
    }


def _candidate_from_search_link(anchor: Tag, page_url: str, base_url: str) -> dict[str, Any] | None:
    href = str(anchor.get("href") or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    context = " ".join(
        [
            anchor.get_text(" ", strip=True),
            str(anchor.get("aria-label") or ""),
            str(anchor.get("title") or ""),
            str(anchor.get("class") or ""),
            str(anchor.get("id") or ""),
            href,
        ]
    )
    if not _contains_search_word(context) or _is_excluded_context(context):
        return None
    url = canonical_url(urljoin(page_url, href))
    if not same_organization_url(url, base_url):
        return None
    query_pairs = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    for param in QUERY_PARAM_CANDIDATES:
        if param in query_pairs:
            return {
                "profile_type": "url_template",
                "search_method": "GET",
                "query_param": param,
                "search_url_template": _template_from_url(url, param),
                "extra_params": {key: value for key, value in query_pairs.items() if key != param},
                "source_url": page_url,
                "score_hint": 15,
            }
    return {
        "profile_type": "search_page_link",
        "search_method": "GET",
        "search_page_url": url,
        "source_url": page_url,
        "score_hint": 5,
    }


def _detect_provider_guess(html: bytes, final_url: str) -> str:
    text = html.decode("utf-8", errors="ignore")
    folded = text.casefold()
    host = (urlparse(final_url).hostname or "").casefold()
    checks = [
        ("Finalsite", ("finalsite", "fsresource")),
        ("Blackboard / Schoolwires", ("schoolwires", "blackboard")),
        ("Edlio", ("edlio",)),
        ("Apptegy", ("apptegy",)),
        ("ParentSquare", ("parentsquare",)),
        ("Campus Suite", ("campussuite",)),
        ("SchoolMessenger", ("schoolmessenger",)),
        ("WordPress", ("wp-content", "wordpress")),
        ("Drupal", ("drupal",)),
        ("Google Programmable Search", ("cse.google.com", "google custom search")),
        ("Algolia", ("algolia",)),
        ("SearchStax / Solr", ("searchstax", "solr")),
        ("SharePoint", ("sharepoint",)),
    ]
    for provider, markers in checks:
        if any(marker in folded or marker in host for marker in markers):
            return provider
    return "Custom"


def _discover_candidates_from_html(html: bytes, page_url: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    candidates: list[dict[str, Any]] = []
    for form in soup.find_all("form"):
        candidate = _candidate_from_form(form, page_url, base_url)
        if candidate:
            candidates.append(candidate)
    for anchor in soup.find_all("a", href=True):
        candidate = _candidate_from_search_link(anchor, page_url, base_url)
        if candidate:
            candidates.append(candidate)
    return candidates


def _edlio_config_value(text: str, key: str) -> str:
    pattern = rf"window\.edlio\.{re.escape(key)}\s*=\s*['\"]([^'\"]*)['\"]"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_edlio_config(html: bytes) -> dict[str, str]:
    text = html.decode("utf-8", errors="ignore")
    website_id = _edlio_config_value(text, "websiteId")
    district_id = _edlio_config_value(text, "districtId")
    search_domain = _edlio_config_value(text, "searchDomain") or f"https://{EDLIO_SEARCH_HOST}/"
    if not website_id and not district_id:
        return {}
    identifier = district_id if district_id and district_id != "0" else website_id
    if not identifier:
        return {}
    return {
        "website_id": website_id,
        "district_id": district_id,
        "identifier": identifier,
        "search_domain": search_domain,
    }


def _edlio_candidate_from_html(html: bytes, page_url: str, base_url: str) -> dict[str, Any] | None:
    config = _extract_edlio_config(html)
    if not config:
        return None
    search_domain = config["search_domain"].rstrip("/") + "/"
    api_url = urljoin(search_domain, f"{config['identifier']}/search")
    extra_params = {"offset": "0"}
    if config.get("website_id"):
        extra_params["boostWebsiteId"] = config["website_id"]
    return {
        "profile_type": "known_platform",
        "search_method": "GET",
        "query_param": "q",
        "search_url_template": _template_from_url(api_url, "q", extra_params),
        "extra_params": extra_params,
        "source_url": page_url,
        "provider_guess": "Edlio",
        "result_format": "edlio_json",
        "score_hint": 45,
        "uses_external_provider": True,
        "external_provider_host": EDLIO_SEARCH_HOST,
        "raw_provider_config": config,
    }


def _common_template_candidates(base_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for template in COMMON_URL_TEMPLATES:
        absolute = urljoin(base_url, template)
        query = dict(parse_qsl(urlparse(absolute).query, keep_blank_values=True))
        query_param = next((key for key, value in query.items() if value == "{query}"), "")
        candidates.append(
            {
                "profile_type": "url_template",
                "search_method": "GET",
                "query_param": query_param,
                "search_url_template": absolute,
                "extra_params": {key: value for key, value in query.items() if value != "{query}"},
                "source_url": base_url,
                "score_hint": 0,
            }
        )
    return candidates


def _looks_like_search_url(url: str) -> bool:
    folded = url.casefold()
    return any(marker.casefold() in folded for marker in SEARCH_LOOP_MARKERS)


def _looks_like_homepage_redirect(final_url: str, base_url: str, query_text: str) -> bool:
    final = urlparse(final_url)
    base = urlparse(base_url)
    if (final.hostname or "").casefold().removeprefix("www.") != (base.hostname or "").casefold().removeprefix("www."):
        return False
    final_path = (final.path or "/").rstrip("/") or "/"
    base_path = (base.path or "/").rstrip("/") or "/"
    query_present = query_text.casefold() in (final.query or "").casefold()
    return final_path == base_path and not query_present


def _html_suggests_javascript_search(html: bytes, provider_guess: str) -> bool:
    folded = html.decode("utf-8", errors="ignore").casefold()
    return (
        provider_guess == "Finalsite"
        or "search-results" in folded
        or "site search" in folded
        or "fssearch" in folded
        or "data-search" in folded
    )


def _looks_like_challenge_page(status_code: int | None, html: bytes | str, url: str) -> bool:
    text = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else str(html or "")
    folded = text.casefold()
    host = (urlparse(url).hostname or "").casefold()
    if status_code in {403, 429} and any(marker in folded for marker in ("cloudflare", "cf-chl", "captcha", "checking your browser", "forbidden")):
        return True
    return "cloudflare" in host and any(marker in folded for marker in ("captcha", "challenge", "cf-chl"))


def _valid_result_url(url: str, base_url: str, generated_search_url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if urlparse(url).path.casefold().endswith(NON_CONTENT_SUFFIXES):
        return False
    if canonical_url(url) == canonical_url(generated_search_url):
        return False
    if _looks_like_search_url(url):
        return False
    return same_organization_url(url, base_url)


def parse_search_results_page(
    html: bytes,
    final_url: str,
    base_url: str,
    query_text: str,
    profile: dict[str, Any],
    *,
    max_links: int = 10,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    generated_search_url = build_search_url(profile.get("search_url_template") or final_url, query_text)
    likely_containers: list[Tag] = []
    for node in soup.find_all(True):
        descriptor = " ".join([str(node.get("id") or ""), " ".join(str(item) for item in node.get("class") or [])])
        if any(marker in descriptor.casefold() for marker in ("result", "search-result", "results", "site-search")):
            likely_containers.append(node)
    containers = likely_containers or [soup]

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for container in containers:
        for anchor in container.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            url = canonical_url(urljoin(final_url, href))
            if url in seen or not _valid_result_url(url, base_url, generated_search_url):
                continue
            parent_text = collapse_ws(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
            title = collapse_ws(anchor.get_text(" ", strip=True)) or filename_title(url)
            snippet = parent_text
            seen.add(url)
            results.append(
                {
                    "url": url,
                    "title": title[:500],
                    "snippet": snippet[:1000],
                    "rank": len(results) + 1,
                    "source": "search_results_page",
                }
            )
            if len(results) >= max_links:
                return results
    return results


def _extract_edlio_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("items"),
        payload.get("results"),
        payload.get("hits"),
    ]
    hits = payload.get("hits")
    if isinstance(hits, dict):
        candidates.append(hits.get("hits"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _edlio_text(value: Any) -> str:
    text = str(value or "")
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return collapse_ws(text)


def parse_edlio_search_results(
    payload: bytes | dict[str, Any],
    base_url: str,
    generated_search_url: str,
    *,
    max_links: int = 10,
) -> list[dict[str, Any]]:
    if isinstance(payload, bytes):
        try:
            data = json.loads(payload.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return []
    else:
        data = payload

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _extract_edlio_items(data):
        source = item.get("_source") if isinstance(item.get("_source"), dict) else item
        href = str(
            source.get("Url")
            or source.get("url")
            or source.get("URL")
            or source.get("link")
            or ""
        ).strip()
        if not href:
            continue
        url = canonical_url(urljoin(base_url, href))
        if url in seen or not _valid_result_url(url, base_url, generated_search_url):
            continue
        title = _edlio_text(source.get("Title") or source.get("title") or source.get("Name") or filename_title(url))
        snippet = _edlio_text(source.get("PreviewText") or source.get("previewText") or source.get("description") or source.get("Description"))
        seen.add(url)
        results.append(
            {
                "url": url,
                "title": title[:500],
                "snippet": snippet[:1000],
                "rank": len(results) + 1,
                "source": "edlio_search_api",
            }
        )
        if len(results) >= max_links:
            return results
    return results


def _is_edlio_candidate(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("result_format") == "edlio_json"
        or (
            candidate.get("provider_guess") == "Edlio"
            and (urlparse(candidate.get("search_url_template") or "").hostname or "").casefold() == EDLIO_SEARCH_HOST
        )
    )


def _is_edlio_profile(profile: dict[str, Any]) -> bool:
    return (
        profile.get("provider_guess") == "Edlio"
        and (urlparse(profile.get("search_url_template") or "").hostname or "").casefold() == EDLIO_SEARCH_HOST
    )


def _fetch_and_score_result(
    session: requests.Session,
    url: str,
    query_text: str,
    settings: SearchSettings,
) -> dict[str, Any] | None:
    response, content = fetch_limited(session, url, settings)
    if response.status_code >= 400:
        return None
    final_url = canonical_url(response.url)
    content_type_header = response.headers.get("Content-Type", "").casefold()
    is_pdf = "application/pdf" in content_type_header or urlparse(final_url).path.casefold().endswith(".pdf")
    if is_pdf:
        content_type = "application/pdf"
        title = filename_title(final_url)
        headings: list[str] = []
        text = parse_pdf(content)
    elif "html" in content_type_header or "text/plain" in content_type_header or not content_type_header:
        content_type = "text/html"
        title, headings, text, _links = parse_html(content, final_url)
    else:
        return None
    match = score_match(query_text, title, headings, text, final_url, content_type)
    if not match:
        return None
    match["status_code"] = response.status_code
    return match


def browser_render_search_results_page(
    url: str,
    settings: SearchSettings,
    *,
    wait_ms: int = 1500,
) -> tuple[str, bytes]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for browser-backed district search. Install requirements and run: py -m playwright install chromium") from exc

    timeout_ms = int(max(1.0, settings.browser_render_timeout_seconds) * 1000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=settings.user_agent)
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            final_url = page.url
            html = page.content().encode("utf-8")
            if _looks_like_challenge_page(None, html, final_url):
                raise RuntimeError("Browser rendered a challenge or CAPTCHA page.")
            return final_url, html
        finally:
            browser.close()


def _test_candidate(
    candidate: dict[str, Any],
    district: dict[str, Any],
    base_url: str,
    test_query: str,
    session: requests.Session,
    settings: SearchSettings,
    robots: Any,
    provider_guess: str,
    debug_logger: RunDebugLogger | None,
) -> dict[str, Any]:
    attempted_url = build_search_url(candidate.get("search_url_template") or "", test_query)
    raw: dict[str, Any] = {"candidate": candidate, "attempted_url": attempted_url, "confirmed_urls": []}
    confidence = float(candidate.get("score_hint") or 0)
    result_links: list[dict[str, Any]] = []
    status_code: int | None = None
    content_type = ""
    error_message = ""
    success = False
    is_edlio = _is_edlio_candidate(candidate)

    debug_log(debug_logger, "profile_candidate_test", district=district.get("agency_name"), url=attempted_url)
    if candidate.get("search_method") != "GET":
        error_message = "Only GET district search profiles are executed in this version."
    elif not attempted_url or not can_fetch(robots, settings, attempted_url):
        error_message = "Blocked by robots.txt."
    else:
        try:
            response, content = fetch_limited(session, attempted_url, settings)
            status_code = response.status_code
            content_type = response.headers.get("Content-Type", "")
            raw["final_url"] = response.url
            raw["content_length"] = len(content)
            if _looks_like_challenge_page(response.status_code, content, response.url):
                confidence -= 50
                error_message = "Site returned a challenge or CAPTCHA page."
                raw["diagnosis"] = "blocked_by_challenge"
            if response.status_code < 400:
                confidence += 30
                if not is_edlio and _looks_like_homepage_redirect(response.url, base_url, test_query):
                    confidence -= 50
                    error_message = "Search request redirected to the homepage without preserving the query."
                    raw["diagnosis"] = "redirected_to_homepage"
                elif is_edlio:
                    result_links = parse_edlio_search_results(
                        content,
                        base_url,
                        attempted_url,
                        max_links=10,
                    )
                    raw["result_links"] = result_links
                    raw["result_format"] = "edlio_json"
                    if result_links:
                        confidence += 20
                    if len(result_links) > 1:
                        confidence += 10
                    if any(same_organization_url(item["url"], base_url) for item in result_links):
                        confidence += 20
                    if not result_links:
                        error_message = "Edlio search API returned no parseable same-domain result links."
                        raw["diagnosis"] = "no_parseable_edlio_links"

                    confirmed = 0
                    for item in result_links[:5]:
                        if not can_fetch(robots, settings, item["url"]):
                            continue
                        try:
                            match = _fetch_and_score_result(session, item["url"], test_query, settings)
                        except Exception as exc:
                            raw.setdefault("result_errors", []).append({"url": item["url"], "error": str(exc)})
                            continue
                        if match:
                            confirmed += 1
                            raw["confirmed_urls"].append(match["url"])
                            if confirmed == 1:
                                confidence += 30
                        if settings.delay_seconds:
                            time.sleep(settings.delay_seconds)
                    success = confirmed > 0
                elif "html" in content_type.casefold() or not content_type:
                    result_links = parse_search_results_page(
                        content,
                        response.url,
                        base_url,
                        test_query,
                        candidate,
                        max_links=10,
                    )
                    raw["result_links"] = result_links
                    if result_links:
                        confidence += 20
                    if len(result_links) > 1:
                        confidence += 10
                    if any(same_organization_url(item["url"], base_url) for item in result_links):
                        confidence += 20
                    title, headings, _text, _links = parse_html(content, response.url)
                    if "search" in " ".join([title, *headings]).casefold():
                        confidence += 10
                    if not result_links:
                        if _html_suggests_javascript_search(content, provider_guess):
                            error_message = "Search page returned HTML but no parseable result links; results may require JavaScript or a provider-specific parser."
                            raw["diagnosis"] = "no_parseable_links_probable_javascript"
                        else:
                            error_message = "Search page returned HTML but no parseable same-domain result links."
                            raw["diagnosis"] = "no_parseable_links"

                    confirmed = 0
                    for item in result_links[:5]:
                        if not can_fetch(robots, settings, item["url"]):
                            continue
                        try:
                            match = _fetch_and_score_result(session, item["url"], test_query, settings)
                        except Exception as exc:
                            raw.setdefault("result_errors", []).append({"url": item["url"], "error": str(exc)})
                            continue
                        if match:
                            confirmed += 1
                            raw["confirmed_urls"].append(match["url"])
                            if confirmed == 1:
                                confidence += 30
                        if settings.delay_seconds:
                            time.sleep(settings.delay_seconds)
                    success = confirmed > 0
                else:
                    error_message = f"Unsupported content type: {content_type}"
            else:
                confidence -= 50
                if not error_message:
                    error_message = f"HTTP {response.status_code}"
        except Exception as exc:
            confidence -= 50
            error_message = str(exc)

    if not result_links:
        confidence -= 30
    confidence = max(0.0, min(100.0, confidence))
    status = "working" if success and confidence >= 70 else "manual_review" if confidence >= 40 else "search_found_but_failed"
    if error_message == "Blocked by robots.txt.":
        status = "blocked_by_robots"
    elif raw.get("diagnosis") == "blocked_by_challenge":
        status = "blocked_by_challenge"
    elif raw.get("diagnosis") == "no_parseable_links_probable_javascript":
        status = "requires_javascript"

    raw["confidence"] = confidence
    raw["success"] = success
    raw["error_message"] = error_message
    debug_log(
        debug_logger,
        "profile_candidate_success" if success else "profile_candidate_failed",
        district=district.get("agency_name"),
        url=attempted_url,
        result_count=len(result_links),
        confidence=confidence,
        error=error_message,
    )
    return {
        **candidate,
        "website_normalized": base_url,
        "provider_guess": provider_guess,
        "profile_status": status,
        "confidence": confidence,
        "test_query": test_query,
        "test_result_count": len(result_links),
        "test_success": 1 if success else 0,
        "attempted_url": attempted_url,
        "status_code": status_code,
        "content_type": content_type,
        "error_message": error_message,
        "raw": raw,
    }


def _save_profile_result(
    district_id: int,
    result: dict[str, Any],
    *,
    db_path: Path | str | None = None,
    replace_existing: bool = True,
) -> dict[str, Any]:
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        if replace_existing:
            conn.execute("DELETE FROM district_search_profiles WHERE district_id = ?", (district_id,))
        cursor = conn.execute(
            """
            INSERT INTO district_search_profiles (
                district_id, website_normalized, profile_status, profile_type,
                provider_guess, search_url_template, search_method, query_param,
                extra_params_json, same_domain_only, requires_javascript,
                uses_external_provider, external_provider_host, confidence,
                test_query, test_result_count, test_success, last_tested_at,
                last_discovered_at, error_message, raw_discovery_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                district_id,
                result.get("website_normalized"),
                result.get("profile_status") or "error",
                result.get("profile_type"),
                result.get("provider_guess"),
                result.get("search_url_template"),
                result.get("search_method"),
                result.get("query_param"),
                json_dumps(result.get("extra_params") or {}),
                1 if result.get("requires_javascript") else 0,
                1 if result.get("uses_external_provider") else 0,
                result.get("external_provider_host"),
                float(result.get("confidence") or 0),
                result.get("test_query"),
                int(result.get("test_result_count") or 0),
                int(result.get("test_success") or 0),
                now,
                now,
                result.get("error_message"),
                json_dumps(result.get("raw") or {}),
                now,
                now,
            ),
        )
        profile_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO district_search_profile_tests (
                profile_id, district_id, test_query, attempted_url,
                status_code, content_type, result_count, success, confidence,
                error_message, raw_test_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                district_id,
                result.get("test_query") or "",
                result.get("attempted_url"),
                result.get("status_code"),
                result.get("content_type"),
                int(result.get("test_result_count") or 0),
                int(result.get("test_success") or 0),
                float(result.get("confidence") or 0),
                result.get("error_message"),
                json_dumps(result.get("raw") or {}),
                now,
            ),
        )
        conn.commit()
        saved = conn.execute("SELECT * FROM district_search_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(saved)


def _apptegy_requires_javascript_result(
    *,
    base_url: str,
    final_home_url: str,
    discovery_base_url: str,
    test_query: str,
    raw: dict[str, Any] | None = None,
    confidence: float = 40.0,
) -> dict[str, Any]:
    return {
        "website_normalized": base_url,
        "profile_status": "requires_javascript",
        "profile_type": "known_platform",
        "provider_guess": "Apptegy",
        "search_url_template": "",
        "search_method": "",
        "query_param": "",
        "requires_javascript": 1,
        "confidence": confidence,
        "test_query": test_query,
        "test_result_count": 0,
        "test_success": 0,
        "error_message": "Apptegy site did not expose a simple search endpoint; use browser-backed search interaction or an Apptegy-specific parser.",
        "raw": {
            **(raw or {}),
            "base_url": base_url,
            "final_home_url": final_home_url,
            "discovery_base_url": discovery_base_url,
            "provider_guess": "Apptegy",
            "diagnosis": "apptegy_requires_browser_or_provider_parser",
        },
    }


def get_best_search_profile(
    district_id: int,
    db_path: Path | str | None = None,
    *,
    statuses: tuple[str, ...] = ("working",),
) -> dict[str, Any] | None:
    init_db(db_path)
    status_placeholders = ",".join("?" for _ in statuses)
    with connect_db(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM district_search_profiles
            WHERE district_id = ?
              AND profile_status IN ({status_placeholders})
              AND search_method = 'GET'
              AND search_url_template IS NOT NULL
              AND search_url_template != ''
            ORDER BY confidence DESC, last_tested_at DESC, id DESC
            LIMIT 1
            """,
            (district_id, *statuses),
        ).fetchone()
    return _row_to_dict(row)


def discover_district_search_profile(
    district: dict[str, Any],
    *,
    test_query: str = "calendar",
    settings: SearchSettings | None = None,
    force: bool = False,
    debug_logger: RunDebugLogger | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    settings = settings or SearchSettings()
    district_id = int(district.get("id") or 0)
    if not district_id:
        raise ValueError("District search profile discovery requires district['id'].")
    if not force:
        existing = get_best_search_profile(district_id, db_path)
        if existing:
            return existing

    base_url = prefer_https_url(district.get("website_normalized") or normalize_website(district.get("website"))[0])
    if not base_url:
        return _save_profile_result(
            district_id,
            {
                "profile_status": "error",
                "error_message": "District has no normalized website.",
                "test_query": test_query,
                "raw": {"district": district.get("agency_name")},
            },
            db_path=db_path,
            replace_existing=True,
        )

    debug_log(debug_logger, "profile_discovery_start", district=district.get("agency_name"), website=base_url)
    session = make_session(settings)
    robots, _sitemaps = load_robots(session, base_url, settings)
    if not can_fetch(robots, settings, base_url):
        return _save_profile_result(
            district_id,
            {
                "website_normalized": base_url,
                "profile_status": "blocked_by_robots",
                "error_message": "Homepage blocked by robots.txt.",
                "test_query": test_query,
                "raw": {"base_url": base_url},
            },
            db_path=db_path,
            replace_existing=True,
        )

    try:
        response, homepage = fetch_limited(session, base_url, settings)
        if _looks_like_challenge_page(response.status_code, homepage, response.url):
            return _save_profile_result(
                district_id,
                {
                    "website_normalized": base_url,
                    "profile_status": "blocked_by_challenge",
                    "error_message": "Homepage returned a challenge or CAPTCHA page.",
                    "test_query": test_query,
                    "raw": {"base_url": base_url, "final_url": response.url, "status_code": response.status_code},
                },
                db_path=db_path,
                replace_existing=True,
            )
        response.raise_for_status()
    except Exception as exc:
        debug_log(
            debug_logger,
            "profile_homepage_failed",
            district=district.get("agency_name"),
            url=base_url,
            error=str(exc),
        )
        return _save_profile_result(
            district_id,
            {
                "website_normalized": base_url,
                "profile_status": "error",
                "error_message": str(exc),
                "test_query": test_query,
                "raw": {"base_url": base_url},
            },
            db_path=db_path,
            replace_existing=True,
        )

    final_home_url = response.url
    discovery_base_url = final_home_url if same_organization_url(final_home_url, base_url) else base_url
    provider_guess = _detect_provider_guess(homepage, final_home_url)
    debug_log(
        debug_logger,
        "profile_homepage_fetched",
        district=district.get("agency_name"),
        url=base_url,
        final_url=final_home_url,
        discovery_base_url=discovery_base_url,
        provider_guess=provider_guess,
    )

    discovered: list[dict[str, Any]] = []
    edlio_candidate = _edlio_candidate_from_html(homepage, final_home_url, discovery_base_url)
    if edlio_candidate:
        discovered.append(edlio_candidate)
        debug_log(
            debug_logger,
            "profile_known_platform_candidate",
            district=district.get("agency_name"),
            provider="Edlio",
            url_template=edlio_candidate.get("search_url_template"),
        )
    discovered.extend(_discover_candidates_from_html(homepage, final_home_url, discovery_base_url))
    search_page_urls = [
        candidate["search_page_url"]
        for candidate in discovered
        if candidate.get("profile_type") == "search_page_link" and candidate.get("search_page_url")
    ][:3]
    for search_page_url in search_page_urls:
        if not can_fetch(robots, settings, search_page_url):
            continue
        try:
            page_response, page_html = fetch_limited(session, search_page_url, settings)
            if page_response.status_code < 400:
                nested_edlio_candidate = _edlio_candidate_from_html(page_html, page_response.url, discovery_base_url)
                if nested_edlio_candidate:
                    discovered.append(nested_edlio_candidate)
                discovered.extend(_discover_candidates_from_html(page_html, page_response.url, discovery_base_url))
                debug_log(debug_logger, "profile_search_link_candidate", district=district.get("agency_name"), url=search_page_url)
        except Exception as exc:
            debug_log(debug_logger, "profile_candidate_failed", district=district.get("agency_name"), url=search_page_url, error=str(exc))
        finally:
            if settings.delay_seconds:
                time.sleep(settings.delay_seconds)
    discovered.extend(_common_template_candidates(discovery_base_url))

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in discovered:
        if candidate.get("profile_type") == "search_page_link":
            continue
        if candidate.get("search_method") != "GET":
            continue
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        debug_log(
            debug_logger,
            "profile_url_template_candidate",
            district=district.get("agency_name"),
            url_template=candidate.get("search_url_template"),
        )
        if len(candidates) >= 10:
            break

    if not candidates:
        if provider_guess == "Apptegy":
            return _save_profile_result(
                district_id,
                _apptegy_requires_javascript_result(
                    base_url=base_url,
                    final_home_url=final_home_url,
                    discovery_base_url=discovery_base_url,
                    test_query=test_query,
                    raw={"candidates": []},
                ),
                db_path=db_path,
                replace_existing=True,
            )
        return _save_profile_result(
            district_id,
            {
                "website_normalized": base_url,
                "profile_status": "no_search_found",
                "provider_guess": provider_guess,
                "test_query": test_query,
                "raw": {"base_url": base_url, "final_home_url": final_home_url, "discovery_base_url": discovery_base_url, "candidates": []},
            },
            db_path=db_path,
            replace_existing=True,
        )

    test_results: list[dict[str, Any]] = []
    for candidate in candidates:
        result = _test_candidate(candidate, district, discovery_base_url, test_query, session, settings, robots, provider_guess, debug_logger)
        test_results.append(result)
        if result["profile_status"] == "working" and result["confidence"] >= 80:
            break
        if settings.delay_seconds:
            time.sleep(settings.delay_seconds)

    best = sorted(test_results, key=lambda item: (item.get("test_success") or 0, item.get("confidence") or 0), reverse=True)[0]
    best["raw"] = {
        **(best.get("raw") or {}),
        "base_url": base_url,
        "final_home_url": final_home_url,
        "discovery_base_url": discovery_base_url,
        "provider_guess": provider_guess,
        "candidate_count": len(candidates),
        "tested_count": len(test_results),
    }
    if provider_guess == "Apptegy" and best.get("profile_status") != "working":
        best = _apptegy_requires_javascript_result(
            base_url=base_url,
            final_home_url=final_home_url,
            discovery_base_url=discovery_base_url,
            test_query=test_query,
            raw=best.get("raw") or {},
            confidence=max(40.0, float(best.get("confidence") or 0)),
        )
    saved = _save_profile_result(district_id, best, db_path=db_path, replace_existing=True)
    debug_log(
        debug_logger,
        "profile_saved",
        district=district.get("agency_name"),
        profile_id=saved.get("id"),
        status=saved.get("profile_status"),
        confidence=saved.get("confidence"),
    )
    return saved


def discover_profiles_for_districts(
    districts: list[dict[str, Any]],
    *,
    test_query: str = "calendar",
    settings: SearchSettings | None = None,
    force: bool = False,
    limit: int | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    settings = settings or SearchSettings()
    selected = districts[:limit] if limit is not None else districts
    summary: dict[str, Any] = {
        "districts_matched": len(districts),
        "districts_tested": 0,
        "working_profiles": 0,
        "no_search_found": 0,
        "manual_review": 0,
        "errors": 0,
        "statuses": {},
        "provider_guesses": {},
    }
    for district in selected:
        try:
            profile = discover_district_search_profile(
                district,
                test_query=test_query,
                settings=settings,
                force=force,
                db_path=db_path,
            )
        except Exception as exc:
            LOGGER.exception("Profile discovery failed for %s: %s", district.get("agency_name"), exc)
            summary["errors"] += 1
            continue
        summary["districts_tested"] += 1
        status = profile.get("profile_status") or "error"
        provider = profile.get("provider_guess") or "Unknown"
        summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
        summary["provider_guesses"][provider] = summary["provider_guesses"].get(provider, 0) + 1
        if status == "working":
            summary["working_profiles"] += 1
        elif status == "no_search_found":
            summary["no_search_found"] += 1
        elif status == "manual_review":
            summary["manual_review"] += 1
        elif status in {"error", "blocked_by_robots", "search_found_but_failed"}:
            summary["errors"] += 1
    return summary


def search_with_district_profile(
    district: dict[str, Any],
    query_text: str,
    settings: SearchSettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
    db_path: Path | str | None = None,
    use_browser_for_javascript: bool = False,
) -> list[dict[str, Any]]:
    district_id = int(district.get("id") or 0)
    base_url = prefer_https_url(district.get("website_normalized") or normalize_website(district.get("website"))[0])
    if not district_id or not base_url:
        debug_log(debug_logger, "district_skipped", district=district.get("agency_name"), reason="missing_profile_context")
        return []
    allowed_statuses = ("working", "requires_javascript") if use_browser_for_javascript else ("working",)
    profile = get_best_search_profile(district_id, db_path, statuses=allowed_statuses)
    if not profile:
        debug_log(debug_logger, "district_search_profile_loaded", district=district.get("agency_name"), status="missing")
        return []

    search_url = build_search_url(profile["search_url_template"], query_text)
    session = make_session(settings)
    robots, _sitemaps = load_robots(session, base_url, settings)
    if not can_fetch(robots, settings, search_url):
        debug_log(debug_logger, "district_search_request", district=district.get("agency_name"), url=search_url, allowed=False)
        return []
    debug_log(
        debug_logger,
        "district_search_profile_loaded",
        district=district.get("agency_name"),
        profile_id=profile.get("id"),
        confidence=profile.get("confidence"),
    )
    try:
        debug_log(debug_logger, "district_search_request", district=district.get("agency_name"), url=search_url, allowed=True)
        if profile.get("profile_status") == "requires_javascript":
            if not use_browser_for_javascript and not settings.browser_for_javascript:
                debug_log(debug_logger, "district_search_profile_failed", district=district.get("agency_name"), error="requires_javascript")
                return []
            final_url, html = browser_render_search_results_page(search_url, settings)
            status_code = 200
            debug_log(debug_logger, "district_search_browser_rendered", district=district.get("agency_name"), url=search_url, final_url=final_url)
        else:
            response, html = fetch_limited(session, search_url, settings)
            final_url = response.url
            status_code = response.status_code
            if _looks_like_challenge_page(response.status_code, html, response.url):
                debug_log(debug_logger, "district_search_profile_failed", district=district.get("agency_name"), error="blocked_by_challenge")
                return []
        if status_code >= 400:
            debug_log(
                debug_logger,
                "district_search_result_page_fetched",
                district=district.get("agency_name"),
                url=search_url,
                status_code=status_code,
                matched=False,
            )
            return []
        max_links = max(settings.max_results_per_district * 3, 10)
        if _is_edlio_profile(profile):
            result_links = parse_edlio_search_results(
                html,
                base_url,
                search_url,
                max_links=max_links,
            )
        else:
            result_links = parse_search_results_page(
                html,
                final_url,
                base_url,
                query_text,
                profile,
                max_links=max_links,
            )
        debug_log(
            debug_logger,
            "district_search_result_page_fetched",
            district=district.get("agency_name"),
            url=final_url,
            status_code=status_code,
            result_links=len(result_links),
        )
    except Exception as exc:
        LOGGER.info("District search profile request failed for %s: %s", district.get("agency_name"), exc)
        debug_log(debug_logger, "district_search_profile_failed", district=district.get("agency_name"), error=str(exc))
        return []

    result_map: dict[str, dict[str, Any]] = {}
    for item in result_links:
        if cancel_requested and cancel_requested():
            break
        url = item["url"]
        debug_log(debug_logger, "district_search_result_link", district=district.get("agency_name"), url=url, rank=item.get("rank"))
        if not can_fetch(robots, settings, url):
            debug_log(debug_logger, "district_search_result_rejected", district=district.get("agency_name"), url=url, reason="robots")
            continue
        try:
            match = _fetch_and_score_result(session, url, query_text, settings)
        except Exception as exc:
            debug_log(debug_logger, "district_search_result_rejected", district=district.get("agency_name"), url=url, reason=str(exc))
            match = None
        if match:
            match["search_source"] = "district_search+fetch"
            match["score"] += max(0, 20 - int(item.get("rank") or 1))
            result_map[match["url"]] = match
            debug_log(
                debug_logger,
                "district_search_result_confirmed",
                district=district.get("agency_name"),
                url=match["url"],
                score=match["score"],
            )
        else:
            debug_log(debug_logger, "district_search_result_rejected", district=district.get("agency_name"), url=url, reason="no_match")
        if settings.delay_seconds:
            time.sleep(settings.delay_seconds)

    return sorted(result_map.values(), key=lambda item: item["score"], reverse=True)[: settings.max_results_per_district]
