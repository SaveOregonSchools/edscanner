from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse, urlunparse

from common import connect_db, init_db, utc_now_iso


MEETING_TEXT_FIELDS = (
    "meeting_date",
    "meeting_start_time",
    "meeting_end_time",
    "meeting_datetime_text",
    "title",
    "meeting_type",
    "location_name",
    "location_address",
    "description",
    "agenda_url",
    "minutes_url",
    "packet_url",
    "public_notice_url",
    "video_url",
    "livestream_url",
    "source_url",
    "status",
)

AGENDA_TEXT_FIELDS = (
    "display_number",
    "item_type",
    "title",
    "description",
    "presenter",
    "department",
    "action_requested",
    "motion_text",
    "vote_text",
    "result_text",
    "source_url",
)


def object_to_dict(value: Any) -> dict[str, Any]:
    """Return a shallow record view for mappings and dataclass-like objects."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    values = getattr(value, "__dict__", None)
    if isinstance(values, Mapping):
        return {key: item for key, item in values.items() if not str(key).startswith("_")}
    return {}


def value_of(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    return default


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "size_bytes": len(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    values = object_to_dict(value)
    return _jsonable(values) if values else str(value)


def stable_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return " ".join(str(value).strip().split())


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping) or isinstance(value, (str, bytes, bytearray)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def canonicalize_url(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    clean, _fragment = urldefrag(raw)
    parsed = urlparse(clean)
    if not parsed.scheme or not parsed.netloc:
        return clean.rstrip("/")
    scheme = parsed.scheme.casefold()
    netloc = parsed.netloc.casefold()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def _json_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return stable_json(value)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _is_confirmed_manual_source(raw: Any) -> bool:
    metadata = _json_mapping(raw)
    return bool(
        str(metadata.get("discovery_method") or "").casefold() == "manual_entry"
        and metadata.get("operator_confirmed_district_identity") is True
        and metadata.get("verified") is True
    )


def upsert_board_source(
    district_id: int,
    source: Any,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Insert or refresh one exact source without replacing older source URLs."""

    init_db(db_path)
    wrapped_source = value_of(source, "source")
    if wrapped_source is not None and not value_of(source, "source_url", "public_url", "url"):
        source = wrapped_source
    platform = (_text(value_of(source, "platform", "platform_name")) or "generic").casefold()
    source_url = canonicalize_url(value_of(source, "source_url", "url"))
    if not source_url:
        raise ValueError("A board source URL is required.")
    status = (_text(value_of(source, "source_status", "status")) or "manual_review").casefold()
    now = utc_now_iso()
    last_discovered_at = _optional_text(value_of(source, "last_discovered_at")) or now
    last_checked_at = _optional_text(value_of(source, "last_checked_at")) or now
    raw = value_of(source, "raw_discovery_json", "raw", "metadata")
    raw_json = _json_text(raw)
    incoming_confirmed_manual = _is_confirmed_manual_source(raw_json)
    schedule_action: str | None = None
    superseded_source_id: int | None = None

    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_active = conn.execute(
            """
            SELECT id, platform, source_url, source_status, raw_discovery_json
            FROM board_sources
            WHERE district_id = ? AND is_active = 1
            LIMIT 1
            """,
            (int(district_id),),
        ).fetchone()
        is_exact_current = bool(
            existing_active is not None
            and str(existing_active["platform"]).casefold() == platform
            and canonicalize_url(existing_active["source_url"]).casefold()
            == source_url.casefold()
        )
        existing_is_confirmed_manual = bool(
            existing_active is not None
            and str(existing_active["source_status"] or "").casefold() == "working"
            and _is_confirmed_manual_source(existing_active["raw_discovery_json"])
        )
        preserve_manual_authority = bool(
            existing_is_confirmed_manual
            and not is_exact_current
            and not incoming_confirmed_manual
        )
        # A known-good current source must not be displaced by a weaker lead.
        # The current source itself remains current when its latest health check
        # changes status, so operators retain one stable source history record.
        promote_to_current = (
            (status == "working" and not preserve_manual_authority)
            or is_exact_current
            or (existing_active is None and status not in {"not_found", "error"})
        )
        if (
            is_exact_current
            and existing_is_confirmed_manual
            and not incoming_confirmed_manual
        ):
            # Automated health refreshes may update the exact source without
            # erasing the operator-confirmed authority marker that protects it.
            raw_json = str(existing_active["raw_discovery_json"] or "") or raw_json
        if promote_to_current:
            if existing_active is not None and not is_exact_current:
                superseded_source_id = int(existing_active["id"])
            conn.execute(
                """
                UPDATE board_sources
                SET is_active = 0, superseded_at = ?, updated_at = ?
                WHERE district_id = ? AND is_active = 1
                  AND NOT (platform = ? AND source_url = ? COLLATE NOCASE)
                """,
                (now, now, int(district_id), platform, source_url),
            )
        conn.execute(
            """
            INSERT INTO board_sources (
                district_id, platform, source_status, source_url,
                organization_external_id, platform_tenant, discovered_from_url,
                confidence, requires_javascript, is_active, superseded_at, last_discovered_at,
                last_successful_sync_at, last_checked_at, error_message,
                raw_discovery_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(district_id, platform, source_url) DO UPDATE SET
                source_status = excluded.source_status,
                organization_external_id = COALESCE(
                    excluded.organization_external_id,
                    board_sources.organization_external_id
                ),
                platform_tenant = COALESCE(excluded.platform_tenant, board_sources.platform_tenant),
                discovered_from_url = COALESCE(
                    excluded.discovered_from_url,
                    board_sources.discovered_from_url
                ),
                confidence = excluded.confidence,
                requires_javascript = excluded.requires_javascript,
                is_active = excluded.is_active,
                superseded_at = CASE
                    WHEN excluded.is_active = 1 THEN NULL
                    ELSE board_sources.superseded_at
                END,
                last_discovered_at = COALESCE(
                    excluded.last_discovered_at,
                    board_sources.last_discovered_at
                ),
                last_successful_sync_at = COALESCE(
                    excluded.last_successful_sync_at,
                    board_sources.last_successful_sync_at
                ),
                last_checked_at = COALESCE(excluded.last_checked_at, board_sources.last_checked_at),
                error_message = excluded.error_message,
                raw_discovery_json = COALESCE(
                    excluded.raw_discovery_json,
                    board_sources.raw_discovery_json
                ),
                updated_at = excluded.updated_at
            """,
            (
                int(district_id),
                platform,
                status,
                source_url,
                _optional_text(
                    value_of(
                        source,
                        "organization_external_id",
                        "external_source_id",
                        "organization_id",
                    )
                ),
                _optional_text(value_of(source, "platform_tenant", "tenant")),
                canonicalize_url(value_of(source, "discovered_from_url")) or None,
                max(0.0, min(100.0, _float(value_of(source, "confidence"), 0.0))),
                1 if bool(value_of(source, "requires_javascript", default=False)) else 0,
                1 if promote_to_current else 0,
                None,
                last_discovered_at,
                _optional_text(value_of(source, "last_successful_sync_at")),
                last_checked_at,
                _optional_text(value_of(source, "error_message", "error")),
                raw_json,
                now,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM board_sources
            WHERE district_id = ? AND platform = ? AND source_url = ? COLLATE NOCASE
            """,
            (int(district_id), platform, source_url),
        ).fetchone()
        if row is not None and superseded_source_id is not None:
            old_schedule = conn.execute(
                """
                SELECT id FROM board_sync_schedules
                WHERE board_source_id = ? AND enabled = 1
                """,
                (superseded_source_id,),
            ).fetchone()
            if old_schedule is not None:
                replacement_schedule = conn.execute(
                    "SELECT id FROM board_sync_schedules WHERE board_source_id = ?",
                    (int(row["id"]),),
                ).fetchone()
                if replacement_schedule is None:
                    conn.execute(
                        """
                        UPDATE board_sync_schedules
                        SET board_source_id = ?, claim_token = NULL,
                            claim_expires_at = NULL, last_error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (int(row["id"]), now, int(old_schedule["id"])),
                    )
                    schedule_action = "transferred"
                else:
                    message = (
                        "Disabled when this source was superseded because the replacement "
                        "source already had a schedule."
                    )
                    conn.execute(
                        """
                        UPDATE board_sync_schedules
                        SET enabled = 0, claim_token = NULL, claim_expires_at = NULL,
                            last_error = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (message, now, int(old_schedule["id"])),
                    )
                    schedule_action = "disabled_conflict"
        conn.commit()
    if row is None:
        raise RuntimeError("Board source upsert did not produce a row.")
    result = dict(row)
    if schedule_action is not None:
        result["_schedule_action"] = schedule_action
        result["_superseded_source_id"] = superseded_source_id
    return result


def get_board_source(
    source_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute("SELECT * FROM board_sources WHERE id = ?", (int(source_id),)).fetchone()
    return dict(row) if row else None


def _normalized_document_reference(document: Any) -> dict[str, Any]:
    source_url = canonicalize_url(value_of(document, "source_url", "url"))
    return {
        "external_document_id": _optional_text(
            value_of(document, "external_document_id", "external_id", "id")
        ),
        "document_type": _text(value_of(document, "document_type", "type"), "other").casefold(),
        "title": _optional_text(value_of(document, "title", "name")),
        "source_url": source_url or None,
        "resolved_url": canonicalize_url(value_of(document, "resolved_url", "final_url")) or None,
        "filename": _optional_text(value_of(document, "filename")),
        "mime_type": _optional_text(value_of(document, "mime_type", "content_type")),
    }


def _agenda_for_hash(item: Any, *, sequence: int = 0, depth: int = 0) -> dict[str, Any]:
    record: dict[str, Any] = {
        "external_item_id": _optional_text(value_of(item, "external_item_id", "external_id", "id")),
        "sequence_number": _integer(
            value_of(item, "sequence_number", "order_index", "sequence"),
            sequence,
        ),
        "display_number": _optional_text(
            value_of(item, "display_number", "item_number", "number")
        ),
        "depth": _integer(value_of(item, "depth"), depth),
    }
    for field in AGENDA_TEXT_FIELDS[1:]:
        record[field] = _optional_text(value_of(item, field))
    documents = _items(value_of(item, "documents", "attachments", default=[]))
    record["documents"] = sorted(
        (_normalized_document_reference(document) for document in documents),
        key=stable_json,
    )
    children = _items(value_of(item, "children", "items", default=[]))
    record["children"] = [
        _agenda_for_hash(child, sequence=index, depth=record["depth"] + 1)
        for index, child in enumerate(children, start=1)
    ]
    return record


def normalized_meeting_content(meeting: Any) -> dict[str, Any]:
    hash_payload = getattr(meeting, "hash_payload", None)
    if callable(hash_payload):
        payload = object_to_dict(hash_payload())
        # Adapter metadata may contain retrieval timestamps or debug evidence. The
        # normalized revision payload intentionally excludes those volatile fields.
        for agenda_item in payload.get("agenda_items", []) or []:
            if isinstance(agenda_item, dict):
                agenda_item.pop("metadata", None)
                for document in agenda_item.get("documents", []) or []:
                    if isinstance(document, dict):
                        document.pop("metadata", None)
        for document in payload.get("documents", []) or []:
            if isinstance(document, dict):
                document.pop("metadata", None)
        return _jsonable(payload)
    normalized: dict[str, Any] = {}
    for field in MEETING_TEXT_FIELDS:
        aliases = {
            "source_url": ("source_url", "url", "public_url"),
            "meeting_start_time": ("meeting_start_time", "start_time"),
            "meeting_end_time": ("meeting_end_time", "end_time"),
            "meeting_datetime_text": ("meeting_datetime_text", "datetime_text"),
        }.get(field, (field,))
        value = value_of(meeting, *aliases)
        if field.endswith("_url") or field == "source_url":
            normalized[field] = canonicalize_url(value) or None
        else:
            normalized[field] = _optional_text(value)
    agenda_items = _items(value_of(meeting, "agenda_items", "items", "agenda", default=[]))
    normalized["agenda_items"] = [
        _agenda_for_hash(item, sequence=index, depth=0)
        for index, item in enumerate(agenda_items, start=1)
    ]
    documents = _items(value_of(meeting, "documents", "attachments", default=[]))
    normalized["documents"] = sorted(
        (_normalized_document_reference(document) for document in documents),
        key=stable_json,
    )
    return normalized


def deterministic_meeting_identity(board_source_id: int, meeting: Any) -> str:
    explicit = _optional_text(
        value_of(meeting, "external_meeting_id", "external_id", "meeting_id", "id")
    )
    if explicit:
        return explicit
    basis = {
        "board_source_id": int(board_source_id),
        "meeting_date": _optional_text(value_of(meeting, "meeting_date", "date")),
        "title": _optional_text(value_of(meeting, "title", "name")),
        "source_url": canonicalize_url(value_of(meeting, "source_url", "url", "public_url")),
    }
    return f"derived:{content_hash(basis)[:32]}"


def meeting_content_hash(meeting: Any) -> str:
    return content_hash(normalized_meeting_content(meeting))


def _meeting_values(meeting: Any, source: Mapping[str, Any], external_id: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "platform": (
            _text(value_of(meeting, "platform"))
            or _text(source.get("platform"))
            or "generic"
        ).casefold(),
        "external_meeting_id": external_id,
        "meeting_date": _optional_text(value_of(meeting, "meeting_date", "date")),
        "meeting_start_time": _optional_text(value_of(meeting, "meeting_start_time", "start_time")),
        "meeting_end_time": _optional_text(value_of(meeting, "meeting_end_time", "end_time")),
        "meeting_datetime_text": _optional_text(
            value_of(meeting, "meeting_datetime_text", "datetime_text")
        ),
        "title": _optional_text(value_of(meeting, "title", "name")),
        "meeting_type": _optional_text(value_of(meeting, "meeting_type", "type")),
        "location_name": _optional_text(value_of(meeting, "location_name", "location")),
        "location_address": _optional_text(value_of(meeting, "location_address", "address")),
        "description": _optional_text(value_of(meeting, "description")),
        "status": (
            "cancelled"
            if bool(value_of(meeting, "is_cancelled", default=False))
            else (_text(value_of(meeting, "status")) or "published").casefold()
        ),
    }
    for field in (
        "agenda_url",
        "minutes_url",
        "packet_url",
        "public_notice_url",
        "video_url",
        "livestream_url",
    ):
        values[field] = canonicalize_url(value_of(meeting, field)) or None
    values["source_url"] = (
        canonicalize_url(value_of(meeting, "source_url", "url", "public_url"))
        or str(source["source_url"])
    )
    return values


def _upsert_board_meeting_conn(
    conn: Any,
    district_id: int,
    board_source_id: int,
    meeting: Any,
    *,
    raw_snapshot_path: Path | str | None = None,
    http_status: int | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    source_row = conn.execute(
        "SELECT * FROM board_sources WHERE id = ?",
        (int(board_source_id),),
    ).fetchone()
    if source_row is None:
        raise ValueError(f"Board source not found: {board_source_id}")
    source = dict(source_row)
    if int(source["district_id"]) != int(district_id):
        raise ValueError("The board source does not belong to the supplied district.")

    external_id = deterministic_meeting_identity(board_source_id, meeting)
    values = _meeting_values(meeting, source, external_id)
    normalized_json = stable_json(normalized_meeting_content(meeting))
    digest = hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
    now = utc_now_iso()
    seen_at = _optional_text(value_of(meeting, "last_seen_at")) or now
    checked_at = _optional_text(value_of(meeting, "last_checked_at")) or retrieved_at or now
    retrieved_at = retrieved_at or _optional_text(value_of(meeting, "retrieved_at")) or now
    snapshot = str(raw_snapshot_path or value_of(meeting, "raw_snapshot_path") or "").strip() or None
    if http_status is None:
        raw_http_status = value_of(meeting, "http_status", "status_code")
        http_status = _integer(raw_http_status) if raw_http_status is not None else None

    existing = conn.execute(
        """
        SELECT * FROM board_meetings
        WHERE board_source_id = ? AND external_meeting_id = ?
        """,
        (int(board_source_id), external_id),
    ).fetchone()
    created = existing is None
    changed = bool(existing is not None and existing["content_hash"] != digest)

    columns = (
        "platform",
        "external_meeting_id",
        "meeting_date",
        "meeting_start_time",
        "meeting_end_time",
        "meeting_datetime_text",
        "title",
        "meeting_type",
        "location_name",
        "location_address",
        "description",
        "agenda_url",
        "minutes_url",
        "packet_url",
        "public_notice_url",
        "video_url",
        "livestream_url",
        "source_url",
        "status",
    )
    if created:
        cursor = conn.execute(
            f"""
            INSERT INTO board_meetings (
                district_id, board_source_id, {', '.join(columns)},
                revision_detected, first_seen_at, last_seen_at, last_checked_at,
                content_hash, created_at, updated_at
            ) VALUES (?, ?, {','.join('?' for _ in columns)}, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(district_id),
                int(board_source_id),
                *(values[column] for column in columns),
                now,
                seen_at,
                checked_at,
                digest,
                now,
                now,
            ),
        )
        meeting_id = int(cursor.lastrowid)
    else:
        meeting_id = int(existing["id"])
        assignments = ", ".join(f"{column} = ?" for column in columns)
        conn.execute(
            f"""
            UPDATE board_meetings
            SET {assignments},
                revision_detected = CASE WHEN ? THEN 1 ELSE revision_detected END,
                last_seen_at = ?, last_checked_at = ?, content_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                *(values[column] for column in columns),
                1 if changed else 0,
                seen_at,
                checked_at,
                digest,
                now,
                meeting_id,
            ),
        )

    hash_version = conn.execute(
        """
        SELECT id, version_number FROM board_meeting_versions
        WHERE board_meeting_id = ? AND content_hash = ?
        """,
        (meeting_id, digest),
    ).fetchone()
    version_created = False
    if hash_version is None:
        next_version = int(
            conn.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS value
                FROM board_meeting_versions WHERE board_meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()["value"]
        )
        conn.execute(
            """
            INSERT INTO board_meeting_versions (
                board_meeting_id, version_number, content_hash, raw_snapshot_path,
                normalized_json, http_status, retrieved_at, first_seen_at,
                last_seen_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                next_version,
                digest,
                snapshot,
                normalized_json,
                http_status,
                retrieved_at,
                now,
                seen_at,
                now,
            ),
        )
        version_created = True
        version_number = next_version
    else:
        version_number = int(hash_version["version_number"])
        conn.execute(
            """
            UPDATE board_meeting_versions
            SET last_seen_at = ?,
                raw_snapshot_path = COALESCE(raw_snapshot_path, ?),
                http_status = COALESCE(?, http_status),
                retrieved_at = COALESCE(?, retrieved_at)
            WHERE id = ?
            """,
            (seen_at, snapshot, http_status, retrieved_at, int(hash_version["id"])),
        )

    row = conn.execute("SELECT * FROM board_meetings WHERE id = ?", (meeting_id,)).fetchone()
    result = dict(row)
    result.update(
        {
            "created": created,
            "changed": changed,
            "version_created": version_created,
            "version_number": version_number,
        }
    )
    return result


def upsert_board_meeting(
    district_id: int,
    board_source_id: int,
    meeting: Any,
    *,
    raw_snapshot_path: Path | str | None = None,
    http_status: int | None = None,
    retrieved_at: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = _upsert_board_meeting_conn(
            conn,
            district_id,
            board_source_id,
            meeting,
            raw_snapshot_path=raw_snapshot_path,
            http_status=http_status,
            retrieved_at=retrieved_at,
        )
        from board.search import index_meeting

        index_meeting(int(result["id"]), conn=conn)
        conn.commit()
    return result


def _agenda_external_id(
    item: Any,
    *,
    parent_external_id: str | None,
    sequence_number: int,
    display_number: str | None,
) -> str:
    explicit = _optional_text(value_of(item, "external_item_id", "external_id", "id"))
    if explicit:
        return explicit
    basis = {
        "parent": parent_external_id,
        "display_number": display_number,
        "sequence_number": sequence_number,
    }
    if not display_number:
        basis["title"] = _optional_text(value_of(item, "title", "name"))
    return f"derived:{content_hash(basis)[:32]}"


def _flatten_agenda_items(items: Any) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    sequence_counter = 0

    def visit(item: Any, parent_external_id: str | None, inherited_depth: int) -> None:
        nonlocal sequence_counter
        sequence_counter += 1
        sequence_number = _integer(
            value_of(item, "sequence_number", "order_index", "sequence"),
            sequence_counter,
        )
        display_number = _optional_text(
            value_of(item, "display_number", "item_number", "number")
        )
        explicit_parent = _optional_text(
            value_of(item, "parent_external_item_id", "parent_external_id", "parent_id")
        )
        parent_external = explicit_parent or parent_external_id
        external_id = _agenda_external_id(
            item,
            parent_external_id=parent_external,
            sequence_number=sequence_number,
            display_number=display_number,
        )
        depth = _integer(value_of(item, "depth"), inherited_depth)
        record: dict[str, Any] = {
            "external_item_id": external_id,
            "parent_external_item_id": parent_external,
            "sequence_number": sequence_number,
            "display_number": display_number,
            "depth": max(0, depth),
            "item_type": _optional_text(value_of(item, "item_type", "type")),
            "title": _optional_text(value_of(item, "title", "name")),
            "description": _optional_text(value_of(item, "description")),
            "presenter": _optional_text(value_of(item, "presenter")),
            "department": _optional_text(value_of(item, "department")),
            "action_requested": _optional_text(value_of(item, "action_requested")),
            "motion_text": _optional_text(value_of(item, "motion_text", "motion")),
            "vote_text": _optional_text(value_of(item, "vote_text", "vote")),
            "result_text": _optional_text(value_of(item, "result_text", "result")),
            "source_url": canonicalize_url(value_of(item, "source_url", "url")) or None,
            "raw": object_to_dict(item),
        }
        record["normalized_text"] = " ".join(
            part
            for part in (record.get(field) for field in AGENDA_TEXT_FIELDS)
            if isinstance(part, str) and part
        )
        hash_record = {
            key: value
            for key, value in record.items()
            if key not in {"raw", "normalized_text"}
        }
        record["content_hash"] = content_hash(hash_record)
        flattened.append(record)
        for child in _items(value_of(item, "children", "items", default=[])):
            visit(child, external_id, depth + 1)

    for root in _items(items):
        visit(root, None, 0)
    return flattened


def _upsert_agenda_items_conn(
    conn: Any,
    board_meeting_id: int,
    items: Any,
) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM board_meetings WHERE id = ?", (int(board_meeting_id),)).fetchone() is None:
        raise ValueError(f"Board meeting not found: {board_meeting_id}")
    flattened = _flatten_agenda_items(items)
    current_external_ids = {record["external_item_id"] for record in flattened}
    for record in flattened:
        if record["parent_external_item_id"] not in current_external_ids:
            record["parent_external_item_id"] = None
    id_by_external = {
        str(row["external_item_id"]): int(row["id"])
        for row in conn.execute(
            "SELECT id, external_item_id FROM board_agenda_items WHERE board_meeting_id = ?",
            (int(board_meeting_id),),
        )
    }
    now = utc_now_iso()
    pending = list(flattened)
    saved: dict[str, dict[str, Any]] = {}

    while pending:
        progressed = False
        for record in list(pending):
            parent_external = record["parent_external_item_id"]
            if parent_external and parent_external not in id_by_external:
                continue
            parent_id = id_by_external.get(parent_external) if parent_external else None
            conn.execute(
                """
                INSERT INTO board_agenda_items (
                    board_meeting_id, parent_item_id, external_item_id,
                    sequence_number, display_number, depth, item_type, title,
                    description, presenter, department, action_requested,
                    motion_text, vote_text, result_text, source_url,
                    normalized_text, content_hash, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board_meeting_id, external_item_id) DO UPDATE SET
                    parent_item_id = excluded.parent_item_id,
                    sequence_number = excluded.sequence_number,
                    display_number = excluded.display_number,
                    depth = excluded.depth,
                    item_type = excluded.item_type,
                    title = excluded.title,
                    description = excluded.description,
                    presenter = excluded.presenter,
                    department = excluded.department,
                    action_requested = excluded.action_requested,
                    motion_text = excluded.motion_text,
                    vote_text = excluded.vote_text,
                    result_text = excluded.result_text,
                    source_url = excluded.source_url,
                    normalized_text = excluded.normalized_text,
                    content_hash = excluded.content_hash,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    int(board_meeting_id),
                    parent_id,
                    record["external_item_id"],
                    record["sequence_number"],
                    record["display_number"],
                    record["depth"],
                    record["item_type"],
                    record["title"],
                    record["description"],
                    record["presenter"],
                    record["department"],
                    record["action_requested"],
                    record["motion_text"],
                    record["vote_text"],
                    record["result_text"],
                    record["source_url"],
                    record["normalized_text"],
                    record["content_hash"],
                    stable_json(record["raw"]),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM board_agenda_items
                WHERE board_meeting_id = ? AND external_item_id = ?
                """,
                (int(board_meeting_id), record["external_item_id"]),
            ).fetchone()
            id_by_external[record["external_item_id"]] = int(row["id"])
            saved[record["external_item_id"]] = dict(row)
            pending.remove(record)
            progressed = True
        if progressed:
            continue
        # A malformed or missing parent should not prevent later agenda items.
        for record in pending:
            record["parent_external_item_id"] = None

    if current_external_ids:
        placeholders = ",".join("?" for _ in current_external_ids)
        conn.execute(
            f"""
            DELETE FROM board_agenda_items
            WHERE board_meeting_id = ? AND external_item_id NOT IN ({placeholders})
            """,
            [int(board_meeting_id), *sorted(current_external_ids)],
        )
    else:
        conn.execute(
            "DELETE FROM board_agenda_items WHERE board_meeting_id = ?",
            (int(board_meeting_id),),
        )

    return [saved[record["external_item_id"]] for record in flattened]


def upsert_agenda_items(
    board_meeting_id: int,
    items: Any,
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = _upsert_agenda_items_conn(conn, board_meeting_id, items)
        from board.search import index_meeting

        index_meeting(int(board_meeting_id), conn=conn)
        conn.commit()
    return saved


def persist_meeting_bundle(
    district_id: int,
    board_source_id: int,
    meeting: Any,
    *,
    agenda_items: Any | None = None,
    raw_snapshot_path: Path | str | None = None,
    http_status: int | None = None,
    retrieved_at: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Persist current meeting metadata, its version, and agenda in one short transaction."""

    init_db(db_path)
    meeting_value = meeting
    if agenda_items is not None:
        meeting_value = object_to_dict(meeting)
        meeting_value["agenda_items"] = _items(agenda_items)
    else:
        agenda_items = value_of(meeting, "agenda_items", "items", "agenda", default=[])

    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = _upsert_board_meeting_conn(
            conn,
            district_id,
            board_source_id,
            meeting_value,
            raw_snapshot_path=raw_snapshot_path,
            http_status=http_status,
            retrieved_at=retrieved_at,
        )
        saved_items = _upsert_agenda_items_conn(conn, int(result["id"]), agenda_items)
        from board.search import index_meeting

        index_meeting(int(result["id"]), conn=conn)
        conn.commit()
    result["agenda_items"] = saved_items
    return result


# Concise aliases used by sync code and developer probes.
save_board_source = upsert_board_source
save_board_meeting = persist_meeting_bundle
upsert_source = upsert_board_source
upsert_meeting = upsert_board_meeting


__all__ = [
    "canonicalize_url",
    "content_hash",
    "deterministic_meeting_identity",
    "get_board_source",
    "meeting_content_hash",
    "normalized_meeting_content",
    "object_to_dict",
    "persist_meeting_bundle",
    "save_board_meeting",
    "save_board_source",
    "stable_json",
    "upsert_agenda_items",
    "upsert_board_meeting",
    "upsert_board_source",
    "upsert_meeting",
    "upsert_source",
    "value_of",
]
