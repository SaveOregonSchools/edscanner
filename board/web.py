from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, url_for

from board.runs import (
    board_discovery_preview,
    board_sync_preview,
    create_board_discovery_run,
    create_board_sync_run,
    execute_board_discovery_run,
    execute_board_sync_run,
)
from board.search import count_board_content, search_board_content
from common import (
    BOARD_DISCOVERY_RUN_LOGS_DIR,
    BOARD_DOCUMENTS_DIR,
    BOARD_SNAPSHOTS_DIR,
    BOARD_SYNC_RUN_LOGS_DIR,
    BOARD_WORKERS,
    connect_db,
    init_db,
    list_filter_options,
    utc_now_iso,
)


LOGGER = logging.getLogger(__name__)
bp = Blueprint("boards", __name__, url_prefix="/school-boards")
BOARD_RUN_QUEUE: queue.Queue[tuple[str, int]] = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False

PLATFORMS = ["boardbook", "diligent_community", "boarddocs", "simbli", "civicclerk", "generic"]
SOURCE_STATUSES = [
    "working",
    "not_found",
    "manual_review",
    "requires_javascript",
    "blocked_by_challenge",
    "blocked_by_robots",
    "platform_changed_or_parser_broken",
    "error",
]
PAGE_SIZE = 50


def _optional_int(value: Any) -> int | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _optional_int(value)
    return max(minimum, min(maximum, parsed if parsed is not None else default))


def _selected(name: str) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in request.values.getlist(name) if value.strip()))


def _valid_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        abort(400, description=f"Invalid date: {text}")


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _elapsed(started_at: Any, finished_at: Any) -> str:
    if not started_at:
        return "Not started"
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            if finished_at
            else datetime.now(timezone.utc)
        )
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except ValueError:
        return "Unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _page_url(endpoint: str, **fixed: Any) -> Callable[[int], str]:
    values = request.args.to_dict(flat=False)

    def build(page: int) -> str:
        params: list[tuple[str, str]] = []
        for key, entries in values.items():
            if key == "page":
                continue
            params.extend((key, str(entry)) for entry in entries)
        params.append(("page", str(page)))
        return f"{url_for(endpoint, **fixed)}?{urlencode(params)}"

    return build


def enqueue_discovery_run(run_id: int) -> None:
    BOARD_RUN_QUEUE.put(("discovery", int(run_id)))
    LOGGER.info("Queued board discovery run %s", run_id)


def enqueue_sync_run(run_id: int) -> None:
    BOARD_RUN_QUEUE.put(("sync", int(run_id)))
    LOGGER.info("Queued board sync run %s", run_id)


