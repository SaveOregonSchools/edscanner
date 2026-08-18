from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for
from markupsafe import Markup, escape

from common import (
    BRAVE_SEARCH_API_KEY_ENV,
    CONTRACT_ARCHIVE_DIR,
    CONTRACT_DISCOVERY_RUN_LOGS_DIR,
    CONTRACT_DISCOVERY_WORKERS,
    CONTRACT_RESCAN_DAYS,
    IMPORTS_DIR,
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    OLLAMA_ENDPOINTS_ENV,
    OLLAMA_MODEL_ENV,
    MAX_PAGES_PER_DISTRICT,
    PROFILE_DISCOVERY_RUN_LOGS_DIR,
    PROFILE_DISCOVERY_WORKERS,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    SEARCH_RUN_LOGS_DIR,
    SEARCH_RUN_WORKERS,
    collect_db_stats,
    configure_logging,
    connect_db,
    current_db_path,
    discover_import_files,
    get_local_setting,
    has_brave_search_api_key,
    init_db,
    list_filter_options,
    set_local_setting,
    utc_now_iso,
)
from contract_discovery import (
    UNIT_TYPES,
    create_contract_discovery_run,
    execute_contract_discovery_run,
    export_contract_discovery_csv,
)
from ai_matcher import (
    get_ollama_endpoints,
    get_ollama_model,
    local_llm_is_configured,
    parse_ollama_endpoints,
    test_ollama_endpoints,
)
from import_districts import ImportErrorWithContext, import_districts
from search_engine import (
    RunDebugLogger,
    SearchSettings,
    debug_log,
    count_matching_districts,
    create_search_run,
    execute_search_run,
    export_search_run_csv,
    normalize_search_method,
    parse_optional_int,
)
from board.web import bp as board_blueprint, start_board_worker


configure_logging()
init_db()

app = Flask(__name__)
app.config["SECRET_KEY"] = "edscanner-local-dev"
app.register_blueprint(board_blueprint)
LOGGER = logging.getLogger(__name__)
SEARCH_QUEUE: queue.Queue[int] = queue.Queue()
PROFILE_DISCOVERY_QUEUE: queue.Queue[int] = queue.Queue()
CONTRACT_DISCOVERY_QUEUE: queue.Queue[int] = queue.Queue()
WORKER_STARTED = False
PROFILE_DISCOVERY_WORKER_STARTED = False
CONTRACT_DISCOVERY_WORKER_STARTED = False


PROFILE_STATUSES = [
    "working",
    "no_search_found",
    "manual_review",
    "search_found_but_failed",
    "requires_javascript",
    "blocked_by_challenge",
    "blocked_by_robots",
    "external_search_only",
    "error",
]
PROFILE_DISCOVERY_MAX_DISTRICTS = 1000


def selected_values(name: str) -> list[str]:
    return [value for value in request.values.getlist(name) if str(value).strip()]


def selected_profile_statuses() -> list[str]:
    values = selected_values("profile_status")
    legacy_value = request.values.get("profile_status", "").strip()
    if legacy_value and legacy_value not in values:
        values.append(legacy_value)
    out: list[str] = []
    allowed = {*PROFILE_STATUSES, "__never__"}
    for value in values:
        if value in allowed and value not in out:
            out.append(value)
    return out


def profile_status_filter_to_json(statuses: list[str]) -> str:
    return json.dumps(statuses)


