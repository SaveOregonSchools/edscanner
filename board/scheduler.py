from __future__ import annotations

import calendar
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from common import connect_db, init_db, utc_now_iso


LOGGER = logging.getLogger(__name__)
FREQUENCIES = ("daily", "weekly", "monthly")
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
CLAIM_SECONDS = 300
DEFAULT_POLL_SECONDS = 30.0


class ScheduleValidationError(ValueError):
    pass


class DuplicateScheduleError(ScheduleValidationError):
    pass


@dataclass(frozen=True)
class ScheduleClaim:
    schedule_id: int
    token: str
    scheduled_for: str


def local_now() -> datetime:
    """Return the application server's local wall-clock time without sub-seconds."""

    return datetime.now().replace(microsecond=0)


def _naive_local(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _local_iso(value: datetime) -> str:
    return _naive_local(value).isoformat(timespec="seconds")


def validate_schedule_values(
    *,
    frequency: str,
    hour_24: int,
    minute: int,
    weekday: int | None = None,
    day_of_month: int | None = None,
) -> dict[str, int | str | None]:
    normalized_frequency = str(frequency or "").strip().casefold()
    if normalized_frequency not in FREQUENCIES:
        raise ScheduleValidationError("Frequency must be daily, weekly, or monthly.")
    try:
        normalized_hour = int(hour_24)
        normalized_minute = int(minute)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError("A valid run time is required.") from exc
    if not 0 <= normalized_hour <= 23:
        raise ScheduleValidationError("Run hour must be between 1 and 12 with AM or PM.")
    if not 0 <= normalized_minute <= 59:
        raise ScheduleValidationError("Run minute must be between 0 and 59.")

    normalized_weekday: int | None = None
    normalized_day: int | None = None
    if normalized_frequency == "weekly":
        try:
            normalized_weekday = int(weekday)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("Weekly schedules require a day of the week.") from exc
        if not 0 <= normalized_weekday <= 6:
            raise ScheduleValidationError("Weekly day must be Monday through Sunday.")
    elif normalized_frequency == "monthly":
        try:
            normalized_day = int(day_of_month)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("Monthly schedules require a day of the month.") from exc
        if not 1 <= normalized_day <= 31:
            raise ScheduleValidationError("Monthly day must be between 1 and 31.")

    return {
        "frequency": normalized_frequency,
        "hour_24": normalized_hour,
        "minute": normalized_minute,
        "weekday": normalized_weekday,
        "day_of_month": normalized_day,
    }


def calculate_next_run(
    *,
    frequency: str,
    hour_24: int,
    minute: int,
    weekday: int | None = None,
    day_of_month: int | None = None,
    after: datetime | None = None,
) -> datetime:
    """Return the first scheduled local wall-clock time strictly after ``after``."""

    values = validate_schedule_values(
        frequency=frequency,
        hour_24=hour_24,
        minute=minute,
        weekday=weekday,
        day_of_month=day_of_month,
    )
    cursor = _naive_local(after or local_now())
    run_time = {"hour": int(values["hour_24"]), "minute": int(values["minute"]), "second": 0}

    if values["frequency"] == "daily":
        candidate = cursor.replace(**run_time)
        if candidate <= cursor:
            candidate += timedelta(days=1)
        return candidate

    if values["frequency"] == "weekly":
        days_ahead = (int(values["weekday"]) - cursor.weekday()) % 7
        candidate = (cursor + timedelta(days=days_ahead)).replace(**run_time)
        if candidate <= cursor:
            candidate += timedelta(days=7)
        return candidate

    requested_day = int(values["day_of_month"])

    def in_month(year: int, month: int) -> datetime:
        actual_day = min(requested_day, calendar.monthrange(year, month)[1])
        return datetime(year, month, actual_day, **run_time)

    candidate = in_month(cursor.year, cursor.month)
    if candidate <= cursor:
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        candidate = in_month(year, month)
    return candidate


def _source_for_schedule(conn: Any, board_source_id: int) -> Mapping[str, Any]:
    row = conn.execute(
        """
        SELECT bs.*, d.agency_name, d.state, d.agency_type
        FROM board_sources bs
        JOIN districts d ON d.id = bs.district_id
        WHERE bs.id = ?
        """,
        (int(board_source_id),),
    ).fetchone()
    if row is None:
        raise LookupError(f"Board source not found: {board_source_id}")
    if not int(row["is_active"] or 0):
        raise ScheduleValidationError("Only an active board source can be scheduled.")
    if row["source_status"] != "working":
        raise ScheduleValidationError("Only a working board source can be scheduled.")
    return row


def create_schedule(
    *,
    board_source_id: int,
    frequency: str,
    hour_24: int,
    minute: int,
    weekday: int | None = None,
    day_of_month: int | None = None,
    enabled: bool = True,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path)
    values = validate_schedule_values(
        frequency=frequency,
        hour_24=hour_24,
        minute=minute,
        weekday=weekday,
        day_of_month=day_of_month,
    )
    current = _naive_local(now or local_now())
    next_run = calculate_next_run(**values, after=current)
    timestamp = _local_iso(current)
    with connect_db(db_path) as conn:
        _source_for_schedule(conn, board_source_id)
        try:
            cursor = conn.execute(
                """
                INSERT INTO board_sync_schedules (
                    board_source_id, frequency, weekday, day_of_month,
                    hour_24, minute, enabled, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(board_source_id),
                    values["frequency"],
                    values["weekday"],
                    values["day_of_month"],
                    values["hour_24"],
                    values["minute"],
                    1 if enabled else 0,
                    _local_iso(next_run),
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if "board_sync_schedules.board_source_id" in str(exc):
                raise DuplicateScheduleError("That board source already has a schedule.") from exc
            raise
    return int(cursor.lastrowid)


def update_schedule(
    schedule_id: int,
    *,
    frequency: str,
    hour_24: int,
    minute: int,
    weekday: int | None = None,
    day_of_month: int | None = None,
    enabled: bool = True,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> None:
    values = validate_schedule_values(
        frequency=frequency,
        hour_24=hour_24,
        minute=minute,
        weekday=weekday,
        day_of_month=day_of_month,
    )
    current = _naive_local(now or local_now())
    next_run = calculate_next_run(**values, after=current)
    with connect_db(db_path) as conn:
        changed = conn.execute(
            """
            UPDATE board_sync_schedules
            SET frequency = ?, weekday = ?, day_of_month = ?, hour_24 = ?, minute = ?,
                enabled = ?, next_run_at = ?, claim_token = NULL, claim_expires_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                values["frequency"],
                values["weekday"],
                values["day_of_month"],
                values["hour_24"],
                values["minute"],
                1 if enabled else 0,
                _local_iso(next_run),
                _local_iso(current),
                int(schedule_id),
            ),
        ).rowcount
        conn.commit()
    if not changed:
        raise LookupError(f"Board schedule not found: {schedule_id}")


