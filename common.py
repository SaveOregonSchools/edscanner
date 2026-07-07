from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
IMPORTS_DIR = APP_ROOT / "imports"
EXPORTS_DIR = APP_ROOT / "exports"
LOGS_DIR = APP_ROOT / "logs"
SEARCH_RUN_LOGS_DIR = LOGS_DIR / "search_runs"

DEFAULT_DB_PATH = DATA_DIR / "edscanner.db"
DB_PATH = Path(os.getenv("EDSCANNER_DB_PATH", DEFAULT_DB_PATH)).expanduser().resolve()
LOG_PATH = LOGS_DIR / "edscanner.log"

APP_VERSION = "0.1"
USER_AGENT = os.getenv(
    "EDSCANNER_USER_AGENT",
    "EdScanner/0.1 (+https://github.com/SaveOregonSchools/edscanner; public school district content search)",
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
VERIFY_SSL = _env_bool("EDSCANNER_VERIFY_SSL", True)


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
    for path in (DATA_DIR, IMPORTS_DIR, EXPORTS_DIR, LOGS_DIR, SEARCH_RUN_LOGS_DIR):
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
                score REAL NOT NULL DEFAULT 0,
                snippet TEXT,
                matched_terms_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_search_results_run
                ON search_results(search_run_id);
            CREATE INDEX IF NOT EXISTS idx_search_results_district
                ON search_results(district_id);
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
        if "cancel_requested" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;")
        if "debug_logging" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN debug_logging INTEGER NOT NULL DEFAULT 0;")
        if "debug_log_path" not in existing_columns:
            conn.execute("ALTER TABLE search_runs ADD COLUMN debug_log_path TEXT;")
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
