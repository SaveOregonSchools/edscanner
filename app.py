from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for
from markupsafe import Markup, escape

from common import (
    BRAVE_SEARCH_API_KEY_ENV,
    IMPORTS_DIR,
    MAX_PAGES_PER_DISTRICT,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    SEARCH_RUN_LOGS_DIR,
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
from import_districts import ImportErrorWithContext, import_districts
from search_engine import (
    SearchSettings,
    count_matching_districts,
    create_search_run,
    execute_search_run,
    export_search_run_csv,
    normalize_search_method,
    parse_optional_int,
)


configure_logging()
init_db()

app = Flask(__name__)
app.config["SECRET_KEY"] = "edscanner-local-dev"
LOGGER = logging.getLogger(__name__)
SEARCH_QUEUE: queue.Queue[int] = queue.Queue()
PROFILE_DISCOVERY_QUEUE: queue.Queue[int] = queue.Queue()
WORKER_STARTED = False
PROFILE_DISCOVERY_WORKER_STARTED = False


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


def selected_values(name: str) -> list[str]:
    return [value for value in request.values.getlist(name) if str(value).strip()]


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
    profile_status: str,
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
    if profile_status == "__never__":
        clauses.append("p.id IS NULL")
    elif profile_status:
        clauses.append("p.profile_status = ?")
        params.append(profile_status)
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
    profile_status: str,
    provider_guess: str,
) -> int:
    return len(
        list_profile_filtered_districts(
            states,
            agency_types,
            min_enrollment,
            max_enrollment,
            profile_status,
            provider_guess,
            MAX_TOTAL_DISTRICTS_PER_RUN * 1000,
        )
    )


def create_profile_discovery_run(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_status: str,
    provider_guess: str,
    max_districts: int,
    test_query: str,
    force: bool,
) -> int:
    matched_count = count_profile_filtered_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        profile_status,
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
                test_query, force, cancel_requested, status, districts_matched,
                districts_planned, districts_processed, profiles_working,
                profiles_failed, profiles_manual_review, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', ?, ?, 0, 0, 0, 0, ?)
            """,
            (
                json.dumps(states),
                json.dumps(agency_types),
                min_enrollment,
                max_enrollment,
                profile_status,
                provider_guess,
                max_districts,
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
        run["profile_status_filter"] or "",
        run["provider_guess_filter"] or "",
        max_districts,
    )

    processed = 0
    working = 0
    failed = 0
    manual_review = 0
    cancelled = False
    settings = SearchSettings(max_pages_per_district=10)
    try:
        with connect_db() as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'running',
                    districts_planned = ?,
                    districts_processed = 0,
                    profiles_working = 0,
                    profiles_failed = 0,
                    profiles_manual_review = 0,
                    finished_at = NULL,
                    error_message = NULL
                WHERE id = ?
                """,
                (len(districts), run_id),
            )
            conn.commit()

        for district in districts:
            if is_profile_discovery_cancel_requested(run_id):
                cancelled = True
                break
            try:
                profile = discover_district_search_profile(
                    district,
                    test_query=run["test_query"] or "calendar",
                    settings=settings,
                    force=bool(run["force"]),
                )
                status = profile.get("profile_status") or "error"
                if status == "working":
                    working += 1
                elif status == "manual_review":
                    manual_review += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                LOGGER.exception("Profile discovery failed for %s: %s", district.get("agency_name"), exc)
            processed += 1
            with connect_db() as conn:
                conn.execute(
                    """
                    UPDATE profile_discovery_runs
                    SET districts_processed = ?,
                        profiles_working = ?,
                        profiles_failed = ?,
                        profiles_manual_review = ?
                    WHERE id = ?
                    """,
                    (processed, working, failed, manual_review, run_id),
                )
                conn.commit()

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
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (final_status, processed, working, failed, manual_review, utc_now_iso(), error_message, run_id),
            )
            conn.commit()
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
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (processed, working, failed, manual_review, utc_now_iso(), str(exc), run_id),
            )
            conn.commit()
        raise


app.jinja_env.filters["fmt_int"] = fmt_int
app.jinja_env.filters["fmt_dt"] = fmt_dt
app.jinja_env.filters["highlight"] = highlight


def enqueue_search_run(run_id: int) -> None:
    SEARCH_QUEUE.put(run_id)
    LOGGER.info("Queued search run %s", run_id)


def enqueue_profile_discovery_run(run_id: int) -> None:
    PROFILE_DISCOVERY_QUEUE.put(run_id)
    LOGGER.info("Queued profile discovery run %s", run_id)


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


if os.getenv("EDSCANNER_DISABLE_WORKER", "").casefold() not in {"1", "true", "yes", "on"}:
    start_search_worker()
    start_profile_discovery_worker()


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
        action = request.form.get("action", "save")
        if action == "clear_brave_key":
            set_local_setting(BRAVE_SEARCH_API_KEY_ENV, "")
            flash("Brave Search API key cleared.", "success")
        else:
            api_key = request.form.get("brave_api_key", "").strip()
            if not api_key:
                flash("Enter a Brave Search API key or use Clear key.", "error")
            else:
                set_local_setting(BRAVE_SEARCH_API_KEY_ENV, api_key)
                flash("Brave Search API key saved to the local .env file.", "success")
        return redirect(url_for("settings_page"))

    key_present = has_brave_search_api_key()
    key = get_local_setting(BRAVE_SEARCH_API_KEY_ENV)
    masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "saved" if key_present else ""
    return render_template(
        "settings.html",
        key_present=key_present,
        masked_key=masked_key,
        env_key_name=BRAVE_SEARCH_API_KEY_ENV,
    )


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
    profile_status = request.values.get("profile_status", "").strip()
    provider_guess = request.values.get("provider_guess", "").strip()
    max_districts = clamp_int(parse_optional_int(request.values.get("max_districts")), 10, 1, 100)
    test_query = request.values.get("test_query", "calendar").strip() or "calendar"
    force = request.values.get("force", "").casefold() in {"1", "true", "yes", "on"}

    if request.method == "POST":
        run_id = create_profile_discovery_run(
            states,
            agency_types,
            min_enrollment,
            max_enrollment,
            profile_status,
            provider_guess,
            max_districts,
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
    if profile_status == "__never__":
        clauses.append("p.id IS NULL")
    elif profile_status:
        clauses.append("p.profile_status = ?")
        params.append(profile_status)
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
                   profiles_manual_review, started_at, finished_at
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
        profile_status=profile_status,
        provider_guess=provider_guess,
        max_districts=max_districts,
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
    in_progress_count = 1 if run["status"] == "running" and int(run["districts_searched"] or 0) < planned_districts else 0
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
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=False)
