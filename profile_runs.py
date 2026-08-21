from __future__ import annotations

import json
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import (
    MAX_TOTAL_DISTRICTS_PER_RUN,
    PROFILE_DISCOVERY_RUN_LOGS_DIR,
    PROFILE_DISCOVERY_WORKERS,
    connect_db,
    init_db,
    utc_now_iso,
)
from search_engine import RunDebugLogger, SearchSettings, clean_district_ids, debug_log
from site_search_discovery import discover_district_search_profile


LOGGER = logging.getLogger(__name__)

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
    values = [str(item) for item in parsed] if isinstance(parsed, list) else [str(parsed)]
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


def _scope_clauses(
    states: list[str] | None,
    agency_types: list[str] | None,
    min_enrollment: int | None,
    max_enrollment: int | None,
    district_ids: list[int] | tuple[int, ...] | None,
) -> tuple[list[str], list[Any]]:
    clauses = ["d.has_searchable_website = 1"]
    params: list[Any] = []
    states = [str(value).strip() for value in states or [] if str(value).strip()]
    agency_types = [str(value).strip() for value in agency_types or [] if str(value).strip()]
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
    cleaned_ids = clean_district_ids(district_ids)
    if cleaned_ids is not None:
        if cleaned_ids:
            clauses.append(f"d.id IN ({','.join('?' for _ in cleaned_ids)})")
            params.extend(cleaned_ids)
        else:
            clauses.append("1 = 0")
    return clauses, params


_LATEST_PROFILE_JOIN = """
LEFT JOIN (
    SELECT p1.*
    FROM district_search_profiles p1
    JOIN (
        SELECT district_id, MAX(id) AS id
        FROM district_search_profiles
        GROUP BY district_id
    ) latest ON latest.id = p1.id
) p ON p.district_id = d.id
"""


def district_search_coverage(
    states: list[str] | None = None,
    agency_types: list[str] | None = None,
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    *,
    district_ids: list[int] | tuple[int, ...] | None = None,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    init_db(db_path)
    stale_cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=180)
    ).isoformat()
    clauses, params = _scope_clauses(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        district_ids,
    )
    where_sql = " WHERE " + " AND ".join(clauses)
    with connect_db(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS matching_count,
                SUM(CASE WHEN p.profile_status = 'working'
                              AND p.search_method = 'GET'
                              AND COALESCE(p.search_url_template, '') != '' THEN 1 ELSE 0 END) AS working_count,
                SUM(CASE WHEN p.profile_status = 'requires_javascript'
                              AND p.search_method = 'GET'
                              AND COALESCE(p.search_url_template, '') != '' THEN 1 ELSE 0 END) AS javascript_count,
                SUM(CASE WHEN p.profile_status = 'requires_javascript'
                              AND (p.search_method != 'GET' OR COALESCE(p.search_url_template, '') = '') THEN 1 ELSE 0 END) AS javascript_unusable_count,
                SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END) AS missing_count,
                SUM(CASE WHEN p.profile_status = 'manual_review' THEN 1 ELSE 0 END) AS manual_review_count,
                SUM(CASE WHEN p.profile_status = 'error' THEN 1 ELSE 0 END) AS error_count,
                SUM(CASE WHEN p.profile_status = 'search_found_but_failed' THEN 1 ELSE 0 END) AS search_found_but_failed_count,
                SUM(CASE WHEN p.profile_status = 'blocked_by_challenge' THEN 1 ELSE 0 END) AS blocked_by_challenge_count,
                SUM(CASE WHEN p.profile_status = 'blocked_by_robots' THEN 1 ELSE 0 END) AS blocked_by_robots_count,
                SUM(CASE WHEN p.profile_status = 'no_search_found' THEN 1 ELSE 0 END) AS no_search_found_count,
                SUM(CASE WHEN p.profile_status = 'external_search_only' THEN 1 ELSE 0 END) AS external_search_only_count,
                SUM(CASE WHEN p.profile_status IN ('working', 'requires_javascript')
                              AND (p.last_discovered_at IS NULL OR p.last_discovered_at < ?)
                         THEN 1 ELSE 0 END) AS stale_count,
                SUM(CASE WHEN p.profile_status IN (
                    'error', 'search_found_but_failed', 'blocked_by_robots',
                    'blocked_by_challenge', 'no_search_found', 'external_search_only'
                ) THEN 1 ELSE 0 END) AS unavailable_count
            FROM districts d
            {_LATEST_PROFILE_JOIN}
            {where_sql}
            """,
            [stale_cutoff, *params],
        ).fetchone()
    return {
        "matching_count": int(row["matching_count"] or 0),
        "working_count": int(row["working_count"] or 0),
        "javascript_count": int(row["javascript_count"] or 0),
        "javascript_unusable_count": int(row["javascript_unusable_count"] or 0),
        "missing_count": int(row["missing_count"] or 0),
        "manual_review_count": int(row["manual_review_count"] or 0),
        "error_count": int(row["error_count"] or 0),
        "search_found_but_failed_count": int(row["search_found_but_failed_count"] or 0),
        "blocked_by_challenge_count": int(row["blocked_by_challenge_count"] or 0),
        "blocked_by_robots_count": int(row["blocked_by_robots_count"] or 0),
        "no_search_found_count": int(row["no_search_found_count"] or 0),
        "external_search_only_count": int(row["external_search_only_count"] or 0),
        "stale_count": int(row["stale_count"] or 0),
        "unavailable_count": int(row["unavailable_count"] or 0),
    }


def list_profile_filtered_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_statuses: list[str],
    provider_guess: str,
    limit: int,
    *,
    district_ids: list[int] | tuple[int, ...] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses, params = _scope_clauses(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        district_ids,
    )
    add_profile_status_filter_sql(clauses, params, profile_statuses)
    if provider_guess:
        clauses.append("p.provider_guess = ?")
        params.append(provider_guess)
    where_sql = " WHERE " + " AND ".join(clauses)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT d.*
            FROM districts d
            {_LATEST_PROFILE_JOIN}
            {where_sql}
            ORDER BY d.state, d.agency_name, d.id
            LIMIT ?
            """,
            [*params, max(0, int(limit))],
        ).fetchall()
    return [dict(row) for row in rows]


