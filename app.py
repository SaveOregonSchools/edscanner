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
    IMPORTS_DIR,
    MAX_PAGES_PER_DISTRICT,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    SEARCH_RUN_LOGS_DIR,
    collect_db_stats,
    configure_logging,
    connect_db,
    current_db_path,
    discover_import_files,
    init_db,
    list_filter_options,
    utc_now_iso,
)
from import_districts import ImportErrorWithContext, import_districts
from search_engine import (
    SearchSettings,
    count_matching_districts,
    create_search_run,
    execute_search_run,
    export_search_run_csv,
    parse_optional_int,
)


configure_logging()
init_db()

app = Flask(__name__)
app.config["SECRET_KEY"] = "edscanner-local-dev"
LOGGER = logging.getLogger(__name__)
SEARCH_QUEUE: queue.Queue[int] = queue.Queue()
WORKER_STARTED = False


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


app.jinja_env.filters["fmt_int"] = fmt_int
app.jinja_env.filters["fmt_dt"] = fmt_dt
app.jinja_env.filters["highlight"] = highlight


def enqueue_search_run(run_id: int) -> None:
    SEARCH_QUEUE.put(run_id)
    LOGGER.info("Queued search run %s", run_id)


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


if os.getenv("EDSCANNER_DISABLE_WORKER", "").casefold() not in {"1", "true", "yes", "on"}:
    start_search_worker()


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
    match_count = count_matching_districts(states, agency_types, min_enrollment, max_enrollment)
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
        query_text=request.values.get("query_text", ""),
        match_count=match_count,
        recent_runs=recent_runs,
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
    debug_logging = request.form.get("debug_logging", "").casefold() in {"1", "true", "yes", "on"}
    if not query_text:
        flash("Search text is required.", "error")
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
            settings=SearchSettings(max_pages_per_district=max_pages_per_district),
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