def profile_status_filter_from_value(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = text
    if isinstance(parsed, list):
        values = [str(item) for item in parsed]
    else:
        values = [str(parsed)]
    allowed = {*PROFILE_STATUSES, "__never__"}
    return [value for value in values if value in allowed]


def add_profile_status_filter_sql(
    clauses: list[str],
    params: list[Any],
    profile_statuses: list[str],
) -> None:
    statuses = [status for status in profile_statuses if status != "__never__"]
    include_never = "__never__" in profile_statuses
    parts: list[str] = []
    if include_never:
        parts.append("p.id IS NULL")
    if statuses:
        parts.append(f"p.profile_status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if parts:
        clauses.append("(" + " OR ".join(parts) + ")")


def clamp_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    return max(minimum, min(maximum, value))


def fmt_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def fmt_profile_status_filter(value: str | None) -> str:
    statuses = profile_status_filter_from_value(value)
    if not statuses:
        return "Any status"
    labels = ["Never tested" if status == "__never__" else status for status in statuses]
    return ", ".join(labels)


def elapsed_seconds(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at)
    except ValueError:
        return ""
    seconds = max(0, int((finish - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining = divmod(seconds, 60)
    return f"{minutes}m {remaining}s"


def highlight(text: str | None, query_text: str | None) -> Markup:
    text = str(text or "")
    query_text = str(query_text or "").strip()
    if not text or not query_text:
        return escape(text)
    pattern = re.compile(re.escape(query_text), re.IGNORECASE)
    parts: list[Markup] = []
    last = 0
    for match in pattern.finditer(text):
        parts.append(escape(text[last : match.start()]))
        parts.append(Markup("<mark>") + escape(text[match.start() : match.end()]) + Markup("</mark>"))
        last = match.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)


def district_search_coverage(
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
) -> dict[str, int]:
    clauses: list[str] = ["d.has_searchable_website = 1"]
    params: list[Any] = []
    states = [value for value in states or [] if value]
    agency_types = [value for value in agency_types or [] if value]
    if states:
        clauses.append(f"d.state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if agency_types:
        clauses.append(f"d.agency_type IN ({','.join('?' for _ in agency_types)})")
        params.extend(agency_types)
    if min_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae >= ?")
        params.append(min_enrollment)
    if max_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae <= ?")
        params.append(max_enrollment)
    where_sql = " WHERE " + " AND ".join(clauses)
    with connect_db() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS matching_count,
                SUM(CASE WHEN p.profile_status = 'working' THEN 1 ELSE 0 END) AS working_count,
                SUM(CASE WHEN p.profile_status = 'requires_javascript' THEN 1 ELSE 0 END) AS javascript_count,
                SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END) AS missing_count
            FROM districts d
            LEFT JOIN (
                SELECT p1.*
                FROM district_search_profiles p1
                JOIN (
                    SELECT district_id, MAX(id) AS id
                    FROM district_search_profiles
                    GROUP BY district_id
                ) latest ON latest.id = p1.id
            ) p ON p.district_id = d.id
            {where_sql}
            """,
            params,
        ).fetchone()
    return {
        "matching_count": int(row["matching_count"] or 0),
        "working_count": int(row["working_count"] or 0),
        "javascript_count": int(row["javascript_count"] or 0),
        "missing_count": int(row["missing_count"] or 0),
    }


def list_profile_filtered_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_statuses: list[str],
    provider_guess: str,
    limit: int,
) -> list[dict[str, Any]]:
    clauses: list[str] = ["d.has_searchable_website = 1"]
    params: list[Any] = []
    if states:
        clauses.append(f"d.state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if agency_types:
        clauses.append(f"d.agency_type IN ({','.join('?' for _ in agency_types)})")
        params.extend(agency_types)
    if min_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae >= ?")
        params.append(min_enrollment)
    if max_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae <= ?")
        params.append(max_enrollment)
    add_profile_status_filter_sql(clauses, params, profile_statuses)
    if provider_guess:
        clauses.append("p.provider_guess = ?")
        params.append(provider_guess)
    where_sql = " WHERE " + " AND ".join(clauses)
    with connect_db() as conn:
        rows = conn.execute(
            f"""
            SELECT d.*
            FROM districts d
            LEFT JOIN (
                SELECT p1.*
                FROM district_search_profiles p1
                JOIN (
                    SELECT district_id, MAX(id) AS id
                    FROM district_search_profiles
                    GROUP BY district_id
                ) latest ON latest.id = p1.id
            ) p ON p.district_id = d.id
            {where_sql}
            ORDER BY d.state, d.agency_name
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def count_profile_filtered_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_statuses: list[str],
    provider_guess: str,
) -> int:
    return len(
        list_profile_filtered_districts(
            states,
            agency_types,
            min_enrollment,
            max_enrollment,
            profile_statuses,
            provider_guess,
            MAX_TOTAL_DISTRICTS_PER_RUN * 1000,
        )
    )


def create_profile_discovery_run(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_statuses: list[str],
    provider_guess: str,
    max_districts: int,
    max_workers: int,
    test_query: str,
    force: bool,
) -> int:
    matched_count = count_profile_filtered_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        profile_statuses,
        provider_guess,
    )
    planned_count = min(matched_count, max_districts)
    now = utc_now_iso()
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO profile_discovery_runs (
                states_json, agency_types_json, min_enrollment, max_enrollment,
                profile_status_filter, provider_guess_filter, max_districts,
                max_workers, test_query, force, cancel_requested, status, districts_matched,
                districts_planned, districts_processed, profiles_working,
                profiles_failed, profiles_manual_review, profiles_requires_javascript, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', ?, ?, 0, 0, 0, 0, 0, ?)
            """,
            (
                json.dumps(states),
                json.dumps(agency_types),
                min_enrollment,
                max_enrollment,
                profile_status_filter_to_json(profile_statuses),
                provider_guess,
                max_districts,
                max_workers,
                test_query,
                1 if force else 0,
                matched_count,
                planned_count,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
    return run_id


def is_profile_discovery_cancel_requested(run_id: int) -> bool:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM profile_discovery_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return bool(row and row["cancel_requested"])


def execute_profile_discovery_run(run_id: int) -> None:
    from site_search_discovery import discover_district_search_profile

    with connect_db() as conn:
        run = conn.execute("SELECT * FROM profile_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Profile discovery run not found: {run_id}")

    states = json.loads(run["states_json"] or "[]")
    agency_types = json.loads(run["agency_types_json"] or "[]")
    max_districts = int(run["max_districts"] or 1)
    districts = list_profile_filtered_districts(
        states,
        agency_types,
        run["min_enrollment"],
        run["max_enrollment"],
        profile_status_filter_from_value(run["profile_status_filter"]),
        run["provider_guess_filter"] or "",
        max_districts,
    )

    processed = 0
    working = 0
    failed = 0
    manual_review = 0
    requires_javascript = 0
    cancelled = False
    settings = SearchSettings(max_pages_per_district=10)
    max_workers = clamp_int(run["max_workers"], PROFILE_DISCOVERY_WORKERS, 1, 8)
    debug_path = PROFILE_DISCOVERY_RUN_LOGS_DIR / f"profile-discovery-run-{run_id}.log"
    debug_logger = RunDebugLogger(debug_path)
    counter_lock = threading.Lock()

    def update_run_progress() -> None:
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET districts_processed = ?,
                    profiles_working = ?,
                    profiles_failed = ?,
                    profiles_manual_review = ?,
                    profiles_requires_javascript = ?
                WHERE id = ?
                """,
                (processed, working, failed, manual_review, requires_javascript, run_id),
            )
            conn.commit()

    def discover_one(district: dict[str, Any]) -> tuple[str, str]:
        profile = discover_district_search_profile(
            district,
            test_query=run["test_query"] or "calendar",
            settings=settings,
            force=bool(run["force"]),
            debug_logger=debug_logger,
        )
        return profile.get("profile_status") or "error", profile.get("provider_guess") or ""

    try:
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'running',
                    districts_planned = ?,
                    max_workers = ?,
                    debug_log_path = ?,
                    districts_processed = 0,
                    profiles_working = 0,
                    profiles_failed = 0,
                    profiles_manual_review = 0,
                    profiles_requires_javascript = 0,
                    finished_at = NULL,
                    error_message = NULL
                WHERE id = ?
                """,
                (len(districts), max_workers, str(debug_path), run_id),
            )
            conn.commit()
        debug_log(
            debug_logger,
            "profile_discovery_run_start",
            run_id=run_id,
            district_count=len(districts),
            max_workers=max_workers,
            test_query=run["test_query"] or "calendar",
            force=bool(run["force"]),
        )

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ProfileDiscovery") as executor:
            future_to_district = {
                executor.submit(discover_one, district): district
                for district in districts
                if not is_profile_discovery_cancel_requested(run_id)
            }
            for future in as_completed(future_to_district):
                district = future_to_district[future]
                if is_profile_discovery_cancel_requested(run_id):
                    cancelled = True
                    for pending in future_to_district:
                        pending.cancel()
                    break
                try:
                    status, provider_guess = future.result()
                    debug_log(
                        debug_logger,
                        "profile_discovery_district_finish",
                        run_id=run_id,
                        district=district.get("agency_name"),
                        status=status,
                        provider_guess=provider_guess,
                    )
                except Exception as exc:
                    status = "error"
                    LOGGER.exception("Profile discovery failed for %s: %s", district.get("agency_name"), exc)
                    debug_log(
                        debug_logger,
                        "profile_discovery_district_error",
                        run_id=run_id,
                        district=district.get("agency_name"),
                        error=str(exc),
                    )
                with counter_lock:
                    processed += 1
                    if status == "working":
                        working += 1
                    elif status == "manual_review":
                        manual_review += 1
                    elif status == "requires_javascript":
                        requires_javascript += 1
                    else:
                        failed += 1
                    update_run_progress()

        if is_profile_discovery_cancel_requested(run_id):
            cancelled = True

        final_status = "cancelled" if cancelled else "completed"
        error_message = "Cancelled by user." if cancelled else None
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = ?,
                    districts_processed = ?,
                    profiles_working = ?,
                    profiles_failed = ?,
                    profiles_manual_review = ?,
                    profiles_requires_javascript = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (final_status, processed, working, failed, manual_review, requires_javascript, utc_now_iso(), error_message, run_id),
            )
            conn.commit()
        debug_log(
            debug_logger,
            "profile_discovery_run_finish",
            run_id=run_id,
            status=final_status,
            processed=processed,
            working=working,
            failed=failed,
            manual_review=manual_review,
            requires_javascript=requires_javascript,
        )
    except Exception as exc:
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'failed',
                    districts_processed = ?,
                    profiles_working = ?,
                    profiles_failed = ?,
                    profiles_manual_review = ?,
                    profiles_requires_javascript = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (processed, working, failed, manual_review, requires_javascript, utc_now_iso(), str(exc), run_id),
            )
            conn.commit()
        raise


app.jinja_env.filters["fmt_int"] = fmt_int
app.jinja_env.filters["fmt_dt"] = fmt_dt
app.jinja_env.filters["fmt_profile_status_filter"] = fmt_profile_status_filter
app.jinja_env.filters["highlight"] = highlight


def enqueue_search_run(run_id: int) -> None:
    SEARCH_QUEUE.put(run_id)
    LOGGER.info("Queued search run %s", run_id)


def enqueue_profile_discovery_run(run_id: int) -> None:
    PROFILE_DISCOVERY_QUEUE.put(run_id)
    LOGGER.info("Queued profile discovery run %s", run_id)


def enqueue_contract_discovery_run(run_id: int) -> None:
    CONTRACT_DISCOVERY_QUEUE.put(run_id)
    LOGGER.info("Queued contract discovery run %s", run_id)


def search_worker() -> None:
    while True:
        run_id = SEARCH_QUEUE.get()
        try:
            with connect_db() as conn:
                run = conn.execute("SELECT status FROM search_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["status"] not in {"queued", "running"}:
                continue
            execute_search_run(run_id)
        except Exception:
            LOGGER.exception("Queued search run %s failed", run_id)
        finally:
            SEARCH_QUEUE.task_done()


def profile_discovery_worker() -> None:
    while True:
        run_id = PROFILE_DISCOVERY_QUEUE.get()
        try:
            with connect_db() as conn:
                run = conn.execute("SELECT status FROM profile_discovery_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["status"] not in {"queued", "running"}:
                continue
            execute_profile_discovery_run(run_id)
        except Exception:
            LOGGER.exception("Queued profile discovery run %s failed", run_id)
        finally:
            PROFILE_DISCOVERY_QUEUE.task_done()


def contract_discovery_worker() -> None:
    while True:
        run_id = CONTRACT_DISCOVERY_QUEUE.get()
        try:
            with connect_db() as conn:
                run = conn.execute("SELECT status FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["status"] not in {"queued", "running"}:
                continue
            execute_contract_discovery_run(run_id)
        except Exception:
            LOGGER.exception("Queued contract discovery run %s failed", run_id)
        finally:
            CONTRACT_DISCOVERY_QUEUE.task_done()


def start_search_worker() -> None:
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE search_runs
            SET status = 'queued',
                error_message = 'Run was queued again after app restart.'
            WHERE status = 'running'
            """
        )
        queued_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM search_runs WHERE status = 'queued' ORDER BY id"
            )
        ]
        conn.commit()
    thread = threading.Thread(target=search_worker, name="EdScannerSearchWorker", daemon=True)
    thread.start()
    WORKER_STARTED = True
    for run_id in queued_ids:
        enqueue_search_run(run_id)


