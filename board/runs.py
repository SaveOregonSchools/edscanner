from __future__ import annotations

import json
import logging
import threading
from hashlib import sha256
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from board.discovery import DiscoveryOutcome, discover_board_source
from board.documents import document_identity, store_board_document
from board.http import (
    BoardHTTPClient,
    BoardHTTPError,
    BoardHTTPSettings,
    ResponseTooLarge,
    RobotsDenied,
)
from board.models import BoardSource, DocumentRef, DownloadedDocument, MeetingRef
from board.provider_directories import (
    BOARD_BOOK_DIRECTORY_URL,
    BoardBookDirectoryCatalog,
    load_enabled_boardbook_directory,
    provider_directory_enabled,
)
from board.storage import persist_meeting_bundle, upsert_board_source
from common import (
    BOARD_DISCOVERY_RUN_LOGS_DIR,
    BOARD_DOCUMENTS_DIR,
    BOARD_MAX_DOCUMENTS_PER_MEETING,
    BOARD_MAX_MEETINGS_PER_SOURCE,
    BOARD_INCOMPLETE_RECHECK_DAYS,
    BOARD_OLD_RECHECK_DAYS,
    BOARD_PER_HOST_WORKERS,
    BOARD_RECENT_RECHECK_DAYS,
    BOARD_REQUEST_DELAY_SECONDS,
    BOARD_SYNC_RUN_LOGS_DIR,
    BOARD_SNAPSHOTS_DIR,
    BOARD_WORKERS,
    connect_db,
    init_db,
    utc_now_iso,
)
from search_engine import RunDebugLogger, debug_log


LOGGER = logging.getLogger(__name__)
TERMINAL_ITEM_STATUSES = {
    "working",
    "not_found",
    "manual_review",
    "requires_javascript",
    "blocked_by_challenge",
    "blocked_by_robots",
    "failed",
    "error",
    "completed",
    "completed_with_errors",
    "cancelled",
}
_PERSIST_LOCK = threading.Lock()


