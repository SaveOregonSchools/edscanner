from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

import common
from board.search import search_board_content
from common import connect_db, init_db


_EXPORT_NAME_RE = re.compile(r"[^a-z0-9-]+")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_SEARCH_BATCH_SIZE = 500


@dataclass(frozen=True)
class BoardExport:
    path: Path
    row_count: int


def safe_csv_cell(value: Any) -> str:
    """Return a spreadsheet-safe, consistently serialized CSV cell."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    first_visible = text.lstrip()[:1]
    if text.startswith(_FORMULA_PREFIXES) or first_visible in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _export_path(prefix: str, export_dir: Path | str | None = None) -> Path:
    root = Path(export_dir or common.EXPORTS_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_prefix = _EXPORT_NAME_RE.sub("-", str(prefix).strip().casefold()).strip("-")
    if not safe_prefix:
        safe_prefix = "school-board-export"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"edscanner-{safe_prefix}-{timestamp}-{uuid4().hex[:8]}.csv"
    path = (root / filename).resolve()
    path.relative_to(root)
    return path


def _write_export(
    prefix: str,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    export_dir: Path | str | None = None,
) -> BoardExport:
    path = _export_path(prefix, export_dir)
    count = 0
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(fieldnames)
            for row in rows:
                writer.writerow([safe_csv_cell(row.get(field)) for field in fieldnames])
                count += 1
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return BoardExport(path=path, row_count=count)


def _source_query_parts(
    *,
    state: str = "",
    agency_type: str = "",
    platform: str = "",
    source_status: str = "",
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
) -> tuple[str, str, list[Any]]:
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
        params.append(platform.casefold())
    if source_status == "__unchecked__":
        clauses.append("bs.id IS NULL")
    elif source_status:
        clauses.append("bs.source_status = ?")
        params.append(source_status.casefold())
    if min_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae >= ?")
        params.append(int(min_enrollment))
    if max_enrollment is not None:
        clauses.append("d.total_enrollment_excludes_ae <= ?")
        params.append(int(max_enrollment))
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    join_sql = """
        LEFT JOIN board_sources bs ON bs.id = (
            SELECT candidate.id FROM board_sources candidate
            WHERE candidate.district_id = d.id
            ORDER BY candidate.is_active DESC, candidate.updated_at DESC, candidate.id DESC LIMIT 1
        )
    """
    return join_sql, where_sql, params


SOURCE_FIELDS = (
    "district_id",
    "agency_id_nces",
    "district_name",
    "state",
    "agency_type",
    "total_enrollment_excludes_ae",
    "district_website",
    "board_source_id",
    "platform",
    "source_status",
    "original_source_url",
    "discovered_from_url",
    "organization_external_id",
    "platform_tenant",
    "confidence",
    "requires_javascript",
    "is_active",
    "last_discovered_at",
    "last_checked_at",
    "last_successful_sync_at",
    "source_error_message",
    "source_created_at",
    "source_updated_at",
)


def export_board_sources_csv(
    *,
    state: str = "",
    agency_type: str = "",
    platform: str = "",
    source_status: str = "",
    min_enrollment: int | None = None,
    max_enrollment: int | None = None,
    db_path: Path | str | None = None,
    export_dir: Path | str | None = None,
) -> BoardExport:
    init_db(db_path)
    join_sql, where_sql, params = _source_query_parts(
        state=state,
        agency_type=agency_type,
        platform=platform,
        source_status=source_status,
        min_enrollment=min_enrollment,
        max_enrollment=max_enrollment,
    )
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT d.id AS district_id, d.agency_id_nces,
                   d.agency_name AS district_name, d.state, d.agency_type,
                   d.total_enrollment_excludes_ae, d.website AS district_website,
                   bs.id AS board_source_id, bs.platform, bs.source_status,
                   bs.source_url AS original_source_url, bs.discovered_from_url,
                   bs.organization_external_id, bs.platform_tenant, bs.confidence,
                   bs.requires_javascript, bs.is_active, bs.last_discovered_at,
                   bs.last_checked_at, bs.last_successful_sync_at,
                   bs.error_message AS source_error_message,
                   bs.created_at AS source_created_at, bs.updated_at AS source_updated_at
            FROM districts d {join_sql} {where_sql}
            ORDER BY d.state, d.agency_name, d.id
            """,
            params,
        )
        return _write_export(
            "board-sources",
            SOURCE_FIELDS,
            (dict(row) for row in rows),
            export_dir=export_dir,
        )


