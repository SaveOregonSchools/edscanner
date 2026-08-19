from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


LOGGER = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
IMPORTS_DIR = APP_ROOT / "imports"
EXPORTS_DIR = APP_ROOT / "exports"
DOWNLOADS_DIR = APP_ROOT / "downloads"
CONTRACT_ARCHIVE_DIR = DOWNLOADS_DIR / "contracts"
BOARD_DOCUMENTS_DIR = DATA_DIR / "board_documents"
BOARD_SNAPSHOTS_DIR = DATA_DIR / "board_snapshots"
LOGS_DIR = APP_ROOT / "logs"
SEARCH_RUN_LOGS_DIR = LOGS_DIR / "search_runs"
PROFILE_DISCOVERY_RUN_LOGS_DIR = LOGS_DIR / "profile_discovery_runs"
CONTRACT_DISCOVERY_RUN_LOGS_DIR = LOGS_DIR / "contract_discovery_runs"
BOARD_DISCOVERY_RUN_LOGS_DIR = LOGS_DIR / "board_discovery_runs"
BOARD_SYNC_RUN_LOGS_DIR = LOGS_DIR / "board_sync_runs"
ENV_PATH = APP_ROOT / ".env"


def parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = value.replace('\\"', '"').replace("\\\\", "\\")
    return key, value


def read_env_file(path: Path | str | None = None) -> dict[str, str]:
    env_path = Path(path or ENV_PATH)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed:
            key, value = parsed
            values[key] = value
    return values


def load_local_env(path: Path | str | None = None) -> None:
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


load_local_env()

DEFAULT_DB_PATH = DATA_DIR / "edscanner.db"
DB_PATH = Path(os.getenv("EDSCANNER_DB_PATH", DEFAULT_DB_PATH)).expanduser().resolve()
LOG_PATH = LOGS_DIR / "edscanner.log"

APP_VERSION = "0.1"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
USER_AGENT = os.getenv(
    "EDSCANNER_USER_AGENT",
    DEFAULT_BROWSER_USER_AGENT,
)


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


MAX_PAGES_PER_DISTRICT = _env_int("EDSCANNER_MAX_PAGES_PER_DISTRICT", 100, minimum=1)
MAX_RESULTS_PER_DISTRICT = _env_int("EDSCANNER_MAX_RESULTS_PER_DISTRICT", 5, minimum=1)
REQUEST_TIMEOUT_SECONDS = _env_float("EDSCANNER_REQUEST_TIMEOUT", 15.0, minimum=1.0)
REQUEST_DELAY_SECONDS = _env_float("EDSCANNER_REQUEST_DELAY", 0.75, minimum=0.0)
MAX_PDF_SIZE_BYTES = _env_int("EDSCANNER_MAX_PDF_SIZE_MB", 10, minimum=1) * 1024 * 1024
MAX_HTML_SIZE_BYTES = _env_int("EDSCANNER_MAX_HTML_SIZE_MB", 5, minimum=1) * 1024 * 1024
MAX_TOTAL_DISTRICTS_PER_RUN = _env_int("EDSCANNER_MAX_TOTAL_DISTRICTS_PER_RUN", 25, minimum=1)
PROFILE_DISCOVERY_WORKERS = _env_int("EDSCANNER_PROFILE_DISCOVERY_WORKERS", 3, minimum=1)
CONTRACT_DISCOVERY_WORKERS = _env_int("EDSCANNER_CONTRACT_DISCOVERY_WORKERS", 3, minimum=1)
CONTRACT_RESCAN_DAYS = _env_int("EDSCANNER_CONTRACT_RESCAN_DAYS", 180, minimum=0)
CONTRACT_DISTRICT_DIR_NAME_MAX = _env_int("EDSCANNER_CONTRACT_DISTRICT_DIR_NAME_MAX", 50, minimum=20)
SEARCH_RUN_WORKERS = _env_int("EDSCANNER_SEARCH_RUN_WORKERS", 4, minimum=1)
BOARD_WORKERS = _env_int("EDSCANNER_BOARD_WORKERS", 4, minimum=1)
BOARD_PER_HOST_WORKERS = _env_int("EDSCANNER_BOARD_PER_HOST_WORKERS", 2, minimum=1)
BOARD_MAX_DOCUMENT_SIZE_BYTES = _env_int("EDSCANNER_BOARD_MAX_DOCUMENT_MB", 25, minimum=1) * 1024 * 1024
BOARD_MAX_DOCUMENTS_PER_MEETING = _env_int("EDSCANNER_BOARD_MAX_DOCUMENTS_PER_MEETING", 100, minimum=1)
BOARD_MAX_MEETINGS_PER_SOURCE = _env_int("EDSCANNER_BOARD_MAX_MEETINGS_PER_SOURCE", 250, minimum=1)
BOARD_REQUEST_DELAY_SECONDS = _env_float("EDSCANNER_BOARD_REQUEST_DELAY", 0.75, minimum=0.0)
BOARD_HTTP_MAX_REDIRECTS = _env_int("EDSCANNER_BOARD_HTTP_MAX_REDIRECTS", 5, minimum=0)
BOARD_HTTP_CACHE_MAX_ENTRIES = _env_int(
    "EDSCANNER_BOARD_HTTP_CACHE_MAX_ENTRIES", 128, minimum=0
)
BOARD_HTTP_CACHE_MAX_BYTES = (
    _env_int("EDSCANNER_BOARD_HTTP_CACHE_MAX_MB", 32, minimum=0) * 1024 * 1024
)
BOARD_ALLOW_PRIVATE_NETWORKS = _env_bool(
    "EDSCANNER_BOARD_ALLOW_PRIVATE_NETWORKS", False
)
BOARD_IPV4_ONLY = _env_bool("EDSCANNER_BOARD_IPV4_ONLY", True)
BOARD_ALLOW_INSECURE_SSL_FALLBACK = _env_bool(
    "EDSCANNER_BOARD_ALLOW_INSECURE_SSL_FALLBACK", False
)
BOARD_INSECURE_SSL_FALLBACK_HOSTS = tuple(
    host.strip().casefold()
    for host in os.getenv("EDSCANNER_BOARD_INSECURE_SSL_HOSTS", "").split(",")
    if host.strip()
)
BOARD_RECENT_RECHECK_DAYS = _env_int("EDSCANNER_BOARD_RECENT_RECHECK_DAYS", 60, minimum=1)
BOARD_INCOMPLETE_RECHECK_DAYS = _env_int("EDSCANNER_BOARD_INCOMPLETE_RECHECK_DAYS", 14, minimum=1)
BOARD_OLD_RECHECK_DAYS = _env_int("EDSCANNER_BOARD_OLD_RECHECK_DAYS", 90, minimum=1)
VERIFY_SSL = _env_bool("EDSCANNER_VERIFY_SSL", True)
RESPECT_ROBOTS = _env_bool("EDSCANNER_RESPECT_ROBOTS", False)
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
# Native Ollama configuration. Endpoints are stored as a JSON array in priority
# order; the older OpenAI-compatible names remain readable for compatibility.
OLLAMA_ENDPOINTS_ENV = "EDSCANNER_OLLAMA_ENDPOINTS"
OLLAMA_MODEL_ENV = "EDSCANNER_OLLAMA_MODEL"
LLM_BASE_URL_ENV = "EDSCANNER_LLM_BASE_URL"
LLM_MODEL_ENV = "EDSCANNER_LLM_MODEL"
LLM_API_KEY_ENV = "EDSCANNER_LLM_API_KEY"