def start_profile_discovery_worker() -> None:
    global PROFILE_DISCOVERY_WORKER_STARTED
    if PROFILE_DISCOVERY_WORKER_STARTED:
        return
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE profile_discovery_runs
            SET status = 'queued',
                error_message = 'Run was queued again after app restart.'
            WHERE status = 'running'
            """
        )
        queued_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM profile_discovery_runs WHERE status = 'queued' ORDER BY id"
            )
        ]
        conn.commit()
    thread = threading.Thread(target=profile_discovery_worker, name="EdScannerProfileDiscoveryWorker", daemon=True)
    thread.start()
    PROFILE_DISCOVERY_WORKER_STARTED = True
    for run_id in queued_ids:
        enqueue_profile_discovery_run(run_id)


def start_contract_discovery_worker() -> None:
    global CONTRACT_DISCOVERY_WORKER_STARTED
    if CONTRACT_DISCOVERY_WORKER_STARTED:
        return
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE contract_discovery_runs
            SET status = 'queued', error_message = 'Run was queued again after app restart.'
            WHERE status = 'running'
            """
        )
        queued_ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM contract_discovery_runs WHERE status = 'queued' ORDER BY id")
        ]
        conn.commit()
    thread = threading.Thread(target=contract_discovery_worker, name="EdScannerContractDiscoveryWorker", daemon=True)
    thread.start()
    CONTRACT_DISCOVERY_WORKER_STARTED = True
    for run_id in queued_ids:
        enqueue_contract_discovery_run(run_id)


if os.getenv("EDSCANNER_DISABLE_WORKER", "").casefold() not in {"1", "true", "yes", "on"}:
    start_search_worker()
    start_profile_discovery_worker()
    start_contract_discovery_worker()
    start_board_worker()


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {
        "db_path": current_db_path(),
        "max_total_districts_per_run": MAX_TOTAL_DISTRICTS_PER_RUN,
        "default_max_pages_per_district": MAX_PAGES_PER_DISTRICT,
        "year": datetime.now().year,
    }