def set_schedule_enabled(
    schedule_id: int,
    enabled: bool,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> None:
    current = _naive_local(now or local_now())
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM board_sync_schedules WHERE id = ?", (int(schedule_id),)).fetchone()
        if row is None:
            conn.rollback()
            raise LookupError(f"Board schedule not found: {schedule_id}")
        next_run = calculate_next_run(
            frequency=row["frequency"],
            weekday=row["weekday"],
            day_of_month=row["day_of_month"],
            hour_24=row["hour_24"],
            minute=row["minute"],
            after=current,
        )
        conn.execute(
            """
            UPDATE board_sync_schedules
            SET enabled = ?, next_run_at = ?, claim_token = NULL, claim_expires_at = NULL,
                last_error = NULL, updated_at = ? WHERE id = ?
            """,
            (1 if enabled else 0, _local_iso(next_run), _local_iso(current), int(schedule_id)),
        )
        conn.commit()


def claim_due_schedules(
    *,
    now: datetime | None = None,
    limit: int = 25,
    claim_seconds: int = CLAIM_SECONDS,
    db_path: Path | str | None = None,
    token_factory: Callable[[], str] | None = None,
) -> list[ScheduleClaim]:
    current = _naive_local(now or local_now())
    now_iso = _local_iso(current)
    expiry_iso = _local_iso(current + timedelta(seconds=max(30, int(claim_seconds))))
    make_token = token_factory or (lambda: uuid.uuid4().hex)
    claims: list[ScheduleClaim] = []
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, next_run_at FROM board_sync_schedules
            WHERE enabled = 1 AND next_run_at <= ?
              AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)
            ORDER BY next_run_at, id LIMIT ?
            """,
            (now_iso, now_iso, max(1, min(int(limit), 250))),
        ).fetchall()
        for row in rows:
            token = str(make_token())
            changed = conn.execute(
                """
                UPDATE board_sync_schedules SET claim_token = ?, claim_expires_at = ?, updated_at = ?
                WHERE id = ? AND enabled = 1 AND next_run_at = ?
                  AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)
                """,
                (token, expiry_iso, now_iso, row["id"], row["next_run_at"], now_iso),
            ).rowcount
            if changed:
                claims.append(ScheduleClaim(int(row["id"]), token, str(row["next_run_at"])))
        conn.commit()
    return claims


def _next_from_row(row: Mapping[str, Any], after: datetime) -> str:
    return _local_iso(
        calculate_next_run(
            frequency=str(row["frequency"]),
            weekday=row["weekday"],
            day_of_month=row["day_of_month"],
            hour_24=int(row["hour_24"]),
            minute=int(row["minute"]),
            after=after,
        )
    )


def materialize_claimed_schedule(
    claim: ScheduleClaim,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Atomically turn one claimed occurrence into one exact-source sync run."""

    current = _naive_local(now or local_now())
    current_iso = _local_iso(current)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT s.*, bs.district_id, bs.platform, bs.source_status, bs.is_active,
                   d.state, d.agency_type
            FROM board_sync_schedules s
            JOIN board_sources bs ON bs.id = s.board_source_id
            JOIN districts d ON d.id = bs.district_id
            WHERE s.id = ?
            """,
            (int(claim.schedule_id),),
        ).fetchone()
        if row is None or row["claim_token"] != claim.token or not int(row["enabled"] or 0):
            conn.rollback()
            return None

        existing = conn.execute(
            """
            SELECT board_sync_run_id FROM board_sync_schedule_events
            WHERE schedule_id = ? AND scheduled_for = ?
            """,
            (claim.schedule_id, claim.scheduled_for),
        ).fetchone()
        next_run_at = _next_from_row(row, current)
        if existing is not None:
            conn.execute(
                """
                UPDATE board_sync_schedules SET next_run_at = ?, claim_token = NULL,
                    claim_expires_at = NULL, updated_at = ? WHERE id = ?
                """,
                (next_run_at, current_iso, claim.schedule_id),
            )
            conn.commit()
            return int(existing["board_sync_run_id"]) if existing["board_sync_run_id"] else None

        if not int(row["is_active"] or 0):
            message = "The board source is no longer active; this schedule was disabled."
            conn.execute(
                """
                INSERT INTO board_sync_schedule_events
                    (schedule_id, scheduled_for, status, error_message, created_at)
                VALUES (?, ?, 'skipped', ?, ?)
                """,
                (claim.schedule_id, claim.scheduled_for, message, current_iso),
            )
            conn.execute(
                """
                UPDATE board_sync_schedules SET enabled = 0, next_run_at = ?, last_error = ?,
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?
                """,
                (next_run_at, message, current_iso, claim.schedule_id),
            )
            conn.commit()
            return None

        active_run = conn.execute(
            """
            SELECT r.id, r.status
            FROM board_sync_run_items item
            JOIN board_sync_runs r ON r.id = item.run_id
            WHERE item.board_source_id = ? AND r.status IN ('queued', 'running')
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (int(row["board_source_id"]),),
        ).fetchone()
        if active_run is not None:
            message = f"Skipped because sync #{int(active_run['id'])} is still {active_run['status']}."
            conn.execute(
                """
                INSERT INTO board_sync_schedule_events
                    (schedule_id, scheduled_for, status, error_message, created_at)
                VALUES (?, ?, 'skipped', ?, ?)
                """,
                (claim.schedule_id, claim.scheduled_for, message, current_iso),
            )
            conn.execute(
                """
                UPDATE board_sync_schedules
                SET next_run_at = ?, last_scheduled_for = ?, last_error = ?,
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_run_at, claim.scheduled_for, message, current_iso, claim.schedule_id),
            )
            conn.commit()
            return None

        queued_at = utc_now_iso()
        cursor = conn.execute(
            """
            INSERT INTO board_sync_runs (
                states_json, agency_types_json, platforms_json, source_status,
                sync_mode, force, max_districts, max_workers, debug_logging,
                status, districts_matched, districts_planned, queued_at
            ) VALUES (?, ?, ?, ?, 'monitor', 0, 1, ?, 0, 'queued', 1, 1, ?)
            """,
            (
                json.dumps([row["state"]] if row["state"] else []),
                json.dumps([row["agency_type"]] if row["agency_type"] else []),
                json.dumps([row["platform"]]),
                row["source_status"],
                1,
                queued_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO board_sync_run_items (run_id, district_id, board_source_id, status)
            VALUES (?, ?, ?, 'queued')
            """,
            (run_id, int(row["district_id"]), int(row["board_source_id"])),
        )
        conn.execute(
            """
            INSERT INTO board_sync_schedule_events (
                schedule_id, scheduled_for, board_sync_run_id, status, created_at
            ) VALUES (?, ?, ?, 'queued', ?)
            """,
            (claim.schedule_id, claim.scheduled_for, run_id, current_iso),
        )
        conn.execute(
            """
            UPDATE board_sync_schedules
            SET next_run_at = ?, last_scheduled_for = ?, last_run_at = ?,
                last_sync_run_id = ?, last_error = NULL, claim_token = NULL,
                claim_expires_at = NULL, updated_at = ? WHERE id = ?
            """,
            (
                next_run_at,
                claim.scheduled_for,
                current_iso,
                run_id,
                current_iso,
                claim.schedule_id,
            ),
        )
        conn.commit()
    return run_id


