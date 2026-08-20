from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from common import connect_db, init_db


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _collapse(value: Any) -> str:
    return " ".join(str(value or "").split())


def fts5_available(
    db_path: Path | str | None = None,
    *,
    conn: Any | None = None,
) -> bool:
    if conn is not None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'board_search_fts'"
        ).fetchone()
        return row is not None
    init_db(db_path)
    with connect_db(db_path) as owned:
        return fts5_available(conn=owned)


def build_fts_query(query_text: str) -> str:
    """Convert words and user-quoted phrases to a safe literal FTS5 query."""

    query_text = str(query_text or "").strip()
    if not query_text:
        return ""
    terms: list[str] = []
    for match in re.finditer(r'"([^"]+)"|(\S+)', query_text):
        phrase = _collapse(match.group(1) if match.group(1) is not None else match.group(2))
        if not phrase:
            continue
        phrase = phrase.replace('"', '""')
        terms.append(f'"{phrase}"')
    return " AND ".join(terms)


def _upsert_projection(
    conn: Any,
    *,
    entity_type: str,
    entity_id: int,
    district_id: int,
    meeting_id: int,
    agenda_item_id: int | None,
    document_id: int | None,
    state: str | None,
    platform: str | None,
    meeting_date: str | None,
    document_type: str | None,
    title: str | None,
    body: str | None,
    source_url: str,
    retrieved_at: str | None,
    changed: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO board_search_content (
            entity_type, entity_id, district_id, meeting_id, agenda_item_id,
            document_id, state, platform, meeting_date, document_type,
            title, body, source_url, retrieved_at, changed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_type, entity_id) DO UPDATE SET
            district_id = excluded.district_id,
            meeting_id = excluded.meeting_id,
            agenda_item_id = excluded.agenda_item_id,
            document_id = excluded.document_id,
            state = excluded.state,
            platform = excluded.platform,
            meeting_date = excluded.meeting_date,
            document_type = excluded.document_type,
            title = excluded.title,
            body = excluded.body,
            source_url = excluded.source_url,
            retrieved_at = excluded.retrieved_at,
            changed = excluded.changed
        """,
        (
            entity_type,
            int(entity_id),
            int(district_id),
            int(meeting_id),
            int(agenda_item_id) if agenda_item_id is not None else None,
            int(document_id) if document_id is not None else None,
            state,
            platform,
            meeting_date,
            document_type,
            title,
            body,
            source_url,
            retrieved_at,
            1 if changed else 0,
        ),
    )


def _project_document(conn: Any, document: Any, meeting: Any) -> None:
    document_id = int(document["id"])
    _upsert_projection(
        conn,
        entity_type="document",
        entity_id=document_id,
        district_id=int(meeting["district_id"]),
        meeting_id=int(meeting["id"]),
        agenda_item_id=(
            int(document["agenda_item_id"])
            if document["agenda_item_id"] is not None
            else None
        ),
        document_id=document_id,
        state=meeting["state"],
        platform=meeting["platform"],
        meeting_date=meeting["meeting_date"],
        document_type=document["document_type"],
        title=document["title"] or document["filename"],
        body=document["extracted_text"] or "",
        source_url=document["source_url"],
        retrieved_at=document["retrieved_at"] or document["last_seen_at"],
        changed=bool(meeting["revision_detected"] or document["has_revision"]),
    )


def _index_meeting_conn(conn: Any, meeting_id: int) -> int:
    meeting = conn.execute(
        """
        SELECT m.*, d.agency_name, d.agency_id_nces, d.state
        FROM board_meetings m
        JOIN districts d ON d.id = m.district_id
        WHERE m.id = ?
        """,
        (int(meeting_id),),
    ).fetchone()
    if meeting is None:
        return 0

    meeting_body = " ".join(
        filter(
            None,
            (
                _collapse(meeting["description"]),
                _collapse(meeting["meeting_type"]),
                _collapse(meeting["location_name"]),
                _collapse(meeting["location_address"]),
            ),
        )
    )
    _upsert_projection(
        conn,
        entity_type="meeting",
        entity_id=int(meeting["id"]),
        district_id=int(meeting["district_id"]),
        meeting_id=int(meeting["id"]),
        agenda_item_id=None,
        document_id=None,
        state=meeting["state"],
        platform=meeting["platform"],
        meeting_date=meeting["meeting_date"],
        document_type=None,
        title=meeting["title"],
        body=meeting_body,
        source_url=meeting["source_url"],
        retrieved_at=meeting["last_checked_at"] or meeting["last_seen_at"],
        changed=bool(meeting["revision_detected"]),
    )
    indexed = 1

    for item in conn.execute(
        """
        SELECT * FROM board_agenda_items
        WHERE board_meeting_id = ?
        ORDER BY sequence_number, id
        """,
        (int(meeting_id),),
    ):
        item_id = int(item["id"])
        body = item["normalized_text"] or " ".join(
            filter(
                None,
                (
                    _collapse(item["description"]),
                    _collapse(item["presenter"]),
                    _collapse(item["department"]),
                    _collapse(item["action_requested"]),
                    _collapse(item["motion_text"]),
                    _collapse(item["vote_text"]),
                    _collapse(item["result_text"]),
                ),
            )
        )
        _upsert_projection(
            conn,
            entity_type="agenda_item",
            entity_id=item_id,
            district_id=int(meeting["district_id"]),
            meeting_id=int(meeting["id"]),
            agenda_item_id=item_id,
            document_id=None,
            state=meeting["state"],
            platform=meeting["platform"],
            meeting_date=meeting["meeting_date"],
            document_type=None,
            title=item["title"] or item["display_number"],
            body=body,
            source_url=item["source_url"] or meeting["source_url"],
            retrieved_at=meeting["last_checked_at"] or meeting["last_seen_at"],
            changed=bool(meeting["revision_detected"]),
        )
        indexed += 1

    for document in conn.execute(
        """
        SELECT bd.*,
               EXISTS (
                   SELECT 1 FROM board_document_versions v
                   WHERE v.board_document_id = bd.id AND v.version_number > 1
               ) AS has_revision
        FROM board_documents bd
        WHERE bd.board_meeting_id = ?
        ORDER BY bd.id
        """,
        (int(meeting_id),),
    ):
        document_id = int(document["id"])
        _project_document(conn, document, meeting)
        indexed += 1

    # Normally FK cascades remove stale projections. These guards also repair
    # databases that predate those constraints or were restored with FKs disabled.
    conn.execute(
        """
        DELETE FROM board_search_content
        WHERE meeting_id = ? AND entity_type = 'agenda_item'
          AND NOT EXISTS (
              SELECT 1 FROM board_agenda_items ai
              WHERE ai.id = board_search_content.entity_id
          )
        """,
        (int(meeting_id),),
    )
    conn.execute(
        """
        DELETE FROM board_search_content
        WHERE meeting_id = ? AND entity_type = 'document'
          AND NOT EXISTS (
              SELECT 1 FROM board_documents bd
              WHERE bd.id = board_search_content.entity_id
          )
        """,
        (int(meeting_id),),
    )
    return indexed


def index_meeting(
    meeting_id: int,
    db_path: Path | str | None = None,
    *,
    conn: Any | None = None,
) -> int:
    """Refresh the meeting, agenda-item, and document search projection."""

    if conn is not None:
        return _index_meeting_conn(conn, meeting_id)
    init_db(db_path)
    with connect_db(db_path) as owned:
        owned.execute("BEGIN IMMEDIATE")
        count = _index_meeting_conn(owned, meeting_id)
        owned.commit()
    return count


def index_document(
    document_id: int,
    db_path: Path | str | None = None,
    *,
    conn: Any | None = None,
) -> bool:
    """Refresh one document projection without reindexing all meeting documents."""

    def apply(connection: Any) -> bool:
        row = connection.execute(
            """
            SELECT bd.*,
                   EXISTS (
                       SELECT 1 FROM board_document_versions v
                       WHERE v.board_document_id = bd.id AND v.version_number > 1
                   ) AS has_revision,
                   m.platform, m.meeting_date, m.revision_detected,
                   m.district_id, m.id AS joined_meeting_id,
                   d.state
            FROM board_documents bd
            JOIN board_meetings m ON m.id = bd.board_meeting_id
            JOIN districts d ON d.id = m.district_id
            WHERE bd.id = ?
            """,
            (int(document_id),),
        ).fetchone()
        if row is None:
            return False
        values = dict(row)
        meeting = {
            "id": int(values["joined_meeting_id"]),
            "district_id": int(values["district_id"]),
            "state": values["state"],
            "platform": values["platform"],
            "meeting_date": values["meeting_date"],
            "revision_detected": values["revision_detected"],
        }
        _project_document(connection, values, meeting)
        return True

    if conn is not None:
        return apply(conn)
    init_db(db_path)
    with connect_db(db_path) as owned:
        owned.execute("BEGIN IMMEDIATE")
        indexed = apply(owned)
        owned.commit()
    return indexed


def rebuild_board_search_index(db_path: Path | str | None = None) -> int:
    init_db(db_path)
    with connect_db(db_path) as conn:
        meeting_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM board_meetings ORDER BY id")
        ]
        conn.execute("DELETE FROM board_search_content")
        conn.commit()
    count = 0
    for meeting_id in meeting_ids:
        count += index_meeting(meeting_id, db_path)
    return count


def _append_in_filter(
    clauses: list[str],
    params: list[Any],
    column: str,
    values: Any,
) -> None:
    cleaned = [str(value).strip() for value in _list(values) if str(value).strip()]
    if not cleaned:
        return
    clauses.append(f"{column} IN ({','.join('?' for _ in cleaned)})")
    params.extend(cleaned)


def _filters(
    *,
    states: Any = None,
    date_from: str | None = None,
    date_to: str | None = None,
    district_ids: Any = None,
    district: Any = None,
    platforms: Any = None,
    document_types: Any = None,
    entity_types: Any = None,
    changed_only: bool = False,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    _append_in_filter(clauses, params, "sc.state", states)
    _append_in_filter(clauses, params, "sc.platform", platforms)
    _append_in_filter(clauses, params, "sc.document_type", document_types)
    _append_in_filter(clauses, params, "sc.entity_type", entity_types)

    ids = _list(district_ids)
    if district is not None and district != "":
        if isinstance(district, int) or str(district).strip().isdigit():
            ids.append(int(district))
        else:
            clauses.append("d.agency_name LIKE ? COLLATE NOCASE")
            params.append(f"%{str(district).strip()}%")
    cleaned_ids = [int(value) for value in ids if str(value).strip().isdigit()]
    if cleaned_ids:
        clauses.append(f"sc.district_id IN ({','.join('?' for _ in cleaned_ids)})")
        params.extend(cleaned_ids)
    if date_from:
        clauses.append("sc.meeting_date >= ?")
        params.append(str(date_from))
    if date_to:
        clauses.append("sc.meeting_date <= ?")
        params.append(str(date_to))
    if changed_only:
        clauses.append("sc.changed = 1")
    return clauses, params


SEARCH_SELECT = """
    SELECT sc.id, sc.entity_type, sc.entity_id, sc.district_id,
           sc.meeting_id, sc.agenda_item_id, sc.document_id, sc.state,
           sc.platform, sc.meeting_date, sc.document_type, sc.title,
           sc.body, sc.source_url, sc.retrieved_at, sc.changed,
           d.agency_name AS agency_name,
           d.agency_name AS district_name,
           d.agency_id_nces,
           m.title AS meeting_title,
           m.meeting_type,
           m.source_url AS meeting_source_url,
           m.revision_detected AS meeting_revision_detected,
           ai.display_number AS agenda_display_number,
           ai.title AS agenda_item_title,
           bd.title AS document_title,
           bd.filename AS document_filename,
           bd.text_extraction_status,
           bd.sha256 AS document_sha256
    FROM board_search_content sc
    JOIN districts d ON d.id = sc.district_id
    JOIN board_meetings m ON m.id = sc.meeting_id
    LEFT JOIN board_agenda_items ai ON ai.id = sc.agenda_item_id
    LEFT JOIN board_documents bd ON bd.id = sc.document_id
"""


def _like_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    for match in re.finditer(r'"([^"]+)"|(\S+)', str(query_text or "")):
        term = _collapse(match.group(1) if match.group(1) is not None else match.group(2))
        if term:
            terms.append(term)
    return terms


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _plain_excerpt(body: str, terms: list[str], radius: int = 180) -> str:
    body = _collapse(body)
    if not body:
        return ""
    folded = body.casefold()
    positions = [folded.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    position = min(positions) if positions else 0
    start = max(0, position - radius)
    end = min(len(body), position + radius * 2)
    return f"{'… ' if start else ''}{body[start:end]}{' …' if end < len(body) else ''}"


def _search_like(
    conn: Any,
    query_text: str,
    clauses: list[str],
    params: list[Any],
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    terms = _like_terms(query_text)
    like_clauses: list[str] = []
    like_params: list[Any] = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        like_clauses.append(
            "(COALESCE(sc.title, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(sc.body, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        like_params.extend((pattern, pattern))
    all_clauses = [*like_clauses, *clauses]
    where_sql = f" WHERE {' AND '.join(all_clauses)}" if all_clauses else ""
    rows = conn.execute(
        f"""
        {SEARCH_SELECT}
        {where_sql}
        ORDER BY sc.meeting_date DESC, sc.meeting_id DESC, sc.id
        LIMIT ? OFFSET ?
        """,
        [*like_params, *params, limit, offset],
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        result = dict(row)
        result["rank"] = None
        result["excerpt"] = _plain_excerpt(result.get("body") or result.get("title") or "", terms)
        results.append(result)
    return results


def search_board_content(
    query_text: str = "",
    *,
    states: Any = None,
    state: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    district_ids: Any = None,
    district: Any = None,
    platforms: Any = None,
    platform: str | None = None,
    document_types: Any = None,
    document_type: str | None = None,
    entity_types: Any = None,
    changed_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Search normalized board content while retaining source provenance."""

    init_db(db_path)
    states = [*_list(states), *([state] if state else [])]
    platforms = [*_list(platforms), *([platform] if platform else [])]
    document_types = [*_list(document_types), *([document_type] if document_type else [])]
    clauses, params = _filters(
        states=states,
        date_from=date_from,
        date_to=date_to,
        district_ids=district_ids,
        district=district,
        platforms=platforms,
        document_types=document_types,
        entity_types=entity_types,
        changed_only=changed_only,
    )
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    fts_query = build_fts_query(query_text)

    with connect_db(db_path) as conn:
        if not fts_query or not fts5_available(conn=conn):
            return _search_like(conn, query_text, clauses, params, limit, offset)
        where_sql = " AND ".join(["board_search_fts MATCH ?", *clauses])
        try:
            rows = conn.execute(
                f"""
                SELECT base.*,
                       snippet(board_search_fts, 1, '[', ']', ' … ', 24) AS excerpt,
                       bm25(board_search_fts, 2.0, 1.0) AS rank
                FROM (
                    {SEARCH_SELECT}
                ) AS base
                JOIN board_search_content sc ON sc.id = base.id
                JOIN districts d ON d.id = sc.district_id
                JOIN board_search_fts ON board_search_fts.rowid = sc.id
                WHERE {where_sql}
                ORDER BY rank, sc.meeting_date DESC, sc.id
                LIMIT ? OFFSET ?
                """,
                [fts_query, *params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return _search_like(conn, query_text, clauses, params, limit, offset)


def count_board_content(
    query_text: str = "",
    *,
    states: Any = None,
    state: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    district_ids: Any = None,
    district: Any = None,
    platforms: Any = None,
    platform: str | None = None,
    document_types: Any = None,
    document_type: str | None = None,
    entity_types: Any = None,
    changed_only: bool = False,
    db_path: Path | str | None = None,
) -> int:
    """Count board-search hits using the same filters as ``search_board_content``."""

    init_db(db_path)
    states = [*_list(states), *([state] if state else [])]
    platforms = [*_list(platforms), *([platform] if platform else [])]
    document_types = [*_list(document_types), *([document_type] if document_type else [])]
    clauses, params = _filters(
        states=states,
        date_from=date_from,
        date_to=date_to,
        district_ids=district_ids,
        district=district,
        platforms=platforms,
        document_types=document_types,
        entity_types=entity_types,
        changed_only=changed_only,
    )
    fts_query = build_fts_query(query_text)
    with connect_db(db_path) as conn:
        if fts_query and fts5_available(conn=conn):
            where_sql = " AND ".join(["board_search_fts MATCH ?", *clauses])
            try:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM board_search_fts
                    JOIN board_search_content sc ON sc.id = board_search_fts.rowid
                    JOIN districts d ON d.id = sc.district_id
                    WHERE {where_sql}
                    """,
                    [fts_query, *params],
                ).fetchone()
                return int(row["count"] or 0)
            except sqlite3.OperationalError:
                pass
        terms = _like_terms(query_text)
        like_clauses: list[str] = []
        like_params: list[Any] = []
        for term in terms:
            pattern = f"%{_escape_like(term)}%"
            like_clauses.append(
                "(COALESCE(sc.title, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR COALESCE(sc.body, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            like_params.extend((pattern, pattern))
        all_clauses = [*like_clauses, *clauses]
        where_sql = f" WHERE {' AND '.join(all_clauses)}" if all_clauses else ""
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM board_search_content sc
            JOIN districts d ON d.id = sc.district_id
            {where_sql}
            """,
            [*like_params, *params],
        ).fetchone()
        return int(row["count"] or 0)


search_board = search_board_content
reindex_board_search = rebuild_board_search_index


__all__ = [
    "build_fts_query",
    "count_board_content",
    "fts5_available",
    "index_document",
    "index_meeting",
    "rebuild_board_search_index",
    "reindex_board_search",
    "search_board",
    "search_board_content",
]
