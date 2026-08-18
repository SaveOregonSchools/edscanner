from __future__ import annotations

import csv
import hashlib
import heapq
import io
import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ai_matcher import analyze_contract_candidate, local_llm_is_configured
from common import (
    CONTRACT_ARCHIVE_DIR,
    CONTRACT_DISTRICT_DIR_NAME_MAX,
    CONTRACT_DISCOVERY_RUN_LOGS_DIR,
    CONTRACT_DISCOVERY_WORKERS,
    CONTRACT_RESCAN_DAYS,
    MAX_HTML_SIZE_BYTES,
    MAX_PDF_SIZE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    VERIFY_SSL,
    connect_db,
    json_dumps,
    normalize_website,
    prefer_https_url,
    utc_now_iso,
)
from search_engine import (
    RunDebugLogger,
    SearchSettings,
    build_district_filter_sql,
    canonical_url,
    debug_log,
    fetch_limited,
    load_robots,
    make_session,
    parse_html,
    parse_sitemap,
    same_organization_url,
)


LOGGER = logging.getLogger(__name__)

UNIT_TYPES = (
    "licensed",
    "classified",
    "substitute",
    "administrators",
    "transportation",
    "service",
    "other",
    "unknown",
)
DOCUMENT_TYPES = (
    "base_agreement",
    "extension",
    "mou",
    "amendment",
    "salary_schedule",
    "tentative_agreement",
    "landing_page",
    "other",
)
UNIT_LABELS = {
    "licensed": "Licensed / certified staff",
    "classified": "Classified staff",
    "substitute": "Substitute educators",
    "administrators": "Administrators / supervisors",
    "transportation": "Transportation staff",
    "service": "Service staff",
    "other": "Other bargaining unit",
    "unknown": "Unknown bargaining unit",
}

STRONG_TERMS = (
    "collective bargaining agreement",
    "bargaining agreement",
    "employment contract",
    "labor agreement",
    "union contract",
    "licensed contract",
    "classified contract",
)
NAV_TERMS = {
    "collective": 5,
    "bargaining": 5,
    "agreement": 4,
    "contract": 4,
    "labor relations": 5,
    "employee relations": 4,
    "salary schedule": 4,
    "human resources": 3,
    "staff resources": 2,
    "careers": 3,
    "staff": 2,
    "jobs": 2,
    "employees": 2,
}
DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".rtf")
KNOWN_UNION_ACRONYMS = (
    "OSEA",
    "SEIU",
    "AFSCME",
    "ATU",
    "Teamsters",
    "PAT",
    "PFSP",
    "DCU",
    "BEA",
    "HEA",
    "NCEA",
    "EEA",
    "SEA",
    "TTEA",
    "REA",
    "GBEA",
    "GAEA",
    "MEA",
    "SKEA",
    "ASK ESP",
)


@dataclass(frozen=True)
class ContractDiscoverySettings:
    max_pages_per_district: int = 20
    max_candidates_per_district: int = 50
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    delay_seconds: float = 0.0
    max_pdf_size_bytes: int = MAX_PDF_SIZE_BYTES
    max_html_size_bytes: int = MAX_HTML_SIZE_BYTES
    user_agent: str = USER_AGENT
    verify_ssl: bool = VERIFY_SSL
    include_salary_schedules: bool = True
    use_llm: bool = False
    archive_documents: bool = True
    store_extracted_text: bool = True
    archive_root: Path = CONTRACT_ARCHIVE_DIR
    district_dir_name_max: int = CONTRACT_DISTRICT_DIR_NAME_MAX

    def search_settings(self) -> SearchSettings:
        return SearchSettings(
            max_pages_per_district=max(self.max_pages_per_district, 1),
            max_results_per_district=max(self.max_candidates_per_district, 1),
            request_timeout_seconds=self.request_timeout_seconds,
            delay_seconds=self.delay_seconds,
            max_pdf_size_bytes=self.max_pdf_size_bytes,
            max_html_size_bytes=self.max_html_size_bytes,
            user_agent=self.user_agent,
            verify_ssl=self.verify_ssl,
        )


@dataclass
class Candidate:
    url: str
    title: str
    parent_page_url: str
    parent_context: str
    discovery_source: str
    score: int


def _collapse_ws(value: str) -> str:
    return " ".join(str(value or "").split())


WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _safe_path_segment(value: Any, *, max_length: int, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    segment = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-. ")
    segment = segment[:max(1, max_length)].rstrip("-. ") or fallback
    if segment.casefold() in WINDOWS_RESERVED_NAMES:
        segment = f"{fallback}-{segment}"
    return segment


def district_archive_directory(
    district: dict[str, Any],
    archive_root: Path | str = CONTRACT_ARCHIVE_DIR,
    district_name_max: int = CONTRACT_DISTRICT_DIR_NAME_MAX,
) -> Path:
    """Build a bounded, collision-safe STATE/district archive directory."""

    state = str(district.get("state") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        state = "XX"
    district_name = _safe_path_segment(
        district.get("agency_name"),
        max_length=max(20, district_name_max),
        fallback="district",
    )
    unique_value = district.get("agency_id_nces") or district.get("id") or "unknown"
    unique_id = _safe_path_segment(unique_value, max_length=24, fallback="unknown")
    return Path(archive_root).expanduser().resolve() / state / f"{district_name}--{unique_id}"


def _document_extension(url: str, content_type: str) -> str:
    path_suffix = Path(urlparse(url).path).suffix.casefold()
    if path_suffix in {".pdf", ".doc", ".docx", ".rtf", ".txt", ".html", ".htm"}:
        return ".html" if path_suffix == ".htm" else path_suffix
    media_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
        "text/plain": ".txt",
        "text/html": ".html",
    }.get(media_type, ".bin")


def _write_archive_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def archive_document_files(
    district: dict[str, Any],
    document: dict[str, Any],
    content: bytes,
    extracted_text: str,
    settings: ContractDiscoverySettings,
) -> dict[str, Any]:
    """Archive a fetched source file and optional extracted-text sidecar."""

    content_hash = hashlib.sha256(content).hexdigest() if content else None
    directory = district_archive_directory(
        district,
        settings.archive_root,
        settings.district_dir_name_max,
    )
    descriptive = _safe_path_segment(
        f"{document.get('bargaining_unit_type')} {document.get('document_type')} {document.get('title')}",
        max_length=80,
        fallback="contract-document",
    )
    token = (content_hash or hashlib.sha256(str(document.get("url") or "").encode("utf-8")).hexdigest())[:12]
    stem = f"{descriptive}--{token}"
    local_file_path: str | None = None
    extracted_text_path: str | None = None

    if settings.archive_documents and content:
        source_path = directory / f"{stem}{_document_extension(document.get('url') or '', document.get('content_type') or '')}"
        _write_archive_file(source_path, content)
        local_file_path = str(source_path.resolve())
    if settings.store_extracted_text and extracted_text.strip():
        text_path = directory / f"{stem}.extracted.txt"
        _write_archive_file(text_path, extracted_text.encode("utf-8"))
        extracted_text_path = str(text_path.resolve())

    return {
        "content_sha256": content_hash,
        "file_size_bytes": len(content) if content else None,
        "local_file_path": local_file_path,
        "extracted_text_path": extracted_text_path,
        "archived_at": utc_now_iso() if local_file_path or extracted_text_path else None,
    }


def _link_score(title: str, url: str) -> int:
    haystack = f"{title} {url}".casefold()
    score = 0
    for term, weight in NAV_TERMS.items():
        if term in haystack:
            score += weight
    if "cba" in re.split(r"[^a-z0-9]+", haystack):
        score += 6
    title_tokens = set(re.findall(r"[A-Za-z]+", title))
    if any(acronym in title_tokens for acronym in KNOWN_UNION_ACRONYMS if " " not in acronym and acronym != "Teamsters"):
        score += 4
    if any(urlparse(url).path.casefold().endswith(suffix) for suffix in DOCUMENT_SUFFIXES):
        score += 2
    if any(token in haystack for token in ("proposal", "minutes", "agenda", "news", "board packet")):
        score -= 3
    if any(token in haystack for token in ("procurement", "contractor", "vendor")):
        score -= 6
    return score


def _extract_links(content: bytes, final_url: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(content, "lxml")
    out: list[tuple[str, str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = canonical_url(urljoin(final_url, href))
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        title = _collapse_ws(anchor.get_text(" ", strip=True) or anchor.get("title") or "")
        container = anchor.find_parent(["li", "p", "article", "section"])
        context = _collapse_ws(container.get_text(" ", strip=True) if container else title)[:1200]
        out.append((url, title, context))
    return out


def _page_is_contract_hub(title: str, text: str, url: str) -> bool:
    sample = f"{title} {url} {text[:12000]}".casefold()
    strong_hits = sum(1 for term in STRONG_TERMS if term in sample)
    return strong_hits > 0 or ("contract" in sample and ("union" in sample or "association" in sample))


def discover_contract_candidates(
    district: dict[str, Any],
    settings: ContractDiscoverySettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> list[Candidate]:
    base_url = prefer_https_url(district.get("website_normalized") or normalize_website(district.get("website"))[0])
    if not base_url:
        return []
    search_settings = settings.search_settings()
    session = make_session(search_settings)
    pending: list[tuple[int, int, int, str]] = []
    sequence = 0
    heapq.heappush(pending, (-100, 0, sequence, canonical_url(base_url)))
    queued = {canonical_url(base_url)}
    visited: set[str] = set()
    successful_page_fetches = 0
    candidates: dict[str, Candidate] = {}

    try:
        _robots, robots_sitemaps = load_robots(session, base_url, search_settings)
        sitemap_settings = SearchSettings(
            max_pages_per_district=1000,
            request_timeout_seconds=search_settings.request_timeout_seconds,
            delay_seconds=0,
            max_pdf_size_bytes=search_settings.max_pdf_size_bytes,
            max_html_size_bytes=search_settings.max_html_size_bytes,
            user_agent=search_settings.user_agent,
            verify_ssl=search_settings.verify_ssl,
        )
        sitemap_urls: set[str] = set()
        for sitemap_url in [*robots_sitemaps, urljoin(base_url, "/sitemap.xml")]:
            sitemap_urls.update(parse_sitemap(session, sitemap_url, base_url, sitemap_settings))
        ranked_sitemap_urls = sorted(
            ((_link_score(urlparse(url).path.replace("/", " "), url), url) for url in sitemap_urls),
            key=lambda item: (-item[0], item[1]),
        )
        for score, sitemap_page_url in ranked_sitemap_urls[: max(20, settings.max_pages_per_district * 2)]:
            if score < 4 or sitemap_page_url in queued:
                continue
            sequence += 1
            queued.add(sitemap_page_url)
            heapq.heappush(pending, (-score, 1, sequence, sitemap_page_url))
    except Exception as exc:
        debug_log(debug_logger, "contract_sitemap_error", district=district.get("agency_name"), error=str(exc))

    while pending and len(visited) < settings.max_pages_per_district:
        if cancel_requested and cancel_requested():
            break
        _negative_score, depth, _sequence, url = heapq.heappop(pending)
        if url in visited:
            continue
        visited.add(url)
        try:
            response, content = fetch_limited(session, url, search_settings)
            if response.status_code >= 400:
                continue
            final_url = canonical_url(response.url)
            content_type = response.headers.get("Content-Type", "").casefold()
            if "html" not in content_type and "text/plain" not in content_type and content_type:
                continue
            page_title, _headings, page_text, _plain_links = parse_html(content, final_url)
            successful_page_fetches += 1
            page_links = _extract_links(content, final_url)
            page_contract_hub = _page_is_contract_hub(page_title, page_text, final_url)
            relevant_links = 0
            for link_url, link_title, link_context in page_links:
                score = _link_score(link_title, link_url)
                path = urlparse(link_url).path.casefold()
                is_document = path.endswith(DOCUMENT_SUFFIXES)
                if score >= 5 or (page_contract_hub and score >= 4) or (is_document and score >= 3):
                    relevant_links += 1
                    candidate = Candidate(
                        url=link_url,
                        title=link_title or path.rsplit("/", 1)[-1],
                        parent_page_url=final_url,
                        parent_context=_collapse_ws(f"{page_title} {link_context}"),
                        discovery_source="district_navigation",
                        score=score,
                    )
                    existing = candidates.get(link_url)
                    if existing is None or candidate.score > existing.score:
                        candidates[link_url] = candidate
                if (
                    depth < 3
                    and score >= 2
                    and not is_document
                    and same_organization_url(link_url, base_url)
                    and link_url not in queued
                ):
                    sequence += 1
                    queued.add(link_url)
                    heapq.heappush(pending, (-score, depth + 1, sequence, link_url))
            if relevant_links == 0 and page_contract_hub and url != canonical_url(base_url):
                candidates[final_url] = Candidate(
                    url=final_url,
                    title=page_title or urlparse(final_url).path.rsplit("/", 1)[-1],
                    parent_page_url=final_url,
                    parent_context=_collapse_ws(page_text[:5000]),
                    discovery_source="district_contract_page",
                    score=max(6, _link_score(page_title, final_url)),
                )
        except Exception as exc:
            debug_log(debug_logger, "contract_page_error", district=district.get("agency_name"), url=url, error=str(exc))
        if settings.delay_seconds:
            time.sleep(settings.delay_seconds)

    if successful_page_fetches == 0 and not (cancel_requested and cancel_requested()):
        raise RuntimeError("No district website pages could be fetched successfully.")
    ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.url))
    debug_log(
        debug_logger,
        "contract_candidates_discovered",
        district=district.get("agency_name"),
        pages_visited=len(visited),
        candidates=len(ordered),
    )
    return ordered[: settings.max_candidates_per_district]


def _parse_contract_pdf(content: bytes, *, max_pages: int = 250) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts: list[str] = []
    page_count = min(len(reader.pages), max_pages)
    for index in range(page_count):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            text = ""
        if text:
            parts.append(text)
    return "\n".join(parts)


def _parse_written_date(month: str, day: str, year: str) -> str | None:
    try:
        parsed = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
    except ValueError:
        try:
            parsed = datetime.strptime(f"{month} {day} {year}", "%b %d %Y")
        except ValueError:
            return None
    return parsed.date().isoformat()


def extract_agreement_dates(text: str) -> tuple[str | None, str | None, float]:
    sample = _collapse_ws(text)[:50000]
    months = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    explicit = re.search(
        rf"\b({months})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\s+(?:through|to|until|[-–—])\s+({months})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
        sample,
        re.IGNORECASE,
    )
    if explicit:
        start = _parse_written_date(explicit.group(1).title(), explicit.group(2), explicit.group(3))
        end = _parse_written_date(explicit.group(4).title(), explicit.group(5), explicit.group(6))
        if start and end:
            return start, end, 0.95
    numeric = re.search(
        r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})\s+(?:through|to|until|[-–—])\s+(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})\b",
        sample,
        re.IGNORECASE,
    )
    if numeric:
        try:
            start = date(int(numeric.group(3)), int(numeric.group(1)), int(numeric.group(2))).isoformat()
            end = date(int(numeric.group(6)), int(numeric.group(4)), int(numeric.group(5))).isoformat()
            return start, end, 0.95
        except ValueError:
            pass
    year_range = re.search(r"\b(20\d{2})\s*[-–—]\s*(?:(20)?(\d{2}))\b", sample)
    if year_range:
        start_year = int(year_range.group(1))
        end_year = int((year_range.group(2) or "20") + year_range.group(3))
        if start_year <= end_year <= start_year + 10:
            return f"{start_year:04d}-07-01", f"{end_year:04d}-06-30", 0.72
    return None, None, 0.0


