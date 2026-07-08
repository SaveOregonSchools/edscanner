from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.exceptions import SSLError

from common import (
    BRAVE_SEARCH_API_KEY_ENV,
    MAX_HTML_SIZE_BYTES,
    MAX_PAGES_PER_DISTRICT,
    MAX_PDF_SIZE_BYTES,
    MAX_RESULTS_PER_DISTRICT,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RESPECT_ROBOTS,
    SEARCH_RUN_LOGS_DIR,
    USER_AGENT,
    VERIFY_SSL,
    connect_db,
    get_local_setting,
    init_db,
    json_dumps,
    normalize_website,
    utc_now_iso,
)


LOGGER = logging.getLogger(__name__)
SEARCH_METHODS = {
    "crawler",
    "brave",
    "hybrid",
    "district_search",
    "district_search_hybrid",
    "district_search_browser",
    "district_search_browser_hybrid",
}
MAX_API_RESULTS_PER_DISTRICT = 20
MAX_FOLLOW_DEPTH = 2


@dataclass(frozen=True)
class SearchSettings:
    max_pages_per_district: int = MAX_PAGES_PER_DISTRICT
    max_results_per_district: int = MAX_RESULTS_PER_DISTRICT
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    delay_seconds: float = REQUEST_DELAY_SECONDS
    max_pdf_size_bytes: int = MAX_PDF_SIZE_BYTES
    max_html_size_bytes: int = MAX_HTML_SIZE_BYTES
    max_total_districts_per_run: int = MAX_TOTAL_DISTRICTS_PER_RUN
    user_agent: str = USER_AGENT
    verify_ssl: bool = VERIFY_SSL
    respect_robots: bool = RESPECT_ROBOTS
    search_method: str = "crawler"
    search_provider: str = "brave"
    api_results_per_district: int = 10
    follow_depth: int = 0
    browser_for_javascript: bool = False
    browser_render_timeout_seconds: float = 20.0
    brave_api_key: str = ""
    brave_endpoint: str = "https://api.search.brave.com/res/v1/web/search"


class RunDebugLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        payload = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=True, default=str)}"
            for key, value in sorted(fields.items())
        )
        line = f"{utc_now_iso()} {event}"
        if payload:
            line = f"{line} {payload}"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def debug_log(debug_logger: RunDebugLogger | None, event: str, **fields: Any) -> None:
    if debug_logger is not None:
        debug_logger.log(event, **fields)


