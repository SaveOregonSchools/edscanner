from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any, Mapping


JsonDict = dict[str, Any]


def _first_present(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off", ""}:
        return False
    return default


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Unsupported value for stable JSON: {type(value).__name__}")


def stable_json(value: Any) -> str:
    """Return deterministic compact JSON suitable for revision hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def content_hash(value: Any) -> str:
    """Hash bytes directly and all other values through :func:`stable_json`."""

    payload = value if isinstance(value, bytes) else stable_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class DetectionResult:
    matched: bool
    platform: str
    confidence: float
    reason: str = ""
    canonical_url: str | None = None
    requires_javascript: bool = False
    metadata: JsonDict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.matched

    @property
    def platform_name(self) -> str:
        return self.platform


@dataclass(slots=True)
class BoardSource:
    platform: str
    public_url: str
    external_source_id: str | None = None
    district_id: int | None = None
    organization_name: str | None = None
    status: str = "working"
    requires_javascript: bool = False
    metadata: JsonDict = field(default_factory=dict)

    @property
    def source_url(self) -> str:
        return self.public_url

    @property
    def canonical_url(self) -> str:
        return self.public_url


@dataclass(slots=True)
class BoardSourceResult:
    detection: DetectionResult
    source: BoardSource | None = None
    status: str = "working"
    candidate_url: str | None = None
    error: str | None = None
    metadata: JsonDict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.source is not None and self.status == "working"

    @property
    def platform(self) -> str:
        return self.source.platform if self.source is not None else self.detection.platform

    @property
    def confidence(self) -> float:
        return self.detection.confidence

    @property
    def source_url(self) -> str | None:
        return self.source.public_url if self.source is not None else self.detection.canonical_url

    @property
    def requires_javascript(self) -> bool:
        return bool(
            self.detection.requires_javascript
            or (self.source is not None and self.source.requires_javascript)
        )

    @property
    def organization_external_id(self) -> str | None:
        if self.source is not None and self.source.external_source_id not in (None, ""):
            return self.source.external_source_id
        value = self.detection.metadata.get("external_source_id")
        if value in (None, ""):
            value = self.detection.metadata.get("organization_external_id")
        return str(value) if value not in (None, "") else None

    @property
    def platform_tenant(self) -> str | None:
        metadata = self.source.metadata if self.source is not None else self.detection.metadata
        value = metadata.get("platform_tenant") or metadata.get("tenant")
        return str(value) if value not in (None, "") else None

    @property
    def error_message(self) -> str | None:
        return self.error


@dataclass(slots=True)
class DocumentRef:
    title: str
    url: str
    external_document_id: str | None = None
    document_type: str = "attachment"
    agenda_item_external_id: str | None = None
    content_type: str | None = None
    file_name: str | None = None
    metadata: JsonDict = field(default_factory=dict)

    def identity_key(self) -> tuple[str, str, str]:
        return (
            self.external_document_id or "",
            self.url,
            self.agenda_item_external_id or "",
        )


@dataclass(slots=True)
class AgendaItem:
    external_item_id: str
    title: str
    item_number: str | None = None
    description: str | None = None
    parent_external_item_id: str | None = None
    depth: int = 0
    order_index: int = 0
    timestamp: str | None = None
    documents: list[DocumentRef] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @property
    def attachments(self) -> list[DocumentRef]:
        return self.documents


@dataclass(slots=True)
class MeetingRef:
    external_meeting_id: str
    url: str
    title: str = ""
    meeting_date: str | None = None
    meeting_start_time: str | None = None
    meeting_end_time: str | None = None
    meeting_datetime_text: str | None = None
    meeting_type: str | None = None
    location: str | None = None
    location_address: str | None = None
    agenda_url: str | None = None
    minutes_url: str | None = None
    packet_url: str | None = None
    video_url: str | None = None
    public_notice_url: str | None = None
    livestream_url: str | None = None
    is_cancelled: bool = False
    metadata: JsonDict = field(default_factory=dict)

    @property
    def start_time(self) -> str | None:
        return self.meeting_start_time

    @property
    def end_time(self) -> str | None:
        return self.meeting_end_time


@dataclass(slots=True)
class NormalizedMeeting:
    external_meeting_id: str
    source_url: str
    title: str = ""
    platform: str | None = None
    source_external_id: str | None = None
    meeting_date: str | None = None
    meeting_start_time: str | None = None
    meeting_end_time: str | None = None
    meeting_datetime_text: str | None = None
    meeting_type: str | None = None
    location: str | None = None
    location_address: str | None = None
    description: str | None = None
    agenda_url: str | None = None
    minutes_url: str | None = None
    packet_url: str | None = None
    video_url: str | None = None
    public_notice_url: str | None = None
    livestream_url: str | None = None
    is_cancelled: bool = False
    agenda_items: list[AgendaItem] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @classmethod
    def from_ref(
        cls,
        meeting_ref: MeetingRef,
        *,
        platform: str | None = None,
        source_external_id: str | None = None,
    ) -> "NormalizedMeeting":
        return cls(
            external_meeting_id=meeting_ref.external_meeting_id,
            source_url=meeting_ref.url,
            title=meeting_ref.title,
            platform=platform,
            source_external_id=source_external_id,
            meeting_date=meeting_ref.meeting_date,
            meeting_start_time=meeting_ref.meeting_start_time,
            meeting_end_time=meeting_ref.meeting_end_time,
            meeting_datetime_text=meeting_ref.meeting_datetime_text,
            meeting_type=meeting_ref.meeting_type,
            location=meeting_ref.location,
            location_address=(
                meeting_ref.location_address
                or meeting_ref.metadata.get("location_address")
                or meeting_ref.metadata.get("address")
            ),
            agenda_url=meeting_ref.agenda_url,
            minutes_url=meeting_ref.minutes_url,
            packet_url=meeting_ref.packet_url,
            video_url=meeting_ref.video_url,
            public_notice_url=(
                meeting_ref.public_notice_url
                or meeting_ref.metadata.get("public_notice_url")
            ),
            livestream_url=(
                meeting_ref.livestream_url
                or meeting_ref.metadata.get("livestream_url")
            ),
            is_cancelled=meeting_ref.is_cancelled,
            metadata=dict(meeting_ref.metadata),
        )

    @property
    def url(self) -> str:
        return self.source_url

    def hash_payload(self) -> JsonDict:
        """Return the normalized, volatile-metadata-free revision payload."""

        return {
            "external_meeting_id": self.external_meeting_id,
            "title": self.title,
            "meeting_date": self.meeting_date,
            "meeting_start_time": self.meeting_start_time,
            "meeting_end_time": self.meeting_end_time,
            "meeting_datetime_text": self.meeting_datetime_text,
            "meeting_type": self.meeting_type,
            "location": self.location,
            "location_address": self.location_address,
            "description": self.description,
            "agenda_url": self.agenda_url,
            "minutes_url": self.minutes_url,
            "packet_url": self.packet_url,
            "video_url": self.video_url,
            "public_notice_url": self.public_notice_url,
            "livestream_url": self.livestream_url,
            "is_cancelled": self.is_cancelled,
            "agenda_items": [
                {
                    "external_item_id": item.external_item_id,
                    "item_number": item.item_number,
                    "title": item.title,
                    "description": item.description,
                    "parent_external_item_id": item.parent_external_item_id,
                    "depth": item.depth,
                    "order_index": item.order_index,
                    "timestamp": item.timestamp,
                    "documents": [
                        {
                            "external_document_id": document.external_document_id,
                            "title": document.title,
                            "url": document.url,
                            "document_type": document.document_type,
                            "agenda_item_external_id": document.agenda_item_external_id,
                            "content_type": document.content_type,
                            "file_name": document.file_name,
                        }
                        for document in item.documents
                    ],
                }
                for item in self.agenda_items
            ],
            "documents": [
                {
                    "external_document_id": document.external_document_id,
                    "title": document.title,
                    "url": document.url,
                    "document_type": document.document_type,
                    "agenda_item_external_id": document.agenda_item_external_id,
                    "content_type": document.content_type,
                    "file_name": document.file_name,
                }
                for document in self.documents
            ],
        }

    def content_hash(self) -> str:
        return content_hash(self.hash_payload())


@dataclass(slots=True)
class DownloadedDocument:
    document_ref: DocumentRef
    content: bytes
    final_url: str
    status_code: int
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str | None = None
    metadata: JsonDict = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return content_hash(self.content)

    @property
    def url(self) -> str:
        return self.final_url


def source_from_mapping(value: BoardSource | Mapping[str, Any]) -> BoardSource:
    if isinstance(value, BoardSource):
        return value
    platform = _first_present(value, "platform", "platform_name")
    public_url = _first_present(value, "public_url", "source_url", "url")
    external_source_id = _first_present(value, "external_source_id", "platform_source_id")
    district_id = _first_present(value, "district_id")
    return BoardSource(
        platform=str(platform or "unknown"),
        public_url=str(public_url or ""),
        external_source_id=(str(external_source_id) if external_source_id is not None else None),
        district_id=int(district_id) if district_id not in (None, "") else None,
        organization_name=value.get("organization_name") or value.get("name"),
        status=str(value.get("status") or "working"),
        requires_javascript=_coerce_bool(value.get("requires_javascript")),
        metadata=dict(value.get("metadata") or {}),
    )


def meeting_ref_from_mapping(value: MeetingRef | Mapping[str, Any]) -> MeetingRef:
    if isinstance(value, MeetingRef):
        return value
    meeting_id = _first_present(value, "external_meeting_id", "meeting_id", "id")
    meeting_url = _first_present(value, "url", "source_url", "agenda_url")
    return MeetingRef(
        external_meeting_id=str(meeting_id if meeting_id is not None else ""),
        url=str(meeting_url or ""),
        title=str(value.get("title") or value.get("name") or ""),
        meeting_date=value.get("meeting_date") or value.get("date"),
        meeting_start_time=value.get("meeting_start_time") or value.get("start_time"),
        meeting_end_time=value.get("meeting_end_time") or value.get("end_time"),
        meeting_datetime_text=value.get("meeting_datetime_text") or value.get("datetime_text"),
        meeting_type=value.get("meeting_type"),
        location=value.get("location"),
        location_address=value.get("location_address") or value.get("address"),
        agenda_url=value.get("agenda_url"),
        minutes_url=value.get("minutes_url"),
        packet_url=value.get("packet_url"),
        video_url=value.get("video_url"),
        public_notice_url=value.get("public_notice_url"),
        livestream_url=value.get("livestream_url"),
        is_cancelled=_coerce_bool(value.get("is_cancelled")),
        metadata=dict(value.get("metadata") or {}),
    )


__all__ = [
    "AgendaItem",
    "BoardSource",
    "BoardSourceResult",
    "DetectionResult",
    "DocumentRef",
    "DownloadedDocument",
    "MeetingRef",
    "NormalizedMeeting",
    "content_hash",
    "meeting_ref_from_mapping",
    "source_from_mapping",
    "stable_json",
]
