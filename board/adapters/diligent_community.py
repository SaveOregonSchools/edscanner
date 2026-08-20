from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from board.models import AgendaItem, BoardSource, DetectionResult, DocumentRef, MeetingRef, NormalizedMeeting

from .base import (
    BoardPlatformAdapter,
    Content,
    MeetingLike,
    SourceLike,
    collapse_ws,
    content_text,
    content_type_from_url,
    dedupe_documents,
    dedupe_meetings,
    document_type_from_text,
    html_soup,
    mapping_value,
    meeting_is_on_or_after,
    origin_for,
    parse_bool_value,
    parse_date_value,
    parse_meeting_datetime_text,
    parse_time_value,
    public_absolute_url,
    since_date,
)
from board.models import meeting_ref_from_mapping, source_from_mapping


_HOST_SUFFIXES = (
    ".community.diligentoneplatform.com",
    ".diligent.community",
    ".community.highbond.com",
    ".civicweb.net",
)
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$|^[a-z0-9]$", re.IGNORECASE)


def _tenant(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").casefold()
    for suffix in _HOST_SUFFIXES:
        if not host.endswith(suffix):
            continue
        tenant = host[: -len(suffix)]
        if _TENANT_ID.fullmatch(tenant):
            return tenant
    return None


def _json_payload(content: Content) -> Any | None:
    text = content_text(content).lstrip("\ufeff \t\r\n")
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _record_list(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for name in ("Meetings", "meetings", "value", "results", "Items", "items"):
        value = mapping_value(payload, name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    d_value = mapping_value(payload, "d")
    if d_value is not None and d_value is not payload:
        return _record_list(d_value)
    if mapping_value(payload, "Id", "MeetingId") is not None:
        return [payload]
    for value in payload.values():
        found = _record_list(value)
        if found:
            return found
    return []


def _first_mapping(payload: Any) -> Mapping[str, Any]:
    records = _record_list(payload)
    if records:
        return records[0]
    return payload if isinstance(payload, Mapping) else {}


class DiligentCommunityAdapter(BoardPlatformAdapter):
    platform_name = "diligent_community"

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        tenant = _tenant(url)
        host_match = tenant is not None
        path_match = parsed.path.casefold().startswith("/portal") or "/services/meetingsservice.svc/" in parsed.path.casefold()
        folded = content_text(html).casefold()
        html_match = any(
            marker in folded
            for marker in (
                "meetingsservice.svc",
                "meetinginformation.aspx",
                "diligent community",
                "diligentoneplatform",
            )
        )
        matched = host_match
        confidence = 0.98 if host_match and path_match else 0.95 if host_match else 0.0
        canonical_url = f"https://{host}/Portal/" if matched else None
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=confidence,
            reason=(
                "Diligent Community tenant portal detected."
                if matched
                else (
                    "Diligent branding was found without a canonical tenant host."
                    if html_match
                    else "No canonical Diligent Community tenant host found."
                )
            ),
            canonical_url=canonical_url,
            requires_javascript=False,
            metadata={"external_source_id": tenant, "tenant": tenant},
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, Any] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        source.public_url = f"{origin_for(source.public_url)}/Portal/"
        source.metadata["api_base"] = f"{origin_for(source.public_url)}/Services/MeetingsService.svc"
        return source

    def meeting_listing_url(
        self,
        source: BoardSource,
        since: date | datetime | str | None = None,
    ) -> str:
        today = date.today()
        from_date = since_date(since) or parse_date_value(source.metadata.get("date_from"))
        if not from_date:
            from_date = (today - timedelta(days=730)).isoformat()
        to_date = parse_date_value(source.metadata.get("date_to")) or (today + timedelta(days=365)).isoformat()
        api_base = str(source.metadata.get("api_base") or f"{origin_for(source.public_url)}/Services/MeetingsService.svc")
        return f"{api_base.rstrip('/')}/meetings?{urlencode({'from': from_date, 'to': to_date, 'loadall': 'true'})}"

    def _meeting_from_record(self, record: Mapping[str, Any], url: str, source: BoardSource | None) -> MeetingRef | None:
        meeting_id = mapping_value(record, "Id", "MeetingId", "EventId")
        if meeting_id is None:
            return None
        name = collapse_ws(mapping_value(record, "CleanName", "Name", "MeetingName", "Title", default=""))
        datetime_value = mapping_value(record, "MeetingDateTime", "StartDateTime", "MeetingDate", "Date")
        meeting_date = parse_date_value(datetime_value)
        start_time = parse_time_value(datetime_value) or parse_time_value(mapping_value(record, "MeetingTime", "StartTime"))
        datetime_text = collapse_ws(datetime_value or mapping_value(record, "MeetingDate", default="")) or None
        meeting_type_value = mapping_value(record, "MeetingTypeName", "MeetingType", "Type")
        if isinstance(meeting_type_value, Mapping):
            meeting_type_value = mapping_value(meeting_type_value, "Name", "Description", "Value")
        meeting_type = collapse_ws(meeting_type_value) or None
        location_value = mapping_value(record, "MeetingLocation", "Location", "Address")
        if isinstance(location_value, Mapping):
            location_value = ", ".join(
                collapse_ws(value) for value in location_value.values() if collapse_ws(value)
            )
        location = collapse_ws(location_value) or None
        origin = origin_for(source.public_url if source else url)
        org = str(source.metadata.get("org") or "") if source else ""
        query = {"id": str(meeting_id)}
        if org:
            query["Org"] = org
        detail_url = f"{origin}/Portal/MeetingInformation.aspx?{urlencode(query)}"
        video = mapping_value(record, "VideoUrl", "StreamingUrl", "ExternalCalendar")
        if not isinstance(video, str) or not video.startswith(("http://", "https://")):
            video = None
        return MeetingRef(
            external_meeting_id=str(meeting_id),
            url=detail_url,
            title=name or meeting_type or f"Meeting {meeting_id}",
            meeting_date=meeting_date,
            meeting_start_time=start_time,
            meeting_datetime_text=datetime_text,
            meeting_type=meeting_type,
            location=location,
            agenda_url=detail_url,
            video_url=video,
            is_cancelled=parse_bool_value(mapping_value(record, "Cancelled", "IsCancelled")),
            metadata={
                "meeting_type_id": mapping_value(record, "MeetingTypeId"),
                "published": mapping_value(record, "Published", "IsPublished"),
                "external_calendar": mapping_value(record, "ExternalCalendar"),
            },
        )

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        normalized_source = source_from_mapping(source) if source else None
        payload = _json_payload(content)
        meetings: list[MeetingRef] = []
        if payload is not None:
            for record in _record_list(payload):
                meeting = self._meeting_from_record(record, url, normalized_source)
                if meeting and meeting_is_on_or_after(meeting, since):
                    meetings.append(meeting)
            return dedupe_meetings(meetings)

        soup = html_soup(content)
        for anchor in soup.select("a[href*='MeetingInformation.aspx'], a[data-meeting-id]"):
            detail_url = public_absolute_url(url, anchor.get("href"))
            meeting_id = anchor.get("data-meeting-id")
            if detail_url:
                from .base import query_value

                meeting_id = meeting_id or query_value(detail_url, "id")
            if not detail_url or not meeting_id:
                continue
            row = anchor.find_parent("tr") or anchor.parent
            text = collapse_ws(row.get_text(" ", strip=True))
            meeting_date, meeting_time = parse_meeting_datetime_text(text)
            meeting = MeetingRef(
                external_meeting_id=str(meeting_id),
                url=detail_url,
                title=collapse_ws(anchor.get_text(" ", strip=True)) or text,
                meeting_date=meeting_date,
                meeting_start_time=meeting_time,
                meeting_datetime_text=text,
                agenda_url=detail_url,
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
        return dedupe_meetings(meetings)

    def _documents_from_payload(self, payload: Any, base_url: str) -> tuple[list[DocumentRef], list[str]]:
        documents: list[DocumentRef] = []
        agenda_html: list[str] = []
        records: list[Mapping[str, Any]] = []
        if isinstance(payload, Mapping):
            candidates = mapping_value(payload, "Documents", "documents", "MeetingDocuments")
            if isinstance(candidates, list):
                records.extend(item for item in candidates if isinstance(item, Mapping))
        if not records:
            records = [
                record
                for record in _record_list(payload)
                if any(
                    mapping_value(record, name) is not None
                    for name in ("DocumentId", "DocumentType", "Format", "Html", "AgendaHtml")
                )
            ]
        for record in records:
            html_value = mapping_value(record, "Html", "AgendaHtml", "Content")
            if isinstance(html_value, str) and "<" in html_value:
                agenda_html.append(html_value)
            document_id = mapping_value(record, "Id", "DocumentId")
            if document_id is None:
                continue
            title = collapse_ws(mapping_value(record, "Name", "Title", default=f"Document {document_id}"))
            format_value = collapse_ws(mapping_value(record, "Format", "Extension", default=""))
            document_url = f"{origin_for(base_url)}/document/{document_id}"
            documents.append(
                DocumentRef(
                    external_document_id=str(document_id),
                    title=title,
                    url=document_url,
                    document_type=document_type_from_text(
                        collapse_ws(mapping_value(record, "DocumentType", default=title)),
                        document_url,
                    ),
                    content_type=content_type_from_url(f"file.{format_value.lstrip('.')}") if format_value else None,
                    file_name=(f"{title}.{format_value.lstrip('.')}" if format_value else title),
                    metadata={"last_modified": mapping_value(record, "LastModified")},
                )
            )
        return dedupe_documents(documents), agenda_html

    def _agenda_items_from_html(self, html: str, base_url: str) -> tuple[list[AgendaItem], list[DocumentRef]]:
        soup = html_soup(html)
        candidates = soup.select(
            "[data-agenda-item-id], [data-item-id], li.agenda-item, tr.agenda-item, .agendaItem, .agenda-item"
        )
        if not candidates:
            candidates = [node for node in soup.find_all(["li", "tr"]) if node.find("a", href=True)]
        items: list[AgendaItem] = []
        documents: list[DocumentRef] = []
        for index, node in enumerate(candidates):
            item_id = str(
                node.get("data-agenda-item-id")
                or node.get("data-item-id")
                or node.get("id")
                or f"html-{index + 1}"
            )
            title_node = node.find(["h1", "h2", "h3", "h4", "strong"]) or node
            title = collapse_ws(title_node.get_text(" ", strip=True))
            if not title:
                continue
            parent = node.find_parent("li")
            parent_id = None
            depth = 0
            if parent and parent is not node:
                parent_id = str(parent.get("data-agenda-item-id") or parent.get("data-item-id") or parent.get("id") or "") or None
                depth = len(node.find_parents("li")) - 1
            item_documents: list[DocumentRef] = []
            for anchor in node.find_all("a", href=True):
                link = public_absolute_url(base_url, anchor.get("href"))
                if not link or "/document/" not in link.casefold():
                    continue
                document_id = urlsplit(link).path.rstrip("/").split("/")[-1]
                document = DocumentRef(
                    external_document_id=document_id,
                    title=collapse_ws(anchor.get_text(" ", strip=True)) or f"Document {document_id}",
                    url=link,
                    document_type=document_type_from_text(anchor.get_text(" ", strip=True), link),
                    agenda_item_external_id=item_id,
                    content_type=content_type_from_url(link),
                )
                item_documents.append(document)
                documents.append(document)
            items.append(
                AgendaItem(
                    external_item_id=item_id,
                    title=title,
                    parent_external_item_id=parent_id,
                    depth=max(0, depth),
                    order_index=index,
                    documents=dedupe_documents(item_documents),
                )
            )
        return items, dedupe_documents(documents)

    def parse_meeting_detail(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        meeting_ref: MeetingLike | None = None,
    ) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source) if source else None
        normalized_ref = meeting_ref_from_mapping(meeting_ref) if meeting_ref else MeetingRef("", url)
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=normalized_source.external_source_id if normalized_source else None,
        )
        meeting.source_url = url
        payload = _json_payload(content)
        if payload is not None:
            record = _first_mapping(payload)
            title = collapse_ws(mapping_value(record, "CleanName", "Name", "Title", default=""))
            meeting.title = title or meeting.title
            date_value = mapping_value(record, "MeetingDateTime", "MeetingDate", "StartDateTime")
            meeting.meeting_date = parse_date_value(date_value) or meeting.meeting_date
            meeting.meeting_start_time = parse_time_value(date_value) or meeting.meeting_start_time
            location = mapping_value(record, "MeetingLocation", "Location")
            if isinstance(location, str):
                meeting.location = collapse_ws(location) or meeting.location
            meeting.description = collapse_ws(mapping_value(record, "Description", "Notes", default="")) or meeting.description
            documents, html_values = self._documents_from_payload(payload, url)
            agenda_items: list[AgendaItem] = []
            embedded_documents: list[DocumentRef] = []
            for html_value in html_values:
                parsed_items, parsed_documents = self._agenda_items_from_html(html_value, url)
                agenda_items.extend(parsed_items)
                embedded_documents.extend(parsed_documents)
            meeting.agenda_items = agenda_items
            meeting.documents = dedupe_documents([*documents, *embedded_documents])
            return meeting

        soup = html_soup(content)
        heading = soup.find(["h1", "h2"])
        if heading:
            heading_text = collapse_ws(heading.get_text(" ", strip=True))
            meeting.title = meeting.title or heading_text
            meeting.meeting_date = meeting.meeting_date or parse_date_value(heading_text)
            meeting.meeting_start_time = meeting.meeting_start_time or parse_time_value(heading_text)
        items, documents = self._agenda_items_from_html(content_text(content), url)
        meeting.agenda_items = items
        meeting.documents = documents
        return meeting

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        meeting_id = normalized_ref.external_meeting_id
        api_base = str(
            normalized_source.metadata.get("api_base")
            or f"{origin_for(normalized_source.public_url)}/Services/MeetingsService.svc"
        ).rstrip("/")
        data_response = self.fetch_url(f"{api_base}/meetings/{meeting_id}/meetingData")
        meeting = self.parse_meeting_detail(
            data_response.content,
            data_response.final_url,
            normalized_source,
            normalized_ref,
        )
        documents_response = self.fetch_url(f"{api_base}/meetings/{meeting_id}/meetingDocuments")
        documents_payload = _json_payload(documents_response.content)
        documents, html_values = self._documents_from_payload(documents_payload, documents_response.final_url)
        agenda_items = list(meeting.agenda_items)
        embedded_documents: list[DocumentRef] = []
        for html_value in html_values:
            parsed_items, parsed_documents = self._agenda_items_from_html(html_value, documents_response.final_url)
            agenda_items.extend(parsed_items)
            embedded_documents.extend(parsed_documents)
        if agenda_items:
            meeting.agenda_items = agenda_items
        meeting.documents = dedupe_documents([*meeting.documents, *documents, *embedded_documents])
        meeting.metadata.update(
            {
                "meeting_data_url": data_response.final_url,
                "meeting_documents_url": documents_response.final_url,
            }
        )
        return meeting


__all__ = ["DiligentCommunityAdapter"]