def _board_worker() -> None:
    while True:
        kind, run_id = BOARD_RUN_QUEUE.get()
        try:
            table = "board_discovery_runs" if kind == "discovery" else "board_sync_runs"
            with connect_db() as conn:
                row = conn.execute(f"SELECT status FROM {table} WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["status"] != "queued":
                continue
            if kind == "discovery":
                execute_board_discovery_run(run_id)
            else:
                execute_board_sync_run(run_id)
        except Exception:
            LOGGER.exception("Queued board %s run %s failed", kind, run_id)
        finally:
            BOARD_RUN_QUEUE.task_done()


def start_board_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        init_db()
        with connect_db() as conn:
            conn.execute(
                "UPDATE board_discovery_runs SET status = 'queued', error_message = 'Run resumed after app restart.' WHERE status = 'running'"
            )
            conn.execute(
                "UPDATE board_discovery_run_items SET status = 'queued', started_at = NULL WHERE status = 'running'"
            )
            conn.execute(
                "UPDATE board_sync_runs SET status = 'queued', error_message = 'Run resumed after app restart.' WHERE status = 'running'"
            )
            conn.execute(
                "UPDATE board_sync_run_items SET status = 'queued', started_at = NULL WHERE status = 'running'"
            )
            queued = [
                (str(row["kind"]), int(row["id"]))
                for row in conn.execute(
                    """
                    SELECT 'discovery' AS kind, id, queued_at FROM board_discovery_runs WHERE status = 'queued'
                    UNION ALL
                    SELECT 'sync' AS kind, id, queued_at FROM board_sync_runs WHERE status = 'queued'
                    ORDER BY queued_at, id
                    """
                )
            ]
            conn.commit()
        thread = threading.Thread(target=_board_worker, name="EdScannerBoardWorker", daemon=True)
        thread.start()
        _WORKER_STARTED = True
        for work in queued:
            BOARD_RUN_QUEUE.put(work)


@bp.route("")
def overview() -> str:
    with connect_db() as conn:
        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM districts) AS total_districts,
                (SELECT COUNT(DISTINCT district_id) FROM board_sources WHERE is_active = 1) AS source_districts,
                (SELECT COUNT(*) FROM board_sources WHERE is_active = 1 AND source_status = 'working') AS working_sources,
                (SELECT COUNT(*) FROM board_sources WHERE is_active = 1 AND source_status IN ('manual_review', 'requires_javascript')) AS manual_review,
                (SELECT COUNT(*) FROM board_sources WHERE is_active = 1 AND source_status IN ('error', 'blocked_by_challenge', 'blocked_by_robots', 'platform_changed_or_parser_broken')) AS failed_sources,
                (SELECT COUNT(*) FROM board_meetings) AS meetings,
                (SELECT COUNT(*) FROM board_documents) AS documents
            """
        ).fetchone()
        summary = dict(totals)
        summary["unchecked_districts"] = max(
            0, int(summary["total_districts"] or 0) - int(summary["source_districts"] or 0)
        )
        platform_rows = conn.execute(
            """
            SELECT bs.platform, COUNT(DISTINCT bs.district_id) AS districts,
                   SUM(CASE WHEN bs.source_status = 'working' THEN 1 ELSE 0 END) AS working,
                   SUM(CASE WHEN bs.source_status IN ('error', 'blocked_by_challenge', 'blocked_by_robots', 'platform_changed_or_parser_broken') THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN bs.source_status IN ('manual_review', 'requires_javascript') THEN 1 ELSE 0 END) AS manual_review,
                   MAX(bs.last_successful_sync_at) AS last_sync,
                   (
                       SELECT COUNT(*) FROM board_sync_run_items sri
                       JOIN board_sources failed_source ON failed_source.id = sri.board_source_id
                       WHERE failed_source.platform = bs.platform
                         AND sri.status IN ('failed', 'error', 'completed_with_errors')
                         AND datetime(sri.finished_at) >= datetime('now', '-1 day')
                   ) AS recent_failures
            FROM board_sources bs WHERE bs.is_active = 1
            GROUP BY bs.platform ORDER BY districts DESC, bs.platform
            """
        ).fetchall()
        discovery_runs = conn.execute(
            "SELECT * FROM board_discovery_runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
        sync_runs = conn.execute("SELECT * FROM board_sync_runs ORDER BY id DESC LIMIT 5").fetchall()
    return render_template(
        "board_overview.html",
        summary=summary,
        platform_rows=platform_rows,
        discovery_runs=discovery_runs,
        sync_runs=sync_runs,
    )


@bp.route("/sources")
def sources() -> str:
    state = request.args.get("state", "").strip()
    agency_type = request.args.get("agency_type", "").strip()
    platform = request.args.get("platform", "").strip().casefold()
    source_status = request.args.get("source_status", "").strip().casefold()
    min_enrollment = _optional_int(request.args.get("min_enrollment"))
    max_enrollment = _optional_int(request.args.get("max_enrollment"))
    page = _clamp(request.args.get("page"), 1, 1, 1_000_000)
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("d.state = ?")
        params.append(state)
    if agency_type:
        clauses.append("d.agency_type = ?")
        params.append(agency_type)
    if platform:
        clauses.append("bs.platform = ?")
        params.append(platform)
    if source_status == "__unchecked__":
        clauses.append("bs.id IS NULL")
    elif source_status:
        clauses.append("bs.source_status = ?")
        params.append(source_status)
    if min_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae >= ?")
        params.append(min_enrollment)
    if max_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae <= ?")
        params.append(max_enrollment)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    join_sql = """
        LEFT JOIN board_sources bs ON bs.id = (
            SELECT candidate.id FROM board_sources candidate
            WHERE candidate.district_id = d.id
            ORDER BY candidate.is_active DESC, candidate.updated_at DESC, candidate.id DESC LIMIT 1
        )
    """
    with connect_db() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS count FROM districts d {join_sql} {where_sql}", params).fetchone()["count"]
            or 0
        )
        rows = conn.execute(
            f"""
            SELECT d.*, bs.platform, bs.source_status, bs.source_url, bs.confidence,
                   bs.error_message, bs.last_checked_at, bs.last_successful_sync_at
            FROM districts d {join_sql} {where_sql}
            ORDER BY d.state, d.agency_name, d.id LIMIT ? OFFSET ?
            """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
    return render_template(
        "board_sources.html",
        rows=rows,
        total=total,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        page=page,
        page_url=_page_url("boards.sources"),
        options=list_filter_options(),
        platforms=PLATFORMS,
        source_statuses=SOURCE_STATUSES,
        state=state,
        agency_type=agency_type,
        platform=platform,
        source_status=source_status,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
    )


@bp.route("/discover", methods=["GET", "POST"])
def discovery() -> str | Response:
    selected_states = _selected("states")
    selected_agency_types = _selected("agency_types")
    min_enrollment = _optional_int(request.values.get("min_enrollment"))
    max_enrollment = _optional_int(request.values.get("max_enrollment"))
    platform_filter = request.values.get("platform_filter", "").strip().casefold()
    status_filter = request.values.get("status_filter", "__unchecked__").strip().casefold()
    max_districts = _clamp(request.values.get("max_districts"), 1000, 1, 1000)
    max_workers = _clamp(request.values.get("max_workers"), BOARD_WORKERS, 1, 8)
    force = request.form.get("force") == "1" if request.method == "POST" else False
    debug_logging = request.form.get("debug_logging") == "1" if request.method == "POST" else True
    if request.method == "POST":
        if force and status_filter == "__unchecked__":
            status_filter = ""
        run_id = create_board_discovery_run(
            states=selected_states,
            agency_types=selected_agency_types,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            platform_filter=platform_filter,
            status_filter=status_filter,
            max_districts=max_districts,
            max_workers=max_workers,
            force=force,
            debug_logging=debug_logging,
        )
        enqueue_discovery_run(run_id)
        flash(f"Board source discovery #{run_id} was queued.", "success")
        return redirect(url_for("boards.discovery_run_detail", run_id=run_id))
    matching_count = board_discovery_preview(
        states=selected_states,
        agency_types=selected_agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platform_filter=platform_filter,
        status_filter=status_filter,
        force=force,
    )
    with connect_db() as conn:
        recent_runs = conn.execute(
            "SELECT * FROM board_discovery_runs ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return render_template(
        "board_discovery.html",
        matching_count=matching_count,
        recent_runs=recent_runs,
        options=list_filter_options(),
        platforms=PLATFORMS,
        source_statuses=SOURCE_STATUSES,
        selected_states=selected_states,
        selected_agency_types=selected_agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platform_filter=platform_filter,
        status_filter=status_filter,
        max_districts=max_districts,
        max_workers=max_workers,
        force=force,
        debug_logging=debug_logging,
    )


@bp.route("/sync", methods=["GET", "POST"])
def sync() -> str | Response:
    selected_states = _selected("states")
    selected_agency_types = _selected("agency_types")
    selected_platforms = _selected("platforms")
    source_status = request.values.get("source_status", "working").strip().casefold()
    sync_mode = request.values.get("sync_mode", "monitor").strip().casefold()
    if sync_mode not in {"monitor", "backfill", "discovery_only"}:
        abort(400)
    min_enrollment = _optional_int(request.values.get("min_enrollment"))
    max_enrollment = _optional_int(request.values.get("max_enrollment"))
    date_from = _valid_date(request.values.get("date_from"))
    date_to = _valid_date(request.values.get("date_to"))
    if date_from and date_to and date_from > date_to:
        abort(400, description="The start date must not be after the end date.")
    last_sync_age_days = _optional_int(request.values.get("last_sync_age_days"))
    last_sync_before = None
    if last_sync_age_days is not None:
        last_sync_before = (
            datetime.now(timezone.utc) - timedelta(days=max(0, last_sync_age_days))
        ).isoformat(timespec="seconds")
    max_districts = _clamp(request.values.get("max_districts"), 1000, 1, 1000)
    max_workers = _clamp(request.values.get("max_workers"), BOARD_WORKERS, 1, 8)
    force = request.form.get("force") == "1" if request.method == "POST" else False
    debug_logging = request.form.get("debug_logging") == "1" if request.method == "POST" else True
    if request.method == "POST":
        if sync_mode == "discovery_only":
            run_id = create_board_discovery_run(
                states=selected_states,
                agency_types=selected_agency_types,
                min_enrollment=min_enrollment,
                max_enrollment=max_enrollment,
                status_filter="" if force else "__unchecked__",
                max_districts=max_districts,
                max_workers=max_workers,
                force=force,
                debug_logging=debug_logging,
            )
            enqueue_discovery_run(run_id)
            flash(f"Board source discovery #{run_id} was queued.", "success")
            return redirect(url_for("boards.discovery_run_detail", run_id=run_id))
        run_id = create_board_sync_run(
            states=selected_states,
            agency_types=selected_agency_types,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            platforms=selected_platforms,
            source_status=source_status,
            last_sync_before=last_sync_before,
            date_from=date_from,
            date_to=date_to,
            sync_mode=sync_mode,
            force=force,
            max_districts=max_districts,
            max_workers=max_workers,
            debug_logging=debug_logging,
        )
        enqueue_sync_run(run_id)
        flash(f"Board sync #{run_id} was queued.", "success")
        return redirect(url_for("boards.sync_run_detail", run_id=run_id))
    preview = board_sync_preview(
        states=selected_states,
        agency_types=selected_agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platforms=selected_platforms,
        source_status=source_status,
        last_sync_before=last_sync_before,
    )
    with connect_db() as conn:
        recent_runs = conn.execute("SELECT * FROM board_sync_runs ORDER BY id DESC LIMIT 20").fetchall()
    return render_template(
        "board_sync.html",
        preview=preview,
        recent_runs=recent_runs,
        options=list_filter_options(),
        platforms=PLATFORMS,
        source_statuses=SOURCE_STATUSES,
        selected_states=selected_states,
        selected_agency_types=selected_agency_types,
        selected_platforms=selected_platforms,
        source_status=source_status,
        sync_mode=sync_mode,
        last_sync_age_days=last_sync_age_days,
        date_from=date_from,
        date_to=date_to,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        max_districts=max_districts,
        max_workers=max_workers,
        force=force,
        debug_logging=debug_logging,
    )


@bp.route("/discovery-runs/<int:run_id>")
def discovery_run_detail(run_id: int) -> str:
    with connect_db() as conn:
        run = conn.execute("SELECT * FROM board_discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        items = conn.execute(
            """
            SELECT i.*, d.agency_name, d.state, bs.platform, bs.source_url,
                   bs.error_message AS source_error
            FROM board_discovery_run_items i
            JOIN districts d ON d.id = i.district_id
            LEFT JOIN board_sources bs ON bs.id = i.board_source_id
            WHERE i.run_id = ? ORDER BY i.id
            """,
            (run_id,),
        ).fetchall()
    return render_template(
        "board_discovery_run_detail.html",
        run=run,
        items=items,
        states=_json_list(run["states_json"]),
        agency_types=_json_list(run["agency_types_json"]),
        elapsed=_elapsed(run["started_at"], run["finished_at"]),
    )


@bp.route("/sync-runs/<int:run_id>")
def sync_run_detail(run_id: int) -> str:
    with connect_db() as conn:
        run = conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        items = conn.execute(
            """
            SELECT i.*, d.agency_name, d.state, bs.platform
            FROM board_sync_run_items i
            JOIN districts d ON d.id = i.district_id
            JOIN board_sources bs ON bs.id = i.board_source_id
            WHERE i.run_id = ? ORDER BY i.id
            """,
            (run_id,),
        ).fetchall()
    return render_template(
        "board_sync_run_detail.html",
        run=run,
        items=items,
        states=_json_list(run["states_json"]),
        agency_types=_json_list(run["agency_types_json"]),
        platforms=_json_list(run["platforms_json"]),
        elapsed=_elapsed(run["started_at"], run["finished_at"]),
    )


def _cancel_run(kind: str, run_id: int) -> Response:
    if kind == "discovery":
        table, item_table, endpoint = (
            "board_discovery_runs",
            "board_discovery_run_items",
            "boards.discovery_run_detail",
        )
    else:
        table, item_table, endpoint = "board_sync_runs", "board_sync_run_items", "boards.sync_run_detail"
    with connect_db() as conn:
        run = conn.execute(f"SELECT status FROM {table} WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        if run["status"] == "queued":
            conn.execute(
                f"UPDATE {table} SET cancel_requested = 1, status = 'cancelled', finished_at = ? WHERE id = ?",
                (utc_now_iso(), run_id),
            )
            conn.execute(
                f"UPDATE {item_table} SET status = 'cancelled', finished_at = ? WHERE run_id = ? AND status = 'queued'",
                (utc_now_iso(), run_id),
            )
        elif run["status"] == "running":
            conn.execute(f"UPDATE {table} SET cancel_requested = 1 WHERE id = ?", (run_id,))
        conn.commit()
    flash(f"Cancellation requested for board {kind} #{run_id}.", "success")
    return redirect(url_for(endpoint, run_id=run_id))


@bp.post("/discovery-runs/<int:run_id>/cancel")
def cancel_discovery_run(run_id: int) -> Response:
    return _cancel_run("discovery", run_id)


@bp.post("/sync-runs/<int:run_id>/cancel")
def cancel_sync_run(run_id: int) -> Response:
    return _cancel_run("sync", run_id)


def _run_log(table: str, run_id: int, root: Path) -> Response:
    with connect_db() as conn:
        run = conn.execute(f"SELECT debug_log_path FROM {table} WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        abort(404)
    if not run["debug_log_path"]:
        abort(404)
    path = Path(run["debug_log_path"]).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    with path.open("rb") as handle:
        if path.stat().st_size > 512_000:
            handle.seek(-512_000, 2)
        content = handle.read().decode("utf-8", errors="replace")
    return Response(content, mimetype="text/plain")


@bp.get("/discovery-runs/<int:run_id>/debug-log")
def discovery_run_log(run_id: int) -> Response:
    return _run_log("board_discovery_runs", run_id, BOARD_DISCOVERY_RUN_LOGS_DIR)


@bp.get("/sync-runs/<int:run_id>/debug-log")
def sync_run_log(run_id: int) -> Response:
    return _run_log("board_sync_runs", run_id, BOARD_SYNC_RUN_LOGS_DIR)


@bp.route("/meetings")
def meetings() -> str:
    filters = {
        key: request.args.get(key, "").strip()
        for key in (
            "q",
            "district",
            "state",
            "platform",
            "meeting_type",
            "date_from",
            "date_to",
            "has_agenda",
            "has_minutes",
            "has_attachments",
            "has_video",
            "changed",
        )
    }
    page = _clamp(request.args.get("page"), 1, 1, 1_000_000)
    clauses: list[str] = []
    params: list[Any] = []
    if filters["q"]:
        clauses.append("(m.title LIKE ? OR EXISTS (SELECT 1 FROM board_search_content sc WHERE sc.meeting_id = m.id AND (sc.title LIKE ? OR sc.body LIKE ?)))")
        term = f"%{filters['q']}%"
        params.extend([term, term, term])
    if filters["district"]:
        clauses.append("d.agency_name LIKE ?")
        params.append(f"%{filters['district']}%")
    for key, column in (("state", "d.state"), ("platform", "m.platform"), ("meeting_type", "m.meeting_type")):
        if filters[key]:
            clauses.append(f"{column} = ?")
            params.append(filters[key])
    if filters["date_from"]:
        clauses.append("m.meeting_date >= ?")
        params.append(_valid_date(filters["date_from"]))
    if filters["date_to"]:
        clauses.append("m.meeting_date <= ?")
        params.append(_valid_date(filters["date_to"]))
    for key, expression in (
        ("has_agenda", "m.agenda_url IS NOT NULL"),
        ("has_minutes", "m.minutes_url IS NOT NULL"),
        ("has_attachments", "EXISTS (SELECT 1 FROM board_documents bd WHERE bd.board_meeting_id = m.id)"),
        ("has_video", "(m.video_url IS NOT NULL OR m.livestream_url IS NOT NULL)"),
        ("changed", "m.revision_detected = 1"),
    ):
        if filters[key] == "1":
            clauses.append(expression)
        elif filters[key] == "0":
            clauses.append(f"NOT ({expression})")
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect_db() as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS count FROM board_meetings m JOIN districts d ON d.id = m.district_id {where_sql}",
                params,
            ).fetchone()["count"]
            or 0
        )
        rows = conn.execute(
            f"""
            SELECT m.*, d.agency_name, d.state,
                   (SELECT COUNT(*) FROM board_agenda_items ai WHERE ai.board_meeting_id = m.id) AS agenda_item_count,
                   (SELECT COUNT(*) FROM board_documents bd WHERE bd.board_meeting_id = m.id) AS document_count
            FROM board_meetings m JOIN districts d ON d.id = m.district_id
            {where_sql}
            ORDER BY COALESCE(m.meeting_date, '') DESC, m.id DESC LIMIT ? OFFSET ?
            """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        meeting_types = [
            row["meeting_type"]
            for row in conn.execute(
                "SELECT DISTINCT meeting_type FROM board_meetings WHERE meeting_type IS NOT NULL AND meeting_type != '' ORDER BY meeting_type"
            )
        ]
    return render_template(
        "board_meetings.html",
        rows=rows,
        total=total,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        page=page,
        page_url=_page_url("boards.meetings"),
        filters=filters,
        options=list_filter_options(),
        platforms=PLATFORMS,
        meeting_types=meeting_types,
    )


def _agenda_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {int(row["id"]): {**row, "children": []} for row in rows}
    roots: list[dict[str, Any]] = []
    for row in by_id.values():
        parent_id = row.get("parent_item_id")
        if parent_id and int(parent_id) in by_id:
            by_id[int(parent_id)]["children"].append(row)
        else:
            roots.append(row)
    return roots


@bp.route("/meetings/<int:meeting_id>")
def meeting_detail(meeting_id: int) -> str:
    with connect_db() as conn:
        meeting = conn.execute(
            """
            SELECT m.*, d.agency_name, d.state FROM board_meetings m
            JOIN districts d ON d.id = m.district_id WHERE m.id = ?
            """,
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            abort(404)
        agenda_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM board_agenda_items WHERE board_meeting_id = ? ORDER BY sequence_number, id",
                (meeting_id,),
            )
        ]
        documents = [
            dict(row)
            for row in conn.execute(
                """
                SELECT bd.*, ai.title AS agenda_item_title FROM board_documents bd
                LEFT JOIN board_agenda_items ai ON ai.id = bd.agenda_item_id
                WHERE bd.board_meeting_id = ? ORDER BY bd.document_type, bd.title, bd.id
                """,
                (meeting_id,),
            )
        ]
        meeting_versions = conn.execute(
            "SELECT * FROM board_meeting_versions WHERE board_meeting_id = ? ORDER BY version_number DESC",
            (meeting_id,),
        ).fetchall()
        document_versions = conn.execute(
            """
            SELECT dv.*, bd.title FROM board_document_versions dv
            JOIN board_documents bd ON bd.id = dv.board_document_id
            WHERE bd.board_meeting_id = ? ORDER BY dv.created_at DESC, dv.id DESC
            """,
            (meeting_id,),
        ).fetchall()
    documents_by_item: dict[int, list[dict[str, Any]]] = {}
    for document in documents:
        if document.get("agenda_item_id"):
            documents_by_item.setdefault(int(document["agenda_item_id"]), []).append(document)
    return render_template(
        "board_meeting_detail.html",
        meeting=meeting,
        agenda_tree=_agenda_tree(agenda_rows),
        documents=documents,
        documents_by_item=documents_by_item,
        meeting_versions=meeting_versions,
        document_versions=document_versions,
    )


@bp.route("/documents/<int:document_id>/file")
def document_file(document_id: int) -> Response:
    with connect_db() as conn:
        document = conn.execute("SELECT * FROM board_documents WHERE id = ?", (document_id,)).fetchone()
    if document is None or not document["local_path"]:
        abort(404)
    path = Path(document["local_path"]).resolve()
    try:
        path.relative_to(BOARD_DOCUMENTS_DIR.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=document["filename"] or path.name)


@bp.route("/meeting-versions/<int:version_id>/snapshot")
def meeting_version_snapshot(version_id: int) -> Response:
    with connect_db() as conn:
        version = conn.execute(
            "SELECT raw_snapshot_path FROM board_meeting_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
    if version is None or not version["raw_snapshot_path"]:
        abort(404)
    path = Path(version["raw_snapshot_path"]).resolve()
    try:
        path.relative_to(BOARD_SNAPSHOTS_DIR.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.route("/document-versions/<int:version_id>/file")
def document_version_file(version_id: int) -> Response:
    with connect_db() as conn:
        version = conn.execute(
            "SELECT local_path FROM board_document_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
    if version is None or not version["local_path"]:
        abort(404)
    path = Path(version["local_path"]).resolve()
    try:
        path.relative_to(BOARD_DOCUMENTS_DIR.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.route("/search")
def search() -> str:
    query = request.args.get("q", "").strip()
    filters = {
        key: request.args.get(key, "").strip()
        for key in ("state", "district_id", "platform", "document_type", "date_from", "date_to")
    }
    page = _clamp(request.args.get("page"), 1, 1, 1_000_000)
    rows: list[dict[str, Any]] = []
    total = 0
    if query:
        search_kwargs = dict(
            state=filters["state"] or None,
            district_ids=[int(filters["district_id"])] if filters["district_id"].isdigit() else None,
            platform=filters["platform"] or None,
            document_type=filters["document_type"] or None,
            date_from=_valid_date(filters["date_from"]),
            date_to=_valid_date(filters["date_to"]),
        )
        total = count_board_content(query, **search_kwargs)
        rows = search_board_content(
            query,
            **search_kwargs,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
    with connect_db() as conn:
        districts = conn.execute(
            """
            SELECT DISTINCT d.id, d.state, d.agency_name FROM districts d
            JOIN board_meetings m ON m.district_id = d.id ORDER BY d.state, d.agency_name
            """
        ).fetchall()
        document_types = [
            row["document_type"]
            for row in conn.execute(
                "SELECT DISTINCT document_type FROM board_documents WHERE document_type IS NOT NULL ORDER BY document_type"
            )
        ]
    return render_template(
        "board_search.html",
        query=query,
        filters=filters,
        rows=rows,
        total=total,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        page=page,
        page_url=_page_url("boards.search"),
        options=list_filter_options(),
        districts=districts,
        platforms=PLATFORMS,
        document_types=document_types,
    )


@bp.route("/runs")
def runs() -> str:
    with connect_db() as conn:
        discovery_runs = conn.execute(
            "SELECT * FROM board_discovery_runs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        sync_runs = conn.execute("SELECT * FROM board_sync_runs ORDER BY id DESC LIMIT 100").fetchall()
    return render_template(
        "board_runs.html",
        discovery_runs=discovery_runs,
        sync_runs=sync_runs,
    )


__all__ = [
    "bp",
    "enqueue_discovery_run",
    "enqueue_sync_run",
    "start_board_worker",
]
