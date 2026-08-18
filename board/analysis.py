from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from common import connect_db, init_db, utc_now_iso

from board.storage import object_to_dict, stable_json


ENTITY_TABLES = {
    "meeting": "board_meetings",
    "agenda_item": "board_agenda_items",
    "document": "board_documents",
}


class BoardAnalyzer:
    """Optional analysis interface; the default implementation is deliberately inert."""

    provider = "disabled"
    model = ""
    prompt_version = ""

    def analyze_meeting(
        self,
        meeting: Any,
        *,
        agenda_items: Any = None,
        documents: Any = None,
    ) -> Mapping[str, Any] | None:
        return None

    def analyze_agenda_item(
        self,
        agenda_item: Any,
        *,
        meeting: Any = None,
        documents: Any = None,
    ) -> Mapping[str, Any] | None:
        return None

    def analyze_document(
        self,
        document: Any,
        *,
        meeting: Any = None,
        agenda_item: Any = None,
    ) -> Mapping[str, Any] | None:
        return None


class NoOpBoardAnalyzer(BoardAnalyzer):
    pass


DEFAULT_BOARD_ANALYZER = NoOpBoardAnalyzer()


def analysis_input_hash(value: Any) -> str:
    payload = value if isinstance(value, bytes) else stable_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _structured_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converted = object_to_dict(value)
    if converted:
        return converted
    raise TypeError("Board analysis results must be structured mappings or dataclass-like objects.")


def store_analysis_result(
    *,
    entity_type: str,
    entity_id: int,
    analysis_type: str,
    result: Any,
    input_hash: str | None = None,
    input_content: Any = None,
    provider: str = "",
    model: str = "",
    prompt_version: str = "",
    confidence: float | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Idempotently persist structured output apart from collected source records."""

    entity_type = str(entity_type or "").strip().casefold()
    if entity_type not in ENTITY_TABLES:
        raise ValueError(f"Unsupported board analysis entity type: {entity_type}")
    analysis_type = str(analysis_type or "").strip()
    if not analysis_type:
        raise ValueError("An analysis type is required.")
    structured = _structured_result(result)
    if input_hash is None:
        if input_content is None:
            raise ValueError("input_hash or input_content is required for reproducible analysis.")
        input_hash = analysis_input_hash(input_content)
    input_hash = str(input_hash).strip().casefold()
    if not input_hash:
        raise ValueError("A non-empty input hash is required.")
    if confidence is None and structured.get("confidence") is not None:
        try:
            confidence = float(structured["confidence"])
        except (TypeError, ValueError):
            confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, float(confidence)))

    # Empty strings, rather than NULL, make the schema's composite uniqueness
    # constraint idempotent for the disabled/local-provider cases too.
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    prompt_version = str(prompt_version or "").strip()
    now = utc_now_iso()
    init_db(db_path)
    with connect_db(db_path) as conn:
        entity = conn.execute(
            f"SELECT 1 FROM {ENTITY_TABLES[entity_type]} WHERE id = ?",
            (int(entity_id),),
        ).fetchone()
        if entity is None:
            raise ValueError(f"Board {entity_type} not found: {entity_id}")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO board_analysis (
                entity_type, entity_id, analysis_type, provider, model,
                prompt_version, input_hash, result_json, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                entity_type, entity_id, analysis_type, provider, model,
                prompt_version, input_hash
            ) DO UPDATE SET
                result_json = excluded.result_json,
                confidence = excluded.confidence
            """,
            (
                entity_type,
                int(entity_id),
                analysis_type,
                provider,
                model,
                prompt_version,
                input_hash,
                stable_json(structured),
                confidence,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM board_analysis
            WHERE entity_type = ? AND entity_id = ? AND analysis_type = ?
              AND provider = ? AND model = ? AND prompt_version = ?
              AND input_hash = ?
            """,
            (
                entity_type,
                int(entity_id),
                analysis_type,
                provider,
                model,
                prompt_version,
                input_hash,
            ),
        ).fetchone()
        conn.commit()
    saved = dict(row)
    saved["result"] = json.loads(saved["result_json"])
    return saved


def list_analysis_results(
    entity_type: str,
    entity_id: int,
    *,
    analysis_type: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses = ["entity_type = ?", "entity_id = ?"]
    params: list[Any] = [str(entity_type).strip().casefold(), int(entity_id)]
    if analysis_type:
        clauses.append("analysis_type = ?")
        params.append(str(analysis_type))
    with connect_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM board_analysis
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            """,
            params,
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["result"] = json.loads(item["result_json"])
        except json.JSONDecodeError:
            item["result"] = {}
        results.append(item)
    return results


def latest_analysis_result(
    entity_type: str,
    entity_id: int,
    *,
    analysis_type: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    rows = list_analysis_results(
        entity_type,
        entity_id,
        analysis_type=analysis_type,
        db_path=db_path,
    )
    return rows[0] if rows else None


def analyze_and_store(
    analyzer: BoardAnalyzer,
    *,
    entity_type: str,
    entity_id: int,
    entity: Any,
    analysis_type: str = "structured",
    input_content: Any = None,
    context: Mapping[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    context = dict(context or {})
    if entity_type == "meeting":
        result = analyzer.analyze_meeting(entity, **context)
    elif entity_type == "agenda_item":
        result = analyzer.analyze_agenda_item(entity, **context)
    elif entity_type == "document":
        result = analyzer.analyze_document(entity, **context)
    else:
        raise ValueError(f"Unsupported board analysis entity type: {entity_type}")
    if result is None:
        return None
    return store_analysis_result(
        entity_type=entity_type,
        entity_id=entity_id,
        analysis_type=analysis_type,
        result=result,
        input_content=entity if input_content is None else input_content,
        provider=getattr(analyzer, "provider", ""),
        model=getattr(analyzer, "model", ""),
        prompt_version=getattr(analyzer, "prompt_version", ""),
        db_path=db_path,
    )


save_analysis_result = store_analysis_result


__all__ = [
    "BoardAnalyzer",
    "DEFAULT_BOARD_ANALYZER",
    "NoOpBoardAnalyzer",
    "analysis_input_hash",
    "analyze_and_store",
    "latest_analysis_result",
    "list_analysis_results",
    "save_analysis_result",
    "store_analysis_result",
]