def parse_optional_int(value: Any) -> int | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _clean_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def build_district_filter_sql(
    states: list[str] | tuple[str, ...] | None = None,
    agency_types: list[str] | tuple[str, ...] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    only_searchable: bool = True,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if only_searchable:
        clauses.append("has_searchable_website = 1")
    states = _clean_list(list(states or []))
    agency_types = _clean_list(list(agency_types or []))
    if states:
        clauses.append(f"state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if agency_types:
        clauses.append(f"agency_type IN ({','.join('?' for _ in agency_types)})")
        params.extend(agency_types)
    if min_enrollment is not None:
        clauses.append("total_enrollment_excludes_ae >= ?")
        params.append(min_enrollment)
    if max_enrollment is not None:
        clauses.append("total_enrollment_excludes_ae <= ?")
        params.append(max_enrollment)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def count_matching_districts(
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    where_sql, params = build_district_filter_sql(states, agency_types, min_enrollment, max_enrollment)
    with connect_db(db_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM districts{where_sql}", params).fetchone()
    return int(row["count"] or 0)


def list_matching_districts(
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    limit: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    where_sql, params = build_district_filter_sql(states, agency_types, min_enrollment, max_enrollment)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params = [*params, limit]
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM districts
            {where_sql}
            ORDER BY state, agency_name
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def make_session(settings: SearchSettings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
        }
    )
    return session


def request_get(session: requests.Session, url: str, settings: SearchSettings, **kwargs) -> requests.Response:
    try:
        return session.get(url, verify=settings.verify_ssl, **kwargs)
    except SSLError:
        if not settings.verify_ssl:
            raise
        LOGGER.info("SSL verification failed for %s; retrying without certificate verification", url)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return session.get(url, verify=False, **kwargs)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold()


def _core_host(hostname: str) -> str:
    hostname = hostname.casefold().strip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def same_organization_url(url: str, base_url: str) -> bool:
    host = _host(url)
    base_host = _host(base_url)
    if not host or not base_host:
        return False
    host_core = _core_host(host)
    base_core = _core_host(base_host)
    return host_core == base_core or host_core.endswith(f".{base_core}")


def canonical_url(url: str) -> str:
    clean, _fragment = urldefrag(url)
    parsed = urlparse(clean.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def add_url(queue: OrderedDict[str, None], url: str, base_url: str, max_size: int) -> None:
    url = canonical_url(url)
    if not url or len(queue) >= max_size:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return
    if not same_organization_url(url, base_url):
        return
    queue.setdefault(url, None)


def fetch_limited(session: requests.Session, url: str, settings: SearchSettings) -> tuple[requests.Response, bytes]:
    response = request_get(
        session,
        url,
        settings,
        timeout=settings.request_timeout_seconds,
        allow_redirects=True,
        stream=True,
    )
    content_type = response.headers.get("Content-Type", "").casefold()
    max_bytes = settings.max_pdf_size_bytes if "pdf" in content_type or urlparse(url).path.casefold().endswith(".pdf") else settings.max_html_size_bytes
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise ValueError(f"Response too large for configured limit: {content_length} bytes")
    data = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(f"Response exceeded configured limit of {max_bytes} bytes")
    return response, bytes(data)


def load_robots(session: requests.Session, base_url: str, settings: SearchSettings) -> tuple[robotparser.RobotFileParser | None, list[str]]:
    robots_url = urljoin(base_url, "/robots.txt")
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    sitemaps: list[str] = []
    try:
        response = request_get(session, robots_url, settings, timeout=settings.request_timeout_seconds)
        if response.status_code >= 400:
            return None, sitemaps
        lines = response.text.splitlines()
        parser.parse(lines)
        for line in lines:
            if line.casefold().startswith("sitemap:"):
                sitemap = line.split(":", 1)[1].strip()
                if sitemap:
                    sitemaps.append(sitemap)
        return parser, sitemaps
    except requests.RequestException as exc:
        LOGGER.info("robots.txt fetch failed for %s: %s", base_url, exc)
        return None, sitemaps


def can_fetch(parser: robotparser.RobotFileParser | None, settings: SearchSettings, url: str) -> bool:
    if not settings.respect_robots:
        return True
    if parser is None:
        return True
    try:
        return parser.can_fetch(settings.user_agent, url)
    except Exception:
        return True


def _xml_bytes(content: bytes, url: str) -> bytes:
    if url.casefold().endswith(".gz"):
        return gzip.decompress(content)
    return content


def parse_sitemap(
    session: requests.Session,
    sitemap_url: str,
    base_url: str,
    settings: SearchSettings,
    *,
    depth: int = 0,
    seen: set[str] | None = None,
) -> list[str]:
    if seen is None:
        seen = set()
    sitemap_url = canonical_url(sitemap_url)
    if depth > 2 or sitemap_url in seen:
        return []
    seen.add(sitemap_url)
    try:
        response, content = fetch_limited(session, sitemap_url, settings)
        if response.status_code >= 400:
            return []
        root = ET.fromstring(_xml_bytes(content, sitemap_url))
    except Exception as exc:
        LOGGER.info("Sitemap parse failed for %s: %s", sitemap_url, exc)
        return []

    def tag_name(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].casefold()

    urls: list[str] = []
    if tag_name(root) == "sitemapindex":
        for loc in root.iter():
            if tag_name(loc) != "loc" or not loc.text:
                continue
            child_url = loc.text.strip()
            if same_organization_url(child_url, base_url):
                urls.extend(parse_sitemap(session, child_url, base_url, settings, depth=depth + 1, seen=seen))
            if len(urls) >= settings.max_pages_per_district * 3:
                break
        return urls

    for loc in root.iter():
        if tag_name(loc) != "loc" or not loc.text:
            continue
        url = canonical_url(loc.text.strip())
        if same_organization_url(url, base_url):
            urls.append(url)
        if len(urls) >= settings.max_pages_per_district * 3:
            break
    return urls


def parse_html(content: bytes, final_url: str) -> tuple[str, list[str], str, list[str]]:
    soup = BeautifulSoup(content, "lxml")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = [tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2", "h3"])]
    text = soup.get_text(" ", strip=True)
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        links.append(urljoin(final_url, href))
    return title, headings, text, links


def parse_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for PDF parsing.") from exc

    reader = PdfReader(io.BytesIO(content))
    text_parts: list[str] = []
    for page in reader.pages[:50]:
        try:
            text_parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(text_parts)


def collapse_ws(text: str) -> str:
    return " ".join(str(text or "").split())


def make_snippet(text: str, query_text: str, radius: int = 200) -> str:
    haystack = text or ""
    index = haystack.casefold().find(query_text.casefold())
    if index < 0:
        return collapse_ws(haystack[: radius * 2])
    start = max(index - radius, 0)
    end = min(index + len(query_text) + radius, len(haystack))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(haystack) else ""
    return collapse_ws(f"{prefix}{haystack[start:end]}{suffix}")


def score_match(
    query_text: str,
    title: str,
    headings: list[str],
    body_text: str,
    url: str,
    content_type: str,
) -> dict[str, Any] | None:
    query_fold = query_text.casefold()
    title_fold = title.casefold()
    heading_text = " ".join(headings)
    heading_fold = heading_text.casefold()
    body_fold = body_text.casefold()
    occurrences = body_fold.count(query_fold)
    title_match = query_fold in title_fold
    heading_match = query_fold in heading_fold
    if not occurrences and not title_match and not heading_match:
        return None

    score = 0.0
    if title_match:
        score += 50
    if heading_match:
        score += 25
    score += 10 * min(occurrences, 10)
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) <= 1:
        score += 5
    if parsed.path in {"", "/"}:
        score += 5
    if content_type == "application/pdf":
        score -= 2

    return {
        "url": url,
        "title": collapse_ws(title)[:500],
        "content_type": content_type,
        "score": score,
        "snippet": make_snippet(body_text, query_text),
        "matched_terms": [query_text],
    }


def filename_title(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or urlparse(url).hostname or url
    return name.replace("-", " ").replace("_", " ")


def discover_seed_urls(
    session: requests.Session,
    base_url: str,
    settings: SearchSettings,
    parser: robotparser.RobotFileParser | None,
    robots_sitemaps: list[str],
) -> OrderedDict[str, None]:
    queue: OrderedDict[str, None] = OrderedDict()
    add_url(queue, base_url, base_url, settings.max_pages_per_district * 4)
    sitemap_candidates = [*robots_sitemaps, urljoin(base_url, "/sitemap.xml")]
    seen_sitemaps: set[str] = set()
    sitemap_urls: list[str] = []
    for sitemap in sitemap_candidates:
        sitemap = canonical_url(sitemap)
        if not sitemap or sitemap in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap)
        if not same_organization_url(sitemap, base_url) or not can_fetch(parser, settings, sitemap):
            continue
        sitemap_urls.extend(parse_sitemap(session, sitemap, base_url, settings))
        if len(sitemap_urls) >= settings.max_pages_per_district * 3:
            break
    for url in sitemap_urls:
        add_url(queue, url, base_url, settings.max_pages_per_district * 4)
    LOGGER.info("Discovered %s sitemap URLs for %s", len(sitemap_urls), base_url)
    return queue


def _search_district_crawler(
    district: dict[str, Any],
    query_text: str,
    settings: SearchSettings | None = None,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> list[dict[str, Any]]:
    settings = settings or SearchSettings()
    base_url = district.get("website_normalized") or normalize_website(district.get("website"))[0]
    if not base_url:
        debug_log(debug_logger, "district_skipped", district=district.get("agency_name"), reason="missing_website")
        return []

    session = make_session(settings)
    robots, robots_sitemaps = load_robots(session, base_url, settings)
    queue = discover_seed_urls(session, base_url, settings, robots, robots_sitemaps)
    crawl_base_url = base_url
    visited: set[str] = set()
    fetched_final_urls: set[str] = set()
    result_urls: set[str] = set()
    results: list[dict[str, Any]] = []

    LOGGER.info("Starting district search: %s (%s)", district.get("agency_name"), base_url)
    debug_log(
        debug_logger,
        "district_start",
        district=district.get("agency_name"),
        state=district.get("state"),
        website=base_url,
        max_pages=settings.max_pages_per_district,
    )
    while queue and len(visited) < settings.max_pages_per_district:
        if cancel_requested and cancel_requested():
            debug_log(debug_logger, "district_cancelled", district=district.get("agency_name"), visited=len(visited))
            break
        url, _ = queue.popitem(last=False)
        url = canonical_url(url)
        if url in visited:
            debug_log(debug_logger, "page_skipped", district=district.get("agency_name"), url=url, reason="visited")
            continue
        if not same_organization_url(url, crawl_base_url):
            debug_log(debug_logger, "page_skipped", district=district.get("agency_name"), url=url, reason="outside_scope")
            continue
        if not can_fetch(robots, settings, url):
            debug_log(debug_logger, "page_skipped", district=district.get("agency_name"), url=url, reason="robots")
            continue
        visited.add(url)

        try:
            response, content = fetch_limited(session, url, settings)
            status_code = response.status_code
            if status_code >= 400:
                debug_log(
                    debug_logger,
                    "page_fetched",
                    district=district.get("agency_name"),
                    url=url,
                    final_url=response.url,
                    status_code=status_code,
                    matched=False,
                    reason="http_error",
                )
                continue
            final_url = canonical_url(response.url)
            if final_url in fetched_final_urls:
                debug_log(
                    debug_logger,
                    "page_skipped",
                    district=district.get("agency_name"),
                    url=url,
                    final_url=final_url,
                    reason="duplicate_final_url",
                )
                continue
            fetched_final_urls.add(final_url)
            if len(visited) == 1 and _host(final_url) and _host(final_url) != _host(crawl_base_url):
                LOGGER.info("Using redirected district host for crawl scope: %s -> %s", crawl_base_url, final_url)
                crawl_base_url = final_url
            content_type_header = response.headers.get("Content-Type", "").casefold()
            is_pdf = "application/pdf" in content_type_header or urlparse(final_url).path.casefold().endswith(".pdf")
            if is_pdf:
                content_type = "application/pdf"
                title = filename_title(final_url)
                headings: list[str] = []
                try:
                    text = parse_pdf(content)
                except Exception as exc:
                    LOGGER.info("PDF parse failed for %s: %s", final_url, exc)
                    debug_log(
                        debug_logger,
                        "page_fetched",
                        district=district.get("agency_name"),
                        url=url,
                        final_url=final_url,
                        status_code=status_code,
                        content_type="application/pdf",
                        matched=False,
                        reason="pdf_parse_failed",
                        error=str(exc),
                    )
                    continue
            elif "html" in content_type_header or "text/plain" in content_type_header or not content_type_header:
                content_type = "text/html"
                title, headings, text, links = parse_html(content, final_url)
                for link in links:
                    if len(visited) + len(queue) >= settings.max_pages_per_district * 4:
                        break
                    add_url(queue, link, crawl_base_url, settings.max_pages_per_district * 4)
            else:
                debug_log(
                    debug_logger,
                    "page_fetched",
                    district=district.get("agency_name"),
                    url=url,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type_header,
                    matched=False,
                    reason="unsupported_content_type",
                )
                continue

            match = score_match(query_text, title, headings, text, final_url, content_type)
            if match:
                if match["url"] in result_urls:
                    debug_log(
                        debug_logger,
                        "page_result",
                        district=district.get("agency_name"),
                        url=final_url,
                        status_code=status_code,
                        content_type=content_type,
                        matched=True,
                        duplicate=True,
                        score=match["score"],
                    )
                    continue
                result_urls.add(match["url"])
                match["status_code"] = status_code
                match["search_source"] = "crawler"
                results.append(match)
                debug_log(
                    debug_logger,
                    "page_result",
                    district=district.get("agency_name"),
                    url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    matched=True,
                    score=match["score"],
                    title=match.get("title"),
                    matched_terms=match.get("matched_terms", []),
                )
            else:
                debug_log(
                    debug_logger,
                    "page_result",
                    district=district.get("agency_name"),
                    url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    matched=False,
                    title=title,
                )
        except Exception as exc:
            LOGGER.info("Page fetch/search failed for %s: %s", url, exc)
            debug_log(debug_logger, "page_error", district=district.get("agency_name"), url=url, error=str(exc))
            continue
        finally:
            if settings.delay_seconds:
                time.sleep(settings.delay_seconds)

    top_results = sorted(results, key=lambda item: item["score"], reverse=True)[: settings.max_results_per_district]
    LOGGER.info(
        "Finished district search: %s visited=%s results=%s",
        district.get("agency_name"),
        len(visited),
        len(top_results),
    )
    debug_log(
        debug_logger,
        "district_finish",
        district=district.get("agency_name"),
        visited=len(visited),
        stored_results=len(top_results),
        result_urls=[result["url"] for result in top_results],
    )
    return top_results


def normalize_search_method(value: str | None) -> str:
    method = str(value or "crawler").strip().casefold()
    return method if method in SEARCH_METHODS else "crawler"


def clamp_api_results(value: int | None) -> int:
    if value is None:
        value = 10
    return max(1, min(int(value), MAX_API_RESULTS_PER_DISTRICT))


def clamp_follow_depth(value: int | None) -> int:
    if value is None:
        value = 0
    return max(0, min(int(value), MAX_FOLLOW_DEPTH))


def strip_search_markup(value: str | None) -> str:
    if not value:
        return ""
    return collapse_ws(BeautifulSoup(str(value), "html.parser").get_text(" "))


def site_search_query(query_text: str, base_url: str) -> str:
    host = _core_host(_host(base_url))
    query = str(query_text or "").strip()
    if not query.startswith('"') and not query.endswith('"'):
        query = f'"{query}"'
    return f"{query} site:{host}"


def brave_api_search(
    query_text: str,
    base_url: str,
    settings: SearchSettings,
    debug_logger: RunDebugLogger | None = None,
) -> list[dict[str, Any]]:
    api_key = settings.brave_api_key or get_local_setting(BRAVE_SEARCH_API_KEY_ENV)
    if not api_key:
        raise RuntimeError("Brave Search API key is required for Brave search mode.")

    session = make_session(settings)
    session.headers.update(
        {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        }
    )
    query = site_search_query(query_text, base_url)
    params = {
        "q": query,
        "count": clamp_api_results(settings.api_results_per_district),
        "safesearch": "off",
        "search_lang": "en",
    }
    debug_log(debug_logger, "brave_request", query=query, count=params["count"])
    response = request_get(
        session,
        settings.brave_endpoint,
        settings,
        params=params,
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    raw_results = data.get("web", {}).get("results", []) if isinstance(data, dict) else []
    results: list[dict[str, Any]] = []
    for rank, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        url = canonical_url(str(item.get("url") or ""))
        if not url or not same_organization_url(url, base_url):
            debug_log(debug_logger, "brave_result_skipped", url=url, rank=rank, reason="outside_scope")
            continue
        title = strip_search_markup(item.get("title"))
        snippet = strip_search_markup(item.get("description"))
        result = {
            "url": url,
            "title": title[:500] or filename_title(url),
            "content_type": "search/api",
            "status_code": None,
            "search_source": "brave",
            "score": max(5.0, 55.0 - rank),
            "snippet": snippet[:1000],
            "matched_terms": [query_text],
        }
        results.append(result)
        debug_log(debug_logger, "brave_result", rank=rank, url=url, title=result["title"], snippet=result["snippet"])
    return results


def enqueue_brave_url(queue: OrderedDict[str, int], url: str, base_url: str, depth: int, max_size: int) -> None:
    url = canonical_url(url)
    if not url or len(queue) >= max_size:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return
    if not same_organization_url(url, base_url):
        return
    queue.setdefault(url, depth)


def _search_district_brave(
    district: dict[str, Any],
    query_text: str,
    settings: SearchSettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> list[dict[str, Any]]:
    base_url = district.get("website_normalized") or normalize_website(district.get("website"))[0]
    if not base_url:
        debug_log(debug_logger, "district_skipped", district=district.get("agency_name"), reason="missing_website")
        return []

    api_results = brave_api_search(query_text, base_url, settings, debug_logger)
    result_map: dict[str, dict[str, Any]] = {result["url"]: result for result in api_results}
    session = make_session(settings)
    robots, _robots_sitemaps = load_robots(session, base_url, settings)
    queue: OrderedDict[str, int] = OrderedDict()
    max_pages = max(1, settings.max_pages_per_district)
    follow_depth = clamp_follow_depth(settings.follow_depth)
    for result in api_results:
        enqueue_brave_url(queue, result["url"], base_url, 0, max_pages * 4)

    visited: set[str] = set()
    fetched_final_urls: set[str] = set()
    crawl_base_url = base_url
    debug_log(
        debug_logger,
        "brave_fetch_start",
        district=district.get("agency_name"),
        api_results=len(api_results),
        follow_depth=follow_depth,
        max_pages=max_pages,
    )
    while queue and len(visited) < max_pages:
        if cancel_requested and cancel_requested():
            debug_log(debug_logger, "district_cancelled", district=district.get("agency_name"), visited=len(visited))
            break
        url, depth = queue.popitem(last=False)
        url = canonical_url(url)
        if url in visited:
            continue
        if not same_organization_url(url, crawl_base_url):
            debug_log(debug_logger, "page_skipped", district=district.get("agency_name"), url=url, reason="outside_scope")
            continue
        if not can_fetch(robots, settings, url):
            debug_log(debug_logger, "page_skipped", district=district.get("agency_name"), url=url, reason="robots")
            continue
        visited.add(url)

        try:
            response, content = fetch_limited(session, url, settings)
            status_code = response.status_code
            final_url = canonical_url(response.url)
            if status_code >= 400:
                debug_log(debug_logger, "page_fetched", url=url, final_url=final_url, status_code=status_code, matched=False)
                continue
            if final_url in fetched_final_urls:
                continue
            fetched_final_urls.add(final_url)
            content_type_header = response.headers.get("Content-Type", "").casefold()
            is_pdf = "application/pdf" in content_type_header or urlparse(final_url).path.casefold().endswith(".pdf")
            if is_pdf:
                content_type = "application/pdf"
                title = filename_title(final_url)
                headings: list[str] = []
                try:
                    text = parse_pdf(content)
                except Exception as exc:
                    debug_log(debug_logger, "page_fetched", url=url, final_url=final_url, status_code=status_code, matched=False, error=str(exc))
                    continue
            elif "html" in content_type_header or "text/plain" in content_type_header or not content_type_header:
                content_type = "text/html"
                title, headings, text, links = parse_html(content, final_url)
                if depth < follow_depth:
                    for link in links:
                        enqueue_brave_url(queue, link, crawl_base_url, depth + 1, max_pages * 4)
            else:
                debug_log(
                    debug_logger,
                    "page_fetched",
                    url=url,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type_header,
                    matched=False,
                    reason="unsupported_content_type",
                )
                continue

            match = score_match(query_text, title, headings, text, final_url, content_type)
            if match:
                match["status_code"] = status_code
                match["search_source"] = "brave+fetch" if depth == 0 else "brave-follow"
                match["score"] += 20 if depth == 0 else 8
                result_map[match["url"]] = match
                debug_log(
                    debug_logger,
                    "page_result",
                    district=district.get("agency_name"),
                    url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    matched=True,
                    depth=depth,
                    score=match["score"],
                )
            else:
                debug_log(
                    debug_logger,
                    "page_result",
                    district=district.get("agency_name"),
                    url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    matched=False,
                    depth=depth,
                    title=title,
                )
        except Exception as exc:
            LOGGER.info("Brave result fetch/search failed for %s: %s", url, exc)
            debug_log(debug_logger, "page_error", district=district.get("agency_name"), url=url, error=str(exc))
        finally:
            if settings.delay_seconds:
                time.sleep(settings.delay_seconds)

    top_results = sorted(result_map.values(), key=lambda item: item["score"], reverse=True)[: settings.max_results_per_district]
    debug_log(
        debug_logger,
        "district_finish",
        district=district.get("agency_name"),
        visited=len(visited),
        stored_results=len(top_results),
        result_urls=[result["url"] for result in top_results],
    )
    return top_results


def search_district(
    district: dict[str, Any],
    query_text: str,
    settings: SearchSettings | None = None,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    settings = settings or SearchSettings()
    method = normalize_search_method(settings.search_method)
    if method == "crawler":
        return _search_district_crawler(
            district,
            query_text,
            settings,
            cancel_requested=cancel_requested,
            debug_logger=debug_logger,
        )
    if method == "brave":
        return _search_district_brave(
            district,
            query_text,
            settings,
            cancel_requested=cancel_requested,
            debug_logger=debug_logger,
        )
    if method in {"district_search", "district_search_hybrid", "district_search_browser", "district_search_browser_hybrid"}:
        from site_search_discovery import search_with_district_profile

        use_browser = method in {"district_search_browser", "district_search_browser_hybrid"}
        district_results = search_with_district_profile(
            district,
            query_text,
            settings,
            cancel_requested=cancel_requested,
            debug_logger=debug_logger,
            db_path=db_path,
            use_browser_for_javascript=use_browser,
        )
        if district_results or method in {"district_search", "district_search_browser"}:
            return district_results
        debug_log(debug_logger, "district_search_fallback_crawler", district=district.get("agency_name"))
        fallback_results = _search_district_crawler(
            district,
            query_text,
            settings,
            cancel_requested=cancel_requested,
            debug_logger=debug_logger,
        )
        for result in fallback_results:
            result["search_source"] = "district_search+fallback_crawler"
        return fallback_results

    try:
        brave_results = _search_district_brave(
            district,
            query_text,
            settings,
            cancel_requested=cancel_requested,
            debug_logger=debug_logger,
        )
        if brave_results:
            return brave_results
    except Exception as exc:
        LOGGER.info("Brave search failed for %s; falling back to crawler: %s", district.get("agency_name"), exc)
        debug_log(debug_logger, "brave_fallback", district=district.get("agency_name"), error=str(exc))
    return _search_district_crawler(
        district,
        query_text,
        settings,
        cancel_requested=cancel_requested,
        debug_logger=debug_logger,
    )


def create_search_run(
    query_text: str,
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    max_districts: int | None = None,
    debug_logging: bool = False,
    db_path: Path | str | None = None,
    settings: SearchSettings | None = None,
    status: str = "queued",
) -> int:
    query_text = str(query_text or "").strip()
    if not query_text:
        raise ValueError("Search text is required.")
    init_db(db_path)
    settings = settings or SearchSettings()
    cap = max_districts or settings.max_total_districts_per_run
    cap = max(1, min(cap, settings.max_total_districts_per_run))
    search_method = normalize_search_method(settings.search_method)
    if search_method in {"brave", "hybrid"}:
        search_provider = "brave"
    elif search_method in {"district_search", "district_search_hybrid", "district_search_browser", "district_search_browser_hybrid"}:
        search_provider = "district_search"
    else:
        search_provider = "crawler"
    api_results_per_district = clamp_api_results(settings.api_results_per_district)
    follow_depth = clamp_follow_depth(settings.follow_depth)
    states = _clean_list(states)
    agency_types = _clean_list(agency_types)
    matched_count = count_matching_districts(states, agency_types, min_enrollment, max_enrollment, db_path)

    submitted_at = utc_now_iso()
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_runs (
                query_text, states_json, agency_types_json, min_enrollment,
                max_enrollment, max_districts, max_pages_per_district,
                search_method, search_provider, api_results_per_district,
                follow_depth,
                cancel_requested, debug_logging, debug_log_path, status,
                districts_matched, districts_searched, districts_failed, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, 0, 0, ?)
            """,
            (
                query_text,
                json_dumps(states),
                json_dumps(agency_types),
                min_enrollment,
                max_enrollment,
                cap,
                settings.max_pages_per_district,
                search_method,
                search_provider,
                api_results_per_district,
                follow_depth,
                1 if debug_logging else 0,
                status,
                matched_count,
                submitted_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
    return run_id


def is_cancel_requested(run_id: int, db_path: Path | str | None = None) -> bool:
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM search_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return bool(row and row["cancel_requested"])


def execute_search_run(
    run_id: int,
    *,
    db_path: Path | str | None = None,
    settings: SearchSettings | None = None,
) -> None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        run = conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"Search run not found: {run_id}")
        query_text = run["query_text"]
        states = _clean_list(json_loads_list(run["states_json"]))
        agency_types = _clean_list(json_loads_list(run["agency_types_json"]))
        min_enrollment = run["min_enrollment"]
        max_enrollment = run["max_enrollment"]
        cap = run["max_districts"] or MAX_TOTAL_DISTRICTS_PER_RUN
        max_pages = run["max_pages_per_district"] or MAX_PAGES_PER_DISTRICT
        search_method = normalize_search_method(run["search_method"])
        search_provider = run["search_provider"] or (
            "brave"
            if search_method in {"brave", "hybrid"}
            else "district_search"
            if search_method in {"district_search", "district_search_hybrid", "district_search_browser", "district_search_browser_hybrid"}
            else "crawler"
        )
        api_results_per_district = clamp_api_results(run["api_results_per_district"])
        follow_depth = clamp_follow_depth(run["follow_depth"])
        matched_count = run["districts_matched"] or 0
        debug_enabled = bool(run["debug_logging"])
        already_cancelled = bool(run["cancel_requested"])

    debug_logger: RunDebugLogger | None = None
    if debug_enabled:
        debug_path = SEARCH_RUN_LOGS_DIR / f"search-run-{run_id}.log"
        debug_logger = RunDebugLogger(debug_path)
        with connect_db(db_path) as conn:
            conn.execute(
                "UPDATE search_runs SET debug_log_path = ? WHERE id = ?",
                (str(debug_path), run_id),
            )
            conn.commit()
        debug_log(
            debug_logger,
            "run_loaded",
            run_id=run_id,
            query=query_text,
            states=states,
            agency_types=agency_types,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            max_districts=cap,
            max_pages_per_district=max_pages,
            search_method=search_method,
            search_provider=search_provider,
            api_results_per_district=api_results_per_district,
            follow_depth=follow_depth,
            matched_count=matched_count,
        )

    if already_cancelled:
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'cancelled',
                    finished_at = ?,
                    error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (utc_now_iso(), run_id),
            )
            conn.commit()
        debug_log(debug_logger, "run_cancelled", run_id=run_id, searched=0, failed=0)
        LOGGER.info("Search run %s cancelled before start", run_id)
        return

    base_settings = settings or SearchSettings()
    run_settings = SearchSettings(
        max_pages_per_district=max_pages,
        max_results_per_district=base_settings.max_results_per_district,
        request_timeout_seconds=base_settings.request_timeout_seconds,
        delay_seconds=base_settings.delay_seconds,
        max_pdf_size_bytes=base_settings.max_pdf_size_bytes,
        max_html_size_bytes=base_settings.max_html_size_bytes,
        max_total_districts_per_run=max(cap, 1),
        user_agent=base_settings.user_agent,
        verify_ssl=base_settings.verify_ssl,
        respect_robots=base_settings.respect_robots,
        search_method=search_method,
        search_provider=search_provider,
        api_results_per_district=api_results_per_district,
        follow_depth=follow_depth,
        browser_for_javascript=base_settings.browser_for_javascript
        or search_method in {"district_search_browser", "district_search_browser_hybrid"},
        browser_render_timeout_seconds=base_settings.browser_render_timeout_seconds,
        brave_api_key=base_settings.brave_api_key or get_local_setting(BRAVE_SEARCH_API_KEY_ENV),
        brave_endpoint=base_settings.brave_endpoint,
    )
    districts = list_matching_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        limit=cap,
        db_path=db_path,
    )

    searched = 0
    failed = 0
    cancelled = False
    try:
        LOGGER.info("Search run %s started: query=%r matched=%s cap=%s", run_id, query_text, matched_count, cap)
        debug_log(debug_logger, "run_start", run_id=run_id, district_count=len(districts))
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'running',
                    districts_searched = 0,
                    districts_failed = 0,
                    finished_at = NULL,
                    error_message = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
            conn.execute("DELETE FROM search_results WHERE search_run_id = ?", (run_id,))
            conn.commit()
            for district in districts:
                if is_cancel_requested(run_id, db_path):
                    cancelled = True
                    debug_log(debug_logger, "run_cancel_requested", run_id=run_id, before_district=district.get("agency_name"))
                    break
                try:
                    district_results = search_district(
                        district,
                        query_text,
                        run_settings,
                        cancel_requested=lambda: is_cancel_requested(run_id, db_path),
                        debug_logger=debug_logger,
                        db_path=db_path,
                    )
                    searched += 1
                    now = utc_now_iso()
                    for rank, result in enumerate(district_results, start=1):
                        conn.execute(
                            """
                            INSERT INTO search_results (
                                search_run_id, district_id, district_name, state,
                                agency_type, total_enrollment_excludes_ae, website,
                                result_rank, url, title, content_type, status_code,
                                search_source, score, snippet, matched_terms_json, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                district["id"],
                                district["agency_name"],
                                district["state"],
                                district["agency_type"],
                                district["total_enrollment_excludes_ae"],
                                district["website_normalized"] or district["website"],
                                rank,
                                result["url"],
                                result.get("title"),
                                result.get("content_type"),
                                result.get("status_code"),
                                result.get("search_source"),
                                result.get("score", 0),
                                result.get("snippet"),
                                json_dumps(result.get("matched_terms", [])),
                                now,
                            ),
                        )
                    debug_log(
                        debug_logger,
                        "district_results_stored",
                        run_id=run_id,
                        district=district.get("agency_name"),
                        stored_results=len(district_results),
                    )
                    conn.execute(
                        """
                        UPDATE search_runs
                        SET districts_searched = ?, districts_failed = ?
                        WHERE id = ?
                        """,
                        (searched, failed, run_id),
                    )
                    conn.commit()
                    if is_cancel_requested(run_id, db_path):
                        cancelled = True
                        debug_log(
                            debug_logger,
                            "run_cancel_requested",
                            run_id=run_id,
                            after_district=district.get("agency_name"),
                        )
                        break
                except Exception as exc:
                    searched += 1
                    failed += 1
                    LOGGER.exception("District search failed for %s: %s", district.get("agency_name"), exc)
                    debug_log(
                        debug_logger,
                        "district_error",
                        run_id=run_id,
                        district=district.get("agency_name"),
                        error=str(exc),
                    )
                    conn.execute(
                        """
                        UPDATE search_runs
                        SET districts_searched = ?, districts_failed = ?
                        WHERE id = ?
                        """,
                        (searched, failed, run_id),
                    )
                    conn.commit()
                    if is_cancel_requested(run_id, db_path):
                        cancelled = True
                        debug_log(
                            debug_logger,
                            "run_cancel_requested",
                            run_id=run_id,
                            after_district=district.get("agency_name"),
                        )
                        break

            final_status = "cancelled" if cancelled else "completed"
            error_message = "Cancelled by user." if cancelled else None
            conn.execute(
                """
                UPDATE search_runs
                SET status = ?,
                    districts_searched = ?,
                    districts_failed = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (final_status, searched, failed, utc_now_iso(), error_message, run_id),
            )
            conn.commit()
        debug_log(debug_logger, "run_finish", run_id=run_id, status=final_status, searched=searched, failed=failed)
        LOGGER.info("Search run %s %s: searched=%s failed=%s", run_id, final_status, searched, failed)
    except Exception as exc:
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'failed',
                    districts_searched = ?,
                    districts_failed = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (searched, failed, utc_now_iso(), str(exc), run_id),
            )
            conn.commit()
        LOGGER.exception("Search run %s failed: %s", run_id, exc)
        debug_log(debug_logger, "run_failed", run_id=run_id, searched=searched, failed=failed, error=str(exc))
        raise


def json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def run_search(
    query_text: str,
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    max_districts: int | None = None,
    debug_logging: bool = False,
    db_path: Path | str | None = None,
    settings: SearchSettings | None = None,
) -> int:
    run_id = create_search_run(
        query_text,
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        max_districts=max_districts,
        debug_logging=debug_logging,
        db_path=db_path,
        settings=settings,
        status="running",
    )
    execute_search_run(run_id, db_path=db_path, settings=settings)
    return run_id


def export_search_run_csv(run_id: int, db_path: Path | str | None = None) -> str:
    init_db(db_path)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "search_run_id",
            "query_text",
            "district_name",
            "state",
            "agency_type",
            "total_enrollment_excludes_ae",
            "website",
            "result_rank",
            "title",
            "url",
            "content_type",
            "status_code",
            "search_source",
            "score",
            "snippet",
        ]
    )
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.query_text, sr.*
            FROM search_results sr
            JOIN search_runs r ON r.id = sr.search_run_id
            WHERE sr.search_run_id = ?
            ORDER BY sr.district_name, sr.result_rank
            """,
            (run_id,),
        )
        for row in rows:
            writer.writerow(
                [
                    run_id,
                    row["query_text"],
                    row["district_name"],
                    row["state"],
                    row["agency_type"],
                    row["total_enrollment_excludes_ae"],
                    row["website"],
                    row["result_rank"],
                    row["title"],
                    row["url"],
                    row["content_type"],
                    row["status_code"],
                    row["search_source"],
                    row["score"],
                    row["snippet"],
                ]
            )
    return output.getvalue()