def classify_document_type(text: str, content_type: str = "") -> str:
    value = text.casefold()
    if "salary schedule" in value or "wage schedule" in value:
        return "salary_schedule"
    if "tentative agreement" in value:
        return "tentative_agreement"
    if "extension" in value or "extend" in value and "agreement" in value:
        return "extension"
    if any(term in value for term in ("memorandum of understanding", "memorandum of agreement", " mou ", " moa ")):
        return "mou"
    if "amendment" in value or "addendum" in value:
        return "amendment"
    if any(term in value for term in (*STRONG_TERMS, " cba ")):
        return "base_agreement"
    if "html" in content_type:
        return "landing_page"
    return "other"


def classify_bargaining_unit(text: str) -> tuple[str, float]:
    original = _collapse_ws(text)
    value = f" {original.casefold()} "
    if any(term in value for term in (" substitute ", " substitutes ", "substitute teacher", "substitute educator", "guest teacher", "substitute association")):
        return "substitute", 0.95
    if re.search(r"\bATU\b", original):
        return "transportation", 0.95
    if re.search(r"\bSEIU\b", original):
        return "service", 0.95
    if re.search(r"\b(?:OSEA|PFSP)\b", original) or "osea" in original.casefold():
        return "classified", 0.95
    if re.search(r"\bASK\s+ESP\b", original):
        return "classified", 0.95
    if re.search(r"\b(?:PAT|BEA|HEA|NCEA|EEA|SEA|TTEA|REA|GBEA|GAEA|MEA|SKEA)\b", original):
        return "licensed", 0.92
    if any(term in value for term in (" administrator ", " supervisor ", " confidential ", "administrator association", "association of administrators", "administrators and confidential", "management and confidential", "supervisory-technical")):
        return "administrators", 0.9
    if any(term in value for term in ("amalgamated transit union", " atu ", "bus drivers", "transportation employees")):
        return "transportation", 0.9
    if any(term in value for term in ("service employees international union", " seiu ", "nutrition services", "custodians and nutrition")):
        return "service", 0.9
    if any(term in value for term in ("oregon school employees association", " osea ", " classified ", "classified staff", "classified employees", "education support professionals", "paraeducators")):
        return "classified", 0.92
    if any(term in value for term in (" licensed ", " certified ", "licensed employees", "licensed staff", "certified employees", "certified staff", "teachers association", "education association", "association of teachers", "licensed educator")):
        return "licensed", 0.88
    return "unknown", 0.2