@app.route("/")
def index():
    stats = collect_db_stats()
    with connect_db() as conn:
        recent_runs = conn.execute(
            """
            SELECT id, query_text, status, districts_matched, districts_searched,
                   districts_failed, started_at, finished_at
            FROM search_runs
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()
    return render_template("index.html", stats=stats, recent_runs=recent_runs)


@app.route("/import", methods=["GET", "POST"])
def import_page():
    summary = None
    if request.method == "POST":
        source_name = request.form.get("source_file", "__auto__")
        try:
            source_path = None
            if source_name != "__auto__":
                candidate = (IMPORTS_DIR / source_name).resolve()
                if candidate.parent != IMPORTS_DIR.resolve():
                    abort(400)
                source_path = candidate
            summary = import_districts(source_path)
            flash("Import completed.", "success")
        except ImportErrorWithContext as exc:
            flash(str(exc), "error")

    files = [
        {
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for path in discover_import_files()
    ]
    stats = collect_db_stats()
    return render_template("import.html", files=files, stats=stats, summary=summary)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        action = request.form.get("action", "save_brave")
        if action == "clear_brave_key":
            set_local_setting(BRAVE_SEARCH_API_KEY_ENV, "")
            flash("Brave Search API key cleared.", "success")
        elif action == "save_brave":
            api_key = request.form.get("brave_api_key", "").strip()
            if not api_key:
                flash("Enter a Brave Search API key or use Clear key.", "error")
            else:
                set_local_setting(BRAVE_SEARCH_API_KEY_ENV, api_key)
                flash("Brave Search API key saved to the local .env file.", "success")
        elif action in {"clear_llm", "clear_ollama"}:
            set_local_setting(OLLAMA_ENDPOINTS_ENV, "")
            set_local_setting(OLLAMA_MODEL_ENV, "")
            set_local_setting(LLM_BASE_URL_ENV, "")
            set_local_setting(LLM_MODEL_ENV, "")
            set_local_setting(LLM_API_KEY_ENV, "")
            flash("Ollama server settings cleared.", "success")
        elif action in {"save_llm", "save_ollama", "save_test_ollama"}:
            raw_endpoints = request.form.get("ollama_endpoints", "").strip()
            if not raw_endpoints:
                raw_endpoints = request.form.get("llm_base_url", "").strip()
            endpoints = parse_ollama_endpoints(raw_endpoints)
            model = (
                request.form.get("ollama_model", "").strip()
                or request.form.get("llm_model", "").strip()
            )
            api_key = request.form.get("llm_api_key", "").strip()
            invalid_endpoints = [
                value
                for value in endpoints
                if not re.match(r"^https?://", value, re.IGNORECASE)
            ]
            if not endpoints or invalid_endpoints or not model:
                flash("Enter one or more HTTP(S) Ollama server URLs and a model name.", "error")
            else:
                set_local_setting(OLLAMA_ENDPOINTS_ENV, json.dumps(endpoints, separators=(",", ":")))
                set_local_setting(OLLAMA_MODEL_ENV, model)
                set_local_setting(LLM_BASE_URL_ENV, "")
                set_local_setting(LLM_MODEL_ENV, "")
                if api_key:
                    set_local_setting(LLM_API_KEY_ENV, api_key)
                flash(f"Saved {len(endpoints)} Ollama server(s) in priority order.", "success")
                if action == "save_test_ollama":
                    results = test_ollama_endpoints(endpoints, timeout_seconds=5)
                    reachable = [item for item in results if item["ok"]]
                    model_hosts = [item for item in reachable if model in item["models"]]
                    if model_hosts:
                        flash(
                            f"Connection successful. {model} is available on "
                            f"{model_hosts[0]['endpoint']}; {len(reachable)} of "
                            f"{len(results)} server(s) reachable.",
                            "success",
                        )
                    elif reachable:
                        flash(
                            f"Reached {len(reachable)} server(s), but {model} was not "
                            "found in their installed model lists.",
                            "error",
                        )
                    else:
                        flash("No configured Ollama server could be reached.", "error")
        return redirect(url_for("settings_page"))

    key_present = has_brave_search_api_key()
    key = get_local_setting(BRAVE_SEARCH_API_KEY_ENV)
    masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "saved" if key_present else ""
    llm_key = get_local_setting(LLM_API_KEY_ENV)
    return render_template(
        "settings.html",
        key_present=key_present,
        masked_key=masked_key,
        env_key_name=BRAVE_SEARCH_API_KEY_ENV,
        llm_configured=local_llm_is_configured(),
        ollama_endpoints_text="\n".join(get_ollama_endpoints()),
        llm_model=get_ollama_model(),
        llm_key_present=bool(llm_key),
        llm_masked_key=(f"{llm_key[:4]}...{llm_key[-4:]}" if len(llm_key) > 10 else "saved" if llm_key else ""),
    )


@app.route("/contracts", methods=["GET", "POST"])
def contracts_page():
    options = list_filter_options()
    states = selected_values("states")
    if request.method == "GET" and not request.query_string and "OR" in options["states"]:
        states = ["OR"]
    agency_types = selected_values("agency_types")
    min_enrollment = parse_optional_int(request.values.get("min_enrollment"))
    max_enrollment = parse_optional_int(request.values.get("max_enrollment"))
    max_districts = clamp_int(parse_optional_int(request.values.get("max_districts")), 20, 1, 100)
    max_pages_per_district = clamp_int(parse_optional_int(request.values.get("max_pages_per_district")), 20, 3, 60)
    max_workers = clamp_int(parse_optional_int(request.values.get("max_workers")), CONTRACT_DISCOVERY_WORKERS, 1, 8)
    rescan_after_days = clamp_int(
        parse_optional_int(request.values.get("rescan_after_days")),
        CONTRACT_RESCAN_DAYS,
        0,
        3650,
    )
    use_llm = request.values.get("use_llm", "").casefold() in {"1", "true", "yes", "on"}
    checked_default = "" if request.method == "POST" else "1"
    include_salary_schedules = request.values.get("include_salary_schedules", checked_default).casefold() in {"1", "true", "yes", "on"}
    archive_documents = request.values.get("archive_documents", checked_default).casefold() in {"1", "true", "yes", "on"}
    store_extracted_text = request.values.get("store_extracted_text", checked_default).casefold() in {"1", "true", "yes", "on"}
    recheck_expired = request.values.get("recheck_expired", checked_default).casefold() in {"1", "true", "yes", "on"}
    force_rescan = request.values.get("force_rescan", "").casefold() in {"1", "true", "yes", "on"}

    if request.method == "POST":
        if use_llm and not local_llm_is_configured():
            flash("Configure the local AI server in Settings or run without AI classification.", "error")
            return redirect(url_for("contracts_page"))
        run_id = create_contract_discovery_run(
            states=states,
            agency_types=agency_types,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            max_districts=max_districts,
            max_pages_per_district=max_pages_per_district,
            max_workers=max_workers,
            use_llm=use_llm,
            include_salary_schedules=include_salary_schedules,
            archive_documents=archive_documents,
            store_extracted_text=store_extracted_text,
            rescan_after_days=rescan_after_days,
            recheck_expired=recheck_expired,
            force_rescan=force_rescan,
        )
        enqueue_contract_discovery_run(run_id)
        return redirect(url_for("contract_discovery_run_detail", run_id=run_id))

    with connect_db() as conn:
        recent_runs = conn.execute(
            """
            SELECT id, status, districts_planned, districts_processed, districts_failed,
                   districts_skipped_recent, packages_found, documents_found,
                   use_llm, archive_documents, started_at, finished_at
            FROM contract_discovery_runs ORDER BY id DESC LIMIT 10
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM district_contract_packages) AS packages,
                (SELECT COUNT(*) FROM district_contract_packages WHERE agreement_status = 'current') AS current_packages,
                (SELECT COUNT(*) FROM district_contract_packages WHERE review_status = 'needs_review') AS needs_review,
                (SELECT COUNT(DISTINCT district_id) FROM district_contract_packages) AS districts,
                (SELECT COUNT(*) FROM district_contract_scan_status WHERE last_successful_scan_at IS NOT NULL) AS successful_scans,
                (SELECT COUNT(DISTINCT district_id) FROM district_contract_documents WHERE local_file_path IS NOT NULL) AS archived_districts
            """
        ).fetchone()
        today = date.today().isoformat()
        district_scan_statuses = conn.execute(
            """
            SELECT s.*, d.agency_name, d.state,
                   EXISTS (
                       SELECT 1 FROM district_contract_packages expired
                       WHERE expired.district_id = d.id
                         AND expired.review_status != 'rejected'
                         AND expired.expiration_date IS NOT NULL
                         AND expired.expiration_date < ?
                         AND NOT EXISTS (
                             SELECT 1 FROM district_contract_packages successor
                             WHERE successor.district_id = expired.district_id
                               AND successor.bargaining_unit_type = expired.bargaining_unit_type
                               AND successor.review_status != 'rejected'
                               AND successor.expiration_date > ?
                         )
                   ) AS has_expired_contract,
                   (
                       SELECT MIN(p.expiration_date)
                       FROM district_contract_packages p
                       WHERE p.district_id = d.id
                         AND p.review_status != 'rejected'
                         AND p.expiration_date > ?
                   ) AS next_expiration_date
            FROM district_contract_scan_status s
            JOIN districts d ON d.id = s.district_id
            ORDER BY COALESCE(s.last_successful_scan_at, s.last_attempted_at) DESC
            LIMIT 30
            """,
            (today, today, today),
        ).fetchall()
    return render_template(
        "contracts.html",
        options=options,
        selected_states=states,
        selected_agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        max_districts=max_districts,
        max_pages_per_district=max_pages_per_district,
        max_workers=max_workers,
        rescan_after_days=rescan_after_days,
        use_llm=use_llm,
        include_salary_schedules=include_salary_schedules,
        archive_documents=archive_documents,
        store_extracted_text=store_extracted_text,
        recheck_expired=recheck_expired,
        force_rescan=force_rescan,
        contract_archive_dir=str(CONTRACT_ARCHIVE_DIR),
        llm_configured=local_llm_is_configured(),
        recent_runs=recent_runs,
        totals=totals,
        district_scan_statuses=district_scan_statuses,
    )


@app.route("/contracts/runs/<int:run_id>")
def contract_discovery_run_detail(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT * FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        packages = conn.execute(
            """
            SELECT * FROM district_contract_packages
            WHERE discovery_run_id = ?
            ORDER BY district_name, bargaining_unit_type, COALESCE(union_name, '')
            """,
            (run_id,),
        ).fetchall()
        documents = conn.execute(
            """
            SELECT * FROM district_contract_documents
            WHERE discovery_run_id = ?
            ORDER BY package_id, document_type, title
            """,
            (run_id,),
        ).fetchall()
    documents_by_package: dict[int, list[Any]] = {}
    for document in documents:
        documents_by_package.setdefault(int(document["package_id"]), []).append(document)
    planned = int(run["districts_planned"] or 0)
    processed = int(run["districts_processed"] or 0)
    in_progress_count = min(int(run["max_workers"] or 1), max(0, planned - processed)) if run["status"] == "running" else 0
    return render_template(
        "contract_run_detail.html",
        run=run,
        packages=packages,
        documents_by_package=documents_by_package,
        unit_types=UNIT_TYPES,
        states=json.loads(run["states_json"] or "[]"),
        agency_types=json.loads(run["agency_types_json"] or "[]"),
        elapsed=elapsed_seconds(run["started_at"], run["finished_at"]),
        in_progress_count=in_progress_count,
        left_count=max(0, planned - processed - in_progress_count),
    )


def _send_archived_contract_file(document_id: int, column: str, *, extracted_text: bool = False):
    if column not in {"local_file_path", "extracted_text_path"}:
        abort(404)
    with connect_db() as conn:
        document = conn.execute(
            f"SELECT title, content_type, {column} AS archive_path FROM district_contract_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if document is None or not document["archive_path"]:
        abort(404)
    path = Path(document["archive_path"]).expanduser().resolve()
    archive_root = CONTRACT_ARCHIVE_DIR.resolve()
    if archive_root not in path.parents or not path.is_file():
        abort(404)
    source_type = str(document["content_type"] or "").split(";", 1)[0].casefold()
    force_download = not extracted_text and source_type in {"text/html", "application/xhtml+xml", "image/svg+xml"}
    return send_file(
        path,
        mimetype=(
            "text/plain; charset=utf-8"
            if extracted_text
            else "application/octet-stream"
            if force_download
            else document["content_type"] or None
        ),
        as_attachment=force_download,
        download_name=path.name,
    )


@app.route("/contracts/documents/<int:document_id>/archive")
def archived_contract_document(document_id: int):
    return _send_archived_contract_file(document_id, "local_file_path")


@app.route("/contracts/documents/<int:document_id>/text")
def archived_contract_text(document_id: int):
    return _send_archived_contract_file(document_id, "extracted_text_path", extracted_text=True)


@app.route("/contracts/runs/<int:run_id>/cancel", methods=["POST"])
def cancel_contract_discovery_run(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT status FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        if run["status"] == "queued":
            conn.execute(
                "UPDATE contract_discovery_runs SET cancel_requested = 1, status = 'cancelled', finished_at = ?, error_message = 'Cancelled before start.' WHERE id = ?",
                (utc_now_iso(), run_id),
            )
            flash("Contract discovery run cancelled.", "success")
        elif run["status"] == "running":
            conn.execute(
                "UPDATE contract_discovery_runs SET cancel_requested = 1, error_message = 'Cancellation requested.' WHERE id = ?",
                (run_id,),
            )
            flash("Cancellation requested. Results already found will be kept.", "success")
        else:
            flash(f"Contract discovery run #{run_id} is already {run['status']}.", "info")
        conn.commit()
    return redirect(url_for("contract_discovery_run_detail", run_id=run_id))


@app.route("/contracts/packages/<int:package_id>/review", methods=["POST"])
def review_contract_package(package_id: int):
    unit_type = request.form.get("bargaining_unit_type", "unknown").strip()
    if unit_type not in UNIT_TYPES:
        abort(400)
    agreement_status = request.form.get("agreement_status", "unknown").strip()
    review_status = request.form.get("review_status", "unreviewed").strip()
    if agreement_status not in {"current", "expired", "future", "unknown"}:
        abort(400)
    if review_status not in {"unreviewed", "needs_review", "verified", "rejected"}:
        abort(400)
    effective_date = request.form.get("effective_date", "").strip() or None
    expiration_date = request.form.get("expiration_date", "").strip() or None
    for value in (effective_date, expiration_date):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            abort(400)
    with connect_db() as conn:
        package = conn.execute("SELECT discovery_run_id FROM district_contract_packages WHERE id = ?", (package_id,)).fetchone()
        if package is None:
            abort(404)
        conn.execute(
            """
            UPDATE district_contract_packages
            SET bargaining_unit_type = ?, bargaining_unit_name = ?, union_name = ?,
                effective_date = ?, expiration_date = ?, agreement_status = ?,
                review_status = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                unit_type,
                request.form.get("bargaining_unit_name", "").strip(),
                request.form.get("union_name", "").strip() or None,
                effective_date,
                expiration_date,
                agreement_status,
                review_status,
                request.form.get("notes", "").strip(),
                utc_now_iso(),
                package_id,
            ),
        )
        conn.commit()
        run_id = int(package["discovery_run_id"])
    flash("Contract package review saved.", "success")
    return redirect(url_for("contract_discovery_run_detail", run_id=run_id) + f"#package-{package_id}")