def quote_env_value(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_local_setting(name: str, default: str = "") -> str:
    return os.getenv(name) or read_env_file().get(name, default)


def set_local_setting(name: str, value: str) -> None:
    ensure_directories()
    value = str(value or "").strip()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out: list[str] = []
    found = False
    for line in lines:
        parsed = parse_env_line(line)
        if parsed and parsed[0] == name:
            found = True
            if value:
                out.append(f"{name}={quote_env_value(value)}")
        else:
            out.append(line)
    if value and not found:
        out.append(f"{name}={quote_env_value(value)}")
    text = "\n".join(out).strip()
    ENV_PATH.write_text((text + "\n") if text else "", encoding="utf-8")
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def has_brave_search_api_key() -> bool:
    return bool(get_local_setting(BRAVE_SEARCH_API_KEY_ENV).strip())


STATE_NAME_TO_ABBR = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


INVALID_WEBSITE_VALUES = {
    "",
    "+",
    "-",
    "--",
    "na",
    "n/a",
    "none",
    "null",
    "nan",
    "not available",
    "not reported",
    "unavailable",
    "missing",
    "no website",
    "†",
    "‡",
    "â€ ",
    "â€¡",
    "–",
    "—",
}


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        DOWNLOADS_DIR,
        CONTRACT_ARCHIVE_DIR,
        BOARD_DOCUMENTS_DIR,
        BOARD_SNAPSHOTS_DIR,
        IMPORTS_DIR,
        EXPORTS_DIR,
        LOGS_DIR,
        SEARCH_RUN_LOGS_DIR,
        PROFILE_DISCOVERY_RUN_LOGS_DIR,
        CONTRACT_DISCOVERY_RUN_LOGS_DIR,
        BOARD_DISCOVERY_RUN_LOGS_DIR,
        BOARD_SYNC_RUN_LOGS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def configure_logging(debug: bool = False) -> None:
    ensure_directories()
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_db_path() -> str:
    return str(DB_PATH)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def connect_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    ensure_directories()
    path = Path(db_path or DB_PATH).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    ensure_directories()
    with connect_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS districts (
                id INTEGER PRIMARY KEY,
                source_file TEXT,
                source_row_number INTEGER,
                agency_id_nces TEXT,
                agency_name TEXT,
                state TEXT,
                agency_type TEXT,
                total_enrollment_excludes_ae INTEGER,
                website TEXT,
                website_normalized TEXT,
                has_searchable_website INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_districts_agency_id_nces
                ON districts(agency_id_nces)
                WHERE agency_id_nces IS NOT NULL AND agency_id_nces != '';

            CREATE INDEX IF NOT EXISTS idx_districts_state ON districts(state);
            CREATE INDEX IF NOT EXISTS idx_districts_agency_type ON districts(agency_type);
            CREATE INDEX IF NOT EXISTS idx_districts_enrollment
                ON districts(total_enrollment_excludes_ae);
            CREATE INDEX IF NOT EXISTS idx_districts_searchable
                ON districts(has_searchable_website);

            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY,
                query_text TEXT NOT NULL,
                states_json TEXT,
                agency_types_json TEXT,
                min_enrollment INTEGER,
                max_enrollment INTEGER,
                max_districts INTEGER,
                max_pages_per_district INTEGER,
                search_method TEXT NOT NULL DEFAULT 'crawler',
                search_provider TEXT,
                api_results_per_district INTEGER,
                follow_depth INTEGER NOT NULL DEFAULT 0,
                max_workers INTEGER,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                debug_logging INTEGER NOT NULL DEFAULT 0,
                debug_log_path TEXT,
                status TEXT NOT NULL,
                districts_matched INTEGER NOT NULL DEFAULT 0,
                districts_searched INTEGER NOT NULL DEFAULT 0,
                districts_failed INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY,
                search_run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                district_name TEXT,
                state TEXT,
                agency_type TEXT,
                total_enrollment_excludes_ae INTEGER,
                website TEXT,
                result_rank INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                content_type TEXT,
                status_code INTEGER,
                search_source TEXT,
                score REAL NOT NULL DEFAULT 0,
                snippet TEXT,
                matched_terms_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_search_results_run
                ON search_results(search_run_id);
            CREATE INDEX IF NOT EXISTS idx_search_results_district
                ON search_results(district_id);

            CREATE TABLE IF NOT EXISTS district_search_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                website_normalized TEXT,
                profile_status TEXT NOT NULL,
                profile_type TEXT,
                provider_guess TEXT,
                search_url_template TEXT,
                search_method TEXT,
                query_param TEXT,
                extra_params_json TEXT,
                result_selector TEXT,
                result_link_selector TEXT,
                result_title_selector TEXT,
                result_snippet_selector TEXT,
                same_domain_only INTEGER NOT NULL DEFAULT 1,
                requires_javascript INTEGER NOT NULL DEFAULT 0,
                uses_external_provider INTEGER NOT NULL DEFAULT 0,
                external_provider_host TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                test_query TEXT,
                test_result_count INTEGER NOT NULL DEFAULT 0,
                test_success INTEGER NOT NULL DEFAULT 0,
                last_tested_at TEXT,
                last_discovered_at TEXT,
                error_message TEXT,
                raw_discovery_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_district_search_profiles_district
                ON district_search_profiles(district_id);
            CREATE INDEX IF NOT EXISTS idx_district_search_profiles_status
                ON district_search_profiles(profile_status);
            CREATE INDEX IF NOT EXISTS idx_district_search_profiles_provider
                ON district_search_profiles(provider_guess);

            CREATE TABLE IF NOT EXISTS district_search_profile_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER REFERENCES district_search_profiles(id) ON DELETE SET NULL,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                test_query TEXT NOT NULL,
                attempted_url TEXT,
                status_code INTEGER,
                content_type TEXT,
                result_count INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                error_message TEXT,
                raw_test_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_district_search_profile_tests_district
                ON district_search_profile_tests(district_id);

            CREATE TABLE IF NOT EXISTS search_provider_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_name TEXT NOT NULL,
                pattern_name TEXT NOT NULL,
                host_contains TEXT,
                html_marker TEXT,
                form_action_contains TEXT,
                query_param_candidates_json TEXT,
                url_templates_json TEXT,
                result_link_selector TEXT,
                notes TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                states_json TEXT,
                agency_types_json TEXT,
                min_enrollment INTEGER,
                max_enrollment INTEGER,
                profile_status_filter TEXT,
                provider_guess_filter TEXT,
                max_districts INTEGER,
                max_workers INTEGER,
                test_query TEXT,
                force INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                districts_matched INTEGER NOT NULL DEFAULT 0,
                districts_planned INTEGER NOT NULL DEFAULT 0,
                districts_processed INTEGER NOT NULL DEFAULT 0,
                profiles_working INTEGER NOT NULL DEFAULT 0,
                profiles_failed INTEGER NOT NULL DEFAULT 0,
                profiles_manual_review INTEGER NOT NULL DEFAULT 0,
                profiles_requires_javascript INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                debug_log_path TEXT
            );

            CREATE TABLE IF NOT EXISTS contract_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                states_json TEXT,
                agency_types_json TEXT,
                min_enrollment INTEGER,
                max_enrollment INTEGER,
                max_districts INTEGER NOT NULL,
                max_pages_per_district INTEGER NOT NULL DEFAULT 20,
                max_workers INTEGER NOT NULL DEFAULT 3,
                use_llm INTEGER NOT NULL DEFAULT 0,
                include_salary_schedules INTEGER NOT NULL DEFAULT 1,
                archive_documents INTEGER NOT NULL DEFAULT 1,
                store_extracted_text INTEGER NOT NULL DEFAULT 1,
                rescan_after_days INTEGER NOT NULL DEFAULT 180,
                recheck_expired INTEGER NOT NULL DEFAULT 1,
                force_rescan INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                districts_matched INTEGER NOT NULL DEFAULT 0,
                districts_planned INTEGER NOT NULL DEFAULT 0,
                districts_processed INTEGER NOT NULL DEFAULT 0,
                districts_failed INTEGER NOT NULL DEFAULT 0,
                districts_skipped_recent INTEGER NOT NULL DEFAULT 0,
                packages_found INTEGER NOT NULL DEFAULT 0,
                documents_found INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_message TEXT,
                debug_log_path TEXT
            );

            CREATE TABLE IF NOT EXISTS district_contract_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_run_id INTEGER NOT NULL REFERENCES contract_discovery_runs(id) ON DELETE CASCADE,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                district_name TEXT,
                state TEXT,
                website TEXT,
                bargaining_unit_type TEXT NOT NULL DEFAULT 'unknown',
                bargaining_unit_name TEXT,
                union_name TEXT,
                effective_date TEXT,
                expiration_date TEXT,
                agreement_status TEXT NOT NULL DEFAULT 'unknown',
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                confidence REAL NOT NULL DEFAULT 0,
                source_page_url TEXT,
                current_as_of TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_contract_packages_run
                ON district_contract_packages(discovery_run_id);
            CREATE INDEX IF NOT EXISTS idx_contract_packages_district
                ON district_contract_packages(district_id);
            CREATE INDEX IF NOT EXISTS idx_contract_packages_unit
                ON district_contract_packages(bargaining_unit_type);
            CREATE INDEX IF NOT EXISTS idx_contract_packages_expiration
                ON district_contract_packages(district_id, expiration_date, bargaining_unit_type);

            CREATE TABLE IF NOT EXISTS district_contract_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL REFERENCES district_contract_packages(id) ON DELETE CASCADE,
                discovery_run_id INTEGER NOT NULL REFERENCES contract_discovery_runs(id) ON DELETE CASCADE,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                document_type TEXT NOT NULL DEFAULT 'other',
                title TEXT,
                url TEXT NOT NULL,
                parent_page_url TEXT,
                content_type TEXT,
                status_code INTEGER,
                discovery_source TEXT,
                effective_date TEXT,
                expiration_date TEXT,
                agreement_status TEXT NOT NULL DEFAULT 'unknown',
                confidence REAL NOT NULL DEFAULT 0,
                snippet TEXT,
                content_sha256 TEXT,
                file_size_bytes INTEGER,
                local_file_path TEXT,
                extracted_text_path TEXT,
                archived_at TEXT,
                llm_analysis_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(discovery_run_id, district_id, url)
            );

            CREATE INDEX IF NOT EXISTS idx_contract_documents_package
                ON district_contract_documents(package_id);
            CREATE INDEX IF NOT EXISTS idx_contract_documents_run
                ON district_contract_documents(discovery_run_id);

            CREATE INDEX IF NOT EXISTS idx_contract_documents_hash
                ON district_contract_documents(content_sha256);

            CREATE TABLE IF NOT EXISTS district_contract_scan_status (
                district_id INTEGER PRIMARY KEY REFERENCES districts(id) ON DELETE CASCADE,
                last_run_id INTEGER REFERENCES contract_discovery_runs(id) ON DELETE SET NULL,
                last_attempted_at TEXT,
                last_successful_scan_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'never',
                last_error TEXT,
                packages_found INTEGER NOT NULL DEFAULT 0,
                documents_found INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_contract_scan_status_success
                ON district_contract_scan_status(last_successful_scan_at);

            CREATE TABLE IF NOT EXISTS board_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                source_status TEXT NOT NULL,
                source_url TEXT NOT NULL COLLATE NOCASE,
                organization_external_id TEXT,
                platform_tenant TEXT,
                discovered_from_url TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                requires_javascript INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                superseded_at TEXT,
                last_discovered_at TEXT,
                last_successful_sync_at TEXT,
                last_checked_at TEXT,
                error_message TEXT,
                raw_discovery_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(district_id, platform, source_url)
            );

            CREATE INDEX IF NOT EXISTS idx_board_sources_district
                ON board_sources(district_id);
            CREATE INDEX IF NOT EXISTS idx_board_sources_platform_status
                ON board_sources(platform, source_status);
            CREATE INDEX IF NOT EXISTS idx_board_sources_last_sync
                ON board_sources(last_successful_sync_at);
            CREATE TABLE IF NOT EXISTS board_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                states_json TEXT,
                agency_types_json TEXT,
                min_enrollment INTEGER,
                max_enrollment INTEGER,
                platform_filter TEXT,
                status_filter TEXT,
                max_districts INTEGER NOT NULL,
                max_workers INTEGER NOT NULL,
                force INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                debug_logging INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                districts_matched INTEGER NOT NULL DEFAULT 0,
                districts_planned INTEGER NOT NULL DEFAULT 0,
                districts_processed INTEGER NOT NULL DEFAULT 0,
                sources_working INTEGER NOT NULL DEFAULT 0,
                sources_not_found INTEGER NOT NULL DEFAULT 0,
                sources_manual_review INTEGER NOT NULL DEFAULT 0,
                sources_failed INTEGER NOT NULL DEFAULT 0,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                debug_log_path TEXT
            );

            CREATE TABLE IF NOT EXISTS board_discovery_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES board_discovery_runs(id) ON DELETE CASCADE,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                board_source_id INTEGER REFERENCES board_sources(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(run_id, district_id)
            );

            CREATE INDEX IF NOT EXISTS idx_board_discovery_items_run_status
                ON board_discovery_run_items(run_id, status);

            CREATE TABLE IF NOT EXISTS board_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                states_json TEXT,
                agency_types_json TEXT,
                min_enrollment INTEGER,
                max_enrollment INTEGER,
                platforms_json TEXT,
                source_status TEXT,
                last_sync_before TEXT,
                date_from TEXT,
                date_to TEXT,
                sync_mode TEXT NOT NULL,
                force INTEGER NOT NULL DEFAULT 0,
                max_districts INTEGER NOT NULL,
                max_workers INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                debug_logging INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                districts_matched INTEGER NOT NULL DEFAULT 0,
                districts_planned INTEGER NOT NULL DEFAULT 0,
                districts_processed INTEGER NOT NULL DEFAULT 0,
                meetings_discovered INTEGER NOT NULL DEFAULT 0,
                meetings_added INTEGER NOT NULL DEFAULT 0,
                meetings_updated INTEGER NOT NULL DEFAULT 0,
                documents_added INTEGER NOT NULL DEFAULT 0,
                documents_updated INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                debug_log_path TEXT
            );

            CREATE TABLE IF NOT EXISTS board_sync_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES board_sync_runs(id) ON DELETE CASCADE,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                board_source_id INTEGER NOT NULL REFERENCES board_sources(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                meetings_discovered INTEGER NOT NULL DEFAULT 0,
                meetings_added INTEGER NOT NULL DEFAULT 0,
                meetings_updated INTEGER NOT NULL DEFAULT 0,
                documents_added INTEGER NOT NULL DEFAULT 0,
                documents_updated INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(run_id, board_source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_board_sync_items_run_status
                ON board_sync_run_items(run_id, status);

            CREATE TABLE IF NOT EXISTS board_sync_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_source_id INTEGER NOT NULL UNIQUE
                    REFERENCES board_sources(id) ON DELETE CASCADE,
                frequency TEXT NOT NULL,
                weekday INTEGER,
                day_of_month INTEGER,
                hour_24 INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_run_at TEXT NOT NULL,
                last_scheduled_for TEXT,
                last_run_at TEXT,
                last_sync_run_id INTEGER
                    REFERENCES board_sync_runs(id) ON DELETE SET NULL,
                last_error TEXT,
                claim_token TEXT,
                claim_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (frequency IN ('daily', 'weekly', 'monthly')),
                CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6),
                CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31),
                CHECK (hour_24 BETWEEN 0 AND 23),
                CHECK (minute BETWEEN 0 AND 59)
            );

            CREATE INDEX IF NOT EXISTS idx_board_sync_schedules_due
                ON board_sync_schedules(enabled, next_run_at, claim_expires_at);

            CREATE TABLE IF NOT EXISTS board_sync_schedule_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL
                    REFERENCES board_sync_schedules(id) ON DELETE CASCADE,
                scheduled_for TEXT NOT NULL,
                board_sync_run_id INTEGER
                    REFERENCES board_sync_runs(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(schedule_id, scheduled_for)
            );

            CREATE INDEX IF NOT EXISTS idx_board_sync_schedule_events_run
                ON board_sync_schedule_events(board_sync_run_id);

            CREATE TABLE IF NOT EXISTS board_meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                board_source_id INTEGER NOT NULL REFERENCES board_sources(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                external_meeting_id TEXT NOT NULL,
                meeting_date TEXT,
                meeting_start_time TEXT,
                meeting_end_time TEXT,
                meeting_datetime_text TEXT,
                title TEXT,
                meeting_type TEXT,
                location_name TEXT,
                location_address TEXT,
                description TEXT,
                agenda_url TEXT,
                minutes_url TEXT,
                packet_url TEXT,
                public_notice_url TEXT,
                video_url TEXT,
                livestream_url TEXT,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'published',
                revision_detected INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_checked_at TEXT,
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(board_source_id, external_meeting_id)
            );

            CREATE INDEX IF NOT EXISTS idx_board_meetings_district_date
                ON board_meetings(district_id, meeting_date DESC);
            CREATE INDEX IF NOT EXISTS idx_board_meetings_date
                ON board_meetings(meeting_date DESC);
            CREATE INDEX IF NOT EXISTS idx_board_meetings_platform
                ON board_meetings(platform);
            CREATE INDEX IF NOT EXISTS idx_board_meetings_source_external
                ON board_meetings(board_source_id, external_meeting_id);

            CREATE TABLE IF NOT EXISTS board_meeting_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_meeting_id INTEGER NOT NULL REFERENCES board_meetings(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                raw_snapshot_path TEXT,
                normalized_json TEXT NOT NULL,
                http_status INTEGER,
                retrieved_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(board_meeting_id, version_number),
                UNIQUE(board_meeting_id, content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_board_meeting_versions_meeting
                ON board_meeting_versions(board_meeting_id, version_number DESC);

            CREATE TABLE IF NOT EXISTS board_agenda_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_meeting_id INTEGER NOT NULL REFERENCES board_meetings(id) ON DELETE CASCADE,
                parent_item_id INTEGER REFERENCES board_agenda_items(id) ON DELETE CASCADE,
                external_item_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL DEFAULT 0,
                display_number TEXT,
                depth INTEGER NOT NULL DEFAULT 0,
                item_type TEXT,
                title TEXT,
                description TEXT,
                presenter TEXT,
                department TEXT,
                action_requested TEXT,
                motion_text TEXT,
                vote_text TEXT,
                result_text TEXT,
                source_url TEXT,
                normalized_text TEXT,
                content_hash TEXT NOT NULL,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(board_meeting_id, external_item_id)
            );

            CREATE INDEX IF NOT EXISTS idx_board_agenda_items_meeting_sequence
                ON board_agenda_items(board_meeting_id, sequence_number);
            CREATE INDEX IF NOT EXISTS idx_board_agenda_items_parent
                ON board_agenda_items(parent_item_id);

            CREATE TABLE IF NOT EXISTS board_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                board_meeting_id INTEGER NOT NULL REFERENCES board_meetings(id) ON DELETE CASCADE,
                agenda_item_id INTEGER REFERENCES board_agenda_items(id) ON DELETE SET NULL,
                identity_key TEXT NOT NULL,
                document_type TEXT NOT NULL DEFAULT 'other',
                title TEXT,
                source_url TEXT NOT NULL,
                resolved_url TEXT,
                mime_type TEXT,
                filename TEXT,
                local_path TEXT,
                external_document_id TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                http_status INTEGER,
                http_etag TEXT,
                http_last_modified TEXT,
                retrieval_metadata_json TEXT,
                text_extraction_status TEXT NOT NULL DEFAULT 'pending',
                extracted_text TEXT,
                error_message TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                retrieved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(board_meeting_id, identity_key)
            );

            CREATE INDEX IF NOT EXISTS idx_board_documents_district
                ON board_documents(district_id);
            CREATE INDEX IF NOT EXISTS idx_board_documents_meeting
                ON board_documents(board_meeting_id);
            CREATE INDEX IF NOT EXISTS idx_board_documents_agenda_item
                ON board_documents(agenda_item_id);
            CREATE INDEX IF NOT EXISTS idx_board_documents_sha256
                ON board_documents(sha256);

            CREATE TABLE IF NOT EXISTS board_document_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_document_id INTEGER NOT NULL REFERENCES board_documents(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER,
                local_path TEXT,
                extracted_text TEXT,
                text_extraction_status TEXT,
                http_etag TEXT,
                http_last_modified TEXT,
                retrieval_metadata_json TEXT,
                first_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(board_document_id, version_number),
                UNIQUE(board_document_id, sha256)
            );

            CREATE INDEX IF NOT EXISTS idx_board_document_versions_document
                ON board_document_versions(board_document_id, version_number DESC);

            CREATE TABLE IF NOT EXISTS board_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                analysis_type TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                prompt_version TEXT,
                input_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                confidence REAL,
                created_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, analysis_type, provider, model, prompt_version, input_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_board_analysis_entity
                ON board_analysis(entity_type, entity_id);

            CREATE TABLE IF NOT EXISTS board_search_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                district_id INTEGER NOT NULL REFERENCES districts(id) ON DELETE CASCADE,
                meeting_id INTEGER NOT NULL REFERENCES board_meetings(id) ON DELETE CASCADE,
                agenda_item_id INTEGER REFERENCES board_agenda_items(id) ON DELETE CASCADE,
                document_id INTEGER REFERENCES board_documents(id) ON DELETE CASCADE,
                state TEXT,
                platform TEXT,
                meeting_date TEXT,
                document_type TEXT,
                title TEXT,
                body TEXT,
                source_url TEXT NOT NULL,
                retrieved_at TEXT,
                changed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(entity_type, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_board_search_content_filters
                ON board_search_content(state, meeting_date, district_id, platform, document_type);
            """
        )
        existing_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(search_runs)")
        }
        if "max_districts" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN max_districts INTEGER;")
        if "max_pages_per_district" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN max_pages_per_district INTEGER;")
        if "search_method" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN search_method TEXT NOT NULL DEFAULT 'crawler';")
        if "search_provider" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN search_provider TEXT;")
        if "api_results_per_district" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN api_results_per_district INTEGER;")
        if "follow_depth" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN follow_depth INTEGER NOT NULL DEFAULT 0;")
        if "max_workers" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN max_workers INTEGER;")
        if "cancel_requested" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;")
        if "debug_logging" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN debug_logging INTEGER NOT NULL DEFAULT 0;")
        if "debug_log_path" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN debug_log_path TEXT;")
        existing_result_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(search_results)")
        }
        if "search_source" not in existing_result_columns:
            conn.execute("ALTER TABLE search_results ADD COLUMN search_source TEXT;")
        existing_discovery_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(profile_discovery_runs)")
        }
        if "profile_status_filter" not in existing_discovery_columns:
            conn.execute("ALTER TABLE profile_discovery_runs ADD COLUMN profile_status_filter TEXT;")
        if "provider_guess_filter" not in existing_discovery_columns:
            conn.execute("ALTER TABLE profile_discovery_runs ADD COLUMN provider_guess_filter TEXT;")
        if "cancel_requested" not in existing_discovery_columns:
            conn.execute("ALTER TABLE profile_discovery_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;")
        if "max_workers" not in existing_discovery_columns:
            conn.execute("ALTER TABLE profile_discovery_runs ADD COLUMN max_workers INTEGER;")
        if "profiles_requires_javascript" not in existing_discovery_columns:
            conn.execute("ALTER TABLE profile_discovery_runs ADD COLUMN profiles_requires_javascript INTEGER NOT NULL DEFAULT 0;")
        existing_contract_run_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(contract_discovery_runs)")
        }
        contract_run_migrations = {
            "archive_documents": "INTEGER NOT NULL DEFAULT 1",
            "store_extracted_text": "INTEGER NOT NULL DEFAULT 1",
            "rescan_after_days": "INTEGER NOT NULL DEFAULT 180",
            "recheck_expired": "INTEGER NOT NULL DEFAULT 1",
            "force_rescan": "INTEGER NOT NULL DEFAULT 0",
            "districts_skipped_recent": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in contract_run_migrations.items():
            if column not in existing_contract_run_columns:
                conn.execute(f"ALTER TABLE contract_discovery_runs ADD COLUMN {column} {declaration};")
        existing_contract_document_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(district_contract_documents)")
        }
        contract_document_migrations = {
            "file_size_bytes": "INTEGER",
            "local_file_path": "TEXT",
            "extracted_text_path": "TEXT",
            "archived_at": "TEXT",
        }
        for column, declaration in contract_document_migrations.items():
            if column not in existing_contract_document_columns:
                conn.execute(f"ALTER TABLE district_contract_documents ADD COLUMN {column} {declaration};")
        existing_board_source_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(board_sources)")
        }
        if "is_active" not in existing_board_source_columns:
            conn.execute("ALTER TABLE board_sources ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
        if "superseded_at" not in existing_board_source_columns:
            conn.execute("ALTER TABLE board_sources ADD COLUMN superseded_at TEXT;")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_board_sources_district_active "
            "ON board_sources(district_id, is_active, updated_at DESC)"
        )
        existing_board_document_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(board_documents)")
        }
        if "retrieval_metadata_json" not in existing_board_document_columns:
            conn.execute(
                "ALTER TABLE board_documents ADD COLUMN retrieval_metadata_json TEXT;"
            )
        existing_board_document_version_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(board_document_versions)")
        }
        if "retrieval_metadata_json" not in existing_board_document_version_columns:
            conn.execute(
                "ALTER TABLE board_document_versions "
                "ADD COLUMN retrieval_metadata_json TEXT;"
            )
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'board_search_fts'"
        ).fetchone()
        try:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS board_search_fts USING fts5(
                    title,
                    body,
                    content='board_search_content',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TRIGGER IF NOT EXISTS board_search_content_ai
                AFTER INSERT ON board_search_content BEGIN
                    INSERT INTO board_search_fts(rowid, title, body)
                    VALUES (new.id, new.title, new.body);
                END;

                CREATE TRIGGER IF NOT EXISTS board_search_content_ad
                AFTER DELETE ON board_search_content BEGIN
                    INSERT INTO board_search_fts(board_search_fts, rowid, title, body)
                    VALUES ('delete', old.id, old.title, old.body);
                END;

                CREATE TRIGGER IF NOT EXISTS board_search_content_au
                AFTER UPDATE ON board_search_content BEGIN
                    INSERT INTO board_search_fts(board_search_fts, rowid, title, body)
                    VALUES ('delete', old.id, old.title, old.body);
                    INSERT INTO board_search_fts(rowid, title, body)
                    VALUES (new.id, new.title, new.body);
                END;
                """
            )
            if not fts_exists:
                conn.execute("INSERT INTO board_search_fts(board_search_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError as exc:
            LOGGER.warning("SQLite FTS5 is unavailable; board search will use a LIKE fallback: %s", exc)
        conn.commit()


def clean_source_header(header: str) -> str:
    header = str(header or "").strip()
    if "[District]" in header:
        header = header.split("[District]", 1)[0].strip()
    return " ".join(header.split())


def snake_case_name(value: str) -> str:
    text = clean_source_header(value).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unnamed"


def normalize_state(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return STATE_NAME_TO_ABBR.get(text.casefold(), text)


def normalize_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text or text.casefold() in INVALID_WEBSITE_VALUES:
        return None
    text = text.replace(",", "")
    text = re.sub(r"[^0-9.-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _has_domain_shape(hostname: str) -> bool:
    if not hostname or "." not in hostname:
        return False
    if " " in hostname or "@" in hostname:
        return False
    labels = hostname.strip(".").split(".")
    if len(labels) < 2:
        return False
    if any(not label for label in labels):
        return False
    tld = labels[-1]
    return bool(re.fullmatch(r"[a-zA-Z]{2,63}", tld))


def normalize_website(value: Any) -> tuple[str, int]:
    raw = " ".join(str(value or "").strip().strip('"').strip("'").split())
    if not raw:
        return "", 0
    if raw.casefold() in INVALID_WEBSITE_VALUES:
        return "", 0
    if raw in {"†", "‡", "+", "–", "—"}:
        return "", 0
    if "†" in raw or "‡" in raw:
        return "", 0
    if raw.startswith("//"):
        normalized = f"https:{raw}"
    elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        normalized = raw
    else:
        normalized = f"https://{raw}"
    normalized = normalized.rstrip(" .,\t\r\n")
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").lower()
    if not _has_domain_shape(hostname):
        return "", 0
    return normalized, 1


def prefer_https_url(value: str | None) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "http":
        return url
    hostname = (parsed.hostname or "").casefold()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return url
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def discover_import_files() -> list[Path]:
    ensure_directories()
    files: list[Path] = []
    for suffix in ("*.csv", "*.xlsx", "*.xls"):
        files.extend(IMPORTS_DIR.glob(suffix))
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name.lower()), reverse=True)


def collect_db_stats(db_path: Path | str | None = None) -> dict[str, Any]:
    init_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS district_count,
                SUM(CASE WHEN has_searchable_website = 1 THEN 1 ELSE 0 END) AS searchable_count,
                COUNT(DISTINCT NULLIF(state, '')) AS state_count,
                COUNT(DISTINCT NULLIF(agency_type, '')) AS agency_type_count
            FROM districts
            """
        ).fetchone()
        latest_run = conn.execute(
            """
            SELECT id, query_text, status, started_at, finished_at
            FROM search_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        board_row = conn.execute(
            """
            SELECT
                (SELECT COUNT(DISTINCT district_id) FROM board_sources WHERE source_status = 'working') AS source_districts,
                (SELECT COUNT(*) FROM board_sources WHERE source_status = 'working') AS working_sources,
                (SELECT COUNT(*) FROM board_meetings) AS meetings,
                (SELECT COUNT(*) FROM board_documents) AS documents,
                (SELECT MAX(last_successful_sync_at) FROM board_sources) AS last_sync
            """
        ).fetchone()
        states = [
            row["state"]
            for row in conn.execute(
                "SELECT DISTINCT state FROM districts WHERE state IS NOT NULL AND state != '' ORDER BY state"
            )
        ]
    state_count = int(row["state_count"] or 0)
    state_display = "50 + DC" if state_count == 51 and "DC" in states else f"{state_count:,}"
    return {
        "db_path": str(Path(db_path or DB_PATH).expanduser().resolve()),
        "db_exists": Path(db_path or DB_PATH).expanduser().exists(),
        "district_count": int(row["district_count"] or 0),
        "searchable_count": int(row["searchable_count"] or 0),
        "state_count": state_count,
        "state_display": state_display,
        "agency_type_count": int(row["agency_type_count"] or 0),
        "latest_run": dict(latest_run) if latest_run else None,
        "board_source_districts": int(board_row["source_districts"] or 0),
        "board_working_sources": int(board_row["working_sources"] or 0),
        "board_meetings": int(board_row["meetings"] or 0),
        "board_documents": int(board_row["documents"] or 0),
        "board_last_sync": board_row["last_sync"],
    }


def list_filter_options(db_path: Path | str | None = None) -> dict[str, list[str]]:
    init_db(db_path)
    with connect_db(db_path) as conn:
        states = [
            row["state"]
            for row in conn.execute(
                "SELECT DISTINCT state FROM districts WHERE state IS NOT NULL AND state != '' ORDER BY state"
            )
        ]
        agency_types = [
            row["agency_type"]
            for row in conn.execute(
                """
                SELECT DISTINCT agency_type
                FROM districts
                WHERE agency_type IS NOT NULL AND agency_type != ''
                ORDER BY agency_type
                """
            )
        ]
    return {"states": states, "agency_types": agency_types}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