def hinted_bargaining_units(text: str) -> set[str]:
    value = f" {_collapse_ws(text).casefold()} "
    hints: set[str] = set()
    if any(term in value for term in ("substitute teacher", "substitute educator", "guest teacher")):
        hints.add("substitute")
    if any(term in value for term in (" licensed ", " certified ", " education association", " teachers association")):
        hints.add("licensed")
    if any(term in value for term in (" classified ", " osea ", " support professionals", " paraeducator")):
        hints.add("classified")
    if any(term in value for term in (" administrator ", " supervisor ", " confidential ", " management ")):
        hints.add("administrators")
    if any(term in value for term in (" transportation ", " bus driver", " atu ")):
        hints.add("transportation")
    if any(term in value for term in (" seiu ", " nutrition services", " custodian")):
        hints.add("service")
    return hints


def extract_union_name(text: str) -> str | None:
    sample = _collapse_ws(text)[:15000]
    if "osea" in sample.casefold():
        return "OSEA"
    matches: list[tuple[int, str]] = []
    for acronym in KNOWN_UNION_ACRONYMS:
        flags = re.IGNORECASE if acronym == "Teamsters" else 0
        match = re.search(rf"\b{re.escape(acronym)}\b", sample, flags)
        if match:
            matches.append((match.start(), acronym.upper() if acronym != "Teamsters" else acronym))
    if matches:
        return min(matches)[1]
    phrase = re.search(
        r"\b([A-Z][A-Za-z0-9&.' -]{2,70}(?:Education Association|Employees Association|Association of Teachers|Classified United|Federation of School Professionals|Service Employees International Union))\b",
        sample,
    )
    return _collapse_ws(phrase.group(1)) if phrase else None


def agreement_status(effective_date: str | None, expiration_date: str | None, as_of: str) -> str:
    if effective_date and effective_date > as_of:
        return "future"
    if expiration_date:
        return "current" if expiration_date >= as_of else "expired"
    return "unknown"


def _normalize_llm_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    choice = str(value or "").strip().casefold().replace(" ", "_")
    return choice if choice in allowed else default


def analyze_candidate(
    district: dict[str, Any],
    candidate: Candidate,
    settings: ContractDiscoverySettings,
) -> dict[str, Any] | None:
    search_settings = settings.search_settings()
    session = make_session(search_settings)
    content = b""
    content_type = ""
    status_code: int | None = None
    extracted_text = ""
    final_url = candidate.url
    extraction_note = ""
    try:
        response, content = fetch_limited(session, candidate.url, search_settings)
        status_code = response.status_code
        if status_code >= 400:
            extraction_note = f"HTTP {status_code}"
        else:
            final_url = canonical_url(response.url)
            header_type = response.headers.get("Content-Type", "").casefold()
            path = urlparse(final_url).path.casefold()
            if "pdf" in header_type or path.endswith(".pdf"):
                content_type = "application/pdf"
                try:
                    extracted_text = _parse_contract_pdf(content)
                except Exception as exc:
                    extraction_note = f"PDF text extraction failed: {exc}"
            elif "html" in header_type or "text/plain" in header_type or not header_type:
                content_type = "text/html"
                _title, _headings, extracted_text, _links = parse_html(content, final_url)
            else:
                content_type = header_type.split(";", 1)[0] or "application/octet-stream"
                extraction_note = "Document type stored without text extraction."
    except Exception as exc:
        extraction_note = f"Fetch failed: {exc}"

    primary_evidence = _collapse_ws(f"{candidate.title} {final_url}")
    document_evidence = _collapse_ws(f"{primary_evidence} {extracted_text[:30000]}")
    primary_document_type = classify_document_type(primary_evidence, content_type)
    document_type = primary_document_type
    if document_type == "other" or (
        document_type == "landing_page"
        and any(term in primary_evidence.casefold() for term in ("agreement", "contract", " cba"))
    ):
        document_type = classify_document_type(document_evidence, content_type)
    if content_type == "text/html" and primary_document_type == "landing_page" and document_type == "landing_page":
        return None
    if document_type == "salary_schedule" and not settings.include_salary_schedules:
        return None
    unit_type, unit_confidence = classify_bargaining_unit(primary_evidence)
    primary_unit_unknown = unit_type == "unknown"
    if primary_unit_unknown:
        unit_type, unit_confidence = classify_bargaining_unit(document_evidence)
    if unit_type == "unknown":
        context_hints = hinted_bargaining_units(candidate.parent_context)
        if len(context_hints) == 1:
            unit_type = next(iter(context_hints))
            unit_confidence = 0.65
    if content_type == "text/html" and primary_unit_unknown:
        acronym_hits = {
            acronym
            for acronym in KNOWN_UNION_ACRONYMS
            if re.search(rf"\b{re.escape(acronym)}\b", extracted_text, 0 if acronym != "Teamsters" else re.IGNORECASE)
        }
        if len(acronym_hits) > 1 or len(hinted_bargaining_units(extracted_text)) > 1:
            return None
    if document_type == "salary_schedule" and primary_unit_unknown:
        return None
    union_name = extract_union_name(primary_evidence) or extract_union_name(extracted_text)
    context_hints = hinted_bargaining_units(candidate.parent_context)
    if not union_name and (not context_hints or context_hints == {unit_type}):
        union_name = extract_union_name(candidate.parent_context)
    effective_date, expiration_date, date_confidence = extract_agreement_dates(primary_evidence)
    if not effective_date and not expiration_date:
        effective_date, expiration_date, date_confidence = extract_agreement_dates(document_evidence)
    if not effective_date and not expiration_date and (not context_hints or context_hints == {unit_type}):
        effective_date, expiration_date, date_confidence = extract_agreement_dates(candidate.parent_context)
    confidence = max(0.35, min(0.98, (candidate.score / 12) * 0.45 + unit_confidence * 0.3 + date_confidence * 0.25))
    llm_analysis: dict[str, Any] | None = None
    if settings.use_llm and local_llm_is_configured():
        llm_analysis = analyze_contract_candidate(
            district_name=district.get("agency_name") or "",
            title=candidate.title,
            url=final_url,
            parent_context=candidate.parent_context,
            text_excerpt=extracted_text,
        )
        if llm_analysis and llm_analysis.get("is_labor_agreement_document") is False:
            return None
        if llm_analysis:
            unit_type = _normalize_llm_choice(llm_analysis.get("bargaining_unit_type"), UNIT_TYPES, unit_type)
            document_type = _normalize_llm_choice(llm_analysis.get("document_type"), DOCUMENT_TYPES, document_type)
            union_name = _collapse_ws(llm_analysis.get("union_name") or union_name or "") or None
            effective_date = str(llm_analysis.get("effective_date") or effective_date or "") or None
            expiration_date = str(llm_analysis.get("expiration_date") or expiration_date or "") or None
            try:
                confidence = max(confidence, min(1.0, float(llm_analysis.get("confidence") or 0)))
            except (TypeError, ValueError):
                pass

    as_of = date.today().isoformat()
    snippet_source = extracted_text or candidate.parent_context or extraction_note
    return {
        "document_type": document_type,
        "title": candidate.title or final_url,
        "url": final_url,
        "parent_page_url": candidate.parent_page_url,
        "content_type": content_type,
        "status_code": status_code,
        "discovery_source": candidate.discovery_source,
        "effective_date": effective_date,
        "expiration_date": expiration_date,
        "agreement_status": agreement_status(effective_date, expiration_date, as_of),
        "confidence": round(confidence * 100, 1),
        "snippet": _collapse_ws(snippet_source)[:700],
        "content_sha256": hashlib.sha256(content).hexdigest() if content else None,
        "file_size_bytes": len(content) if content else None,
        "local_file_path": None,
        "extracted_text_path": None,
        "archived_at": None,
        "llm_analysis_json": json_dumps(llm_analysis) if llm_analysis else None,
        "bargaining_unit_type": unit_type,
        "bargaining_unit_name": _collapse_ws((llm_analysis or {}).get("bargaining_unit_name") or "") or UNIT_LABELS[unit_type],
        "union_name": union_name,
        "extraction_note": extraction_note,
        "_archive_content": content,
        "_extracted_text": extracted_text,
    }