def count_profile_filtered_districts(
    states: list[str],
    agency_types: list[str],
    min_enrollment: int | None,
    max_enrollment: int | None,
    profile_statuses: list[str],
    provider_guess: str,
    *,
    district_ids: list[int] | tuple[int, ...] | None = None,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    clauses, params = _scope_clauses(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        district_ids,
    )
    add_profile_status_filter_sql(clauses, params, profile_statuses)
    if provider_guess:
        clauses.append("p.provider_guess = ?")
        params.append(provider_guess)
    where_sql = " WHERE " + " AND ".join(clauses)
    with connect_db(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM districts d
            {_LATEST_PROFILE_JOIN}
            {where_sql}
            """,
            params,
        ).fetchone()
    return int(row["count"] or 0)


def _insert_profile_run_items(conn: Any, run_id: int, districts: list[dict[str, Any]]) -> None:
    now = utc_now_iso()
    conn.executemany(
        """
        INSERT OR IGNORE INTO profile_discovery_run_items (
            run_id, district_id, ordinal, status, attempt, profile_id,
            profile_status, started_at, finished_at, error_message,
            created_at, updated_at, queued_at
        )
        VALUES (?, ?, ?, 'queued', 0, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
        """,
        [
            (run_id, int(district["id"]), ordinal, now, now, now)
            for ordinal, district in enumerate(districts, start=1)
        ],
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
    *,
    district_ids: list[int] | tuple[int, ...] | None = None,
    status: str = "queued",
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    status = str(status or "").strip()
    if status not in {"queued", "staging"}:
        raise ValueError("Profile discovery run status must be queued or staging.")
    cleaned_ids = clean_district_ids(district_ids)
    cap_limit = (
        MAX_TOTAL_DISTRICTS_PER_RUN
        if cleaned_ids is not None
        else PROFILE_DISCOVERY_MAX_DISTRICTS
    )
    district_cap = max(1, min(int(max_districts), cap_limit))
    matched_count = count_profile_filtered_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        profile_statuses,
        provider_guess,
        district_ids=cleaned_ids,
        db_path=db_path,
    )
    districts = list_profile_filtered_districts(
        states,
        agency_types,
        min_enrollment,
        max_enrollment,
        profile_statuses,
        provider_guess,
        district_cap,
        district_ids=cleaned_ids,
        db_path=db_path,
    )
    now = utc_now_iso()
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO profile_discovery_runs (
                states_json, agency_types_json, min_enrollment, max_enrollment,
                profile_status_filter, provider_guess_filter, max_districts,
                max_workers, test_query, force, cancel_requested, status, districts_matched,
                districts_planned, districts_processed, profiles_working,
                profiles_failed, profiles_manual_review, profiles_requires_javascript, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 0, 0, 0, ?)
            """,
            (
                json.dumps(states),
                json.dumps(agency_types),
                min_enrollment,
                max_enrollment,
                profile_status_filter_to_json(profile_statuses),
                provider_guess,
                district_cap,
                max(1, min(int(max_workers), 8)),
                test_query,
                1 if force else 0,
                status,
                matched_count,
                len(districts),
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        _insert_profile_run_items(conn, run_id, districts)
        conn.commit()
    return run_id


def is_profile_discovery_cancel_requested(
    run_id: int,
    db_path: Path | str | None = None,
) -> bool:
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM profile_discovery_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return bool(row and row["cancel_requested"])


def _profile_progress(conn: Any, run_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END) AS processed,
            SUM(CASE WHEN status = 'completed' AND profile_status = 'working' THEN 1 ELSE 0 END) AS working,
            SUM(CASE WHEN status = 'completed' AND profile_status = 'manual_review' THEN 1 ELSE 0 END) AS manual_review,
            SUM(CASE WHEN status = 'completed' AND profile_status = 'requires_javascript' THEN 1 ELSE 0 END) AS requires_javascript,
            SUM(CASE WHEN status = 'failed' OR (
                status = 'completed' AND COALESCE(profile_status, 'error') NOT IN (
                    'working', 'manual_review', 'requires_javascript'
                )
            ) THEN 1 ELSE 0 END) AS failed
        FROM profile_discovery_run_items
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return {
        "processed": int(row["processed"] or 0),
        "working": int(row["working"] or 0),
        "manual_review": int(row["manual_review"] or 0),
        "requires_javascript": int(row["requires_javascript"] or 0),
        "failed": int(row["failed"] or 0),
    }


def _update_profile_run_progress(conn: Any, run_id: int) -> dict[str, int]:
    progress = _profile_progress(conn, run_id)
    conn.execute(
        """
        UPDATE profile_discovery_runs
        SET districts_processed = ?, profiles_working = ?, profiles_failed = ?,
            profiles_manual_review = ?, profiles_requires_javascript = ?
        WHERE id = ?
        """,
        (
            progress["processed"],
            progress["working"],
            progress["failed"],
            progress["manual_review"],
            progress["requires_javascript"],
            run_id,
        ),
    )
    return progress


def _ensure_profile_run_items(run: dict[str, Any], db_path: Path | str | None) -> None:
    with connect_db(db_path) as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM profile_discovery_run_items WHERE run_id = ?",
                (run["id"],),
            ).fetchone()["count"]
            or 0
        )
    if count or not int(run.get("districts_matched") or 0):
        return
    districts = list_profile_filtered_districts(
        json.loads(run.get("states_json") or "[]"),
        json.loads(run.get("agency_types_json") or "[]"),
        run.get("min_enrollment"),
        run.get("max_enrollment"),
        profile_status_filter_from_value(run.get("profile_status_filter")),
        run.get("provider_guess_filter") or "",
        int(run.get("max_districts") or 1),
        db_path=db_path,
    )
    with connect_db(db_path) as conn:
        _insert_profile_run_items(conn, int(run["id"]), districts)
        conn.execute(
            "UPDATE profile_discovery_runs SET districts_planned = ? WHERE id = ?",
            (len(districts), run["id"]),
        )
        conn.commit()


def execute_profile_discovery_run(
    run_id: int,
    *,
    db_path: Path | str | None = None,
) -> None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        loaded = conn.execute(
            "SELECT * FROM profile_discovery_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    if loaded is None:
        raise ValueError(f"Profile discovery run not found: {run_id}")
    run = dict(loaded)
    _ensure_profile_run_items(run, db_path)

    debug_path = PROFILE_DISCOVERY_RUN_LOGS_DIR / f"profile-discovery-run-{run_id}.log"
    debug_logger = RunDebugLogger(debug_path)
    max_workers = max(1, min(int(run.get("max_workers") or PROFILE_DISCOVERY_WORKERS), 8))
    settings = SearchSettings(max_pages_per_district=10)

    if bool(run.get("cancel_requested")):
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    error_message = 'Cancelled before start.'
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, now, run_id),
            )
            progress = _update_profile_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'cancelled', finished_at = ?, error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (now, run_id),
            )
            conn.commit()
        debug_log(debug_logger, "profile_discovery_run_cancelled", run_id=run_id, **progress)
        return

    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status, cancel_requested FROM profile_discovery_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if current is None or current["status"] != "queued":
            conn.rollback()
            return
        if current["cancel_requested"]:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    error_message = 'Cancelled before dispatch.'
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, now, run_id),
            )
            progress = _update_profile_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'cancelled', finished_at = ?, error_message = 'Cancelled before start.'
                WHERE id = ?
                """,
                (now, run_id),
            )
            conn.commit()
            return
        progress = _update_profile_run_progress(conn, run_id)
        claimed = conn.execute(
            """
            UPDATE profile_discovery_runs
            SET status = 'running', max_workers = ?, debug_log_path = ?,
                finished_at = NULL, error_message = NULL
            WHERE id = ? AND status = 'queued'
            """,
            (max_workers, str(debug_path), run_id),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return
        conn.commit()

    with connect_db(db_path) as conn:
        pending_rows = conn.execute(
            """
            SELECT d.*, i.ordinal AS run_item_ordinal
            FROM profile_discovery_run_items i
            JOIN districts d ON d.id = i.district_id
            WHERE i.run_id = ? AND i.status = 'queued'
            ORDER BY i.ordinal
            """,
            (run_id,),
        ).fetchall()
    pending_districts = [dict(row) for row in pending_rows]

    def claim_item(district_id: int) -> bool:
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            cursor = conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'running', attempt = attempt + 1, started_at = ?,
                    updated_at = ?, finished_at = NULL, error_message = NULL
                WHERE run_id = ? AND district_id = ? AND status = 'queued'
                """,
                (now, now, run_id, district_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def discover_one(district: dict[str, Any]) -> dict[str, Any]:
        return discover_district_search_profile(
            district,
            test_query=run.get("test_query") or "calendar",
            settings=settings,
            force=bool(run.get("force")),
            debug_logger=debug_logger,
            db_path=db_path,
        )

    def complete_item(district: dict[str, Any], profile: dict[str, Any]) -> dict[str, int]:
        status = str(profile.get("profile_status") or "error")
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'completed', profile_id = ?, profile_status = ?,
                    finished_at = ?, updated_at = ?, error_message = NULL
                WHERE run_id = ? AND district_id = ?
                """,
                (profile.get("id"), status, now, now, run_id, district["id"]),
            )
            progress = _update_profile_run_progress(conn, run_id)
            conn.commit()
        debug_log(
            debug_logger,
            "profile_discovery_district_finish",
            run_id=run_id,
            district=district.get("agency_name"),
            status=status,
            provider_guess=profile.get("provider_guess") or "",
        )
        return progress

    def fail_item(district: dict[str, Any], error: BaseException) -> dict[str, int]:
        with connect_db(db_path) as conn:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'failed', profile_status = 'error', finished_at = ?,
                    updated_at = ?, error_message = ?
                WHERE run_id = ? AND district_id = ?
                """,
                (now, now, str(error), run_id, district["id"]),
            )
            progress = _update_profile_run_progress(conn, run_id)
            conn.commit()
        return progress

    cancelled = False
    next_index = 0
    futures: dict[Future[dict[str, Any]], dict[str, Any]] = {}
    try:
        debug_log(
            debug_logger,
            "profile_discovery_run_start",
            run_id=run_id,
            district_count=len(pending_districts),
            max_workers=max_workers,
            test_query=run.get("test_query") or "calendar",
            force=bool(run.get("force")),
        )
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ProfileDiscovery") as executor:
            while next_index < len(pending_districts) or futures:
                if is_profile_discovery_cancel_requested(run_id, db_path):
                    cancelled = True
                while (
                    not cancelled
                    and next_index < len(pending_districts)
                    and len(futures) < max_workers
                ):
                    district = pending_districts[next_index]
                    next_index += 1
                    if not claim_item(int(district["id"])):
                        continue
                    futures[executor.submit(discover_one, district)] = district
                if not futures:
                    break
                completed, _not_done = wait(tuple(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                if not completed:
                    continue
                for future in completed:
                    district = futures.pop(future)
                    try:
                        profile = future.result()
                        progress = complete_item(district, profile)
                    except Exception as exc:
                        LOGGER.exception("Profile discovery failed for %s: %s", district.get("agency_name"), exc)
                        debug_log(
                            debug_logger,
                            "profile_discovery_district_error",
                            run_id=run_id,
                            district=district.get("agency_name"),
                            error=str(exc),
                        )
                        progress = fail_item(district, exc)

        if cancelled:
            with connect_db(db_path) as conn:
                now = utc_now_iso()
                conn.execute(
                    """
                    UPDATE profile_discovery_run_items
                    SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        error_message = 'Cancelled before dispatch.'
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (now, now, run_id),
                )
                conn.commit()

        with connect_db(db_path) as conn:
            progress = _update_profile_run_progress(conn, run_id)
            cancel_flag = bool(
                conn.execute(
                    "SELECT cancel_requested FROM profile_discovery_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()["cancel_requested"]
            )
            final_status = "cancelled" if cancelled or cancel_flag else "completed"
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    utc_now_iso(),
                    "Cancelled by user." if final_status == "cancelled" else None,
                    run_id,
                ),
            )
            conn.commit()
        debug_log(
            debug_logger,
            "profile_discovery_run_finish",
            run_id=run_id,
            status=final_status,
            **progress,
        )
    except Exception as exc:
        with connect_db(db_path) as conn:
            progress = _update_profile_run_progress(conn, run_id)
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = 'failed', finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (utc_now_iso(), str(exc), run_id),
            )
            conn.commit()
        debug_log(debug_logger, "profile_discovery_run_failed", run_id=run_id, error=str(exc), **progress)
        raise