def _meeting_query_parts(filters: Mapping[str, str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("q"):
        clauses.append(
            "(m.title LIKE ? OR EXISTS ("
            "SELECT 1 FROM board_search_content sc WHERE sc.meeting_id = m.id "
            "AND (sc.title LIKE ? OR sc.body LIKE ?)))"
        )
        term = f"%{filters['q']}%"
        params.extend([term, term, term])
    if filters.get("district"):
        clauses.append("d.agency_name LIKE ?")
        params.append(f"%{filters['district']}%")
    for key, column in (
        ("state", "d.state"),
        ("platform", "m.platform"),
        ("meeting_type", "m.meeting_type"),
    ):
        if filters.get(key):
            clauses.append(f"{column} = ?")
            params.append(filters[key])
    if filters.get("date_from"):
        clauses.append("m.meeting_date >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("m.meeting_date <= ?")
        params.append(filters["date_to"])
    for key, expression in (
        ("has_agenda", "m.agenda_url IS NOT NULL"),
        ("has_minutes", "m.minutes_url IS NOT NULL"),
        (
            "has_attachments",
            "EXISTS (SELECT 1 FROM board_documents bd WHERE bd.board_meeting_id = m.id)",
        ),
        ("has_video", "(m.video_url IS NOT NULL OR m.livestream_url IS NOT NULL)"),
        ("changed", "m.revision_detected = 1"),
    ):
        if filters.get(key) == "1":
            clauses.append(expression)
        elif filters.get(key) == "0":
            clauses.append(f"NOT ({expression})")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


MEETING_FIELDS = (
    "meeting_id",
    "district_id",
    "agency_id_nces",
    "district_name",
    "state",
    "board_source_id",
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
    "original_source_url",
    "status",
    "revision_detected",
    "agenda_item_count",
    "document_count",
    "first_seen_at",
    "last_seen_at",
    "last_checked_at",
    "content_hash",
    "created_at",
    "updated_at",
)


def export_board_meetings_csv(
    filters: Mapping[str, str],
    *,
    db_path: Path | str | None = None,
    export_dir: Path | str | None = None,
) -> BoardExport:
    init_db(db_path)
    where_sql, params = _meeting_query_parts(filters)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT m.id AS meeting_id, m.district_id, d.agency_id_nces,
                   d.agency_name AS district_name, d.state, m.board_source_id,
                   m.platform, m.external_meeting_id, m.meeting_date,
                   m.meeting_start_time, m.meeting_end_time, m.meeting_datetime_text,
                   m.title, m.meeting_type, m.location_name, m.location_address,
                   m.description, m.agenda_url, m.minutes_url, m.packet_url,
                   m.public_notice_url, m.video_url, m.livestream_url,
                   m.source_url AS original_source_url, m.status, m.revision_detected,
                   (SELECT COUNT(*) FROM board_agenda_items ai
                    WHERE ai.board_meeting_id = m.id) AS agenda_item_count,
                   (SELECT COUNT(*) FROM board_documents bd
                    WHERE bd.board_meeting_id = m.id) AS document_count,
                   m.first_seen_at, m.last_seen_at, m.last_checked_at,
                   m.content_hash, m.created_at, m.updated_at
            FROM board_meetings m
            JOIN districts d ON d.id = m.district_id
            {where_sql}
            ORDER BY COALESCE(m.meeting_date, '') DESC, m.id DESC
            """,
            params,
        )
        return _write_export(
            "board-meetings",
            MEETING_FIELDS,
            (dict(row) for row in rows),
            export_dir=export_dir,
        )


SEARCH_FIELDS = (
    "query_text",
    "entity_type",
    "entity_id",
    "district_id",
    "agency_id_nces",
    "district_name",
    "state",
    "platform",
    "meeting_id",
    "meeting_date",
    "meeting_title",
    "meeting_type",
    "meeting_source_url",
    "agenda_item_id",
    "agenda_display_number",
    "agenda_item_title",
    "document_id",
    "document_type",
    "document_title",
    "document_filename",
    "document_sha256",
    "text_extraction_status",
    "result_title",
    "matching_excerpt",
    "original_source_url",
    "retrieved_at",
    "changed",
    "rank",
)


def _search_rows(query_text: str, search_kwargs: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    offset = 0
    while True:
        batch = search_board_content(
            query_text,
            **search_kwargs,
            limit=_SEARCH_BATCH_SIZE,
            offset=offset,
        )
        for row in batch:
            yield {
                **row,
                "query_text": query_text,
                "result_title": row.get("title"),
                "matching_excerpt": row.get("excerpt"),
                "original_source_url": row.get("source_url"),
            }
        if len(batch) < _SEARCH_BATCH_SIZE:
            return
        offset += len(batch)


def export_board_search_csv(
    query_text: str,
    *,
    state: str | None = None,
    district_ids: Sequence[int] | None = None,
    platform: str | None = None,
    document_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db_path: Path | str | None = None,
    export_dir: Path | str | None = None,
) -> BoardExport:
    init_db(db_path)
    search_kwargs = {
        "state": state,
        "district_ids": district_ids,
        "platform": platform,
        "document_type": document_type,
        "date_from": date_from,
        "date_to": date_to,
        "db_path": db_path,
    }
    return _write_export(
        "board-search",
        SEARCH_FIELDS,
        _search_rows(str(query_text), search_kwargs),
        export_dir=export_dir,
    )


DISCOVERY_LEDGER_FIELDS = (
    "run_id",
    "run_status",
    "states_json",
    "agency_types_json",
    "min_enrollment",
    "max_enrollment",
    "platform_filter",
    "status_filter",
    "force",
    "provider_directory_requested",
    "provider_directory_loaded",
    "provider_directory_organizations",
    "network_ipv4_only",
    "network_https_only",
    "website_moves_accepted",
    "run_queued_at",
    "run_started_at",
    "run_finished_at",
    "run_error_message",
    "ledger_item_id",
    "district_id",
    "agency_id_nces",
    "district_name",
    "state",
    "item_status",
    "item_started_at",
    "item_finished_at",
    "item_error_message",
    "website_original_url",
    "website_final_url",
    "board_source_id",
    "platform",
    "source_status",
    "original_source_url",
    "discovered_from_url",
    "source_error_message",
)


def export_board_discovery_run_csv(
    run_id: int,
    *,
    db_path: Path | str | None = None,
    export_dir: Path | str | None = None,
) -> BoardExport:
    init_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id AS run_id, r.status AS run_status, r.states_json,
                   r.agency_types_json, r.min_enrollment, r.max_enrollment,
                   r.platform_filter, r.status_filter, r.force,
                   r.provider_directory_requested, r.provider_directory_loaded,
                   r.provider_directory_organizations, r.network_ipv4_only,
                   r.network_https_only, r.website_moves_accepted,
                   r.queued_at AS run_queued_at, r.started_at AS run_started_at,
                   r.finished_at AS run_finished_at,
                   r.error_message AS run_error_message,
                   i.id AS ledger_item_id, i.district_id, d.agency_id_nces,
                   d.agency_name AS district_name, d.state,
                   i.status AS item_status, i.started_at AS item_started_at,
                   i.finished_at AS item_finished_at,
                   i.error_message AS item_error_message,
                   i.website_original_url, i.website_final_url,
                   bs.id AS board_source_id, bs.platform, bs.source_status,
                   bs.source_url AS original_source_url, bs.discovered_from_url,
                   bs.error_message AS source_error_message
            FROM board_discovery_runs r
            LEFT JOIN board_discovery_run_items i ON i.run_id = r.id
            LEFT JOIN districts d ON d.id = i.district_id
            LEFT JOIN board_sources bs ON bs.id = i.board_source_id
            WHERE r.id = ?
            ORDER BY i.id
            """,
            (int(run_id),),
        )
        return _write_export(
            f"board-discovery-run-{int(run_id)}",
            DISCOVERY_LEDGER_FIELDS,
            (dict(row) for row in rows),
            export_dir=export_dir,
        )


SYNC_LEDGER_FIELDS = (
    "run_id",
    "run_status",
    "states_json",
    "agency_types_json",
    "platforms_json",
    "source_status_filter",
    "sync_mode",
    "date_from",
    "date_to",
    "force",
    "run_queued_at",
    "run_started_at",
    "run_finished_at",
    "run_error_message",
    "ledger_item_id",
    "district_id",
    "agency_id_nces",
    "district_name",
    "state",
    "item_status",
    "meetings_discovered",
    "meetings_added",
    "meetings_updated",
    "documents_added",
    "documents_updated",
    "item_started_at",
    "item_finished_at",
    "item_error_message",
    "board_source_id",
    "platform",
    "current_source_status",
    "original_source_url",
    "discovered_from_url",
)


def export_board_sync_run_csv(
    run_id: int,
    *,
    db_path: Path | str | None = None,
    export_dir: Path | str | None = None,
) -> BoardExport:
    init_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id AS run_id, r.status AS run_status, r.states_json,
                   r.agency_types_json, r.platforms_json,
                   r.source_status AS source_status_filter, r.sync_mode,
                   r.date_from, r.date_to, r.force,
                   r.queued_at AS run_queued_at, r.started_at AS run_started_at,
                   r.finished_at AS run_finished_at,
                   r.error_message AS run_error_message,
                   i.id AS ledger_item_id, i.district_id, d.agency_id_nces,
                   d.agency_name AS district_name, d.state,
                   i.status AS item_status, i.meetings_discovered,
                   i.meetings_added, i.meetings_updated, i.documents_added,
                   i.documents_updated, i.started_at AS item_started_at,
                   i.finished_at AS item_finished_at,
                   i.error_message AS item_error_message,
                   bs.id AS board_source_id, bs.platform,
                   bs.source_status AS current_source_status,
                   bs.source_url AS original_source_url, bs.discovered_from_url
            FROM board_sync_runs r
            LEFT JOIN board_sync_run_items i ON i.run_id = r.id
            LEFT JOIN districts d ON d.id = i.district_id
            LEFT JOIN board_sources bs ON bs.id = i.board_source_id
            WHERE r.id = ?
            ORDER BY i.id
            """,
            (int(run_id),),
        )
        return _write_export(
            f"board-sync-run-{int(run_id)}",
            SYNC_LEDGER_FIELDS,
            (dict(row) for row in rows),
            export_dir=export_dir,
        )


__all__ = [
    "BoardExport",
    "DISCOVERY_LEDGER_FIELDS",
    "MEETING_FIELDS",
    "SEARCH_FIELDS",
    "SOURCE_FIELDS",
    "SYNC_LEDGER_FIELDS",
    "export_board_discovery_run_csv",
    "export_board_meetings_csv",
    "export_board_search_csv",
    "export_board_sources_csv",
    "export_board_sync_run_csv",
    "safe_csv_cell",
]