def _package_key(document: dict[str, Any]) -> str:
    unit_type = document["bargaining_unit_type"]
    union_name = re.sub(r"[^a-z0-9]+", "", str(document.get("union_name") or "").casefold())
    if union_name:
        return f"{unit_type}:{union_name}"
    source = canonical_url(document.get("parent_page_url") or document["url"])
    return f"{unit_type}:source:{source}"


def assemble_packages(district: dict[str, Any], documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    base_documents = [document for document in documents if document["document_type"] == "base_agreement"]
    supporting_documents = [document for document in documents if document["document_type"] != "base_agreement"]
    for document in base_documents:
        grouped.setdefault(_package_key(document), []).append(document)
    for document in supporting_documents:
        key = _package_key(document)
        if key not in grouped:
            matching_keys = [
                existing_key
                for existing_key, existing_documents in grouped.items()
                if existing_documents[0]["bargaining_unit_type"] == document["bargaining_unit_type"]
            ]
            if len(matching_keys) == 1:
                key = matching_keys[0]
        grouped.setdefault(key, []).append(document)
    as_of = date.today().isoformat()
    packages: list[dict[str, Any]] = []
    for group_documents in grouped.values():
        if all(document["document_type"] == "salary_schedule" for document in group_documents):
            continue
        selected_documents: list[dict[str, Any]] = []
        for document_type in DOCUMENT_TYPES:
            typed = [document for document in group_documents if document["document_type"] == document_type]
            if not typed:
                continue
            if document_type in {"base_agreement", "extension", "salary_schedule", "tentative_agreement"}:
                dated = [document for document in typed if document.get("expiration_date")]
                if dated:
                    latest_end = max(document["expiration_date"] for document in dated)
                    selected_documents.extend(document for document in dated if document["expiration_date"] == latest_end)
                    selected_documents.extend(document for document in typed if not document.get("expiration_date"))
                else:
                    selected_documents.extend(typed)
            elif document_type in {"mou", "amendment"}:
                selected_documents.extend(
                    document
                    for document in typed
                    if not document.get("expiration_date") or document["expiration_date"] >= as_of
                )
            else:
                selected_documents.extend(typed)
        group_documents = selected_documents
        base_documents = [doc for doc in group_documents if doc["document_type"] == "base_agreement"]
        date_documents = base_documents or group_documents
        starts = sorted(doc["effective_date"] for doc in date_documents if doc.get("effective_date"))
        ends = sorted(doc["expiration_date"] for doc in group_documents if doc.get("expiration_date"))
        effective_date = starts[0] if starts else None
        expiration_date = ends[-1] if ends else None
        unit_type = group_documents[0]["bargaining_unit_type"]
        union_name = next((doc.get("union_name") for doc in group_documents if doc.get("union_name")), None)
        source_counts = Counter(doc.get("parent_page_url") for doc in group_documents if doc.get("parent_page_url"))
        source_page_url = source_counts.most_common(1)[0][0] if source_counts else group_documents[0]["url"]
        status = agreement_status(effective_date, expiration_date, as_of)
        review_status = "needs_review" if unit_type == "unknown" or status in {"unknown", "expired"} else "unreviewed"
        notes = "; ".join(dict.fromkeys(doc["extraction_note"] for doc in group_documents if doc.get("extraction_note")))[:1000]
        packages.append(
            {
                "district_id": district["id"],
                "district_name": district.get("agency_name"),
                "state": district.get("state"),
                "website": district.get("website_normalized") or district.get("website"),
                "bargaining_unit_type": unit_type,
                "bargaining_unit_name": group_documents[0].get("bargaining_unit_name") or UNIT_LABELS[unit_type],
                "union_name": union_name,
                "effective_date": effective_date,
                "expiration_date": expiration_date,
                "agreement_status": status,
                "review_status": review_status,
                "confidence": max(float(doc.get("confidence") or 0) for doc in group_documents),
                "source_page_url": source_page_url,
                "current_as_of": as_of,
                "notes": notes,
                "documents": sorted(group_documents, key=lambda doc: (doc["document_type"] != "base_agreement", doc["title"])),
            }
        )
    return sorted(packages, key=lambda item: (item["bargaining_unit_type"], item.get("union_name") or ""))


def discover_district_contracts(
    district: dict[str, Any],
    settings: ContractDiscoverySettings | None = None,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    debug_logger: RunDebugLogger | None = None,
) -> list[dict[str, Any]]:
    settings = settings or ContractDiscoverySettings()
    candidates = discover_contract_candidates(
        district,
        settings,
        cancel_requested=cancel_requested,
        debug_logger=debug_logger,
    )
    documents: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_documents: set[tuple[str, str, str | None, str | None]] = set()
    for candidate in candidates:
        if cancel_requested and cancel_requested():
            break
        document = analyze_candidate(district, candidate, settings)
        if not document or document["url"] in seen_urls:
            continue
        fingerprint = (
            document["document_type"],
            _collapse_ws(document["title"]).casefold(),
            document.get("effective_date"),
            document.get("expiration_date"),
        )
        if fingerprint in seen_documents:
            continue
        seen_urls.add(document["url"])
        seen_documents.add(fingerprint)
        if document["document_type"] == "other" and document["confidence"] < 45:
            continue
        document.update(
            archive_document_files(
                district,
                document,
                document.pop("_archive_content", b""),
                document.pop("_extracted_text", ""),
                settings,
            )
        )
        documents.append(document)
    packages = assemble_packages(district, documents)
    debug_log(
        debug_logger,
        "contract_district_finish",
        district=district.get("agency_name"),
        packages=len(packages),
        documents=len(documents),
    )
    return packages


def create_contract_discovery_run(
    *,
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    max_districts: int = 20,
    max_pages_per_district: int = 20,
    max_workers: int = CONTRACT_DISCOVERY_WORKERS,
    use_llm: bool = False,
    include_salary_schedules: bool = True,
    archive_documents: bool = True,
    store_extracted_text: bool = True,
    rescan_after_days: int = CONTRACT_RESCAN_DAYS,
    recheck_expired: bool = True,
    force_rescan: bool = False,
    db_path: Path | str | None = None,
) -> int:
    states = states or []
    agency_types = agency_types or []
    selected, matched, skipped_recent = select_contract_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        max_districts,
        db_path,
        rescan_after_days=rescan_after_days,
        recheck_expired=recheck_expired,
        force_rescan=force_rescan,
    )
    planned = len(selected)
    now = utc_now_iso()
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO contract_discovery_runs (
                states_json, agency_types_json, min_enrollment, max_enrollment,
                max_districts, max_pages_per_district, max_workers, use_llm,
                include_salary_schedules, archive_documents, store_extracted_text,
                rescan_after_days, recheck_expired, force_rescan, status,
                districts_matched, districts_planned, districts_skipped_recent,
                started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                json_dumps(states),
                json_dumps(agency_types),
                min_enrollment,
                max_enrollment,
                max_districts,
                max_pages_per_district,
                max_workers,
                1 if use_llm else 0,
                1 if include_salary_schedules else 0,
                1 if archive_documents else 0,
                1 if store_extracted_text else 0,
                max(0, int(rescan_after_days)),
                1 if recheck_expired else 0,
                1 if force_rescan else 0,
                matched,
                planned,
                skipped_recent,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
    return run_id


def _district_is_due_for_contract_scan(
    district: dict[str, Any],
    *,
    rescan_after_days: int,
    recheck_expired: bool,
    force_rescan: bool,
) -> bool:
    if force_rescan:
        return True
    if recheck_expired and bool(district.get("has_unreplaced_expired_contract")):
        return True
    successful_at = str(district.get("last_successful_scan_at") or "").strip()
    if not successful_at:
        return True
    try:
        successful_date = datetime.fromisoformat(successful_at.replace("Z", "+00:00")).date()
    except ValueError:
        return True
    return (date.today() - successful_date).days >= max(0, int(rescan_after_days))


def select_contract_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    limit: int,
    db_path: Path | str | None,
    *,
    rescan_after_days: int = CONTRACT_RESCAN_DAYS,
    recheck_expired: bool = True,
    force_rescan: bool = False,
) -> tuple[list[dict[str, Any]], int, int]:
    where_sql, params = build_district_filter_sql(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        only_searchable=False,
    )
    searchable_clause = "d.has_searchable_website = 1"
    where_sql = where_sql.replace("state IN", "d.state IN").replace(
        "agency_type IN", "d.agency_type IN"
    ).replace("total_enrollment_excludes_ae", "d.total_enrollment_excludes_ae")
    where_sql = f"{where_sql} AND {searchable_clause}" if where_sql else f" WHERE {searchable_clause}"
    today = date.today().isoformat()
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT d.*, s.last_successful_scan_at, s.last_status AS contract_scan_status,
                   EXISTS (
                       SELECT 1
                       FROM district_contract_packages expired
                       WHERE expired.district_id = d.id
                         AND expired.review_status != 'rejected'
                         AND expired.expiration_date IS NOT NULL
                         AND expired.expiration_date < ?
                         AND NOT EXISTS (
                             SELECT 1
                             FROM district_contract_packages successor
                             WHERE successor.district_id = expired.district_id
                               AND successor.bargaining_unit_type = expired.bargaining_unit_type
                               AND successor.review_status != 'rejected'
                               AND successor.expiration_date > ?
                         )
                   ) AS has_unreplaced_expired_contract
            FROM districts d
            LEFT JOIN district_contract_scan_status s ON s.district_id = d.id
            {where_sql}
            ORDER BY d.total_enrollment_excludes_ae DESC, d.agency_name COLLATE NOCASE
            """,
            [today, today, *params],
        ).fetchall()
    districts = [dict(row) for row in rows]
    eligible = [
        district
        for district in districts
        if _district_is_due_for_contract_scan(
            district,
            rescan_after_days=rescan_after_days,
            recheck_expired=recheck_expired,
            force_rescan=force_rescan,
        )
    ]
    return eligible[: max(0, int(limit))], len(districts), len(districts) - len(eligible)


def list_contract_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    limit: int,
    db_path: Path | str | None,
    *,
    rescan_after_days: int = CONTRACT_RESCAN_DAYS,
    recheck_expired: bool = True,
    force_rescan: bool = False,
) -> list[dict[str, Any]]:
    return select_contract_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        limit,
        db_path,
        rescan_after_days=rescan_after_days,
        recheck_expired=recheck_expired,
        force_rescan=force_rescan,
    )[0]


def _cancel_requested(run_id: int, db_path: Path | str | None) -> bool:
    with connect_db(db_path) as conn:
        row = conn.execute("SELECT cancel_requested FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def _record_contract_scan_result(
    district_id: int,
    run_id: int,
    *,
    success: bool,
    packages_found: int = 0,
    documents_found: int = 0,
    error: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    now = utc_now_iso()
    with connect_db(db_path) as conn:
        if success:
            conn.execute(
                """
                INSERT INTO district_contract_scan_status (
                    district_id, last_run_id, last_attempted_at,
                    last_successful_scan_at, last_status, last_error,
                    packages_found, documents_found, updated_at
                ) VALUES (?, ?, ?, ?, 'success', NULL, ?, ?, ?)
                ON CONFLICT(district_id) DO UPDATE SET
                    last_run_id = excluded.last_run_id,
                    last_attempted_at = excluded.last_attempted_at,
                    last_successful_scan_at = excluded.last_successful_scan_at,
                    last_status = 'success',
                    last_error = NULL,
                    packages_found = excluded.packages_found,
                    documents_found = excluded.documents_found,
                    updated_at = excluded.updated_at
                """,
                (district_id, run_id, now, now, packages_found, documents_found, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO district_contract_scan_status (
                    district_id, last_run_id, last_attempted_at,
                    last_status, last_error, updated_at
                ) VALUES (?, ?, ?, 'failed', ?, ?)
                ON CONFLICT(district_id) DO UPDATE SET
                    last_run_id = excluded.last_run_id,
                    last_attempted_at = excluded.last_attempted_at,
                    last_status = 'failed',
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (district_id, run_id, now, str(error or "Unknown scan failure")[:2000], now),
            )
        conn.commit()


def _store_packages(run_id: int, packages: list[dict[str, Any]], db_path: Path | str | None) -> int:
    now = utc_now_iso()
    document_count = 0
    with connect_db(db_path) as conn:
        for package in packages:
            cursor = conn.execute(
                """
                INSERT INTO district_contract_packages (
                    discovery_run_id, district_id, district_name, state, website,
                    bargaining_unit_type, bargaining_unit_name, union_name,
                    effective_date, expiration_date, agreement_status, review_status,
                    confidence, source_page_url, current_as_of, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    package["district_id"],
                    package["district_name"],
                    package["state"],
                    package["website"],
                    package["bargaining_unit_type"],
                    package["bargaining_unit_name"],
                    package["union_name"],
                    package["effective_date"],
                    package["expiration_date"],
                    package["agreement_status"],
                    package["review_status"],
                    package["confidence"],
                    package["source_page_url"],
                    package["current_as_of"],
                    package["notes"],
                    now,
                    now,
                ),
            )
            package_id = int(cursor.lastrowid)
            for document in package["documents"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO district_contract_documents (
                        package_id, discovery_run_id, district_id, document_type,
                        title, url, parent_page_url, content_type, status_code,
                        discovery_source, effective_date, expiration_date,
                        agreement_status, confidence, snippet, content_sha256,
                        file_size_bytes, local_file_path, extracted_text_path,
                        archived_at, llm_analysis_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package_id,
                        run_id,
                        package["district_id"],
                        document["document_type"],
                        document["title"],
                        document["url"],
                        document["parent_page_url"],
                        document["content_type"],
                        document["status_code"],
                        document["discovery_source"],
                        document["effective_date"],
                        document["expiration_date"],
                        document["agreement_status"],
                        document["confidence"],
                        document["snippet"],
                        document["content_sha256"],
                        document["file_size_bytes"],
                        document["local_file_path"],
                        document["extracted_text_path"],
                        document["archived_at"],
                        document["llm_analysis_json"],
                        now,
                    ),
                )
                document_count += 1
        conn.commit()
    return document_count


