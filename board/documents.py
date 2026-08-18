from __future__ import annotations

import hashlib
import io
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4
from xml.etree import ElementTree

from pypdf import PdfReader

from common import BOARD_DOCUMENTS_DIR, BOARD_MAX_DOCUMENT_SIZE_BYTES, connect_db, init_db, utc_now_iso
from search_engine import parse_html

from board.models import stable_json
from board.storage import canonicalize_url, value_of


WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    status: str
    text: str = ""
    mime_type: str = "application/octet-stream"
    error_message: str | None = None


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_path_segment(
    value: Any,
    *,
    max_length: int = 80,
    fallback: str = "item",
) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    segment = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-. ")
    segment = segment[: max(1, int(max_length))].rstrip("-. ") or fallback
    if segment.casefold() in WINDOWS_RESERVED_NAMES:
        segment = f"{fallback}-{segment}"
    return segment


def _normalized_mime(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().casefold()


def _filename_from_url(url: str) -> str:
    return Path(unquote(urlparse(url).path)).name


def _document_ref(document: Any) -> Any:
    return value_of(document, "document_ref", default=None) or document


def _document_value(document: Any, *names: str, default: Any = None) -> Any:
    value = value_of(document, *names, default=None)
    if value is not None:
        return value
    ref = _document_ref(document)
    if ref is not document:
        return value_of(ref, *names, default=default)
    return default


def document_identity(document: Any) -> str:
    external_id = str(
        _document_value(document, "external_document_id", "external_id", "id", default="")
        or ""
    ).strip()
    if external_id:
        return f"external:{external_id}"
    ref = _document_ref(document)
    url = canonicalize_url(
        value_of(ref, "source_url", "url")
        or value_of(document, "source_url", "url", "final_url", "resolved_url")
    )
    if not url:
        title = str(_document_value(document, "title", "name", default="") or "").strip()
        if not title:
            raise ValueError("A document external ID, URL, or title is required.")
        return f"title:{hashlib.sha256(title.casefold().encode('utf-8')).hexdigest()[:32]}"
    return f"url:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}"


def detect_document_type(
    content: bytes,
    *,
    mime_type: str = "",
    filename: str = "",
) -> tuple[str, str]:
    mime = _normalized_mime(mime_type)
    suffix = Path(filename).suffix.casefold()
    prefix = content[:512].lstrip().lower()
    if content.startswith(b"%PDF") or mime == "application/pdf" or suffix == ".pdf":
        return "pdf", "application/pdf"
    if content.startswith(b"PK\x03\x04") and (
        mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if mime in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"} or prefix.startswith((b"<!doctype html", b"<html")):
        return "html", mime or "text/html"
    if mime.startswith("text/") or suffix in {".txt", ".csv", ".md", ".log"}:
        return "text", mime or "text/plain"
    return "unsupported", mime or "application/octet-stream"


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _extract_pdf(content: bytes, max_pages: int) -> str:
    reader = PdfReader(io.BytesIO(content))
    parts: list[str] = []
    for page in reader.pages[: max(1, int(max_pages))]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _extract_docx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{namespace}tab":
                parts.append("\t")
            elif node.tag in {f"{namespace}br", f"{namespace}cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_document_text(
    content: bytes,
    *,
    mime_type: str = "",
    filename: str = "",
    max_bytes: int = BOARD_MAX_DOCUMENT_SIZE_BYTES,
    max_pdf_pages: int = 250,
) -> ExtractionResult:
    if len(content) > max(1, int(max_bytes)):
        return ExtractionResult(
            "too_large",
            mime_type=_normalized_mime(mime_type) or "application/octet-stream",
            error_message=f"Document exceeded the configured limit of {max_bytes} bytes.",
        )
    kind, detected_mime = detect_document_type(
        content,
        mime_type=mime_type,
        filename=filename,
    )
    try:
        if kind == "pdf":
            text = _extract_pdf(content, max_pdf_pages)
        elif kind == "docx":
            text = _extract_docx(content)
        elif kind == "html":
            _title, _headings, text, _links = parse_html(content, "https://board.invalid/")
        elif kind == "text":
            text = _decode_text(content)
        else:
            return ExtractionResult("unsupported", mime_type=detected_mime)
    except Exception as exc:
        return ExtractionResult(
            "failed",
            mime_type=detected_mime,
            error_message=str(exc),
        )
    text = text.strip()
    if not text:
        return ExtractionResult("no_text", mime_type=detected_mime)
    return ExtractionResult("extracted", text=text, mime_type=detected_mime)


def _extension(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix.casefold()
    if suffix in {
        ".pdf",
        ".docx",
        ".doc",
        ".rtf",
        ".txt",
        ".csv",
        ".html",
        ".htm",
        ".bin",
    }:
        return ".html" if suffix == ".htm" else suffix
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/msword": ".doc",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
    }.get(_normalized_mime(mime_type), ".bin")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size == len(content):
        return
    temporary = path.with_name(
        f"{path.name}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
    temporary.write_bytes(content)
    temporary.replace(path)


def board_document_directory(
    district: Any,
    meeting: Any,
    *,
    storage_root: Path | str = BOARD_DOCUMENTS_DIR,
) -> Path:
    state = str(district["state"] or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        state = "XX"
    district_key = district["agency_id_nces"] or district["id"]
    meeting_key = meeting["external_meeting_id"] or meeting["id"]
    return (
        Path(storage_root).expanduser().resolve()
        / state
        / safe_path_segment(district_key, max_length=40, fallback=f"district-{district['id']}")
        / safe_path_segment(meeting_key, max_length=64, fallback=f"meeting-{meeting['id']}")
    )


def _archive_path(
    district: Any,
    meeting: Any,
    *,
    title: str,
    filename: str,
    mime_type: str,
    digest: str,
    content: bytes,
    storage_root: Path | str,
) -> Path:
    directory = board_document_directory(district, meeting, storage_root=storage_root)
    descriptive = safe_path_segment(
        Path(filename).stem or title,
        max_length=90,
        fallback="board-document",
    )
    path = directory / f"{descriptive}--{digest[:16]}{_extension(filename, mime_type)}"
    _atomic_write(path, content)
    return path.resolve()


def conditional_request_headers(document: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    etag = _document_value(document, "http_etag", "etag")
    modified = _document_value(document, "http_last_modified", "last_modified")
    if etag:
        headers["If-None-Match"] = str(etag)
    if modified:
        headers["If-Modified-Since"] = str(modified)
    return headers


def store_board_document(
    district_id: int,
    board_meeting_id: int,
    document: Any,
    content: bytes | None = None,
    *,
    agenda_item_id: int | None = None,
    storage_root: Path | str = BOARD_DOCUMENTS_DIR,
    max_bytes: int = BOARD_MAX_DOCUMENT_SIZE_BYTES,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Archive and version one document; identical bytes only refresh last_seen."""

    init_db(db_path)
    with connect_db(db_path) as conn:
        context = conn.execute(
            """
            SELECT m.*, d.state, d.agency_id_nces, d.agency_name
            FROM board_meetings m
            JOIN districts d ON d.id = m.district_id
            WHERE m.id = ?
            """,
            (int(board_meeting_id),),
        ).fetchone()
        if context is None:
            raise ValueError(f"Board meeting not found: {board_meeting_id}")
        if int(context["district_id"]) != int(district_id):
            raise ValueError("The board meeting does not belong to the supplied district.")
        district = {
            "id": int(district_id),
            "state": context["state"],
            "agency_id_nces": context["agency_id_nces"],
        }
        meeting = {
            "id": int(board_meeting_id),
            "external_meeting_id": context["external_meeting_id"],
        }

    status_code_value = _document_value(document, "status_code", "http_status")
    status_code = int(status_code_value) if status_code_value is not None else None
    if content is None:
        supplied = value_of(document, "content", "bytes", "data")
        content = bytes(supplied) if isinstance(supplied, (bytes, bytearray, memoryview)) else None
    if status_code == 304 or (status_code is not None and status_code >= 400):
        content = None

    ref = _document_ref(document)
    source_url = canonicalize_url(
        value_of(ref, "source_url", "url")
        or value_of(document, "source_url", "url")
    )
    resolved_url = canonicalize_url(
        _document_value(document, "resolved_url", "final_url", "url")
    ) or source_url
    if not source_url:
        source_url = resolved_url
    if not source_url:
        raise ValueError("A board document source URL is required.")
    title = str(_document_value(document, "title", "name", default="") or "").strip()
    filename = str(
        _document_value(document, "filename", "file_name", default="")
        or _filename_from_url(resolved_url or source_url)
        or title
        or "board-document"
    ).strip()
    mime_type = _normalized_mime(
        _document_value(document, "mime_type", "content_type", default="")
    )
    identity_key = document_identity(document)
    document_type = str(
        _document_value(document, "document_type", "type", default="other") or "other"
    ).strip().casefold()
    external_document_id = str(
        _document_value(document, "external_document_id", "external_id", default="") or ""
    ).strip() or None
    etag = str(_document_value(document, "etag", "http_etag", default="") or "").strip() or None
    last_modified = str(
        _document_value(document, "last_modified", "http_last_modified", default="") or ""
    ).strip() or None
    retrieved_at = str(
        _document_value(document, "fetched_at", "retrieved_at", default="") or utc_now_iso()
    )
    reference_metadata = value_of(ref, "metadata", default={}) or {}
    download_metadata = value_of(document, "metadata", default={}) or {}
    retrieval_metadata: dict[str, Any] = {}
    if isinstance(reference_metadata, dict):
        retrieval_metadata.update(reference_metadata)
    if isinstance(download_metadata, dict):
        retrieval_metadata.update(download_metadata)
    retrieval_metadata_json = stable_json(retrieval_metadata) if retrieval_metadata else None

    if agenda_item_id is None:
        external_agenda_id = str(
            _document_value(document, "agenda_item_external_id", default="") or ""
        ).strip()
        if external_agenda_id:
            with connect_db(db_path) as conn:
                agenda_row = conn.execute(
                    """
                    SELECT id FROM board_agenda_items
                    WHERE board_meeting_id = ? AND external_item_id = ?
                    """,
                    (int(board_meeting_id), external_agenda_id),
                ).fetchone()
            if agenda_row:
                agenda_item_id = int(agenda_row["id"])

    digest: str | None = None
    local_path: str | None = None
    extraction = ExtractionResult("pending", mime_type=mime_type or "application/octet-stream")
    if content is not None:
        digest = sha256_bytes(content)
        extraction = extract_document_text(
            content,
            mime_type=mime_type,
            filename=filename,
            max_bytes=max_bytes,
        )
        mime_type = extraction.mime_type
        if extraction.status != "too_large":
            local_path = str(
                _archive_path(
                    district,
                    meeting,
                    title=title,
                    filename=filename,
                    mime_type=mime_type,
                    digest=digest,
                    content=content,
                    storage_root=storage_root,
                )
            )
    else:
        supplied_text = str(_document_value(document, "extracted_text", default="") or "").strip()
        supplied_status = str(
            _document_value(document, "text_extraction_status", default="") or ""
        ).strip()
        supplied_error = str(
            _document_value(document, "error_message", "error", default="") or ""
        ).strip() or None
        if supplied_text:
            extraction = ExtractionResult("extracted", supplied_text, mime_type or "text/plain")
        elif supplied_status:
            extraction = ExtractionResult(
                supplied_status,
                mime_type=mime_type or "application/octet-stream",
                error_message=supplied_error,
            )
        elif status_code is not None and status_code >= 400:
            extraction = ExtractionResult(
                "failed",
                mime_type=mime_type or "application/octet-stream",
                error_message=f"HTTP {status_code}",
            )

    now = utc_now_iso()
    with connect_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT * FROM board_documents
            WHERE board_meeting_id = ? AND identity_key = ?
            """,
            (int(board_meeting_id), identity_key),
        ).fetchone()
        created = existing is None
        changed = bool(existing is not None and digest is not None and existing["sha256"] != digest)
        unchanged = bool(existing is not None and digest is not None and existing["sha256"] == digest)

        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO board_documents (
                    district_id, board_meeting_id, agenda_item_id, identity_key,
                    document_type, title, source_url, resolved_url, mime_type,
                    filename, local_path, external_document_id, size_bytes, sha256,
                    http_status, http_etag, http_last_modified,
                    retrieval_metadata_json,
                    text_extraction_status, extracted_text, error_message,
                    first_seen_at, last_seen_at, retrieved_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(district_id),
                    int(board_meeting_id),
                    agenda_item_id,
                    identity_key,
                    document_type,
                    title or filename,
                    source_url,
                    resolved_url,
                    mime_type or None,
                    filename,
                    local_path,
                    external_document_id,
                    len(content) if content is not None else None,
                    digest,
                    status_code,
                    etag,
                    last_modified,
                    retrieval_metadata_json,
                    extraction.status,
                    extraction.text or None,
                    extraction.error_message,
                    now,
                    now,
                    retrieved_at,
                    now,
                    now,
                ),
            )
            document_id = int(cursor.lastrowid)
        else:
            document_id = int(existing["id"])
            active_digest = digest if digest is not None else existing["sha256"]
            active_size = len(content) if content is not None else existing["size_bytes"]
            active_path = local_path if local_path is not None else existing["local_path"]
            active_text = extraction.text if content is not None else existing["extracted_text"]
            active_extraction_status = (
                extraction.status if content is not None or extraction.status != "pending"
                else existing["text_extraction_status"]
            )
            active_error = (
                extraction.error_message
                if extraction.status in {"failed", "too_large"}
                else None
            )
            conn.execute(
                """
                UPDATE board_documents
                SET agenda_item_id = COALESCE(?, agenda_item_id),
                    document_type = ?, title = ?, source_url = ?, resolved_url = ?,
                    mime_type = COALESCE(?, mime_type), filename = ?, local_path = ?,
                    external_document_id = COALESCE(?, external_document_id),
                    size_bytes = ?, sha256 = ?, http_status = COALESCE(?, http_status),
                    http_etag = COALESCE(?, http_etag),
                    http_last_modified = COALESCE(?, http_last_modified),
                    retrieval_metadata_json = COALESCE(?, retrieval_metadata_json),
                    text_extraction_status = ?, extracted_text = ?, error_message = ?,
                    last_seen_at = ?, retrieved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    agenda_item_id,
                    document_type,
                    title or filename,
                    source_url,
                    resolved_url,
                    mime_type or None,
                    filename,
                    active_path,
                    external_document_id,
                    active_size,
                    active_digest,
                    status_code,
                    etag,
                    last_modified,
                    retrieval_metadata_json,
                    active_extraction_status,
                    active_text,
                    active_error,
                    now,
                    retrieved_at,
                    now,
                    document_id,
                ),
            )

        version_created = False
        version_number: int | None = None
        if digest is not None:
            version = conn.execute(
                """
                SELECT id, version_number FROM board_document_versions
                WHERE board_document_id = ? AND sha256 = ?
                """,
                (document_id, digest),
            ).fetchone()
            if version is None:
                version_number = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(version_number), 0) + 1 AS value
                        FROM board_document_versions WHERE board_document_id = ?
                        """,
                        (document_id,),
                    ).fetchone()["value"]
                )
                conn.execute(
                    """
                    INSERT INTO board_document_versions (
                        board_document_id, version_number, sha256, size_bytes,
                        local_path, extracted_text, text_extraction_status,
                        http_etag, http_last_modified, retrieval_metadata_json,
                        first_seen_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        version_number,
                        digest,
                        len(content) if content is not None else None,
                        local_path,
                        extraction.text or None,
                        extraction.status,
                        etag,
                        last_modified,
                        retrieval_metadata_json,
                        now,
                        now,
                    ),
                )
                version_created = True
            else:
                version_number = int(version["version_number"])

        from board.search import index_document

        index_document(document_id, conn=conn)
        row = conn.execute("SELECT * FROM board_documents WHERE id = ?", (document_id,)).fetchone()
        conn.commit()

    result = dict(row)
    result.update(
        {
            "created": created,
            "changed": changed,
            "unchanged": unchanged,
            "version_created": version_created,
            "version_number": version_number,
        }
    )
    return result


save_board_document = store_board_document
upsert_board_document = store_board_document
extract_text = extract_document_text


__all__ = [
    "ExtractionResult",
    "board_document_directory",
    "conditional_request_headers",
    "detect_document_type",
    "document_identity",
    "extract_document_text",
    "extract_text",
    "safe_path_segment",
    "save_board_document",
    "sha256_bytes",
    "store_board_document",
    "upsert_board_document",
]