def _clean_values(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _json_values(values: Iterable[Any] | None) -> str:
    return json.dumps(_clean_values(values), ensure_ascii=False)


def _read_json_values(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return _clean_values(parsed if isinstance(parsed, list) else [])


def _district_conditions(
    *,
    alias: str = "d",
    states: Iterable[str] | None = None,
    agency_types: Iterable[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    require_website: bool = True,
) -> tuple[list[str], list[Any]]:
    clauses = (
        [f"{alias}.website_normalized IS NOT NULL", f"{alias}.website_normalized != ''"]
        if require_website
        else []
    )
    params: list[Any] = []
    state_values = _clean_values(states)
    type_values = _clean_values(agency_types)
    if state_values:
        clauses.append(f"{alias}.state IN ({','.join('?' for _ in state_values)})")
        params.extend(state_values)
    if type_values:
        clauses.append(f"{alias}.agency_type IN ({','.join('?' for _ in type_values)})")
        params.extend(type_values)
    if min_enrollment is not None:
        clauses.append(f"{alias}.total_enrollment_excludes_ae >= ?")
        params.append(int(min_enrollment))
    if max_enrollment is not None:
        clauses.append(f"{alias}.total_enrollment_excludes_ae <= ?")
        params.append(int(max_enrollment))
    return clauses, params


def _latest_source_join() -> str:
    return """
        LEFT JOIN board_sources bs ON bs.id = (
            SELECT candidate.id
            FROM board_sources candidate
            WHERE candidate.district_id = d.id
            ORDER BY candidate.is_active DESC, candidate.updated_at DESC, candidate.id DESC
            LIMIT 1
        )
    """


def _discovery_selection(
    *,
    states: Iterable[str] | None,
    agency_types: Iterable[str] | None,
    min_enrollment: int | None,
    max_enrollment: int | None,
    platform_filter: str,
    status_filter: str,
    force: bool,
    max_districts: int,
    db_path: Path | str | None,
) -> tuple[int, list[dict[str, Any]]]:
    clauses, params = _district_conditions(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        require_website=not provider_directory_enabled(),
    )
    platform_filter = str(platform_filter or "").strip().casefold()
    status_filter = str(status_filter or "").strip().casefold()
    if platform_filter:
        clauses.append("bs.platform = ?")
        params.append(platform_filter)
    if status_filter == "__unchecked__" and not force:
        clauses.append("bs.id IS NULL")
    elif status_filter and status_filter != "__unchecked__":
        clauses.append("bs.source_status = ?")
        params.append(status_filter)
    elif not force:
        clauses.append("bs.id IS NULL")
    where_sql = " WHERE " + " AND ".join(clauses)
    join_sql = _latest_source_join()
    with connect_db(db_path) as conn:
        matched = int(
            conn.execute(
                f"SELECT COUNT(*) AS count FROM districts d {join_sql} {where_sql}",
                params,
            ).fetchone()["count"]
            or 0
        )
        rows = conn.execute(
            f"""
            SELECT d.*, bs.id AS existing_source_id, bs.platform AS existing_platform,
                   bs.source_status AS existing_source_status, bs.source_url AS existing_source_url
            FROM districts d
            {join_sql}
            {where_sql}
            ORDER BY d.state, d.agency_name, d.id
            LIMIT ?
            """,
            [*params, max(1, int(max_districts))],
        ).fetchall()
    return matched, [dict(row) for row in rows]


def board_discovery_preview(
    *,
    states: Iterable[str] | None = None,
    agency_types: Iterable[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    platform_filter: str = "",
    status_filter: str = "__unchecked__",
    force: bool = False,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    matched, _rows = _discovery_selection(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platform_filter=platform_filter,
        status_filter=status_filter,
        force=force,
        max_districts=1,
        db_path=db_path,
    )
    return matched


def create_board_discovery_run(
    *,
    states: Iterable[str] | None = None,
    agency_types: Iterable[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    platform_filter: str = "",
    status_filter: str = "",
    max_districts: int = 1000,
    max_workers: int = BOARD_WORKERS,
    force: bool = False,
    debug_logging: bool = True,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    max_districts = max(1, min(int(max_districts), 1000))
    max_workers = max(1, min(int(max_workers), 8))
    matched, districts = _discovery_selection(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platform_filter=platform_filter,
        status_filter=status_filter,
        force=force,
        max_districts=max_districts,
        db_path=db_path,
    )
    now = utc_now_iso()
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO board_discovery_runs (
                states_json, agency_types_json, min_enrollment, max_enrollment,
                platform_filter, status_filter, max_districts, max_workers,
                force, debug_logging, status, districts_matched,
                districts_planned, queued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                _json_values(states),
                _json_values(agency_types),
                min_enrollment,
                max_enrollment,
                str(platform_filter or "").strip().casefold() or None,
                str(status_filter or "").strip().casefold() or None,
                max_districts,
                max_workers,
                1 if force else 0,
                1 if debug_logging else 0,
                matched,
                len(districts),
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO board_discovery_run_items (run_id, district_id, status) VALUES (?, ?, 'queued')",
            [(run_id, int(district["id"])) for district in districts],
        )
        if debug_logging:
            debug_path = BOARD_DISCOVERY_RUN_LOGS_DIR / f"run-{run_id}.log"
            conn.execute(
                "UPDATE board_discovery_runs SET debug_log_path = ? WHERE id = ?",
                (str(debug_path), run_id),
            )
        conn.commit()
    return run_id


def _run_cancelled(table: str, run_id: int, db_path: Path | str | None) -> bool:
    if table not in {"board_discovery_runs", "board_sync_runs"}:
        raise ValueError("Unexpected run table")
    with connect_db(db_path) as conn:
        row = conn.execute(f"SELECT cancel_requested FROM {table} WHERE id = ?", (run_id,)).fetchone()
    return row is None or bool(row["cancel_requested"])


def _refresh_discovery_counts(run_id: int, db_path: Path | str | None) -> None:
    with connect_db(db_path) as conn:
        counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM board_discovery_run_items WHERE run_id = ? GROUP BY status",
                (run_id,),
            )
        }
        processed = sum(count for status, count in counts.items() if status in TERMINAL_ITEM_STATUSES)
        conn.execute(
            """
            UPDATE board_discovery_runs
            SET districts_processed = ?, sources_working = ?, sources_not_found = ?,
                sources_manual_review = ?, sources_failed = ?
            WHERE id = ?
            """,
            (
                processed,
                counts.get("working", 0),
                counts.get("not_found", 0),
                counts.get("manual_review", 0)
                + counts.get("requires_javascript", 0)
                + counts.get("blocked_by_challenge", 0)
                + counts.get("blocked_by_robots", 0),
                counts.get("failed", 0) + counts.get("error", 0),
                run_id,
            ),
        )
        conn.commit()


def _execute_discovery_item(
    run_id: int,
    item_id: int,
    district: Mapping[str, Any],
    *,
    client: BoardHTTPClient,
    provider_directory: BoardBookDirectoryCatalog | None,
    debug_logger: RunDebugLogger | None,
    db_path: Path | str | None,
) -> None:
    now = utc_now_iso()
    with connect_db(db_path) as conn:
        claimed = conn.execute(
            """
            UPDATE board_discovery_run_items SET status = 'running', started_at = ?
            WHERE id = ? AND run_id = ? AND status = 'queued'
            """,
            (now, item_id, run_id),
        ).rowcount
        conn.commit()
    if not claimed:
        return
    try:
        outcome = discover_board_source(
            district,
            client=client,
            provider_directory=provider_directory,
            allow_browser_fallback=True,
            cancel_requested=lambda: _run_cancelled("board_discovery_runs", run_id, db_path),
            debug_logger=debug_logger,
        )
        if outcome.status == "cancelled":
            status = "cancelled"
            source_id = None
        else:
            payload = asdict(outcome)
            payload.pop("source", None)
            with _PERSIST_LOCK:
                source_row = upsert_board_source(int(district["id"]), payload, db_path=db_path)
            source_id = int(source_row["id"])
            status = outcome.status if outcome.status in TERMINAL_ITEM_STATUSES else "manual_review"
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE board_discovery_run_items
                SET board_source_id = ?, status = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (source_id, status, outcome.error_message or None, utc_now_iso(), item_id),
            )
            conn.commit()
    except Exception as exc:
        LOGGER.exception("Board discovery item %s failed", item_id)
        debug_log(debug_logger, "board_discovery_error", district=district.get("agency_name"), error=str(exc))
        with connect_db(db_path) as conn:
            conn.execute(
                "UPDATE board_discovery_run_items SET status = 'failed', error_message = ?, finished_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), item_id),
            )
            conn.commit()
    finally:
        _refresh_discovery_counts(run_id, db_path)


def execute_board_discovery_run(run_id: int, *, db_path: Path | str | None = None) -> None:
    init_db(db_path)
    debug_logger: RunDebugLogger | None = None
    client: BoardHTTPClient | None = None
    provider_directory: BoardBookDirectoryCatalog | None = None
    try:
        with connect_db(db_path) as conn:
            run = conn.execute("SELECT * FROM board_discovery_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise ValueError(f"Board discovery run not found: {run_id}")
            if run["status"] != "queued":
                return
            claimed = conn.execute(
                "UPDATE board_discovery_runs SET status = 'running', started_at = ?, error_message = NULL WHERE id = ? AND status = 'queued'",
                (utc_now_iso(), run_id),
            ).rowcount
            items = conn.execute(
                """
                SELECT i.id AS item_id, d.*
                FROM board_discovery_run_items i
                JOIN districts d ON d.id = i.district_id
                WHERE i.run_id = ? AND i.status = 'queued'
                ORDER BY i.id
                """,
                (run_id,),
            ).fetchall()
            conn.commit()
        if not claimed:
            return
        if run["debug_log_path"]:
            debug_logger = RunDebugLogger(Path(run["debug_log_path"]))
        debug_log(debug_logger, "board_discovery_started", run_id=run_id, planned=len(items))
        client = BoardHTTPClient(
            BoardHTTPSettings(
                delay_seconds=BOARD_REQUEST_DELAY_SECONDS,
                per_host_concurrency=BOARD_PER_HOST_WORKERS,
                global_concurrency=max(1, int(run["max_workers"])),
            )
        )
        try:
            provider_directory = load_enabled_boardbook_directory(client)
            if provider_directory is not None:
                with connect_db(db_path) as conn:
                    district_universe = conn.execute(
                        """
                        SELECT id, agency_name, state, website,
                               website_normalized, raw_json
                        FROM districts
                        """
                    ).fetchall()
                provider_directory.configure_district_universe(
                    [dict(row) for row in district_universe]
                )
                debug_log(
                    debug_logger,
                    "board_provider_directory_loaded",
                    provider="boardbook",
                    catalog_url=provider_directory.source_url,
                    organizations=len(provider_directory.entries),
                    district_universe=len(district_universe),
                )
        except Exception as exc:
            LOGGER.warning(
                "BoardBook provider directory was enabled but could not be loaded; "
                "continuing with district-site discovery: %s",
                exc,
            )
            debug_log(
                debug_logger,
                "board_provider_directory_error",
                provider="boardbook",
                catalog_url=BOARD_BOOK_DIRECTORY_URL,
                error=str(exc),
            )
        with ThreadPoolExecutor(
            max_workers=max(1, int(run["max_workers"])),
            thread_name_prefix="BoardDiscovery",
        ) as executor:
            futures = [
                executor.submit(
                    _execute_discovery_item,
                    run_id,
                    int(item["item_id"]),
                    dict(item),
                    client=client,
                    provider_directory=provider_directory,
                    debug_logger=debug_logger,
                    db_path=db_path,
                )
                for item in items
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except CancelledError:
                    continue
                if _run_cancelled("board_discovery_runs", run_id, db_path):
                    for pending in futures:
                        pending.cancel()
        cancelled = _run_cancelled("board_discovery_runs", run_id, db_path)
        if cancelled:
            with connect_db(db_path) as conn:
                conn.execute(
                    "UPDATE board_discovery_run_items SET status = 'cancelled', finished_at = ? WHERE run_id = ? AND status = 'queued'",
                    (utc_now_iso(), run_id),
                )
                conn.commit()
        _refresh_discovery_counts(run_id, db_path)
        with connect_db(db_path) as conn:
            counts = conn.execute(
                "SELECT districts_processed, sources_failed FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            final_status = "cancelled" if cancelled else "failed" if int(counts["districts_processed"] or 0) == int(counts["sources_failed"] or 0) and int(counts["sources_failed"] or 0) else "completed"
            conn.execute(
                "UPDATE board_discovery_runs SET status = ?, finished_at = ? WHERE id = ?",
                (final_status, utc_now_iso(), run_id),
            )
            conn.commit()
        debug_log(debug_logger, "board_discovery_finished", run_id=run_id, status=final_status)
    except Exception as exc:
        LOGGER.exception("Board discovery run %s failed", run_id)
        with connect_db(db_path) as conn:
            conn.execute(
                "UPDATE board_discovery_runs SET status = 'failed', error_message = ?, finished_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), run_id),
            )
            conn.commit()
    finally:
        if client is not None:
            client.close()


def _sync_selection(
    *,
    states: Iterable[str] | None,
    agency_types: Iterable[str] | None,
    min_enrollment: int | None,
    max_enrollment: int | None,
    platforms: Iterable[str] | None,
    source_status: str,
    last_sync_before: str | None,
    max_districts: int,
    db_path: Path | str | None,
) -> tuple[int, list[dict[str, Any]]]:
    clauses, params = _district_conditions(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        require_website=False,
    )
    clauses.append("bs.id IS NOT NULL")
    clauses.append("bs.is_active = 1")
    source_status = str(source_status or "working").strip().casefold()
    if source_status:
        clauses.append("bs.source_status = ?")
        params.append(source_status)
    platform_values = [value.casefold() for value in _clean_values(platforms)]
    if platform_values:
        clauses.append(f"bs.platform IN ({','.join('?' for _ in platform_values)})")
        params.extend(platform_values)
    if last_sync_before:
        clauses.append("(bs.last_successful_sync_at IS NULL OR bs.last_successful_sync_at < ?)")
        params.append(last_sync_before)
    where_sql = " WHERE " + " AND ".join(clauses)
    join_sql = _latest_source_join().replace("LEFT JOIN", "JOIN", 1)
    with connect_db(db_path) as conn:
        matched = int(
            conn.execute(
                f"SELECT COUNT(*) AS count FROM districts d {join_sql} {where_sql}",
                params,
            ).fetchone()["count"]
            or 0
        )
        rows = conn.execute(
            f"""
            SELECT bs.*, d.agency_name, d.state, d.agency_type,
                   d.agency_id_nces, d.total_enrollment_excludes_ae,
                   d.website, d.website_normalized
            FROM districts d
            {join_sql}
            {where_sql}
            ORDER BY COALESCE(bs.last_successful_sync_at, ''), d.state, d.agency_name, d.id
            LIMIT ?
            """,
            [*params, max(1, int(max_districts))],
        ).fetchall()
    return matched, [dict(row) for row in rows]


def board_sync_preview(
    *,
    states: Iterable[str] | None = None,
    agency_types: Iterable[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    platforms: Iterable[str] | None = None,
    source_status: str = "working",
    last_sync_before: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    matched, rows = _sync_selection(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platforms=platforms,
        source_status=source_status,
        last_sync_before=last_sync_before,
        max_districts=1_000_000,
        db_path=db_path,
    )
    platform_counts: dict[str, int] = {}
    for row in rows:
        platform_counts[str(row["platform"])] = platform_counts.get(str(row["platform"]), 0) + 1
    return {
        "matching_districts": matched,
        "working_sources": len(rows),
        "never_synced": sum(1 for row in rows if not row.get("last_successful_sync_at")),
        "platforms": [
            {"platform": platform, "count": count}
            for platform, count in sorted(platform_counts.items())
        ],
    }


def create_board_sync_run(
    *,
    states: Iterable[str] | None = None,
    agency_types: Iterable[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    platforms: Iterable[str] | None = None,
    source_status: str = "working",
    last_sync_before: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sync_mode: str = "monitor",
    force: bool = False,
    max_districts: int = 1000,
    max_workers: int = BOARD_WORKERS,
    debug_logging: bool = True,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    sync_mode = str(sync_mode or "monitor").strip().casefold()
    if sync_mode not in {"monitor", "backfill"}:
        raise ValueError(f"Unsupported board sync mode: {sync_mode}")
    max_districts = max(1, min(int(max_districts), 1000))
    max_workers = max(1, min(int(max_workers), 8))
    matched, sources = _sync_selection(
        states=states,
        agency_types=agency_types,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
        platforms=platforms,
        source_status=source_status,
        last_sync_before=last_sync_before,
        max_districts=max_districts,
        db_path=db_path,
    )
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO board_sync_runs (
                states_json, agency_types_json, min_enrollment, max_enrollment,
                platforms_json, source_status, last_sync_before, date_from,
                date_to, sync_mode, force, max_districts, max_workers,
                debug_logging, status, districts_matched, districts_planned,
                queued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                _json_values(states),
                _json_values(agency_types),
                min_enrollment,
                max_enrollment,
                _json_values(platforms),
                str(source_status or "working").strip().casefold(),
                last_sync_before,
                date_from,
                date_to,
                sync_mode,
                1 if force else 0,
                max_districts,
                max_workers,
                1 if debug_logging else 0,
                matched,
                len(sources),
                utc_now_iso(),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO board_sync_run_items (run_id, district_id, board_source_id, status)
            VALUES (?, ?, ?, 'queued')
            """,
            [(run_id, int(source["district_id"]), int(source["id"])) for source in sources],
        )
        if debug_logging:
            debug_path = BOARD_SYNC_RUN_LOGS_DIR / f"run-{run_id}.log"
            conn.execute(
                "UPDATE board_sync_runs SET debug_log_path = ? WHERE id = ?",
                (str(debug_path), run_id),
            )
        conn.commit()
    return run_id


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _meeting_needs_refresh(
    meeting_ref: MeetingRef,
    existing: Mapping[str, Any] | None,
    *,
    sync_mode: str,
    force: bool,
) -> bool:
    if force or sync_mode == "backfill" or existing is None:
        return True
    for field in ("agenda_url", "minutes_url", "packet_url", "video_url"):
        if getattr(meeting_ref, field, None) and not existing.get(field):
            return True
    today = date.today()
    meeting_date = _parse_iso_date(meeting_ref.meeting_date or existing.get("meeting_date"))
    if meeting_date is None or meeting_date >= today - timedelta(days=BOARD_RECENT_RECHECK_DAYS):
        return True
    last_checked = _parse_iso_datetime(existing.get("last_checked_at"))
    incomplete = not existing.get("minutes_url")
    age = datetime.now(timezone.utc) - last_checked if last_checked else timedelta.max
    if incomplete:
        return age >= timedelta(days=BOARD_INCOMPLETE_RECHECK_DAYS)
    return age >= timedelta(days=BOARD_OLD_RECHECK_DAYS)


def _meeting_documents(meeting: Any) -> list[DocumentRef]:
    found: list[DocumentRef] = []
    seen: set[tuple[str, str, str]] = set()
    for document in list(getattr(meeting, "documents", []) or []):
        key = document.identity_key()
        if key not in seen:
            seen.add(key)
            found.append(document)
    for item in list(getattr(meeting, "agenda_items", []) or []):
        for document in list(getattr(item, "documents", []) or []):
            key = document.identity_key()
            if key not in seen:
                seen.add(key)
                found.append(document)
    return found[:BOARD_MAX_DOCUMENTS_PER_MEETING]


def _store_meeting_snapshot(
    district_id: int,
    board_source_id: int,
    external_meeting_id: str,
    content: bytes,
    *,
    storage_root: Path | str = BOARD_SNAPSHOTS_DIR,
) -> Path:
    from board.documents import safe_path_segment

    digest = sha256(content).hexdigest()
    directory = (
        Path(storage_root)
        / f"district-{int(district_id)}"
        / f"source-{int(board_source_id)}"
        / safe_path_segment(external_meeting_id, max_length=64, fallback="meeting")
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"meeting--{digest[:20]}.html"
    if not path.exists():
        temporary = directory / f".{path.name}.{threading.get_ident()}.tmp"
        temporary.write_bytes(content)
        temporary.replace(path)
    return path.resolve()


def _existing_document(
    board_meeting_id: int,
    document: DocumentRef,
    db_path: Path | str | None,
) -> dict[str, Any] | None:
    identity = document_identity(document)
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM board_documents WHERE board_meeting_id = ? AND identity_key = ?",
            (board_meeting_id, identity),
        ).fetchone()
    return dict(row) if row else None


def _download_document(
    adapter: Any,
    document: DocumentRef,
    existing: Mapping[str, Any] | None,
    *,
    force: bool,
) -> DownloadedDocument:
    if existing and not force and (existing.get("http_etag") or existing.get("http_last_modified")):
        response = adapter.client.get(
            document.url,
            etag=existing.get("http_etag"),
            last_modified=existing.get("http_last_modified"),
            max_bytes=adapter.client.settings.max_document_size_bytes,
            force=True,
            raise_for_status=False,
        )
        return DownloadedDocument(
            document_ref=document,
            content=response.content,
            final_url=response.final_url,
            status_code=response.status_code,
            content_type=response.content_type or document.content_type,
            etag=response.etag or existing.get("http_etag"),
            last_modified=response.last_modified or existing.get("http_last_modified"),
            fetched_at=utc_now_iso(),
            metadata={"conditional": True, "not_modified": response.not_modified},
        )
    return adapter.fetch_document(document)


def _refresh_sync_counts(run_id: int, db_path: Path | str | None) -> None:
    with connect_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status NOT IN ('queued', 'running') THEN 1 ELSE 0 END) AS processed,
                COALESCE(SUM(meetings_discovered), 0) AS meetings_discovered,
                COALESCE(SUM(meetings_added), 0) AS meetings_added,
                COALESCE(SUM(meetings_updated), 0) AS meetings_updated,
                COALESCE(SUM(documents_added), 0) AS documents_added,
                COALESCE(SUM(documents_updated), 0) AS documents_updated,
                SUM(CASE WHEN status IN ('failed', 'error', 'completed_with_errors') THEN 1 ELSE 0 END) AS failures
            FROM board_sync_run_items WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE board_sync_runs SET districts_processed = ?, meetings_discovered = ?,
                meetings_added = ?, meetings_updated = ?, documents_added = ?,
                documents_updated = ?, failures = ? WHERE id = ?
            """,
            (
                int(row["processed"] or 0),
                int(row["meetings_discovered"] or 0),
                int(row["meetings_added"] or 0),
                int(row["meetings_updated"] or 0),
                int(row["documents_added"] or 0),
                int(row["documents_updated"] or 0),
                int(row["failures"] or 0),
                run_id,
            ),
        )
        conn.commit()


def _sync_failure_status(exc: BaseException, *, stage: str) -> str:
    """Classify source health without mistaking access failures for empty data."""

    if isinstance(exc, RobotsDenied):
        return "blocked_by_robots"
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403, 429}:
        return "blocked_by_challenge"
    message = str(exc).casefold()
    if "robots.txt disallow" in message or "blocked by robots" in message:
        return "blocked_by_robots"
    if any(
        marker in message
        for marker in (
            "anti-bot challenge",
            "access denied",
            "captcha",
            "verify you are human",
            "too many requests",
            "401 client error",
            "403 client error",
            "429 client error",
            "http 401",
            "http 403",
            "http 429",
            "status 401",
            "status 403",
            "status 429",
        )
    ):
        return "blocked_by_challenge"
    if stage in {"listing", "meeting"} and not isinstance(exc, (BoardHTTPError, OSError)):
        module_name = type(exc).__module__.casefold()
        parser_exception = isinstance(
            exc,
            (AssertionError, AttributeError, IndexError, KeyError, TypeError, ValueError),
        ) or any(
            marker in message
            for marker in ("malformed", "parse", "parser", "unexpected markup", "schema changed")
        )
        if parser_exception and not module_name.startswith(("requests", "urllib3", "sqlite3")):
            return "platform_changed_or_parser_broken"
    return "error"


def _aggregate_sync_failure_status(statuses: Iterable[str]) -> str:
    found = {str(status or "error") for status in statuses}
    for status in (
        "platform_changed_or_parser_broken",
        "blocked_by_robots",
        "blocked_by_challenge",
        "error",
    ):
        if status in found:
            return status
    return "error"


def _store_document_failure(
    district_id: int,
    meeting_id: int,
    document: DocumentRef,
    exc: BaseException,
    *,
    status: str,
    db_path: Path | str | None,
    storage_root: Path | str,
) -> dict[str, Any]:
    failed_document = {
        "external_document_id": document.external_document_id,
        "title": document.title,
        "source_url": document.url,
        "document_type": document.document_type,
        "filename": document.file_name,
        "mime_type": document.content_type,
        "agenda_item_external_id": document.agenda_item_external_id,
        "text_extraction_status": status,
        "error_message": str(exc),
        "retrieved_at": utc_now_iso(),
    }
    with _PERSIST_LOCK:
        return store_board_document(
            district_id,
            meeting_id,
            failed_document,
            storage_root=storage_root,
            db_path=db_path,
        )


def _process_sync_meeting(
    run_id: int,
    item: Mapping[str, Any],
    run: Mapping[str, Any],
    source: BoardSource,
    adapter: Any,
    ref: MeetingRef,
    *,
    client: BoardHTTPClient,
    debug_logger: RunDebugLogger | None,
    db_path: Path | str | None,
    document_storage_root: Path | str,
    snapshot_storage_root: Path | str,
) -> tuple[dict[str, int], list[str]]:
    counters = {
        "meetings_added": 0,
        "meetings_updated": 0,
        "documents_added": 0,
        "documents_updated": 0,
    }
    document_errors: list[str] = []
    debug_log(
        debug_logger,
        "meeting_discovered",
        source_id=item["board_source_id"],
        external_meeting_id=ref.external_meeting_id,
        meeting_date=ref.meeting_date,
        url=ref.url,
    )
    with connect_db(db_path) as conn:
        existing_row = conn.execute(
            """
            SELECT * FROM board_meetings
            WHERE board_source_id = ? AND external_meeting_id = ?
            """,
            (int(item["board_source_id"]), ref.external_meeting_id),
        ).fetchone()
    existing = dict(existing_row) if existing_row else None
    if not _meeting_needs_refresh(
        ref,
        existing,
        sync_mode=str(run["sync_mode"]),
        force=bool(run["force"]),
    ):
        debug_log(
            debug_logger,
            "meeting_unchanged",
            external_meeting_id=ref.external_meeting_id,
            reason="incremental refresh policy",
        )
        return counters, document_errors

    meeting = adapter.fetch_meeting(source, ref)
    snapshot_path: Path | None = None
    snapshot_status: int | None = None
    if source.platform == "boardbook":
        try:
            snapshot = client.get(ref.agenda_url or ref.url, raise_for_status=False)
            snapshot_status = snapshot.status_code
            if snapshot.status_code < 400 and snapshot.content:
                snapshot_path = _store_meeting_snapshot(
                    int(item["district_id"]),
                    int(item["board_source_id"]),
                    ref.external_meeting_id,
                    snapshot.content,
                    storage_root=snapshot_storage_root,
                )
        except Exception as exc:
            debug_log(
                debug_logger,
                "board_snapshot_error",
                meeting_id=ref.external_meeting_id,
                error=str(exc),
            )
    with _PERSIST_LOCK:
        saved = persist_meeting_bundle(
            int(item["district_id"]),
            int(item["board_source_id"]),
            meeting,
            raw_snapshot_path=snapshot_path,
            http_status=snapshot_status,
            db_path=db_path,
        )
    if saved["created"]:
        counters["meetings_added"] += 1
    elif saved["changed"]:
        counters["meetings_updated"] += 1
    debug_log(
        debug_logger,
        "meeting_created" if saved["created"] else "meeting_changed" if saved["changed"] else "meeting_unchanged",
        meeting_id=saved["id"],
        external_meeting_id=ref.external_meeting_id,
        version=saved["version_number"],
        source_url=meeting.source_url,
    )

    for document in _meeting_documents(meeting):
        if _run_cancelled("board_sync_runs", run_id, db_path):
            break
        previous = _existing_document(int(saved["id"]), document, db_path)
        stored: dict[str, Any] | None = None
        try:
            downloaded = _download_document(
                adapter,
                document,
                previous,
                force=bool(run["force"]),
            )
            with _PERSIST_LOCK:
                stored = store_board_document(
                    int(item["district_id"]),
                    int(saved["id"]),
                    downloaded,
                    storage_root=document_storage_root,
                    db_path=db_path,
                )
        except ResponseTooLarge as exc:
            document_errors.append(f"{document.title or document.url}: {exc}")
            try:
                stored = _store_document_failure(
                    int(item["district_id"]),
                    int(saved["id"]),
                    document,
                    exc,
                    status="too_large",
                    db_path=db_path,
                    storage_root=document_storage_root,
                )
            except Exception as store_exc:
                document_errors.append(f"Could not record {document.title or document.url}: {store_exc}")
            debug_log(
                debug_logger,
                "document_too_large",
                meeting_id=saved["id"],
                url=document.url,
                error=str(exc),
            )
        except Exception as exc:
            document_errors.append(f"{document.title or document.url}: {exc}")
            try:
                stored = _store_document_failure(
                    int(item["district_id"]),
                    int(saved["id"]),
                    document,
                    exc,
                    status="failed",
                    db_path=db_path,
                    storage_root=document_storage_root,
                )
            except Exception as store_exc:
                document_errors.append(f"Could not record {document.title or document.url}: {store_exc}")
            debug_log(
                debug_logger,
                "sync_error",
                entity="document",
                meeting_id=saved["id"],
                url=document.url,
                error=str(exc),
            )
        if stored is None:
            continue
        if stored["created"]:
            counters["documents_added"] += 1
        elif stored["changed"]:
            counters["documents_updated"] += 1
        debug_log(
            debug_logger,
            "document_changed" if stored["changed"] else "document_unchanged" if stored["unchanged"] else "document_downloaded",
            document_id=stored["id"],
            meeting_id=saved["id"],
            url=stored["source_url"],
            sha256=stored.get("sha256"),
            extraction_status=stored.get("text_extraction_status"),
        )
        if stored.get("text_extraction_status") == "extracted":
            debug_log(
                debug_logger,
                "document_text_extracted",
                document_id=stored["id"],
                characters=len(stored.get("extracted_text") or ""),
            )
        elif stored.get("text_extraction_status") == "unsupported":
            debug_log(
                debug_logger,
                "unsupported_document",
                document_id=stored["id"],
                mime_type=stored.get("mime_type"),
            )
    return counters, document_errors


def _execute_sync_item(
    run_id: int,
    item: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    client: BoardHTTPClient,
    debug_logger: RunDebugLogger | None,
    db_path: Path | str | None,
    document_storage_root: Path | str,
    snapshot_storage_root: Path | str,
) -> None:
    item_id = int(item["item_id"])
    with connect_db(db_path) as conn:
        claimed = conn.execute(
            "UPDATE board_sync_run_items SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (utc_now_iso(), item_id),
        ).rowcount
        conn.commit()
    if not claimed:
        return
    with connect_db(db_path) as conn:
        source_state = conn.execute(
            "SELECT is_active FROM board_sources WHERE id = ?",
            (int(item["board_source_id"]),),
        ).fetchone()
        if source_state is None or not int(source_state["is_active"] or 0):
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE board_sync_run_items
                SET status = 'cancelled',
                    error_message = 'Source became inactive before synchronization; item cancelled.',
                    finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, item_id),
            )
            conn.commit()
            _refresh_sync_counts(run_id, db_path)
            return
    counters = {
        "meetings_discovered": 0,
        "meetings_added": 0,
        "meetings_updated": 0,
        "documents_added": 0,
        "documents_updated": 0,
    }
    meeting_errors: list[str] = []
    document_errors: list[str] = []
    failure_statuses: list[str] = []
    try:
        from board.adapters import get_adapter

        source = BoardSource(
            platform=str(item["platform"]),
            public_url=str(item["source_url"]),
            external_source_id=item.get("organization_external_id"),
            district_id=int(item["district_id"]),
            organization_name=item.get("agency_name"),
            status=str(item.get("source_status") or "working"),
            requires_javascript=bool(item.get("requires_javascript")),
            metadata={"platform_tenant": item.get("platform_tenant")},
        )
        adapter = get_adapter(
            source.platform,
            client=client,
            allow_browser_fallback=True,
        )
        refs = adapter.list_meetings(source, since=run.get("date_from"))
        if not refs and not run.get("date_from"):
            with connect_db(db_path) as conn:
                previous_meeting_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM board_meetings WHERE board_source_id = ?",
                        (int(item["board_source_id"]),),
                    ).fetchone()["count"]
                    or 0
                )
            if previous_meeting_count:
                raise ValueError(
                    "Meeting listing returned no records for a source that previously "
                    f"contained {previous_meeting_count} meeting(s)."
                )
        date_from = _parse_iso_date(run.get("date_from"))
        date_to = _parse_iso_date(run.get("date_to"))
        refs = [
            ref
            for ref in refs
            if (not date_from or not _parse_iso_date(ref.meeting_date) or _parse_iso_date(ref.meeting_date) >= date_from)
            and (not date_to or not _parse_iso_date(ref.meeting_date) or _parse_iso_date(ref.meeting_date) <= date_to)
        ][:BOARD_MAX_MEETINGS_PER_SOURCE]
        counters["meetings_discovered"] = len(refs)
        debug_log(
            debug_logger,
            "board_meetings_listed",
            run_id=run_id,
            source_id=item["board_source_id"],
            platform=source.platform,
            count=len(refs),
        )
        if run["sync_mode"] != "discovery_only":
            for ref in refs:
                if _run_cancelled("board_sync_runs", run_id, db_path):
                    break
                try:
                    increments, errors = _process_sync_meeting(
                        run_id,
                        item,
                        run,
                        source,
                        adapter,
                        ref,
                        client=client,
                        debug_logger=debug_logger,
                        db_path=db_path,
                        document_storage_root=document_storage_root,
                        snapshot_storage_root=snapshot_storage_root,
                    )
                except Exception as exc:
                    failure_status = _sync_failure_status(exc, stage="meeting")
                    failure_statuses.append(failure_status)
                    message = f"Meeting {ref.external_meeting_id or ref.url}: {exc}"
                    meeting_errors.append(message)
                    LOGGER.warning(
                        "Board sync meeting %s failed for source %s: %s",
                        ref.external_meeting_id,
                        item.get("board_source_id"),
                        exc,
                    )
                    debug_log(
                        debug_logger,
                        "sync_error",
                        entity="meeting",
                        source_id=item["board_source_id"],
                        external_meeting_id=ref.external_meeting_id,
                        url=ref.url,
                        source_status=failure_status,
                        error=str(exc),
                    )
                    continue
                for key, value in increments.items():
                    counters[key] += int(value or 0)
                if errors:
                    failure_statuses.extend(
                        _sync_failure_status(RuntimeError(error), stage="document")
                        for error in errors
                    )
                    document_errors.extend(
                        f"Meeting {ref.external_meeting_id}: {error}" for error in errors
                    )

        cancelled = _run_cancelled("board_sync_runs", run_id, db_path)
        errors = [*meeting_errors, *document_errors]
        completed_cleanly = not cancelled and not errors
        source_message_parts = errors[:5]
        if len(errors) > len(source_message_parts):
            source_message_parts.append(f"{len(errors) - len(source_message_parts)} additional errors")
        if cancelled:
            source_message_parts.append("Cancellation requested.")
        source_message = "; ".join(source_message_parts) or None
        now = utc_now_iso()
        with connect_db(db_path) as conn:
            if completed_cleanly:
                conn.execute(
                    """
                    UPDATE board_sources SET source_status = 'working', last_checked_at = ?,
                        last_successful_sync_at = ?, error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, now, int(item["board_source_id"])),
                )
            elif errors:
                conn.execute(
                    """
                    UPDATE board_sources SET source_status = ?, last_checked_at = ?,
                        error_message = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        _aggregate_sync_failure_status(failure_statuses),
                        now,
                        source_message,
                        now,
                        int(item["board_source_id"]),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE board_sources SET last_checked_at = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        source_message,
                        now,
                        int(item["board_source_id"]),
                    ),
                )
            conn.execute(
                """
                UPDATE board_sync_run_items
                SET status = ?, meetings_discovered = ?, meetings_added = ?,
                    meetings_updated = ?, documents_added = ?, documents_updated = ?,
                    error_message = ?, finished_at = ? WHERE id = ?
                """,
                (
                    "cancelled" if cancelled else "completed_with_errors" if errors else "completed",
                    counters["meetings_discovered"],
                    counters["meetings_added"],
                    counters["meetings_updated"],
                    counters["documents_added"],
                    counters["documents_updated"],
                    source_message,
                    now,
                    item_id,
                ),
            )
            conn.commit()
    except Exception as exc:
        LOGGER.exception("Board sync item %s failed", item_id)
        debug_log(
            debug_logger,
            "board_sync_error",
            run_id=run_id,
            source_id=item.get("board_source_id"),
            error=str(exc),
        )
        cancelled = _run_cancelled("board_sync_runs", run_id, db_path)
        failure_status = _sync_failure_status(exc, stage="listing")
        now = utc_now_iso()
        with connect_db(db_path) as conn:
            if cancelled:
                conn.execute(
                    """
                    UPDATE board_sources SET last_checked_at = ?, error_message = ?,
                        updated_at = ? WHERE id = ?
                    """,
                    (now, f"Cancellation requested. {exc}", now, int(item["board_source_id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE board_sources SET source_status = ?, last_checked_at = ?,
                        error_message = ?, updated_at = ? WHERE id = ?
                    """,
                    (failure_status, now, str(exc), now, int(item["board_source_id"])),
                )
            conn.execute(
                """
                UPDATE board_sync_run_items
                SET status = ?, meetings_discovered = ?, meetings_added = ?,
                    meetings_updated = ?, documents_added = ?, documents_updated = ?,
                    error_message = ?, finished_at = ? WHERE id = ?
                """,
                (
                    "cancelled" if cancelled else "failed",
                    counters["meetings_discovered"],
                    counters["meetings_added"],
                    counters["meetings_updated"],
                    counters["documents_added"],
                    counters["documents_updated"],
                    str(exc),
                    now,
                    item_id,
                ),
            )
            conn.commit()
    finally:
        _refresh_sync_counts(run_id, db_path)


def execute_board_sync_run(
    run_id: int,
    *,
    db_path: Path | str | None = None,
    document_storage_root: Path | str = BOARD_DOCUMENTS_DIR,
    snapshot_storage_root: Path | str = BOARD_SNAPSHOTS_DIR,
) -> None:
    init_db(db_path)
    client: BoardHTTPClient | None = None
    debug_logger: RunDebugLogger | None = None
    try:
        with connect_db(db_path) as conn:
            row = conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"Board sync run not found: {run_id}")
            if row["status"] != "queued":
                return
            claimed = conn.execute(
                "UPDATE board_sync_runs SET status = 'running', started_at = ?, error_message = NULL WHERE id = ? AND status = 'queued'",
                (utc_now_iso(), run_id),
            ).rowcount
            if not claimed:
                conn.commit()
                return
            inactive_finished_at = utc_now_iso()
            conn.execute(
                """
                UPDATE board_sync_run_items
                SET status = 'cancelled',
                    error_message = 'Source is inactive; item cancelled before synchronization.',
                    finished_at = ?
                WHERE run_id = ? AND status IN ('queued', 'running')
                  AND NOT EXISTS (
                      SELECT 1 FROM board_sources bs
                      WHERE bs.id = board_sync_run_items.board_source_id
                        AND bs.is_active = 1
                  )
                """,
                (inactive_finished_at, run_id),
            )
            item_rows = conn.execute(
                """
                SELECT i.id AS item_id, i.board_source_id, bs.*, d.agency_name, d.state
                FROM board_sync_run_items i
                JOIN board_sources bs ON bs.id = i.board_source_id
                JOIN districts d ON d.id = i.district_id
                WHERE i.run_id = ? AND i.status = 'queued' AND bs.is_active = 1
                ORDER BY i.id
                """,
                (run_id,),
            ).fetchall()
            conn.commit()
        run = dict(row)
        if run.get("debug_log_path"):
            debug_logger = RunDebugLogger(Path(run["debug_log_path"]))
        debug_log(debug_logger, "board_sync_started", run_id=run_id, planned=len(item_rows), mode=run["sync_mode"])
        client = BoardHTTPClient(
            BoardHTTPSettings(
                delay_seconds=BOARD_REQUEST_DELAY_SECONDS,
                per_host_concurrency=BOARD_PER_HOST_WORKERS,
                global_concurrency=max(1, int(run["max_workers"])),
            )
        )
        with ThreadPoolExecutor(
            max_workers=max(1, int(run["max_workers"])),
            thread_name_prefix="BoardSync",
        ) as executor:
            futures = [
                executor.submit(
                    _execute_sync_item,
                    run_id,
                    dict(item),
                    run,
                    client=client,
                    debug_logger=debug_logger,
                    db_path=db_path,
                    document_storage_root=document_storage_root,
                    snapshot_storage_root=snapshot_storage_root,
                )
                for item in item_rows
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except CancelledError:
                    continue
                if _run_cancelled("board_sync_runs", run_id, db_path):
                    for pending in futures:
                        pending.cancel()
        cancelled = _run_cancelled("board_sync_runs", run_id, db_path)
        if cancelled:
            with connect_db(db_path) as conn:
                conn.execute(
                    "UPDATE board_sync_run_items SET status = 'cancelled', finished_at = ? WHERE run_id = ? AND status = 'queued'",
                    (utc_now_iso(), run_id),
                )
                conn.commit()
        _refresh_sync_counts(run_id, db_path)
        with connect_db(db_path) as conn:
            result = conn.execute(
                "SELECT districts_processed, failures FROM board_sync_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            final_status = "cancelled" if cancelled else "failed" if int(result["districts_processed"] or 0) == int(result["failures"] or 0) and int(result["failures"] or 0) else "completed"
            conn.execute(
                "UPDATE board_sync_runs SET status = ?, finished_at = ? WHERE id = ?",
                (final_status, utc_now_iso(), run_id),
            )
            conn.commit()
        debug_log(debug_logger, "board_sync_finished", run_id=run_id, status=final_status)
    except Exception as exc:
        LOGGER.exception("Board sync run %s failed", run_id)
        with connect_db(db_path) as conn:
            conn.execute(
                "UPDATE board_sync_runs SET status = 'failed', error_message = ?, finished_at = ? WHERE id = ?",
                (str(exc), utc_now_iso(), run_id),
            )
            conn.commit()
    finally:
        if client is not None:
            client.close()


__all__ = [
    "board_sync_preview",
    "create_board_discovery_run",
    "create_board_sync_run",
    "execute_board_discovery_run",
    "execute_board_sync_run",
]
