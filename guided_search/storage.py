"""SQLite persistence primitives for the Guided District Search state machine.

The module owns storage validation and atomic state changes but performs no AI,
HTTP, Flask, or queue work. Callers may therefore exercise orchestration with a
temporary database and deterministic fakes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from common import connect_db, init_db, json_dumps, utc_now_iso


GUIDED_SEARCH_STATUSES = (
    "draft",
    "planning",
    "needs_clarification",
    "ready",
    "queued",
    "profiling",
    "searching",
    "evaluating",
    "replanning",
    "summarizing",
    "completed",
    "needs_review",
    "cancelled",
    "failed",
)
GUIDED_SEARCH_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
GUIDED_SEARCH_ACTIVE_STATUSES = frozenset(
    {
        "planning",
        "queued",
        "profiling",
        "searching",
        "evaluating",
        "replanning",
        "summarizing",
    }
)
GUIDED_SEARCH_STRATEGY_MODES = frozenset({"fast", "balanced", "thorough"})
GUIDED_SEARCH_CHILD_TYPES = frozenset({"search", "profile_discovery"})
RUN_ITEM_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled", "skipped"}
)

ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"planning", "cancelled"}),
    "planning": frozenset(
        {"needs_clarification", "ready", "needs_review", "cancelled", "failed"}
    ),
    "needs_clarification": frozenset({"planning", "cancelled", "failed"}),
    "ready": frozenset({"queued", "planning", "cancelled", "failed"}),
    "queued": frozenset(
        {
            "planning",
            "profiling",
            "searching",
            "evaluating",
            "summarizing",
            "needs_review",
            "cancelled",
            "failed",
        }
    ),
    "profiling": frozenset(
        {
            "searching",
            "evaluating",
            "replanning",
            "summarizing",
            "needs_review",
            "cancelled",
            "failed",
        }
    ),
    "searching": frozenset(
        {"evaluating", "summarizing", "needs_review", "cancelled", "failed"}
    ),
    "evaluating": frozenset(
        {
            "replanning",
            "searching",
            "needs_clarification",
            "summarizing",
            "completed",
            "needs_review",
            "cancelled",
            "failed",
        }
    ),
    "replanning": frozenset(
        {
            "profiling",
            "searching",
            "needs_clarification",
            "summarizing",
            "needs_review",
            "cancelled",
            "failed",
        }
    ),
    "summarizing": frozenset(
        {"completed", "needs_review", "cancelled", "failed"}
    ),
    "completed": frozenset(),
    "needs_review": frozenset(
        {
            "planning",
            "queued",
            "profiling",
            "searching",
            "evaluating",
            "replanning",
            "summarizing",
            "cancelled",
            "failed",
        }
    ),
    "cancelled": frozenset(),
    "failed": frozenset(),
}


class GuidedSearchStorageError(RuntimeError):
    """Base error for persisted Guided Search state."""


class SessionNotFoundError(GuidedSearchStorageError):
    pass


class StepNotFoundError(GuidedSearchStorageError):
    pass


class InvalidStatusTransitionError(GuidedSearchStorageError):
    pass


class ConcurrentUpdateError(GuidedSearchStorageError):
    pass


class CohortAlreadyFrozenError(GuidedSearchStorageError):
    pass


_UNSET = object()
_SENSITIVE_METADATA_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)

_SESSION_JSON_ALIASES = {
    "examples": "examples_json",
    "scope": "scope_json",
    "latest_plan": "latest_plan_json",
    "latest_evaluation": "latest_evaluation_json",
    "prompt_versions": "prompt_versions_json",
    "resource_policy": "resource_policy_json",
    "profile_coverage": "profile_coverage_json",
    "clarification_questions": "clarification_questions_json",
    "clarification_answers": "clarification_answers_json",
}
_SESSION_JSON_COLUMNS = frozenset(_SESSION_JSON_ALIASES.values())
_SESSION_BOOLEAN_COLUMNS = frozenset(
    {"brave_allowed", "cohort_frozen", "cancel_requested"}
)
_SESSION_UPDATE_COLUMNS = frozenset(
    {
        "example_text",
        "example_url",
        "example_context",
        "examples_json",
        "scope_json",
        "strategy_mode",
        "brave_allowed",
        "stage",
        "latest_plan_json",
        "latest_evaluation_json",
        "final_summary",
        "ai_model",
        "prompt_versions_json",
        "resource_policy_json",
        "profile_coverage_json",
        "current_step_id",
        "current_workers",
        "current_delay_seconds",
        "round_number",
        "max_rounds",
        "max_child_search_runs",
        "district_count",
        "districts_completed",
        "results_found",
        "failures",
        "clarification_round",
        "clarification_questions_json",
        "clarification_answers_json",
        "queued_at",
        "started_at",
        "finished_at",
        "next_wake_at",
        "last_heartbeat_at",
        "error_message",
        "review_reason",
        "debug_log_path",
    }
)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _load_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _safe_audit_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_METADATA_MARKERS):
                safe[key] = "[redacted]"
            else:
                safe[key] = _safe_audit_metadata(raw_value)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_audit_metadata(item) for item in value]
    return value


def _safe_endpoint_identity(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        if parsed.scheme and host:
            host_text = f"[{host}]" if ":" in host else host
            netloc = host_text if parsed.port is None else f"{host_text}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        pass
    return text[:500]


def _session_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _dict(row)
    if item is None:
        return None
    for alias, column in _SESSION_JSON_ALIASES.items():
        default: Any = [] if alias in {
            "examples",
            "clarification_questions",
            "clarification_answers",
        } else {}
        if alias in {"latest_plan", "latest_evaluation"}:
            default = None
        item[alias] = _load_json(item.get(column), default)
    for column in _SESSION_BOOLEAN_COLUMNS:
        item[column] = bool(item.get(column))
    return item


def _step_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _dict(row)
    if item is not None:
        item["input"] = _load_json(item.get("input_json"), None)
        item["output"] = _load_json(item.get("output_json"), None)
    return item


def _model_call_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _dict(row)
    if item is not None:
        item["success"] = bool(item.get("success"))
        item["metadata"] = _load_json(item.get("metadata_json"), None)
    return item


def _evidence_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _dict(row)
    if item is not None:
        item["evaluation"] = _load_json(item.get("evaluation_json"), None)
    return item


def _normalize_ids(values: Iterable[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        district_id = int(value)
        if district_id <= 0:
            raise ValueError("District IDs must be positive integers.")
        if district_id not in seen:
            out.append(district_id)
            seen.add(district_id)
    return out


def _validate_district_ids(conn: sqlite3.Connection, district_ids: Sequence[int]) -> None:
    found: set[int] = set()
    for offset in range(0, len(district_ids), 500):
        chunk = district_ids[offset : offset + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM districts WHERE id IN ({placeholders})", chunk
            )
        )
    missing = [district_id for district_id in district_ids if district_id not in found]
    if missing:
        preview = ", ".join(str(value) for value in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise ValueError(f"Unknown district IDs: {preview}{suffix}")


def _require_session_row(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM guided_search_sessions WHERE id = ?", (int(session_id),)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"Guided Search session not found: {session_id}")
    return row


def _require_step_row(conn: sqlite3.Connection, step_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM guided_search_steps WHERE id = ?", (int(step_id),)
    ).fetchone()
    if row is None:
        raise StepNotFoundError(f"Guided Search step not found: {step_id}")
    return row


def _prepare_session_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "status":
            raise ValueError("Use transition_session() to update session status.")
        column = _SESSION_JSON_ALIASES.get(key, key)
        if column not in _SESSION_UPDATE_COLUMNS:
            raise ValueError(f"Unsupported Guided Search session field: {key}")
        if column in _SESSION_JSON_COLUMNS and value is not None:
            if key in _SESSION_JSON_ALIASES or not isinstance(value, str):
                value = json_dumps(value)
        if column in _SESSION_BOOLEAN_COLUMNS and value is not None:
            value = 1 if bool(value) else 0
        if column == "strategy_mode" and value not in GUIDED_SEARCH_STRATEGY_MODES:
            raise ValueError(f"Unsupported Guided Search strategy mode: {value}")
        prepared[column] = value
    return prepared


def create_session(
    original_objective: str,
    *,
    example_text: str | None = None,
    example_url: str | None = None,
    example_context: str | None = None,
    examples: Sequence[Mapping[str, Any]] | None = None,
    scope: Mapping[str, Any] | None = None,
    strategy_mode: str = "balanced",
    brave_allowed: bool = False,
    status: str = "draft",
    stage: str | None = None,
    ai_model: str | None = None,
    prompt_versions: Mapping[str, str] | None = None,
    resource_policy: Mapping[str, Any] | None = None,
    max_rounds: int = 0,
    max_child_search_runs: int = 0,
    debug_log_path: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    objective = str(original_objective or "")
    if not objective.strip():
        raise ValueError("A Guided Search objective is required.")
    if strategy_mode not in GUIDED_SEARCH_STRATEGY_MODES:
        raise ValueError(f"Unsupported Guided Search strategy mode: {strategy_mode}")
    if status not in GUIDED_SEARCH_STATUSES:
        raise ValueError(f"Unsupported Guided Search status: {status}")
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO guided_search_sessions (
                original_objective, example_text, example_url, example_context,
                examples_json, scope_json, strategy_mode, brave_allowed, status,
                stage, ai_model, prompt_versions_json, resource_policy_json,
                max_rounds, max_child_search_runs, created_at, updated_at,
                debug_log_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                objective,
                example_text,
                example_url,
                example_context,
                json_dumps(list(examples or [])),
                json_dumps(dict(scope or {})),
                strategy_mode,
                1 if brave_allowed else 0,
                status,
                stage or status,
                ai_model,
                json_dumps(dict(prompt_versions or {})),
                json_dumps(dict(resource_policy or {})),
                max(0, int(max_rounds)),
                max(0, int(max_child_search_runs)),
                now,
                now,
                debug_log_path,
            ),
        )
        return int(cursor.lastrowid)


def get_session(
    session_id: int, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM guided_search_sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
    return _session_dict(row)


def list_sessions(
    *,
    statuses: Iterable[str] | None = None,
    due_before: str | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    status_values = [str(value) for value in statuses or []]
    if any(value not in GUIDED_SEARCH_STATUSES for value in status_values):
        raise ValueError("Unsupported Guided Search status filter.")
    if status_values:
        clauses.append(f"status IN ({','.join('?' for _ in status_values)})")
        params.extend(status_values)
    if due_before is not None:
        clauses.append("(next_wake_at IS NULL OR next_wake_at <= ?)")
        params.append(due_before)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    init_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM guided_search_sessions{where_sql} ORDER BY id LIMIT ?",
            [*params, max(1, min(int(limit), 1000))],
        ).fetchall()
    return [_session_dict(row) for row in rows if row is not None]


def update_session(
    session_id: int,
    *,
    expected_version: int | None = None,
    db_path: Path | str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    prepared = _prepare_session_changes(changes)
    if not prepared:
        session = get_session(session_id, db_path)
        if session is None:
            raise SessionNotFoundError(f"Guided Search session not found: {session_id}")
        return session
    now = utc_now_iso()
    assignments = [f"{column} = ?" for column in prepared]
    params = list(prepared.values())
    assignments.extend(["updated_at = ?", "version = version + 1"])
    params.append(now)
    where = "id = ?"
    params.append(int(session_id))
    if expected_version is not None:
        where += " AND version = ?"
        params.append(int(expected_version))
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE guided_search_sessions SET {', '.join(assignments)} WHERE {where}",
            params,
        )
        if not cursor.rowcount:
            if conn.execute(
                "SELECT 1 FROM guided_search_sessions WHERE id = ?", (int(session_id),)
            ).fetchone() is None:
                raise SessionNotFoundError(
                    f"Guided Search session not found: {session_id}"
                )
            raise ConcurrentUpdateError(
                f"Guided Search session {session_id} was updated concurrently."
            )
        row = _require_session_row(conn, session_id)
    session = _session_dict(row)
    assert session is not None
    return session


def transition_session(
    session_id: int,
    new_status: str,
    *,
    stage: str | None = None,
    expected_status: str | Iterable[str] | None = None,
    updates: Mapping[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    if new_status not in GUIDED_SEARCH_STATUSES:
        raise ValueError(f"Unsupported Guided Search status: {new_status}")
    prepared = _prepare_session_changes(dict(updates or {}))
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _require_session_row(conn, session_id)
        current_status = str(current["status"])
        if expected_status is not None:
            expected = (
                {expected_status}
                if isinstance(expected_status, str)
                else {str(value) for value in expected_status}
            )
            if current_status not in expected:
                raise ConcurrentUpdateError(
                    f"Expected session {session_id} in {sorted(expected)}, found {current_status}."
                )
        if (
            new_status != current_status
            and new_status not in ALLOWED_STATUS_TRANSITIONS[current_status]
        ):
            raise InvalidStatusTransitionError(
                f"Cannot transition Guided Search session {session_id} "
                f"from {current_status} to {new_status}."
            )
        if (
            bool(current["cancel_requested"])
            and new_status != current_status
            and new_status not in {"cancelled", "failed"}
        ):
            raise InvalidStatusTransitionError(
                f"Guided Search session {session_id} has a pending cancellation request."
            )
        prepared["stage"] = stage or new_status
        if new_status == "queued" and not current["queued_at"]:
            prepared["queued_at"] = now
        if new_status in GUIDED_SEARCH_ACTIVE_STATUSES and not current["started_at"]:
            prepared["started_at"] = now
        if new_status in GUIDED_SEARCH_TERMINAL_STATUSES:
            prepared["finished_at"] = now
            prepared["next_wake_at"] = None
            prepared["claim_token"] = None
            prepared["claim_expires_at"] = None
        if new_status == "cancelled":
            prepared["cancel_requested"] = 1
        assignments = ["status = ?"]
        params: list[Any] = [new_status]
        assignments.extend(f"{column} = ?" for column in prepared)
        params.extend(prepared.values())
        assignments.extend(["updated_at = ?", "version = version + 1"])
        params.extend([now, int(session_id), int(current["version"])])
        cursor = conn.execute(
            f"""
            UPDATE guided_search_sessions
            SET {', '.join(assignments)}
            WHERE id = ? AND version = ?
            """,
            params,
        )
        if not cursor.rowcount:
            raise ConcurrentUpdateError(
                f"Guided Search session {session_id} was updated concurrently."
            )
        row = _require_session_row(conn, session_id)
    session = _session_dict(row)
    assert session is not None
    return session


def claim_session(
    session_id: int,
    *,
    claim_token: str | None = None,
    claim_seconds: int = 300,
    statuses: Iterable[str] | None = None,
    db_path: Path | str | None = None,
) -> str | None:
    token = str(claim_token or secrets.token_urlsafe(24))
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()
    expires = (now_dt + timedelta(seconds=max(30, int(claim_seconds)))).isoformat()
    status_values = [str(value) for value in (statuses or GUIDED_SEARCH_ACTIVE_STATUSES)]
    if not status_values or any(value not in GUIDED_SEARCH_STATUSES for value in status_values):
        raise ValueError("claim_session() requires valid Guided Search statuses.")
    placeholders = ",".join("?" for _ in status_values)
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            f"""
            UPDATE guided_search_sessions
            SET claim_token = ?, claim_expires_at = ?, last_heartbeat_at = ?,
                updated_at = ?, version = version + 1
            WHERE id = ?
              AND status IN ({placeholders})
              AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)
            """,
            (token, expires, now, now, int(session_id), *status_values, now),
        )
    return token if cursor.rowcount else None


def heartbeat_session_claim(
    session_id: int,
    claim_token: str,
    *,
    claim_seconds: int = 300,
    db_path: Path | str | None = None,
) -> bool:
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()
    expires = (now_dt + timedelta(seconds=max(30, int(claim_seconds)))).isoformat()
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE guided_search_sessions
            SET claim_expires_at = ?, last_heartbeat_at = ?, updated_at = ?,
                version = version + 1
            WHERE id = ? AND claim_token = ?
            """,
            (expires, now, now, int(session_id), str(claim_token)),
        )
    return bool(cursor.rowcount)


