from __future__ import annotations

import csv
import email.utils
import gzip
import io
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
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
    SEARCH_RUN_WORKERS,
    USER_AGENT,
    VERIFY_SSL,
    connect_db,
    get_local_setting,
    init_db,
    json_dumps,
    normalize_website,
    prefer_https_url,
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
MAX_SEARCH_WORKERS = 8
MAX_QUERY_LENGTH = 500
MAX_QUERY_TOKENS = 100
MAX_WILDCARDS_PER_TERM = 8
MAX_OUTBOUND_QUERY_VARIANTS = 8
SEARCH_FIELD_SEPARATOR = "\n\0\n"


class SearchQuerySyntaxError(ValueError):
    """Raised when a district-search expression is not valid."""


@dataclass(frozen=True)
class QueryTerm:
    text: str
    phrase: bool = False
    wildcard: bool = False


@dataclass(frozen=True)
class QueryNot:
    operand: "QueryNode"


@dataclass(frozen=True)
class QueryAnd:
    operands: tuple["QueryNode", ...]


@dataclass(frozen=True)
class QueryOr:
    operands: tuple["QueryNode", ...]


QueryNode = QueryTerm | QueryNot | QueryAnd | QueryOr


@dataclass(frozen=True)
class ParsedSearchQuery:
    raw: str
    root: QueryNode
    positive_terms: tuple[QueryTerm, ...]
    legacy_simple: bool = False


@dataclass(frozen=True)
class SearchQueryCapabilities:
    boolean_operators: bool = False
    parentheses: bool = False
    quoted_phrases: bool = False
    wildcards: bool = False
    negation: bool = False
    wildcards_in_phrases: bool = False


FULL_QUERY_CAPABILITIES = SearchQueryCapabilities(True, True, True, True, True)
PLAIN_QUERY_CAPABILITIES = SearchQueryCapabilities()
PROVIDER_QUERY_CAPABILITIES: dict[str, SearchQueryCapabilities] = {
    "brave": SearchQueryCapabilities(True, True, True, False, True),
    "google programmable search": SearchQueryCapabilities(True, True, True, False, True),
    "bing": SearchQueryCapabilities(True, True, True, False, True),
    "searchstax / solr": FULL_QUERY_CAPABILITIES,
    "sharepoint": SearchQueryCapabilities(True, True, True, False, True),
    "algolia": PLAIN_QUERY_CAPABILITIES,
    "finalsite": PLAIN_QUERY_CAPABILITIES,
    "edlio": PLAIN_QUERY_CAPABILITIES,
    "blackboard / schoolwires": PLAIN_QUERY_CAPABILITIES,
    "apptegy": PLAIN_QUERY_CAPABILITIES,
    "parentsquare": PLAIN_QUERY_CAPABILITIES,
    "campus suite": PLAIN_QUERY_CAPABILITIES,
    "schoolmessenger": PLAIN_QUERY_CAPABILITIES,
    "wordpress": PLAIN_QUERY_CAPABILITIES,
    "drupal": PLAIN_QUERY_CAPABILITIES,
    "custom": PLAIN_QUERY_CAPABILITIES,
}


@dataclass(frozen=True)
class _QueryToken:
    kind: str
    value: str
    position: int


def _query_error(message: str, position: int | None = None) -> SearchQuerySyntaxError:
    suffix = f" at character {position + 1}" if position is not None else ""
    return SearchQuerySyntaxError(f"Invalid search query: {message}{suffix}.")


def _legacy_simple_query(text: str) -> bool:
    if any(character in text for character in '"()*?'):
        return False
    return not any(part in {"AND", "OR", "NOT"} for part in text.split())


def _tokenize_search_query(text: str) -> list[_QueryToken]:
    tokens: list[_QueryToken] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == "(":
            tokens.append(_QueryToken("LPAREN", character, index))
            index += 1
            continue
        if character == ")":
            tokens.append(_QueryToken("RPAREN", character, index))
            index += 1
            continue
        if character == '"':
            start = index
            index += 1
            value: list[str] = []
            while index < len(text) and text[index] != '"':
                if text[index] == "\\":
                    index += 1
                    if index >= len(text):
                        raise _query_error("unfinished escape sequence", index - 1)
                value.append(text[index])
                index += 1
            if index >= len(text):
                raise _query_error("unterminated quoted phrase", start)
            index += 1
            phrase = " ".join("".join(value).split())
            if not phrase:
                raise _query_error("quoted phrases cannot be empty", start)
            tokens.append(_QueryToken("PHRASE", phrase, start))
            continue

        start = index
        value = []
        while index < len(text) and not text[index].isspace() and text[index] not in "()\"":
            if text[index] == "\\":
                index += 1
                if index >= len(text):
                    raise _query_error("unfinished escape sequence", index - 1)
            value.append(text[index])
            index += 1
        if index < len(text) and text[index] == '"':
            raise _query_error("a quote must begin a term", index)
        word = "".join(value)
        if not word:
            raise _query_error("expected a search term", start)
        kind = word if word in {"AND", "OR", "NOT"} else "TERM"
        tokens.append(_QueryToken(kind, word, start))
        if len(tokens) > MAX_QUERY_TOKENS:
            raise _query_error(f"queries may contain at most {MAX_QUERY_TOKENS} terms and operators")
    if len(tokens) > MAX_QUERY_TOKENS:
        raise _query_error(f"queries may contain at most {MAX_QUERY_TOKENS} terms and operators")
    return tokens


def _make_query_term(token: _QueryToken) -> QueryTerm:
    wildcard_count = token.value.count("*") + token.value.count("?")
    if wildcard_count > MAX_WILDCARDS_PER_TERM:
        raise _query_error(
            f"a term may contain at most {MAX_WILDCARDS_PER_TERM} wildcard characters",
            token.position,
        )
    if wildcard_count and not any(character not in "*?" for character in token.value):
        raise _query_error("a wildcard term must contain at least one letter or number", token.position)
    return QueryTerm(
        token.value,
        phrase=token.kind == "PHRASE",
        wildcard=bool(wildcard_count),
    )