@app.route("/contracts/runs/<int:run_id>/export.csv")
def export_contract_discovery_run(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT id FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    return Response(
        export_contract_discovery_csv(run_id),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=edscanner-contract-run-{run_id}.csv"},
    )


@app.route("/contracts/runs/<int:run_id>/debug-log")
def contract_discovery_debug_log_file(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT debug_log_path FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or not run["debug_log_path"]:
        abort(404)
    log_path = Path(run["debug_log_path"]).resolve()
    log_root = CONTRACT_DISCOVERY_RUN_LOGS_DIR.resolve()
    if log_path != log_root and log_root not in log_path.parents:
        abort(404)
    if not log_path.is_file():
        abort(404)
    return send_file(log_path, mimetype="text/plain; charset=utf-8", as_attachment=False, download_name=log_path.name)


@app.route("/districts")
def districts_page():
    state = request.args.get("state", "").strip()
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "agency_name").strip()
    direction = request.args.get("direction", "asc").strip().lower()
    page = max(parse_optional_int(request.args.get("page")) or 1, 1)
    per_page = 100
    offset = (page - 1) * per_page
    sort_columns = {
        "agency_name": "agency_name COLLATE NOCASE",
        "state": "state COLLATE NOCASE",
        "agency_type": "agency_type COLLATE NOCASE",
        "total_enrollment_excludes_ae": "total_enrollment_excludes_ae",
        "website": "COALESCE(NULLIF(website, ''), website_normalized) COLLATE NOCASE",
    }
    if sort not in sort_columns:
        sort = "agency_name"
    if direction not in {"asc", "desc"}:
        direction = "asc"
    order_sql = f"{sort_columns[sort]} {direction.upper()}, agency_name COLLATE NOCASE ASC"
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if query:
        clauses.append("(agency_name LIKE ? OR agency_id_nces LIKE ? OR website LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    options = list_filter_options()
    with connect_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM districts{where_sql}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT id, agency_id_nces, agency_name, state, agency_type,
                   total_enrollment_excludes_ae, website, website_normalized,
                   has_searchable_website
            FROM districts
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
    return render_template(
        "districts.html",
        rows=rows,
        options=options,
        state=state,
        query=query,
        page=page,
        per_page=per_page,
        total=total,
        sort=sort,
        direction=direction,
    )


@app.route("/search")
def search_page():
    options = list_filter_options()
    states = selected_values("states")
    agency_types = selected_values("agency_types")
    min_enrollment = parse_optional_int(request.args.get("min_enrollment"))
    max_enrollment = parse_optional_int(request.args.get("max_enrollment"))
    max_districts = parse_optional_int(request.args.get("max_districts")) or MAX_TOTAL_DISTRICTS_PER_RUN
    max_pages_per_district = parse_optional_int(request.args.get("max_pages_per_district")) or MAX_PAGES_PER_DISTRICT
    max_workers = clamp_int(parse_optional_int(request.values.get("max_workers")), SEARCH_RUN_WORKERS, 1, 8)
    brave_key_present = has_brave_search_api_key()
    default_method = "brave" if brave_key_present else "crawler"
    search_method = normalize_search_method(request.values.get("search_method") or default_method)
    api_results_per_district = clamp_int(parse_optional_int(request.values.get("api_results_per_district")), 10, 1, 20)
    follow_depth = clamp_int(parse_optional_int(request.values.get("follow_depth")), 0, 0, 2)
    match_count = count_matching_districts(states, agency_types, min_enrollment, max_enrollment)
    district_profile_coverage = district_search_coverage(states, agency_types, min_enrollment, max_enrollment)
    estimated_api_calls = min(match_count, max_districts) if search_method in {"brave", "hybrid"} else 0
    estimated_brave_cost = estimated_api_calls * 0.005
    with connect_db() as conn:
        recent_runs = conn.execute(
            """
            SELECT id, query_text, status, districts_matched, districts_searched,
                   districts_failed, started_at, finished_at
            FROM search_runs
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
    return render_template(
        "search.html",
        options=options,
        selected_states=states,
        selected_agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        max_districts=max_districts,
        max_pages_per_district=max_pages_per_district,
        max_workers=max_workers,
        search_method=search_method,
        api_results_per_district=api_results_per_district,
        follow_depth=follow_depth,
        brave_key_present=brave_key_present,
        estimated_api_calls=estimated_api_calls,
        estimated_brave_cost=estimated_brave_cost,
        district_profile_coverage=district_profile_coverage,
        query_text=request.values.get("query_text", ""),
        match_count=match_count,
        recent_runs=recent_runs,
    )


@app.route("/search-profiles", methods=["GET", "POST"])
def search_profiles_page():
    options = list_filter_options()
    states = selected_values("states")
    agency_types = selected_values("agency_types")
    min_enrollment = parse_optional_int(request.values.get("min_enrollment"))
    max_enrollment = parse_optional_int(request.values.get("max_enrollment"))
    profile_statuses_selected = selected_profile_statuses()
    provider_guess = request.values.get("provider_guess", "").strip()
    max_districts = clamp_int(parse_optional_int(request.values.get("max_districts")), 10, 1, PROFILE_DISCOVERY_MAX_DISTRICTS)
    max_workers = clamp_int(parse_optional_int(request.values.get("max_workers")), PROFILE_DISCOVERY_WORKERS, 1, 8)
    test_query = request.values.get("test_query", "calendar").strip() or "calendar"
    force = request.values.get("force", "").casefold() in {"1", "true", "yes", "on"}

    if request.method == "POST":
        run_id = create_profile_discovery_run(
            states,
            agency_types,
            min_enrollment,
            max_enrollment,
            profile_statuses_selected,
            provider_guess,
            max_districts,
            max_workers,
            test_query,
            force,
        )
        enqueue_profile_discovery_run(run_id)
        return redirect(url_for("profile_discovery_run_detail", run_id=run_id))

    clauses: list[str] = ["d.has_searchable_website = 1"]
    params: list[Any] = []
    if states:
        clauses.append(f"d.state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if agency_types:
        clauses.append(f"d.agency_type IN ({','.join('?' for _ in agency_types)})")
        params.extend(agency_types)
    if min_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae >= ?")
        params.append(min_enrollment)
    if max_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae <= ?")
        params.append(max_enrollment)
    add_profile_status_filter_sql(clauses, params, profile_statuses_selected)
    if provider_guess:
        clauses.append("p.provider_guess = ?")
        params.append(provider_guess)
    where_sql = " WHERE " + " AND ".join(clauses)

    with connect_db() as conn:
        total_searchable = conn.execute(
            "SELECT COUNT(*) AS count FROM districts WHERE has_searchable_website = 1"
        ).fetchone()["count"]
        profiles_discovered = conn.execute(
            "SELECT COUNT(DISTINCT district_id) AS count FROM district_search_profiles"
        ).fetchone()["count"]
        status_counts = {
            row["profile_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT p.profile_status, COUNT(*) AS count
                FROM district_search_profiles p
                JOIN (
                    SELECT district_id, MAX(id) AS id
                    FROM district_search_profiles
                    GROUP BY district_id
                ) latest ON latest.id = p.id
                GROUP BY p.profile_status
                """
            )
        }
        providers = [
            row["provider_guess"]
            for row in conn.execute(
                """
                SELECT DISTINCT provider_guess
                FROM district_search_profiles
                WHERE provider_guess IS NOT NULL AND provider_guess != ''
                ORDER BY provider_guess
                """
            )
        ]
        last_discovered_at = conn.execute(
            "SELECT MAX(last_discovered_at) AS value FROM district_search_profiles"
        ).fetchone()["value"]
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM districts d
            LEFT JOIN (
                SELECT p1.*
                FROM district_search_profiles p1
                JOIN (
                    SELECT district_id, MAX(id) AS id
                    FROM district_search_profiles
                    GROUP BY district_id
                ) latest ON latest.id = p1.id
            ) p ON p.district_id = d.id
            {where_sql}
            """,
            params,
        ).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT d.id, d.agency_name, d.state, d.agency_type,
                   d.total_enrollment_excludes_ae, d.website_normalized,
                   p.profile_status, p.profile_type, p.provider_guess,
                   p.search_url_template, p.confidence, p.test_result_count,
                   p.last_discovered_at, p.error_message
            FROM districts d
            LEFT JOIN (
                SELECT p1.*
                FROM district_search_profiles p1
                JOIN (
                    SELECT district_id, MAX(id) AS id
                    FROM district_search_profiles
                    GROUP BY district_id
                ) latest ON latest.id = p1.id
            ) p ON p.district_id = d.id
            {where_sql}
            ORDER BY d.state, d.agency_name
            LIMIT 100
            """,
            params,
        ).fetchall()
        recent_profile_runs = conn.execute(
            """
            SELECT id, status, districts_matched, districts_planned,
                   districts_processed, profiles_working, profiles_failed,
                   profiles_manual_review, profiles_requires_javascript,
                   max_workers, started_at, finished_at
            FROM profile_discovery_runs
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

    summary = {
        "total_searchable": int(total_searchable or 0),
        "profiles_discovered": int(profiles_discovered or 0),
        "never_tested": max(0, int(total_searchable or 0) - int(profiles_discovered or 0)),
        "working": int(status_counts.get("working", 0) or 0),
        "manual_review": int(status_counts.get("manual_review", 0) or 0),
        "no_search_found": int(status_counts.get("no_search_found", 0) or 0),
        "requires_javascript": int(status_counts.get("requires_javascript", 0) or 0),
        "external_search_only": int(status_counts.get("external_search_only", 0) or 0),
        "errors": sum(
            int(status_counts.get(status, 0) or 0)
            for status in ("error", "search_found_but_failed", "blocked_by_robots", "blocked_by_challenge")
        ),
        "last_discovered_at": last_discovered_at,
    }
    return render_template(
        "search_profiles.html",
        options=options,
        rows=rows,
        total=total,
        summary=summary,
        profile_statuses=PROFILE_STATUSES,
        providers=providers,
        selected_states=states,
        selected_agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        profile_statuses_selected=profile_statuses_selected,
        provider_guess=provider_guess,
        max_districts=max_districts,
        profile_discovery_max_districts=PROFILE_DISCOVERY_MAX_DISTRICTS,
        max_workers=max_workers,
        test_query=test_query,
        force=force,
        recent_profile_runs=recent_profile_runs,
    )


@app.route("/search-profiles/runs/<int:run_id>")
def profile_discovery_run_detail(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT * FROM profile_discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        recent_profiles = conn.execute(
            """
            SELECT d.agency_name, d.state, d.agency_type,
                   p.profile_status, p.provider_guess, p.confidence,
                   p.test_result_count, p.search_url_template, p.error_message,
                   p.last_discovered_at
            FROM district_search_profiles p
            JOIN districts d ON d.id = p.district_id
            WHERE p.last_discovered_at >= ?
            ORDER BY p.id DESC
            LIMIT 50
            """,
            (run["started_at"],),
        ).fetchall()
    planned = int(run["districts_planned"] or 0)
    processed = int(run["districts_processed"] or 0)
    in_progress_count = 1 if run["status"] == "running" and processed < planned else 0
    left_count = max(0, planned - processed - in_progress_count)
    return render_template(
        "profile_discovery_run_detail.html",
        run=run,
        recent_profiles=recent_profiles,
        states=json.loads(run["states_json"] or "[]"),
        agency_types=json.loads(run["agency_types_json"] or "[]"),
        elapsed=elapsed_seconds(run["started_at"], run["finished_at"]),
        in_progress_count=in_progress_count,
        left_count=left_count,
    )


@app.route("/search-profiles/runs/<int:run_id>/cancel", methods=["POST"])
def cancel_profile_discovery_run(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT status FROM profile_discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        status = run["status"]
        if status == "queued":
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET cancel_requested = 1,
                    status = 'cancelled',
                    finished_at = ?,
                    error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (utc_now_iso(), run_id),
            )
            flash("Profile discovery run cancelled.", "success")
        elif status == "running":
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET cancel_requested = 1,
                    error_message = 'Cancellation requested. The current district will finish before the run stops.'
                WHERE id = ?
                """,
                (run_id,),
            )
            flash("Cancellation requested. Progress already saved will be kept.", "success")
        else:
            flash(f"Profile discovery run #{run_id} is already {status}.", "info")
        conn.commit()
    return redirect(url_for("profile_discovery_run_detail", run_id=run_id))