def release_session_claim(
    session_id: int,
    claim_token: str,
    *,
    next_wake_at: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE guided_search_sessions
            SET claim_token = NULL, claim_expires_at = NULL, next_wake_at = ?,
                updated_at = ?, version = version + 1
            WHERE id = ? AND claim_token = ?
            """,
            (next_wake_at, now, int(session_id), str(claim_token)),
        )
    return bool(cursor.rowcount)


def request_cancellation(
    session_id: int,
    *,
    reason: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        current = _require_session_row(conn, session_id)
        if current["status"] not in GUIDED_SEARCH_TERMINAL_STATUSES:
            conn.execute(
                """
                UPDATE guided_search_sessions
                SET cancel_requested = 1,
                    error_message = COALESCE(?, error_message),
                    next_wake_at = ?,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (reason, now, now, int(session_id)),
            )
        row = _require_session_row(conn, session_id)
    session = _session_dict(row)
    assert session is not None
    return session


def cancellation_requested(
    session_id: int, db_path: Path | str | None = None
) -> bool:
    init_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM guided_search_sessions WHERE id = ?",
            (int(session_id),),
        ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"Guided Search session not found: {session_id}")
    return bool(row["cancel_requested"])


def freeze_cohort(
    session_id: int,
    district_ids: Iterable[int],
    *,
    db_path: Path | str | None = None,
) -> int:
    normalized = _normalize_ids(district_ids)
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = _require_session_row(conn, session_id)
        existing = [
            int(row["district_id"])
            for row in conn.execute(
                """
                SELECT district_id
                FROM guided_search_session_districts
                WHERE session_id = ?
                ORDER BY ordinal
                """,
                (int(session_id),),
            )
        ]
        if bool(session["cohort_frozen"]):
            if existing != normalized:
                raise CohortAlreadyFrozenError(
                    f"Guided Search session {session_id} already has a frozen district cohort."
                )
            return len(existing)
        _validate_district_ids(conn, normalized)
        conn.executemany(
            """
            INSERT INTO guided_search_session_districts (
                session_id, district_id, ordinal, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (int(session_id), district_id, ordinal, now)
                for ordinal, district_id in enumerate(normalized, start=1)
            ],
        )
        conn.execute(
            """
            UPDATE guided_search_sessions
            SET cohort_frozen = 1, district_count = ?, updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (len(normalized), now, int(session_id)),
        )
    return len(normalized)