class _SearchQueryParser:
    def __init__(self, tokens: list[_QueryToken]):
        self.tokens = tokens
        self.index = 0

    def current(self) -> _QueryToken | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, kind: str) -> _QueryToken | None:
        token = self.current()
        if token is None or token.kind != kind:
            return None
        self.index += 1
        return token

    def parse(self) -> QueryNode:
        root = self.parse_or()
        token = self.current()
        if token is not None:
            if token.kind == "RPAREN":
                raise _query_error("unexpected closing parenthesis", token.position)
            raise _query_error(f"unexpected {token.value!r}", token.position)
        return root

    def parse_or(self) -> QueryNode:
        operands = [self.parse_and()]
        while self.take("OR") is not None:
            if self.current() is None:
                raise _query_error("OR must be followed by a term")
            operands.append(self.parse_and())
        return operands[0] if len(operands) == 1 else QueryOr(tuple(operands))

    def parse_and(self) -> QueryNode:
        operands = [self.parse_not()]
        while True:
            if self.take("AND") is not None:
                if self.current() is None:
                    raise _query_error("AND must be followed by a term")
                operands.append(self.parse_not())
                continue
            token = self.current()
            if token is not None and token.kind in {"TERM", "PHRASE", "LPAREN", "NOT"}:
                operands.append(self.parse_not())
                continue
            break
        return operands[0] if len(operands) == 1 else QueryAnd(tuple(operands))

    def parse_not(self) -> QueryNode:
        token = self.take("NOT")
        if token is not None:
            if self.current() is None:
                raise _query_error("NOT must be followed by a term", token.position)
            return QueryNot(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> QueryNode:
        token = self.current()
        if token is None:
            raise _query_error("expected a search term")
        if token.kind in {"TERM", "PHRASE"}:
            self.index += 1
            return _make_query_term(token)
        if token.kind == "LPAREN":
            self.index += 1
            if self.take("RPAREN") is not None:
                raise _query_error("parentheses cannot be empty", token.position)
            expression = self.parse_or()
            if self.take("RPAREN") is None:
                raise _query_error("missing closing parenthesis", token.position)
            return expression
        if token.kind in {"AND", "OR"}:
            raise _query_error(f"{token.value} must follow a term", token.position)
        if token.kind == "RPAREN":
            raise _query_error("expected a search term before the closing parenthesis", token.position)
        raise _query_error("expected a search term", token.position)


def _positive_query_terms(node: QueryNode, *, negated: bool = False) -> list[QueryTerm]:
    if isinstance(node, QueryTerm):
        return [] if negated else [node]
    if isinstance(node, QueryNot):
        return _positive_query_terms(node.operand, negated=not negated)
    terms: list[QueryTerm] = []
    for operand in node.operands:
        terms.extend(_positive_query_terms(operand, negated=negated))
    return terms


@lru_cache(maxsize=256)
def parse_search_query(query_text: str) -> ParsedSearchQuery:
    raw = str(query_text or "").strip()
    if not raw:
        raise SearchQuerySyntaxError("Search text is required.")
    if len(raw) > MAX_QUERY_LENGTH:
        raise _query_error(f"queries may contain at most {MAX_QUERY_LENGTH} characters")
    if _legacy_simple_query(raw):
        normalized = " ".join(raw.split())
        root: QueryNode = QueryTerm(normalized, phrase=True)
        return ParsedSearchQuery(raw, root, (root,), legacy_simple=True)

    tokens = _tokenize_search_query(raw)
    if not tokens:
        raise SearchQuerySyntaxError("Search text is required.")
    root = _SearchQueryParser(tokens).parse()
    positive_terms = tuple(_positive_query_terms(root))
    if not positive_terms:
        raise _query_error("include at least one term that is not excluded by NOT")
    return ParsedSearchQuery(raw, root, positive_terms)


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
    # Optional, thread-safe execution hook used by Guided Search. Manual searches
    # leave this unset and retain the existing fixed delay/worker behavior.
    resource_controller: Any | None = None


class RunDebugLogger:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        payload = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=True, default=str)}"
            for key, value in sorted(fields.items())
        )
        line = f"{utc_now_iso()} {event}"
        if payload:
            line = f"{line} {payload}"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def debug_log(debug_logger: RunDebugLogger | None, event: str, **fields: Any) -> None:
    if debug_logger is not None:
        debug_logger.log(event, **fields)


def _controller_callable(controller: Any | None, *names: str) -> Callable[..., Any] | None:
    if controller is None:
        return None
    for name in names:
        candidate = getattr(controller, name, None)
        if callable(candidate):
            return candidate
    return None


def _controller_delay_seconds(controller: Any | None, default: float) -> float:
    if controller is None:
        return max(0.0, float(default))
    for name in ("current_delay_seconds", "delay_seconds", "get_delay_seconds"):
        value = getattr(controller, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                LOGGER.exception("Search resource controller could not report its request delay")
                continue
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    nested = getattr(controller, "delay_controller", None)
    if nested is not None:
        getter = getattr(nested, "get_delay", None)
        try:
            value = getter() if callable(getter) else getattr(nested, "delay_seconds", None)
        except Exception:
            LOGGER.exception("Search delay controller could not report its request delay")
            value = None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(default))