@app.route("/search-profiles/runs/<int:run_id>/debug-log")
def profile_discovery_debug_log_file(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT debug_log_path FROM profile_discovery_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    if not run["debug_log_path"]:
        abort(404)

    log_path = Path(run["debug_log_path"]).resolve()
    log_root = PROFILE_DISCOVERY_RUN_LOGS_DIR.resolve()
    if log_path != log_root and log_root not in log_path.parents:
        abort(404)
    if not log_path.exists() or not log_path.is_file():
        abort(404)
    return send_file(
        log_path,
        mimetype="text/plain; charset=utf-8",
        as_attachment=False,
        download_name=log_path.name,
    )


@app.route("/search/run", methods=["POST"])
def run_search_route():
    query_text = request.form.get("query_text", "").strip()
    states = selected_values("states")
    agency_types = selected_values("agency_types")
    min_enrollment = parse_optional_int(request.form.get("min_enrollment"))
    max_enrollment = parse_optional_int(request.form.get("max_enrollment"))
    max_districts = clamp_int(
        parse_optional_int(request.form.get("max_districts")),
        MAX_TOTAL_DISTRICTS_PER_RUN,
        1,
        MAX_TOTAL_DISTRICTS_PER_RUN,
    )
    max_pages_per_district = clamp_int(
        parse_optional_int(request.form.get("max_pages_per_district")),
        MAX_PAGES_PER_DISTRICT,
        1,
        500,
    )
    search_method = normalize_search_method(request.form.get("search_method"))
    api_results_per_district = clamp_int(parse_optional_int(request.form.get("api_results_per_district")), 10, 1, 20)
    follow_depth = clamp_int(parse_optional_int(request.form.get("follow_depth")), 0, 0, 2)
    max_workers = clamp_int(parse_optional_int(request.form.get("max_workers")), SEARCH_RUN_WORKERS, 1, 8)
    debug_logging = request.form.get("debug_logging", "").casefold() in {"1", "true", "yes", "on"}
    if not query_text:
        flash("Search text is required.", "error")
        return redirect(url_for("search_page"))
    if search_method in {"brave", "hybrid"} and not has_brave_search_api_key():
        flash("Save a Brave Search API key in Settings before using Brave or Hybrid search.", "error")
        return redirect(url_for("search_page"))
    try:
        run_id = create_search_run(
            query_text,
            states=states,
            agency_types=agency_types,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            max_districts=max_districts,
            max_workers=max_workers,
            debug_logging=debug_logging,
            settings=SearchSettings(
                max_pages_per_district=max_pages_per_district,
                search_method=search_method,
                api_results_per_district=api_results_per_district,
                follow_depth=follow_depth,
                brave_api_key=get_local_setting(BRAVE_SEARCH_API_KEY_ENV),
            ),
            status="queued",
        )
        enqueue_search_run(run_id)
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("search_page"))
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<int:run_id>")
def run_detail(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        result_rows = conn.execute(
            """
            SELECT *
            FROM search_results
            WHERE search_run_id = ?
            ORDER BY district_name, state, result_rank
            """,
            (run_id,),
        ).fetchall()
        result_count = conn.execute(
            "SELECT COUNT(*) AS count FROM search_results WHERE search_run_id = ?",
            (run_id,),
        ).fetchone()["count"]
    groups: OrderedDict[tuple[int, str, str], list[Any]] = OrderedDict()
    for row in result_rows:
        key = (row["district_id"], row["district_name"], row["state"])
        groups.setdefault(key, []).append(row)
    no_result_count = max(0, int(run["districts_searched"] or 0) - len(groups))
    planned_districts = min(
        int(run["districts_matched"] or 0),
        int(run["max_districts"] or run["districts_matched"] or 0),
    )
    in_progress_count = 0
    if run["status"] == "running" and int(run["districts_searched"] or 0) < planned_districts:
        in_progress_count = min(int(run["max_workers"] or SEARCH_RUN_WORKERS), planned_districts - int(run["districts_searched"] or 0))
    left_count = max(0, planned_districts - int(run["districts_searched"] or 0) - in_progress_count)
    return render_template(
        "run_detail.html",
        run=run,
        groups=groups,
        result_count=result_count,
        no_result_count=no_result_count,
        planned_districts=planned_districts,
        in_progress_count=in_progress_count,
        left_count=left_count,
        elapsed=elapsed_seconds(run["started_at"], run["finished_at"]),
        states=json.loads(run["states_json"] or "[]"),
        agency_types=json.loads(run["agency_types_json"] or "[]"),
    )


@app.route("/runs/<int:run_id>/cancel", methods=["POST"])
def cancel_run(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT status FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        status = run["status"]
        if status == "queued":
            conn.execute(
                """
                UPDATE search_runs
                SET cancel_requested = 1,
                    status = 'cancelled',
                    finished_at = ?,
                    error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (utc_now_iso(), run_id),
            )
            flash("Search run cancelled.", "success")
        elif status == "running":
            conn.execute(
                """
                UPDATE search_runs
                SET cancel_requested = 1,
                    error_message = 'Cancellation requested. The current page or district will finish before the run stops.'
                WHERE id = ?
                """,
                (run_id,),
            )
            flash("Cancellation requested. Progress already saved will be kept.", "success")
        else:
            flash(f"Run #{run_id} is already {status}.", "info")
        conn.commit()
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<int:run_id>/debug-log")
def debug_log_file(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT debug_log_path FROM search_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    if not run["debug_log_path"]:
        abort(404)

    log_path = Path(run["debug_log_path"]).resolve()
    log_root = SEARCH_RUN_LOGS_DIR.resolve()
    if log_path != log_root and log_root not in log_path.parents:
        abort(404)
    if not log_path.exists() or not log_path.is_file():
        abort(404)
    return send_file(
        log_path,
        mimetype="text/plain; charset=utf-8",
        as_attachment=False,
        download_name=log_path.name,
    )


@app.route("/runs/<int:run_id>/export.csv")
def export_run(run_id: int):
    with connect_db() as conn:
        run = conn.execute("SELECT id FROM search_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    csv_text = export_search_run_csv(run_id)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=edscanner-search-run-{run_id}.csv"},
    )


if __name__ == "__main__":
    debug = os.getenv("EDSCANNER_FLASK_DEBUG", "").casefold() in {"1", "true", "yes", "on"}
    host = (os.getenv("EDSCANNER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = clamp_int(parse_optional_int(os.getenv("EDSCANNER_PORT")), 8765, 1, 65535)
    app.run(host=host, port=port, debug=debug, use_reloader=False)