def _release_failed_claim(
    claim: ScheduleClaim,
    message: str,
    *,
    now: datetime,
    db_path: Path | str | None,
) -> None:
    try:
        with connect_db(db_path) as conn:
            conn.execute(
                """
                UPDATE board_sync_schedules
                SET claim_token = NULL, claim_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND claim_token = ?
                """,
                (message[:2000], _local_iso(now), claim.schedule_id, claim.token),
            )
            conn.commit()
    except Exception:
        LOGGER.exception("Could not release failed board schedule claim %s", claim.schedule_id)


def dispatch_due_schedules(
    enqueue_sync: Callable[[int], None],
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
    limit: int = 25,
) -> list[int]:
    current = _naive_local(now or local_now())
    run_ids: list[int] = []
    for claim in claim_due_schedules(now=current, limit=limit, db_path=db_path):
        try:
            run_id = materialize_claimed_schedule(claim, now=current, db_path=db_path)
            if run_id is not None:
                enqueue_sync(run_id)
                run_ids.append(run_id)
        except Exception as exc:
            LOGGER.exception("Scheduled board sync %s could not be queued", claim.schedule_id)
            _release_failed_claim(claim, str(exc), now=current, db_path=db_path)
    return run_ids


def run_scheduler_loop(
    enqueue_sync: Callable[[int], None],
    *,
    stop_event: threading.Event | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    clock: Callable[[], datetime] = local_now,
    db_path: Path | str | None = None,
) -> None:
    """Poll persistent due work; database claims make multiple app processes safe."""

    stopper = stop_event or threading.Event()
    delay = max(1.0, float(poll_seconds))
    while not stopper.is_set():
        try:
            dispatch_due_schedules(enqueue_sync, now=clock(), db_path=db_path)
        except Exception:
            LOGGER.exception("Board schedule polling failed")
        stopper.wait(delay)


__all__ = [
    "DuplicateScheduleError",
    "FREQUENCIES",
    "ScheduleClaim",
    "ScheduleValidationError",
    "WEEKDAYS",
    "calculate_next_run",
    "claim_due_schedules",
    "create_schedule",
    "dispatch_due_schedules",
    "local_now",
    "materialize_claimed_schedule",
    "run_scheduler_loop",
    "set_schedule_enabled",
    "update_schedule",
    "validate_schedule_values",
]