def execute_contract_discovery_run(
    run_id: int,
    *,
    db_path: Path | str | None = None,
    settings: ContractDiscoverySettings | None = None,
) -> None:
    with connect_db(db_path) as conn:
        run = conn.execute("SELECT * FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Contract discovery run not found: {run_id}")
    states = json.loads(run["states_json"] or "[]")
    agency_types = json.loads(run["agency_types_json"] or "[]")
    districts, matched, skipped_recent = select_contract_districts(
        states,
        agency_types,
        run["min_enrollment"],
        run["max_enrollment"],
        int(run["max_districts"]),
        db_path,
        rescan_after_days=(
            int(run["rescan_after_days"])
            if run["rescan_after_days"] is not None
            else CONTRACT_RESCAN_DAYS
        ),
        recheck_expired=bool(run["recheck_expired"]),
        force_rescan=bool(run["force_rescan"]),
    )
    settings = settings or ContractDiscoverySettings(
        max_pages_per_district=int(run["max_pages_per_district"] or 20),
        include_salary_schedules=bool(run["include_salary_schedules"]),
        use_llm=bool(run["use_llm"]),
        archive_documents=bool(run["archive_documents"]),
        store_extracted_text=bool(run["store_extracted_text"]),
    )
    max_workers = max(1, min(8, int(run["max_workers"] or CONTRACT_DISCOVERY_WORKERS)))
    debug_path = CONTRACT_DISCOVERY_RUN_LOGS_DIR / f"contract-discovery-run-{run_id}.log"
    debug_logger = RunDebugLogger(debug_path)
    processed = failed = package_count = document_count = 0
    counter_lock = threading.Lock()

    def update_progress() -> None:
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE contract_discovery_runs
                SET districts_processed = ?, districts_failed = ?,
                    packages_found = ?, documents_found = ?
                WHERE id = ?
                """,
                (processed, failed, package_count, document_count, run_id),
            )
            conn.commit()

    try:
        with connect_db(db_path) as conn:
            conn.execute("DELETE FROM district_contract_packages WHERE discovery_run_id = ?", (run_id,))
            conn.execute(
                """
                UPDATE contract_discovery_runs
                SET status = 'running', districts_planned = ?, districts_processed = 0,
                    districts_failed = 0, packages_found = 0, documents_found = 0,
                    districts_matched = ?, districts_skipped_recent = ?,
                    debug_log_path = ?, finished_at = NULL, error_message = NULL
                WHERE id = ?
                """,
                (len(districts), matched, skipped_recent, str(debug_path), run_id),
            )
            conn.commit()
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ContractDiscovery") as executor:
            future_to_district = {
                executor.submit(
                    discover_district_contracts,
                    district,
                    settings,
                    cancel_requested=lambda: _cancel_requested(run_id, db_path),
                    debug_logger=debug_logger,
                ): district
                for district in districts
                if not _cancel_requested(run_id, db_path)
            }
            for future in as_completed(future_to_district):
                district = future_to_district[future]
                if _cancel_requested(run_id, db_path):
                    for pending in future_to_district:
                        pending.cancel()
                    break
                try:
                    packages = future.result()
                    stored_documents = _store_packages(run_id, packages, db_path)
                    _record_contract_scan_result(
                        int(district["id"]),
                        run_id,
                        success=True,
                        packages_found=len(packages),
                        documents_found=stored_documents,
                        db_path=db_path,
                    )
                    with counter_lock:
                        package_count += len(packages)
                        document_count += stored_documents
                except Exception as exc:
                    LOGGER.exception("Contract discovery failed for %s: %s", district.get("agency_name"), exc)
                    debug_log(debug_logger, "contract_district_error", district=district.get("agency_name"), error=str(exc))
                    _record_contract_scan_result(
                        int(district["id"]),
                        run_id,
                        success=False,
                        error=str(exc),
                        db_path=db_path,
                    )
                    with counter_lock:
                        failed += 1
                finally:
                    with counter_lock:
                        processed += 1
                        update_progress()
        cancelled = _cancel_requested(run_id, db_path)
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE contract_discovery_runs
                SET status = ?, districts_processed = ?, districts_failed = ?,
                    packages_found = ?, documents_found = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    "cancelled" if cancelled else "completed",
                    processed,
                    failed,
                    package_count,
                    document_count,
                    utc_now_iso(),
                    "Cancelled by user." if cancelled else None,
                    run_id,
                ),
            )
            conn.commit()
    except Exception as exc:
        with connect_db(db_path) as conn:
            conn.execute(
                "UPDATE contract_discovery_runs SET status = 'failed', finished_at = ?, error_message = ? WHERE id = ?",
                (utc_now_iso(), str(exc), run_id),
            )
            conn.commit()
        raise


def export_contract_discovery_csv(run_id: int, db_path: Path | str | None = None) -> str:
    output = io.StringIO()
    fields = [
        "run_id",
        "district_name",
        "state",
        "bargaining_unit_type",
        "bargaining_unit_name",
        "union_name",
        "package_status",
        "package_effective_date",
        "package_expiration_date",
        "review_status",
        "document_type",
        "document_title",
        "document_url",
        "source_page_url",
        "document_effective_date",
        "document_expiration_date",
        "content_sha256",
        "file_size_bytes",
        "local_file_path",
        "extracted_text_path",
        "archived_at",
        "confidence",
        "current_as_of",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT p.*, d.document_type, d.title AS document_title, d.url AS document_url,
                   d.effective_date AS document_effective_date,
                   d.expiration_date AS document_expiration_date,
                   d.confidence AS document_confidence, d.content_sha256,
                   d.file_size_bytes, d.local_file_path, d.extracted_text_path,
                   d.archived_at
            FROM district_contract_packages p
            JOIN district_contract_documents d ON d.package_id = p.id
            WHERE p.discovery_run_id = ?
            ORDER BY p.district_name, p.bargaining_unit_type, p.union_name, d.document_type, d.title
            """,
            (run_id,),
        ).fetchall()
    for row in rows:
        writer.writerow(
            {
                "run_id": run_id,
                "district_name": row["district_name"],
                "state": row["state"],
                "bargaining_unit_type": row["bargaining_unit_type"],
                "bargaining_unit_name": row["bargaining_unit_name"],
                "union_name": row["union_name"],
                "package_status": row["agreement_status"],
                "package_effective_date": row["effective_date"],
                "package_expiration_date": row["expiration_date"],
                "review_status": row["review_status"],
                "document_type": row["document_type"],
                "document_title": row["document_title"],
                "document_url": row["document_url"],
                "source_page_url": row["source_page_url"],
                "document_effective_date": row["document_effective_date"],
                "document_expiration_date": row["document_expiration_date"],
                "content_sha256": row["content_sha256"],
                "file_size_bytes": row["file_size_bytes"],
                "local_file_path": row["local_file_path"],
                "extracted_text_path": row["extracted_text_path"],
                "archived_at": row["archived_at"],
                "confidence": row["document_confidence"],
                "current_as_of": row["current_as_of"],
            }
        )
    return output.getvalue()