def get_cohort(
    session_id: int, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        rows = conn.execute(
            """
            SELECT sd.ordinal, sd.created_at AS cohort_created_at, d.*
            FROM guided_search_session_districts sd
            JOIN districts d ON d.id = sd.district_id
            WHERE sd.session_id = ?
            ORDER BY sd.ordinal
            """,
            (int(session_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_cohort_ids(
    session_id: int, db_path: Path | str | None = None
) -> list[int]:
    return [int(row["id"]) for row in get_cohort(session_id, db_path)]


def _freeze_run_cohort(
    *,
    item_table: str,
    run_table: str,
    run_id: int,
    district_ids: Iterable[int],
    db_path: Path | str | None,
) -> int:
    if item_table not in {"search_run_items", "profile_discovery_run_items"}:
        raise ValueError("Unexpected run-item table.")
    if run_table not in {"search_runs", "profile_discovery_runs"}:
        raise ValueError("Unexpected run table.")
    normalized = _normalize_ids(district_ids)
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            f"SELECT 1 FROM {run_table} WHERE id = ?", (int(run_id),)
        ).fetchone() is None:
            raise ValueError(f"Run not found in {run_table}: {run_id}")
        existing = [
            int(row["district_id"])
            for row in conn.execute(
                f"SELECT district_id FROM {item_table} WHERE run_id = ? ORDER BY ordinal",
                (int(run_id),),
            )
        ]
        if existing:
            if set(existing) != set(normalized):
                raise CohortAlreadyFrozenError(
                    f"Run {run_id} already has a different frozen district cohort."
                )
            return len(existing)
        _validate_district_ids(conn, normalized)
        conn.executemany(
            f"""
            INSERT INTO {item_table} (
                run_id, district_id, ordinal, status, attempt, result_count,
                created_at, updated_at, queued_at
            ) VALUES (?, ?, ?, 'queued', 0, 0, ?, ?, ?)
            """,
            [
                (int(run_id), district_id, ordinal, now, now, now)
                for ordinal, district_id in enumerate(normalized, start=1)
            ],
        )
    return len(normalized)


def freeze_search_run_cohort(
    run_id: int,
    district_ids: Iterable[int],
    *,
    db_path: Path | str | None = None,
) -> int:
    return _freeze_run_cohort(
        item_table="search_run_items",
        run_table="search_runs",
        run_id=run_id,
        district_ids=district_ids,
        db_path=db_path,
    )


def freeze_profile_discovery_run_cohort(
    run_id: int,
    district_ids: Iterable[int],
    *,
    db_path: Path | str | None = None,
) -> int:
    return _freeze_run_cohort(
        item_table="profile_discovery_run_items",
        run_table="profile_discovery_runs",
        run_id=run_id,
        district_ids=district_ids,
        db_path=db_path,
    )


def _list_run_items(
    *,
    item_table: str,
    run_id: int,
    statuses: Iterable[str] | None,
    db_path: Path | str | None,
) -> list[dict[str, Any]]:
    if item_table not in {"search_run_items", "profile_discovery_run_items"}:
        raise ValueError("Unexpected run-item table.")
    clauses = ["i.run_id = ?"]
    params: list[Any] = [int(run_id)]
    status_values = [str(value) for value in statuses or []]
    if any(value not in RUN_ITEM_STATUSES for value in status_values):
        raise ValueError("Unsupported run-item status filter.")
    if status_values:
        clauses.append(f"i.status IN ({','.join('?' for _ in status_values)})")
        params.extend(status_values)
    init_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, d.agency_name, d.state, d.agency_type,
                   d.total_enrollment_excludes_ae, d.website, d.website_normalized,
                   d.has_searchable_website
            FROM {item_table} i
            JOIN districts d ON d.id = i.district_id
            WHERE {' AND '.join(clauses)}
            ORDER BY i.ordinal
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_search_run_items(
    run_id: int,
    *,
    statuses: Iterable[str] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    return _list_run_items(
        item_table="search_run_items",
        run_id=run_id,
        statuses=statuses,
        db_path=db_path,
    )


def list_profile_discovery_run_items(
    run_id: int,
    *,
    statuses: Iterable[str] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    return _list_run_items(
        item_table="profile_discovery_run_items",
        run_id=run_id,
        statuses=statuses,
        db_path=db_path,
    )


def _update_run_item(
    *,
    item_table: str,
    run_id: int,
    district_id: int,
    status: str | None,
    attempt: int | None,
    result_count: int | None,
    profile_id: int | None | object,
    profile_status: str | None | object,
    error_message: str | None | object,
    db_path: Path | str | None,
) -> dict[str, Any]:
    if item_table not in {"search_run_items", "profile_discovery_run_items"}:
        raise ValueError("Unexpected run-item table.")
    if status is not None and status not in RUN_ITEM_STATUSES:
        raise ValueError(f"Unsupported run-item status: {status}")
    changes: dict[str, Any] = {}
    if status is not None:
        changes["status"] = status
    if attempt is not None:
        changes["attempt"] = max(0, int(attempt))
    if result_count is not None:
        changes["result_count"] = max(0, int(result_count))
    if profile_id is not _UNSET:
        changes["profile_id"] = profile_id
    if profile_status is not _UNSET:
        changes["profile_status"] = profile_status
    if error_message is not _UNSET:
        changes["error_message"] = error_message
    now = utc_now_iso()
    if status == "queued":
        changes["queued_at"] = now
    elif status == "running":
        changes["started_at"] = now
    elif status in {"completed", "failed", "cancelled", "skipped"}:
        changes["finished_at"] = now
    assignments: list[str] = []
    params: list[Any] = []
    increment_attempt = status == "running" and attempt is None
    if increment_attempt:
        changes.pop("attempt", None)
        assignments.append("attempt = attempt + 1")
    for column, value in changes.items():
        assignments.append(f"{column} = ?")
        params.append(value)
    assignments.append("updated_at = ?")
    params.extend([now, int(run_id), int(district_id)])
    init_db(db_path)
    with connect_db(db_path) as conn:
        cursor = conn.execute(
            f"""
            UPDATE {item_table}
            SET {', '.join(assignments)}
            WHERE run_id = ? AND district_id = ?
            """,
            params,
        )
        if not cursor.rowcount:
            raise ValueError(
                f"Run item not found in {item_table}: run={run_id}, district={district_id}"
            )
        row = conn.execute(
            f"SELECT * FROM {item_table} WHERE run_id = ? AND district_id = ?",
            (int(run_id), int(district_id)),
        ).fetchone()
    assert row is not None
    return dict(row)


def update_search_run_item(
    run_id: int,
    district_id: int,
    *,
    status: str | None = None,
    attempt: int | None = None,
    result_count: int | None = None,
    profile_id: int | None | object = _UNSET,
    profile_status: str | None | object = _UNSET,
    error_message: str | None | object = _UNSET,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    return _update_run_item(
        item_table="search_run_items",
        run_id=run_id,
        district_id=district_id,
        status=status,
        attempt=attempt,
        result_count=result_count,
        profile_id=profile_id,
        profile_status=profile_status,
        error_message=error_message,
        db_path=db_path,
    )


def update_profile_discovery_run_item(
    run_id: int,
    district_id: int,
    *,
    status: str | None = None,
    attempt: int | None = None,
    result_count: int | None = None,
    profile_id: int | None | object = _UNSET,
    profile_status: str | None | object = _UNSET,
    error_message: str | None | object = _UNSET,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    return _update_run_item(
        item_table="profile_discovery_run_items",
        run_id=run_id,
        district_id=district_id,
        status=status,
        attempt=attempt,
        result_count=result_count,
        profile_id=profile_id,
        profile_status=profile_status,
        error_message=error_message,
        db_path=db_path,
    )


def add_step(
    session_id: int,
    step_type: str,
    *,
    status: str = "running",
    short_description: str | None = None,
    input_data: Any = None,
    attempt: int = 1,
    sequence: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    step_type = str(step_type or "").strip()
    if not step_type:
        raise ValueError("A Guided Search step type is required.")
    status = str(status or "").strip()
    if not status:
        raise ValueError("A Guided Search step status is required.")
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_session_row(conn, session_id)
        if sequence is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value "
                "FROM guided_search_steps WHERE session_id = ?",
                (int(session_id),),
            ).fetchone()
            sequence = int(row["value"])
        cursor = conn.execute(
            """
            INSERT INTO guided_search_steps (
                session_id, sequence, step_type, status, short_description,
                input_json, attempt, started_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(session_id),
                int(sequence),
                step_type,
                status,
                short_description,
                None if input_data is None else json_dumps(input_data),
                max(1, int(attempt)),
                now if status == "running" else None,
                now,
                now,
            ),
        )
        step_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE guided_search_sessions
            SET current_step_id = ?, updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            (step_id, now, int(session_id)),
        )
    return step_id


def get_step(
    step_id: int, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM guided_search_steps WHERE id = ?", (int(step_id),)
        ).fetchone()
    return _step_dict(row)


def finish_step(
    step_id: int,
    *,
    status: str = "completed",
    output_data: Any = None,
    error_message: str | None = None,
    short_description: str | None | object = _UNSET,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    if status not in {"completed", "failed", "cancelled", "needs_review", "skipped"}:
        raise ValueError(f"Unsupported terminal Guided Search step status: {status}")
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_step_row(conn, step_id)
        assignments = [
            "status = ?",
            "output_json = ?",
            "error_message = ?",
            "finished_at = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [
            status,
            None if output_data is None else json_dumps(output_data),
            error_message,
            now,
            now,
        ]
        if short_description is not _UNSET:
            assignments.append("short_description = ?")
            params.append(short_description)
        params.append(int(step_id))
        conn.execute(
            f"UPDATE guided_search_steps SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        row = _require_step_row(conn, step_id)
    step = _step_dict(row)
    assert step is not None
    return step


def list_steps(
    session_id: int,
    *,
    statuses: Iterable[str] | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["session_id = ?"]
    params: list[Any] = [int(session_id)]
    status_values = [str(value) for value in statuses or []]
    if status_values:
        clauses.append(f"status IN ({','.join('?' for _ in status_values)})")
        params.extend(status_values)
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        rows = conn.execute(
            f"""
            SELECT * FROM guided_search_steps
            WHERE {' AND '.join(clauses)}
            ORDER BY sequence
            """,
            params,
        ).fetchall()
    return [_step_dict(row) for row in rows if row is not None]


def _child_table(child_type: str) -> str:
    if child_type == "search":
        return "search_runs"
    if child_type == "profile_discovery":
        return "profile_discovery_runs"
    raise ValueError(f"Unsupported Guided Search child type: {child_type}")


def link_child_run(
    session_id: int,
    child_type: str,
    child_run_id: int,
    *,
    step_id: int | None = None,
    round_number: int = 0,
    query_text: str | None = None,
    purpose: str | None = None,
    status: str | None = None,
    activate_from_staging: bool = False,
    db_path: Path | str | None = None,
) -> int:
    child_type = str(child_type or "").strip()
    run_table = _child_table(child_type)
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_session_row(conn, session_id)
        if step_id is not None:
            step = _require_step_row(conn, step_id)
            if int(step["session_id"]) != int(session_id):
                raise ValueError("Guided Search step belongs to a different session.")
        child = conn.execute(
            f"SELECT status FROM {run_table} WHERE id = ?", (int(child_run_id),)
        ).fetchone()
        if child is None:
            raise ValueError(f"Child {child_type} run not found: {child_run_id}")
        existing = conn.execute(
            """
            SELECT id, session_id FROM guided_search_child_runs
            WHERE child_type = ? AND child_run_id = ?
            """,
            (child_type, int(child_run_id)),
        ).fetchone()
        if existing is not None:
            if int(existing["session_id"]) != int(session_id):
                raise ValueError("Child run is already linked to another Guided Search session.")
            return int(existing["id"])
        child_status = str(child["status"] or "")
        if activate_from_staging:
            if child_status != "staging":
                raise ValueError(
                    f"Child {child_type} run must be staged before atomic activation."
                )
            activated = conn.execute(
                f"UPDATE {run_table} SET status = 'queued' WHERE id = ? AND status = 'staging'",
                (int(child_run_id),),
            )
            if activated.rowcount != 1:
                raise ConcurrentUpdateError(
                    f"Child {child_type} run {child_run_id} could not be activated."
                )
            child_status = "queued"
        cursor = conn.execute(
            """
            INSERT INTO guided_search_child_runs (
                session_id, step_id, child_type, child_run_id, round_number,
                query_text, purpose, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(session_id),
                step_id,
                child_type,
                int(child_run_id),
                max(0, int(round_number)),
                query_text,
                purpose,
                status or child_status,
                now,
            ),
        )
    return int(cursor.lastrowid)


def update_child_run(
    child_link_id: int,
    *,
    status: str | None = None,
    query_text: str | None | object = _UNSET,
    purpose: str | None | object = _UNSET,
    started_at: str | None | object = _UNSET,
    finished_at: str | None | object = _UNSET,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if status is not None:
        changes["status"] = status
    if query_text is not _UNSET:
        changes["query_text"] = query_text
    if purpose is not _UNSET:
        changes["purpose"] = purpose
    if started_at is not _UNSET:
        changes["started_at"] = started_at
    if finished_at is not _UNSET:
        changes["finished_at"] = finished_at
    now = utc_now_iso()
    if status == "running" and started_at is _UNSET:
        changes["started_at"] = now
    if status in {"completed", "cancelled", "failed"} and finished_at is _UNSET:
        changes["finished_at"] = now
    if not changes:
        raise ValueError("No child-run changes were supplied.")
    init_db(db_path)
    with connect_db(db_path) as conn:
        params = [*changes.values(), int(child_link_id)]
        cursor = conn.execute(
            f"""
            UPDATE guided_search_child_runs
            SET {', '.join(f'{column} = ?' for column in changes)}
            WHERE id = ?
            """,
            params,
        )
        if not cursor.rowcount:
            raise ValueError(f"Guided Search child link not found: {child_link_id}")
        row = conn.execute(
            "SELECT * FROM guided_search_child_runs WHERE id = ?",
            (int(child_link_id),),
        ).fetchone()
    assert row is not None
    return dict(row)


def list_child_runs(
    session_id: int,
    *,
    child_type: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["session_id = ?"]
    params: list[Any] = [int(session_id)]
    if child_type is not None:
        _child_table(child_type)
        clauses.append("child_type = ?")
        params.append(child_type)
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        rows = conn.execute(
            f"""
            SELECT * FROM guided_search_child_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY round_number, id
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def record_model_call(
    session_id: int,
    *,
    task_type: str,
    prompt_version: str,
    step_id: int | None = None,
    model: str | None = None,
    endpoint_identity: str | None = None,
    attempt: int = 1,
    success: bool = False,
    validation_status: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_duration_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    error_message: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    task_type = str(task_type or "").strip()
    prompt_version = str(prompt_version or "").strip()
    if not task_type or not prompt_version:
        raise ValueError("Model-call task type and prompt version are required.")
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        if step_id is not None:
            step = _require_step_row(conn, step_id)
            if int(step["session_id"]) != int(session_id):
                raise ValueError("Guided Search step belongs to a different session.")
        cursor = conn.execute(
            """
            INSERT INTO guided_search_model_calls (
                session_id, step_id, task_type, model, endpoint_identity,
                prompt_version, attempt, success, validation_status, latency_ms,
                prompt_tokens, completion_tokens, total_duration_ms, metadata_json,
                error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(session_id),
                step_id,
                task_type,
                model,
                _safe_endpoint_identity(endpoint_identity),
                prompt_version,
                max(1, int(attempt)),
                1 if success else 0,
                validation_status,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                total_duration_ms,
                None if metadata is None else json_dumps(_safe_audit_metadata(metadata)),
                error_message,
                now,
            ),
        )
    return int(cursor.lastrowid)


def list_model_calls(
    session_id: int,
    *,
    step_id: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["session_id = ?"]
    params: list[Any] = [int(session_id)]
    if step_id is not None:
        clauses.append("step_id = ?")
        params.append(int(step_id))
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        rows = conn.execute(
            f"""
            SELECT * FROM guided_search_model_calls
            WHERE {' AND '.join(clauses)}
            ORDER BY id
            """,
            params,
        ).fetchall()
    return [_model_call_dict(row) for row in rows if row is not None]


def upsert_evidence(
    session_id: int,
    canonical_url: str,
    *,
    district_id: int | None = None,
    content_fingerprint: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
    score: float = 0.0,
    round_number: int | None = None,
    classification: str | None = None,
    confidence: float | None = None,
    evaluation: Mapping[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> int:
    canonical_url = str(canonical_url or "").strip()
    if not canonical_url:
        raise ValueError("A canonical evidence URL is required.")
    persisted_round = 0 if round_number is None else max(0, int(round_number))
    has_round = round_number is not None
    score = float(score)
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        if district_id is not None:
            _validate_district_ids(conn, [int(district_id)])
        conn.execute(
            """
            INSERT INTO guided_search_evidence (
                session_id, district_id, canonical_url, content_fingerprint,
                title, snippet, best_score, first_round, last_round,
                classification, confidence, evaluation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, canonical_url) DO UPDATE SET
                district_id = CASE
                    WHEN excluded.best_score >= guided_search_evidence.best_score
                    THEN COALESCE(excluded.district_id, guided_search_evidence.district_id)
                    ELSE guided_search_evidence.district_id
                END,
                content_fingerprint = CASE
                    WHEN excluded.best_score >= guided_search_evidence.best_score
                    THEN COALESCE(excluded.content_fingerprint, guided_search_evidence.content_fingerprint)
                    ELSE guided_search_evidence.content_fingerprint
                END,
                title = CASE
                    WHEN excluded.best_score >= guided_search_evidence.best_score
                    THEN COALESCE(excluded.title, guided_search_evidence.title)
                    ELSE guided_search_evidence.title
                END,
                snippet = CASE
                    WHEN excluded.best_score >= guided_search_evidence.best_score
                    THEN COALESCE(excluded.snippet, guided_search_evidence.snippet)
                    ELSE guided_search_evidence.snippet
                END,
                best_score = MAX(guided_search_evidence.best_score, excluded.best_score),
                first_round = CASE
                    WHEN ? THEN MIN(guided_search_evidence.first_round, excluded.first_round)
                    ELSE guided_search_evidence.first_round
                END,
                last_round = CASE
                    WHEN ? THEN MAX(guided_search_evidence.last_round, excluded.last_round)
                    ELSE guided_search_evidence.last_round
                END,
                classification = COALESCE(
                    excluded.classification,
                    guided_search_evidence.classification
                ),
                confidence = COALESCE(excluded.confidence, guided_search_evidence.confidence),
                evaluation_json = COALESCE(
                    excluded.evaluation_json,
                    guided_search_evidence.evaluation_json
                ),
                updated_at = excluded.updated_at
            """,
            (
                int(session_id),
                district_id,
                canonical_url,
                content_fingerprint,
                title,
                snippet,
                score,
                persisted_round,
                persisted_round,
                classification,
                confidence,
                None if evaluation is None else json_dumps(dict(evaluation)),
                now,
                now,
                int(has_round),
                int(has_round),
            ),
        )
        row = conn.execute(
            """
            SELECT id FROM guided_search_evidence
            WHERE session_id = ? AND canonical_url = ?
            """,
            (int(session_id), canonical_url),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def link_evidence_source(
    evidence_id: int,
    *,
    search_run_id: int,
    search_result_id: int,
    child_run_link_id: int | None = None,
    round_number: int = 0,
    query_text: str | None = None,
    purpose: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        evidence = conn.execute(
            "SELECT session_id FROM guided_search_evidence WHERE id = ?",
            (int(evidence_id),),
        ).fetchone()
        if evidence is None:
            raise ValueError(f"Guided Search evidence not found: {evidence_id}")
        result = conn.execute(
            """
            SELECT search_run_id FROM search_results
            WHERE id = ?
            """,
            (int(search_result_id),),
        ).fetchone()
        if result is None or int(result["search_run_id"]) != int(search_run_id):
            raise ValueError("Search result does not belong to the supplied search run.")
        if child_run_link_id is not None:
            child = conn.execute(
                """
                SELECT session_id, child_type, child_run_id
                FROM guided_search_child_runs WHERE id = ?
                """,
                (int(child_run_link_id),),
            ).fetchone()
            if (
                child is None
                or int(child["session_id"]) != int(evidence["session_id"])
                or child["child_type"] != "search"
                or int(child["child_run_id"]) != int(search_run_id)
            ):
                raise ValueError(
                    "Evidence source child link does not match the session/search run."
                )
        conn.execute(
            """
            INSERT OR IGNORE INTO guided_search_evidence_sources (
                evidence_id, child_run_link_id, search_run_id, search_result_id,
                round_number, query_text, purpose, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(evidence_id),
                child_run_link_id,
                int(search_run_id),
                int(search_result_id),
                max(0, int(round_number)),
                query_text,
                purpose,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT id FROM guided_search_evidence_sources
            WHERE evidence_id = ? AND search_result_id = ?
            """,
            (int(evidence_id), int(search_result_id)),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def list_evidence(
    session_id: int,
    *,
    district_id: int | None = None,
    classification: str | None = None,
    limit: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["e.session_id = ?"]
    params: list[Any] = [int(session_id)]
    if district_id is not None:
        clauses.append("e.district_id = ?")
        params.append(int(district_id))
    if classification is not None:
        clauses.append("e.classification = ?")
        params.append(classification)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(1, int(limit)))
    init_db(db_path)
    with connect_db(db_path) as conn:
        _require_session_row(conn, session_id)
        rows = conn.execute(
            f"""
            SELECT e.*, d.agency_name, d.state,
                   (SELECT COUNT(*) FROM guided_search_evidence_sources s
                    WHERE s.evidence_id = e.id) AS source_count
            FROM guided_search_evidence e
            LEFT JOIN districts d ON d.id = e.district_id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.best_score DESC, e.id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [_evidence_dict(row) for row in rows if row is not None]


def list_evidence_sources(
    *,
    evidence_id: int | None = None,
    session_id: int | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    if evidence_id is None and session_id is None:
        raise ValueError("Supply evidence_id or session_id.")
    clauses: list[str] = []
    params: list[Any] = []
    if evidence_id is not None:
        clauses.append("s.evidence_id = ?")
        params.append(int(evidence_id))
    if session_id is not None:
        clauses.append("e.session_id = ?")
        params.append(int(session_id))
    init_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT s.*, e.session_id, e.canonical_url,
                   r.district_id, r.title, r.snippet, r.score
            FROM guided_search_evidence_sources s
            JOIN guided_search_evidence e ON e.id = s.evidence_id
            JOIN search_results r ON r.id = s.search_result_id
            WHERE {' AND '.join(clauses)}
            ORDER BY s.id
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "CohortAlreadyFrozenError",
    "ConcurrentUpdateError",
    "GUIDED_SEARCH_ACTIVE_STATUSES",
    "GUIDED_SEARCH_CHILD_TYPES",
    "GUIDED_SEARCH_STATUSES",
    "GUIDED_SEARCH_STRATEGY_MODES",
    "GUIDED_SEARCH_TERMINAL_STATUSES",
    "GuidedSearchStorageError",
    "InvalidStatusTransitionError",
    "RUN_ITEM_STATUSES",
    "SessionNotFoundError",
    "StepNotFoundError",
    "add_step",
    "cancellation_requested",
    "claim_session",
    "create_session",
    "finish_step",
    "freeze_cohort",
    "freeze_profile_discovery_run_cohort",
    "freeze_search_run_cohort",
    "get_cohort",
    "get_cohort_ids",
    "get_session",
    "get_step",
    "heartbeat_session_claim",
    "link_child_run",
    "link_evidence_source",
    "list_child_runs",
    "list_evidence",
    "list_evidence_sources",
    "list_model_calls",
    "list_profile_discovery_run_items",
    "list_search_run_items",
    "list_sessions",
    "list_steps",
    "record_model_call",
    "release_session_claim",
    "request_cancellation",
    "transition_session",
    "update_child_run",
    "update_profile_discovery_run_item",
    "update_search_run_item",
    "update_session",
    "upsert_evidence",
]