def _parse_retry_after(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _before_controlled_request(settings: SearchSettings, url: str) -> None:
    controller = settings.resource_controller
    if controller is None:
        return
    before_request = _controller_callable(controller, "before_request", "wait_before_request")
    if before_request is not None:
        try:
            before_request(url=url)
            return
        except TypeError:
            try:
                before_request()
                return
            except Exception:
                LOGGER.exception("Search resource controller before-request hook failed")
        except Exception:
            LOGGER.exception("Search resource controller before-request hook failed")
    nested_delay = getattr(controller, "delay_controller", None)
    nested_wait = getattr(nested_delay, "wait", None)
    if callable(nested_wait):
        try:
            nested_wait()
            return
        except Exception:
            LOGGER.exception("Search delay controller wait failed")
    delay = _controller_delay_seconds(controller, settings.delay_seconds)
    if delay:
        time.sleep(delay)


def _observe_controlled_response(settings: SearchSettings, response: requests.Response) -> None:
    controller = settings.resource_controller
    if controller is None:
        return
    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    observer = _controller_callable(controller, "observe_response", "on_response")
    if observer is not None:
        try:
            observer(
                status_code=response.status_code,
                headers=dict(response.headers),
                url=response.url,
                retry_after_seconds=retry_after,
            )
        except TypeError:
            try:
                observer(response.status_code, dict(response.headers))
            except Exception:
                LOGGER.exception("Search resource controller response hook failed")
            else:
                return
        except Exception:
            LOGGER.exception("Search resource controller response hook failed")
        else:
            return
    if response.status_code == 429:
        rate_limit = _controller_callable(controller, "record_rate_limit")
        if rate_limit is not None:
            try:
                rate_limit(response.headers.get("Retry-After"))
                return
            except Exception:
                LOGGER.exception("Search resource controller rate-limit hook failed")
    if retry_after and response.status_code in {429, 503}:
        defer = _controller_callable(controller, "defer_requests", "back_off")
        if defer is not None:
            try:
                defer(retry_after, reason=f"HTTP {response.status_code}")
            except TypeError:
                defer(retry_after)
        else:
            # Only adaptive/controller-backed requests take this path. Preserve
            # the historical manual behavior when no controller is installed.
            time.sleep(min(retry_after, 60.0))


def _observe_controlled_error(settings: SearchSettings, error: BaseException, *, url: str = "") -> None:
    observer = _controller_callable(settings.resource_controller, "observe_error", "on_error")
    if observer is None:
        return
    try:
        observer(error=error, url=url)
    except TypeError:
        observer(error)


def wait_for_request_delay(
    settings: SearchSettings,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    """Apply the legacy post-request delay or notify an adaptive controller.

    Controller-backed execution throttles centrally in ``request_get`` so all
    HTTP requests participate. Manual execution retains its historical fixed,
    per-worker post-request sleep at the existing call sites.
    """

    controller = settings.resource_controller
    if controller is not None:
        after_request = _controller_callable(controller, "after_request")
        if after_request is not None:
            try:
                after_request()
            except Exception:
                LOGGER.exception("Search resource controller after-request hook failed")
        return
    delay = max(0.0, float(settings.delay_seconds))
    if delay and not (cancel_requested and cancel_requested()):
        # Keep the historical one-sleep-per-call behavior for manual searches.
        time.sleep(delay)


def parse_optional_int(value: Any) -> int | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def clamp_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    return max(minimum, min(maximum, value))


def _clean_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def clean_district_ids(values: list[int] | tuple[int, ...] | None) -> list[int] | None:
    if values is None:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            district_id = int(value)
        except (TypeError, ValueError):
            continue
        if district_id <= 0 or district_id in seen:
            continue
        out.append(district_id)
        seen.add(district_id)
    return out


def build_district_filter_sql(
    states: list[str] | tuple[str, ...] | None = None,
    agency_types: list[str] | tuple[str, ...] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    only_searchable: bool = True,
    district_ids: list[int] | tuple[int, ...] | None = None,
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
    cleaned_district_ids = clean_district_ids(district_ids)
    if cleaned_district_ids is not None:
        if cleaned_district_ids:
            clauses.append(f"id IN ({','.join('?' for _ in cleaned_district_ids)})")
            params.extend(cleaned_district_ids)
        else:
            clauses.append("1 = 0")
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def count_matching_districts(
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    db_path: Path | str | None = None,
    *,
    district_ids: list[int] | tuple[int, ...] | None = None,
) -> int:
    init_db(db_path)
    where_sql, params = build_district_filter_sql(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        district_ids=district_ids,
    )
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
    district_ids: list[int] | tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    where_sql, params = build_district_filter_sql(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        district_ids=district_ids,
    )
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
    _before_controlled_request(settings, url)
    try:
        response = session.get(url, verify=settings.verify_ssl, **kwargs)
    except SSLError as exc:
        if not settings.verify_ssl:
            _observe_controlled_error(settings, exc, url=url)
            raise
        LOGGER.info("SSL verification failed for %s; retrying without certificate verification", url)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            response = session.get(url, verify=False, **kwargs)
        except Exception as retry_exc:
            _observe_controlled_error(settings, retry_exc, url=url)
            raise
    except Exception as exc:
        _observe_controlled_error(settings, exc, url=url)
        raise
    _observe_controlled_response(settings, response)
    return response


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


@lru_cache(maxsize=512)
def _term_regex(text: str, wildcard: bool) -> re.Pattern[str]:
    pattern: list[str] = [r"(?<![\w'\N{RIGHT SINGLE QUOTATION MARK}])"] if wildcard else []
    whitespace = False
    for character in text:
        if character.isspace():
            if not whitespace:
                pattern.append(r"\s+")
            whitespace = True
            continue
        whitespace = False
        if wildcard and character == "*":
            pattern.append(r"[\w'\N{RIGHT SINGLE QUOTATION MARK}.\-]*")
        elif wildcard and character == "?":
            pattern.append(r"[\w'\N{RIGHT SINGLE QUOTATION MARK}.\-]")
        else:
            pattern.append(re.escape(character))
    if wildcard:
        pattern.append(r"(?![\w'\N{RIGHT SINGLE QUOTATION MARK}])")
    return re.compile("".join(pattern), re.IGNORECASE)


def _term_match(term: QueryTerm, text: str) -> re.Match[str] | None:
    return _term_regex(term.text, term.wildcard).search(text or "")


def _term_occurrences(term: QueryTerm, text: str, *, maximum: int = 10) -> int:
    count = 0
    for match in _term_regex(term.text, term.wildcard).finditer(text or ""):
        if match.end() == match.start():
            continue
        count += 1
        if count >= maximum:
            break
    return count


def _evaluate_query(node: QueryNode, text: str) -> bool:
    if isinstance(node, QueryTerm):
        return _term_match(node, text) is not None
    if isinstance(node, QueryNot):
        return not _evaluate_query(node.operand, text)
    if isinstance(node, QueryAnd):
        return all(_evaluate_query(operand, text) for operand in node.operands)
    return any(_evaluate_query(operand, text) for operand in node.operands)


def query_matches(query: str | ParsedSearchQuery, text: str) -> bool:
    parsed = query if isinstance(query, ParsedSearchQuery) else parse_search_query(query)
    return _evaluate_query(parsed.root, text or "")


def matched_query_terms(query: str | ParsedSearchQuery, text: str) -> list[QueryTerm]:
    parsed = query if isinstance(query, ParsedSearchQuery) else parse_search_query(query)
    matched: list[QueryTerm] = []
    seen: set[tuple[str, bool, bool]] = set()
    for term in parsed.positive_terms:
        key = (term.text.casefold(), term.phrase, term.wildcard)
        if key not in seen and _term_match(term, text) is not None:
            seen.add(key)
            matched.append(term)
    return matched


def _wildcard_seed(text: str) -> str:
    seeds: list[str] = []
    for word in text.split():
        fragments = [fragment for fragment in re.split(r"[*?]+", word) if fragment]
        seed = max(fragments, key=len, default="")
        if seed:
            seeds.append(seed)
    return " ".join(seeds)


def _query_precedence(node: QueryNode) -> int:
    if isinstance(node, QueryOr):
        return 1
    if isinstance(node, QueryAnd):
        return 2
    if isinstance(node, QueryNot):
        return 3
    return 4


def _serialize_query_node(
    node: QueryNode,
    capabilities: SearchQueryCapabilities,
    *,
    parent_precedence: int = 0,
) -> str:
    if isinstance(node, QueryTerm):
        text = node.text if capabilities.wildcards or not node.wildcard else _wildcard_seed(node.text)
        text = collapse_ws(text)
        if node.phrase and capabilities.quoted_phrases and (not node.wildcard or capabilities.wildcards_in_phrases):
            return f'"{text.replace(chr(34), r"\"")}"'
        return text
    if isinstance(node, QueryNot):
        if not capabilities.negation:
            return ""
        child = _serialize_query_node(node.operand, capabilities, parent_precedence=3)
        if not child:
            return ""
        rendered = f"NOT {child}"
        if _query_precedence(node) < parent_precedence and capabilities.parentheses:
            return f"({rendered})"
        return rendered

    separator = " AND " if isinstance(node, QueryAnd) else " OR "
    parts = [
        rendered
        for operand in node.operands
        if (rendered := _serialize_query_node(
            operand,
            capabilities,
            parent_precedence=_query_precedence(node),
        ))
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    rendered = separator.join(parts)
    if _query_precedence(node) < parent_precedence and capabilities.parentheses:
        return f"({rendered})"
    return rendered


def query_capabilities_for_provider(provider: str | None) -> SearchQueryCapabilities:
    normalized = collapse_ws(provider or "Custom").casefold()
    if normalized in PROVIDER_QUERY_CAPABILITIES:
        return PROVIDER_QUERY_CAPABILITIES[normalized]
    for name, capabilities in PROVIDER_QUERY_CAPABILITIES.items():
        if name != "custom" and name in normalized:
            return capabilities
    return PLAIN_QUERY_CAPABILITIES


def _explicit_profile_capabilities(profile: dict[str, Any]) -> SearchQueryCapabilities | None:
    raw_capabilities: Any = profile.get("query_capabilities")
    if raw_capabilities is None:
        raw = profile.get("raw_discovery_json") or profile.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                raw = None
        if isinstance(raw, dict):
            raw_capabilities = raw.get("query_capabilities")
    if not isinstance(raw_capabilities, dict):
        return None
    return SearchQueryCapabilities(
        boolean_operators=bool(raw_capabilities.get("boolean_operators")),
        parentheses=bool(raw_capabilities.get("parentheses")),
        quoted_phrases=bool(raw_capabilities.get("quoted_phrases")),
        wildcards=bool(raw_capabilities.get("wildcards")),
        negation=bool(raw_capabilities.get("negation")),
        wildcards_in_phrases=bool(raw_capabilities.get("wildcards_in_phrases")),
    )


def query_capabilities_for_profile(profile: dict[str, Any]) -> SearchQueryCapabilities:
    explicit = _explicit_profile_capabilities(profile)
    if explicit is not None:
        return explicit
    return query_capabilities_for_provider(profile.get("provider_guess"))


def translate_search_query(
    query: str | ParsedSearchQuery,
    *,
    provider: str | None = None,
    capabilities: SearchQueryCapabilities | None = None,
) -> str:
    parsed = query if isinstance(query, ParsedSearchQuery) else parse_search_query(query)
    capabilities = capabilities or query_capabilities_for_provider(provider)
    if capabilities.boolean_operators:
        translated = _serialize_query_node(parsed.root, capabilities)
        if translated:
            return translated

    terms: list[str] = []
    seen: set[str] = set()
    for term in parsed.positive_terms:
        text = term.text if capabilities.wildcards or not term.wildcard else _wildcard_seed(term.text)
        text = collapse_ws(text)
        if not text:
            continue
        rendered = (
            f'"{text}"'
            if term.phrase and capabilities.quoted_phrases and (not term.wildcard or capabilities.wildcards_in_phrases)
            else text
        )
        if rendered.casefold() not in seen:
            seen.add(rendered.casefold())
            terms.append(rendered)
    return " ".join(terms)


def _limited_query_groups(
    node: QueryNode,
    *,
    negated: bool = False,
) -> list[tuple[QueryTerm, ...]]:
    if isinstance(node, QueryTerm):
        return [()] if negated else [(node,)]
    if isinstance(node, QueryNot):
        return _limited_query_groups(node.operand, negated=not negated)

    is_and = isinstance(node, QueryAnd)
    combine_as_and = is_and != negated
    child_groups = [
        _limited_query_groups(operand, negated=negated)
        for operand in node.operands
    ]
    if not combine_as_and:
        variants: list[tuple[QueryTerm, ...]] = []
        for groups in child_groups:
            variants.extend(groups)
            if len(variants) >= MAX_OUTBOUND_QUERY_VARIANTS:
                break
        return variants[:MAX_OUTBOUND_QUERY_VARIANTS]

    variants = [()]
    for groups in child_groups:
        combined: list[tuple[QueryTerm, ...]] = []
        for existing in variants:
            for group in groups:
                combined.append((*existing, *group))
                if len(combined) >= MAX_OUTBOUND_QUERY_VARIANTS:
                    break
            if len(combined) >= MAX_OUTBOUND_QUERY_VARIANTS:
                break
        variants = combined
    return variants


def outbound_query_variants(
    query: str | ParsedSearchQuery,
    *,
    provider: str | None = None,
    capabilities: SearchQueryCapabilities | None = None,
) -> tuple[str, ...]:
    parsed = query if isinstance(query, ParsedSearchQuery) else parse_search_query(query)
    capabilities = capabilities or query_capabilities_for_provider(provider)
    if capabilities.boolean_operators:
        return (translate_search_query(parsed, provider=provider, capabilities=capabilities),)

    variants: list[str] = []
    seen: set[str] = set()
    for group in _limited_query_groups(parsed.root):
        rendered_terms: list[str] = []
        term_seen: set[str] = set()
        for term in group:
            text = term.text if capabilities.wildcards or not term.wildcard else _wildcard_seed(term.text)
            text = collapse_ws(text)
            if not text:
                continue
            rendered = (
                f'"{text}"'
                if term.phrase and capabilities.quoted_phrases and (not term.wildcard or capabilities.wildcards_in_phrases)
                else text
            )
            if rendered.casefold() not in term_seen:
                term_seen.add(rendered.casefold())
                rendered_terms.append(rendered)
        rendered_query = " ".join(rendered_terms)
        if rendered_query and rendered_query.casefold() not in seen:
            seen.add(rendered_query.casefold())
            variants.append(rendered_query)
    if not variants:
        variants.append(translate_search_query(parsed, provider=provider, capabilities=capabilities))
    return tuple(variants[:MAX_OUTBOUND_QUERY_VARIANTS])


def translate_query_for_profile(query: str | ParsedSearchQuery, profile: dict[str, Any]) -> str:
    return translate_search_query(
        query,
        provider=profile.get("provider_guess"),
        capabilities=query_capabilities_for_profile(profile),
    )


def query_variants_for_profile(query: str | ParsedSearchQuery, profile: dict[str, Any]) -> tuple[str, ...]:
    return outbound_query_variants(
        query,
        provider=profile.get("provider_guess"),
        capabilities=query_capabilities_for_profile(profile),
    )


def make_snippet(text: str, query_text: str, radius: int = 200) -> str:
    haystack = text or ""
    parsed = parse_search_query(query_text)
    matches = [
        match
        for term in parsed.positive_terms
        if (match := _term_match(term, haystack)) is not None
    ]
    if not matches:
        return collapse_ws(haystack[: radius * 2])
    first_match = min(matches, key=lambda match: match.start())
    index = first_match.start()
    start = max(index - radius, 0)
    end = min(first_match.end() + radius, len(haystack))
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
    parsed_query = parse_search_query(query_text)
    heading_text = " ".join(headings)
    searchable_text = SEARCH_FIELD_SEPARATOR.join([title, heading_text, body_text])
    if not query_matches(parsed_query, searchable_text):
        return None

    matched_terms = matched_query_terms(parsed_query, searchable_text)
    if not matched_terms:
        return None

    score = 0.0
    if parsed_query.legacy_simple:
        term = parsed_query.positive_terms[0]
        occurrences = _term_occurrences(term, body_text)
        if _term_match(term, title):
            score += 50
        if _term_match(term, heading_text):
            score += 25
        score += 10 * occurrences
    else:
        for term in matched_terms:
            if _term_match(term, title):
                score += 30
            if _term_match(term, heading_text):
                score += 15
            score += 7 * _term_occurrences(term, body_text)
        if query_matches(parsed_query, title):
            score += 20
        elif query_matches(parsed_query, SEARCH_FIELD_SEPARATOR.join([title, heading_text])):
            score += 10
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
        "matched_terms": [term.text for term in matched_terms],
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
    base_url = prefer_https_url(district.get("website_normalized") or normalize_website(district.get("website"))[0])
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
            wait_for_request_delay(settings, cancel_requested)

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
    query = translate_search_query(query_text, provider="Brave")
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
    base_url = prefer_https_url(district.get("website_normalized") or normalize_website(district.get("website"))[0])
    if not base_url:
        debug_log(debug_logger, "district_skipped", district=district.get("agency_name"), reason="missing_website")
        return []

    api_results = brave_api_search(query_text, base_url, settings, debug_logger)
    result_map: dict[str, dict[str, Any]] = {}
    for result in api_results:
        api_match = score_match(
            query_text,
            str(result.get("title") or ""),
            [],
            str(result.get("snippet") or ""),
            str(result.get("url") or ""),
            "search/api",
        )
        if api_match:
            result_map[result["url"]] = {**result, "matched_terms": api_match["matched_terms"]}
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
            wait_for_request_delay(settings, cancel_requested)

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
    parse_search_query(query_text)
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


def _policy_int(policy: dict[str, Any], name: str, default: int) -> int:
    try:
        return int(policy.get(name, default))
    except (TypeError, ValueError):
        return default


def _policy_float(policy: dict[str, Any], name: str, default: float) -> float:
    try:
        return float(policy.get(name, default))
    except (TypeError, ValueError):
        return default


def _insert_search_run_items(
    conn: Any,
    run_id: int,
    districts: list[dict[str, Any]],
) -> None:
    now = utc_now_iso()
    conn.executemany(
        """
        INSERT OR IGNORE INTO search_run_items (
            run_id, district_id, ordinal, status, attempt, result_count,
            started_at, finished_at, error_message, created_at, updated_at, queued_at
        )
        VALUES (?, ?, ?, 'queued', 0, 0, NULL, NULL, NULL, ?, ?, ?)
        """,
        [
            (run_id, int(district["id"]), ordinal, now, now, now)
            for ordinal, district in enumerate(districts, start=1)
        ],
    )


def _ensure_search_run_items(run: dict[str, Any], db_path: Path | str | None) -> None:
    with connect_db(db_path) as conn:
        item_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM search_run_items WHERE run_id = ?",
                (run["id"],),
            ).fetchone()["count"]
            or 0
        )
    if item_count or not int(run.get("districts_matched") or 0):
        return
    districts = list_matching_districts(
        _clean_list(json_loads_list(run.get("states_json"))),
        _clean_list(json_loads_list(run.get("agency_types_json"))),
        run.get("min_enrollment"),
        run.get("max_enrollment"),
        limit=int(run.get("max_districts") or MAX_TOTAL_DISTRICTS_PER_RUN),
        db_path=db_path,
    )
    with connect_db(db_path) as conn:
        _insert_search_run_items(conn, int(run["id"]), districts)
        conn.commit()


def _search_run_progress(conn: Any, run_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END) AS searched,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM search_run_items
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return int(row["searched"] or 0), int(row["failed"] or 0)


def _controller_target_workers(
    controller: Any | None,
    default: int,
    maximum: int,
    *,
    backlog: int,
    active: int,
) -> int:
    if controller is None:
        return max(1, min(maximum, default))
    value: Any = None
    for name in ("target_workers", "current_workers", "get_target_workers"):
        candidate = getattr(controller, name, None)
        if callable(candidate):
            try:
                value = candidate(backlog=backlog, active=active, maximum=maximum)
            except TypeError:
                try:
                    value = candidate()
                except Exception:
                    LOGGER.exception("Search resource controller could not report target workers")
                    value = None
            except Exception:
                LOGGER.exception("Search resource controller could not report target workers")
                value = None
        elif candidate is not None:
            value = candidate
        if value is not None:
            break
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _observe_district_completion(
    controller: Any | None,
    *,
    success: bool,
    elapsed_seconds: float,
    result_count: int,
    backlog: int,
    active: int,
) -> None:
    observer = _controller_callable(
        controller,
        "observe_district_completion",
        "on_task_complete",
        "observe_completion",
    )
    if observer is not None:
        try:
            observer(
                success=success,
                elapsed_seconds=elapsed_seconds,
                result_count=result_count,
                backlog=backlog,
                active=active,
            )
        except TypeError:
            try:
                observer(success)
            except Exception:
                LOGGER.exception("Search resource controller completion hook failed")
        except Exception:
            LOGGER.exception("Search resource controller completion hook failed")
        return
    observe_snapshot = _controller_callable(controller, "observe")
    if observe_snapshot is None:
        return
    try:
        from guided_search.resources import collect_resource_snapshot

        snapshot = collect_resource_snapshot(
            backlog=backlog,
            active_workers=active,
            http_error_rate=0.0 if success else 1.0,
            timeout_rate=0.0,
            include_gpu=True,
        )
        observe_snapshot(snapshot)
    except Exception:
        LOGGER.exception("Search resource controller could not observe a resource snapshot")


def _persist_resource_state(
    run_id: int,
    target_workers: int,
    settings: SearchSettings,
    db_path: Path | str | None,
) -> None:
    delay = _controller_delay_seconds(settings.resource_controller, settings.delay_seconds)
    with connect_db(db_path) as conn:
        conn.execute(
            """
            UPDATE search_runs
            SET current_workers = ?, current_delay_seconds = ?
            WHERE id = ?
            """,
            (target_workers, delay, run_id),
        )
        conn.commit()


def create_search_run(
    query_text: str,
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    max_districts: int | None = None,
    max_workers: int | None = None,
    debug_logging: bool = False,
    db_path: Path | str | None = None,
    settings: SearchSettings | None = None,
    status: str = "queued",
    district_ids: list[int] | tuple[int, ...] | None = None,
    adaptive_enabled: bool = False,
    resource_policy: dict[str, Any] | None = None,
) -> int:
    query_text = str(query_text or "").strip()
    parse_search_query(query_text)
    init_db(db_path)
    settings = settings or SearchSettings()
    policy = dict(resource_policy or {})
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
    configured_max_workers = max_workers
    if configured_max_workers is None and adaptive_enabled:
        configured_max_workers = _policy_int(policy, "max_workers", SEARCH_RUN_WORKERS)
    worker_count = clamp_int(configured_max_workers, SEARCH_RUN_WORKERS, 1, MAX_SEARCH_WORKERS)
    initial_workers = worker_count
    if adaptive_enabled:
        initial_workers = clamp_int(
            _policy_int(policy, "initial_workers", worker_count),
            worker_count,
            1,
            worker_count,
        )
    initial_delay = max(
        0.0,
        _policy_float(policy, "initial_delay_seconds", settings.delay_seconds),
    )
    states = _clean_list(states)
    agency_types = _clean_list(agency_types)
    cleaned_district_ids = clean_district_ids(district_ids)
    matched_count = count_matching_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        db_path,
        district_ids=cleaned_district_ids,
    )
    districts = list_matching_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        limit=cap,
        db_path=db_path,
        district_ids=cleaned_district_ids,
    )

    submitted_at = utc_now_iso()
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_runs (
                query_text, states_json, agency_types_json, min_enrollment,
                max_enrollment, max_districts, max_pages_per_district,
                search_method, search_provider, api_results_per_district,
                follow_depth, max_workers, adaptive_enabled, resource_policy_json,
                current_workers, current_delay_seconds,
                cancel_requested, debug_logging, debug_log_path, status,
                districts_matched, districts_searched, districts_failed, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, 0, 0, ?)
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
                worker_count,
                1 if adaptive_enabled else 0,
                json_dumps(policy),
                initial_workers,
                initial_delay,
                1 if debug_logging else 0,
                status,
                matched_count,
                submitted_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        _insert_search_run_items(conn, run_id, districts)
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
    resource_controller: Any | None = None,
) -> None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        loaded = conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
    if loaded is None:
        raise ValueError(f"Search run not found: {run_id}")
    run = dict(loaded)
    _ensure_search_run_items(run, db_path)

    query_text = run["query_text"]
    states = _clean_list(json_loads_list(run.get("states_json")))
    agency_types = _clean_list(json_loads_list(run.get("agency_types_json")))
    min_enrollment = run.get("min_enrollment")
    max_enrollment = run.get("max_enrollment")
    cap = int(run.get("max_districts") or MAX_TOTAL_DISTRICTS_PER_RUN)
    max_pages = int(run.get("max_pages_per_district") or MAX_PAGES_PER_DISTRICT)
    search_method = normalize_search_method(run.get("search_method"))
    search_provider = run.get("search_provider") or (
        "brave"
        if search_method in {"brave", "hybrid"}
        else "district_search"
        if search_method in {"district_search", "district_search_hybrid", "district_search_browser", "district_search_browser_hybrid"}
        else "crawler"
    )
    api_results_per_district = clamp_api_results(run.get("api_results_per_district"))
    follow_depth = clamp_follow_depth(run.get("follow_depth"))
    max_workers = clamp_int(run.get("max_workers"), SEARCH_RUN_WORKERS, 1, MAX_SEARCH_WORKERS)
    initial_workers = clamp_int(run.get("current_workers"), max_workers, 1, max_workers)
    adaptive_enabled = bool(run.get("adaptive_enabled"))
    matched_count = int(run.get("districts_matched") or 0)
    debug_enabled = bool(run.get("debug_logging"))

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

    if bool(run.get("cancel_requested")):
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE search_run_items
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    error_message = 'Cancelled before start.'
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, now, run_id),
            )
            searched, failed = _search_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'cancelled', districts_searched = ?, districts_failed = ?,
                    finished_at = ?, error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (searched, failed, now, run_id),
            )
            conn.commit()
        debug_log(debug_logger, "run_cancelled", run_id=run_id, searched=searched, failed=failed)
        return

    base_settings = settings or SearchSettings()
    active_controller = resource_controller or base_settings.resource_controller
    run_settings = SearchSettings(
        max_pages_per_district=max_pages,
        max_results_per_district=base_settings.max_results_per_district,
        request_timeout_seconds=base_settings.request_timeout_seconds,
        delay_seconds=(
            max(0.0, float(run.get("current_delay_seconds") or 0.0))
            if adaptive_enabled and run.get("current_delay_seconds") is not None
            else base_settings.delay_seconds
        ),
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
        resource_controller=active_controller,
    )

    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status, cancel_requested FROM search_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if current is None or current["status"] != "queued":
            conn.rollback()
            return
        if current["cancel_requested"]:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE search_run_items
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    error_message = 'Cancelled before dispatch.'
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, now, run_id),
            )
            searched, failed = _search_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'cancelled', districts_searched = ?, districts_failed = ?,
                    finished_at = ?, error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (searched, failed, now, run_id),
            )
            conn.commit()
            return
        searched, failed = _search_run_progress(conn, run_id)
        claimed = conn.execute(
            """
            UPDATE search_runs
            SET status = 'running', max_workers = ?, districts_searched = ?,
                districts_failed = ?, finished_at = NULL, error_message = NULL
            WHERE id = ? AND status = 'queued'
            """,
            (max_workers, searched, failed, run_id),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return
        conn.commit()

    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.*, i.ordinal AS run_item_ordinal, i.attempt AS run_item_attempt
            FROM search_run_items i
            JOIN districts d ON d.id = i.district_id
            WHERE i.run_id = ? AND i.status = 'queued'
            ORDER BY i.ordinal
            """,
            (run_id,),
        ).fetchall()
    pending_districts = [dict(row) for row in rows]

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
        max_workers=max_workers,
        current_workers=initial_workers,
        matched_count=matched_count,
        pending_items=len(pending_districts),
    )

    def search_one(district: dict[str, Any]) -> list[dict[str, Any]]:
        return search_district(
            district,
            query_text,
            run_settings,
            cancel_requested=lambda: is_cancel_requested(run_id, db_path),
            debug_logger=debug_logger,
            db_path=db_path,
        )

    def claim_item(district_id: int) -> bool:
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            cursor = conn.execute(
                """
                UPDATE search_run_items
                SET status = 'running', attempt = attempt + 1, started_at = ?,
                    updated_at = ?, finished_at = NULL, error_message = NULL
                WHERE run_id = ? AND district_id = ? AND status = 'queued'
                """,
                (now, now, run_id, district_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def store_district_results(
        district: dict[str, Any],
        district_results: list[dict[str, Any]],
    ) -> tuple[int, int]:
        now = utc_now_iso()
        with connect_db(db_path) as conn:
            # A retry replaces only this district's evidence. Completed districts
            # and their results are never cleared when a run resumes.
            conn.execute(
                "DELETE FROM search_results WHERE search_run_id = ? AND district_id = ?",
                (run_id, district["id"]),
            )
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
            conn.execute(
                """
                UPDATE search_run_items
                SET status = 'completed', result_count = ?, finished_at = ?,
                    updated_at = ?, error_message = NULL
                WHERE run_id = ? AND district_id = ?
                """,
                (len(district_results), now, now, run_id, district["id"]),
            )
            searched_count, failed_count = _search_run_progress(conn, run_id)
            conn.execute(
                "UPDATE search_runs SET districts_searched = ?, districts_failed = ? WHERE id = ?",
                (searched_count, failed_count, run_id),
            )
            conn.commit()
        debug_log(
            debug_logger,
            "district_results_stored",
            run_id=run_id,
            district=district.get("agency_name"),
            stored_results=len(district_results),
        )
        return searched_count, failed_count

    def store_district_failure(district: dict[str, Any], error: BaseException) -> tuple[int, int]:
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE search_run_items
                SET status = 'failed', result_count = 0, finished_at = ?,
                    updated_at = ?, error_message = ?
                WHERE run_id = ? AND district_id = ?
                """,
                (now, now, str(error), run_id, district["id"]),
            )
            searched_count, failed_count = _search_run_progress(conn, run_id)
            conn.execute(
                "UPDATE search_runs SET districts_searched = ?, districts_failed = ? WHERE id = ?",
                (searched_count, failed_count, run_id),
            )
            conn.commit()
        return searched_count, failed_count

    cancelled = False
    next_index = 0
    future_to_district: dict[Future[list[dict[str, Any]]], tuple[dict[str, Any], float]] = {}
    target_workers = initial_workers
    try:
        LOGGER.info("Search run %s started: query=%r matched=%s cap=%s", run_id, query_text, matched_count, cap)
        debug_log(
            debug_logger,
            "run_start",
            run_id=run_id,
            district_count=len(pending_districts),
            max_workers=max_workers,
            target_workers=target_workers,
        )
        _persist_resource_state(run_id, target_workers, run_settings, db_path)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="SearchRun") as executor:
            while next_index < len(pending_districts) or future_to_district:
                if is_cancel_requested(run_id, db_path):
                    cancelled = True
                backlog = len(pending_districts) - next_index
                target_workers = _controller_target_workers(
                    active_controller,
                    target_workers,
                    max_workers,
                    backlog=backlog,
                    active=len(future_to_district),
                )
                _persist_resource_state(run_id, target_workers, run_settings, db_path)

                while (
                    not cancelled
                    and next_index < len(pending_districts)
                    and len(future_to_district) < target_workers
                ):
                    district = pending_districts[next_index]
                    next_index += 1
                    if not claim_item(int(district["id"])):
                        continue
                    future = executor.submit(search_one, district)
                    future_to_district[future] = (district, time.monotonic())

                if not future_to_district:
                    break
                completed, _not_done = wait(
                    tuple(future_to_district),
                    timeout=0.5,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue
                for future in completed:
                    district, started_monotonic = future_to_district.pop(future)
                    elapsed = max(0.0, time.monotonic() - started_monotonic)
                    try:
                        district_results = future.result()
                        searched, failed = store_district_results(district, district_results)
                        success = True
                        result_count = len(district_results)
                    except Exception as exc:
                        searched, failed = store_district_failure(district, exc)
                        success = False
                        result_count = 0
                        LOGGER.exception("District search failed for %s: %s", district.get("agency_name"), exc)
                        debug_log(
                            debug_logger,
                            "district_error",
                            run_id=run_id,
                            district=district.get("agency_name"),
                            error=str(exc),
                        )
                    _observe_district_completion(
                        active_controller,
                        success=success,
                        elapsed_seconds=elapsed,
                        result_count=result_count,
                        backlog=len(pending_districts) - next_index,
                        active=len(future_to_district),
                    )
                    target_workers = _controller_target_workers(
                        active_controller,
                        target_workers,
                        max_workers,
                        backlog=len(pending_districts) - next_index,
                        active=len(future_to_district),
                    )
                    _persist_resource_state(run_id, target_workers, run_settings, db_path)

        if cancelled:
            with connect_db(db_path) as conn:
                now = utc_now_iso()
                conn.execute(
                    """
                    UPDATE search_run_items
                    SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        error_message = 'Cancelled before dispatch.'
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (now, now, run_id),
                )
                conn.commit()

        with connect_db(db_path) as conn:
            searched, failed = _search_run_progress(conn, run_id)
            cancel_row = conn.execute(
                "SELECT cancel_requested FROM search_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            final_status = "cancelled" if cancelled or bool(cancel_row and cancel_row["cancel_requested"]) else "completed"
            error_message = "Cancelled by user." if final_status == "cancelled" else None
            conn.execute(
                """
                UPDATE search_runs
                SET status = ?, districts_searched = ?, districts_failed = ?,
                    finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (final_status, searched, failed, utc_now_iso(), error_message, run_id),
            )
            conn.commit()
        debug_log(debug_logger, "run_finish", run_id=run_id, status=final_status, searched=searched, failed=failed)
        LOGGER.info("Search run %s %s: searched=%s failed=%s", run_id, final_status, searched, failed)
    except Exception as exc:
        with connect_db(db_path) as conn:
            searched, failed = _search_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE search_runs
                SET status = 'failed', districts_searched = ?, districts_failed = ?,
                    finished_at = ?, error_message = ?
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
    max_workers: int | None = None,
    debug_logging: bool = False,
    db_path: Path | str | None = None,
    settings: SearchSettings | None = None,
    district_ids: list[int] | tuple[int, ...] | None = None,
    adaptive_enabled: bool = False,
    resource_policy: dict[str, Any] | None = None,
    resource_controller: Any | None = None,
) -> int:
    run_id = create_search_run(
        query_text,
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        max_districts=max_districts,
        max_workers=max_workers,
        debug_logging=debug_logging,
        db_path=db_path,
        settings=settings,
        status="queued",
        district_ids=district_ids,
        adaptive_enabled=adaptive_enabled,
        resource_policy=resource_policy,
    )
    execute_search_run(
        run_id,
        db_path=db_path,
        settings=settings,
        resource_controller=resource_controller,
    )
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
