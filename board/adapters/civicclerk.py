from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlencode, urljoin, urlsplit

from board.models import AgendaItem, BoardSource, DetectionResult, DocumentRef, MeetingRef, NormalizedMeeting
from board.models import meeting_ref_from_mapping, source_from_mapping

from .base import (
    BoardPlatformAdapter,
    Content,
    MeetingLike,
    SourceLike,
    collapse_ws,
    content_text,
    dedupe_documents,
    dedupe_meetings,
    document_type_from_text,
    html_soup,
    mapping_value,
    meeting_is_on_or_after,
    parse_bool_value,
    parse_date_value,
    parse_time_value,
    since_date,
)


_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$|^[a-z0-9]$", re.IGNORECASE)


def _tenant(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").casefold()
    for suffix in (".portal.civicclerk.com", ".api.civicclerk.com"):
        if not host.endswith(suffix):
            continue
        tenant = host[: -len(suffix)]
        return tenant if _TENANT_ID.fullmatch(tenant) else None
    return None


def _payload(content: Content) -> Any | None:
    text = content_text(content).lstrip("\ufeff \t\r\n")
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        value = mapping_value(payload, "value", "events", "items")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if mapping_value(payload, "id") is not None:
            return [payload]
    return []


def _plain_html(value: Any) -> str:
    text = str(value or "")
    if "<" in text:
        return collapse_ws(html_soup(text).get_text(" ", strip=True))
    return collapse_ws(text)


class CivicClerkAdapter(BoardPlatformAdapter):
    platform_name = "civicclerk"

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        host_match = host.endswith(".portal.civicclerk.com") or host.endswith(".api.civicclerk.com")
        folded = content_text(html).casefold()
        html_match = any(
            marker in folded
            for marker in (
                "public portal • civicclerk",
                "civicclerk events and agendas",
                "portal.civicclerk.com",
                "api.civicclerk.com/v1",
            )
        )
        tenant = _tenant(url)
        matched = bool(host_match and tenant)
        if tenant:
            canonical = f"https://{tenant}.portal.civicclerk.com/"
            api_base = f"https://{tenant}.api.civicclerk.com/v1"
        else:
            canonical = url if matched else None
            api_base = None
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=0.99 if matched else 0.0,
            reason=(
                "Public CivicClerk tenant portal detected."
                if matched
                else (
                    "CivicClerk branding was found without a canonical tenant host."
                    if html_match
                    else "No canonical CivicClerk tenant host found."
                )
            ),
            canonical_url=canonical,
            requires_javascript=False,
            metadata={"external_source_id": tenant, "tenant": tenant, "api_base": api_base},
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, object] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        tenant = source.external_source_id or _tenant(url)
        if tenant:
            source.public_url = f"https://{tenant}.portal.civicclerk.com/"
            source.metadata.update({"tenant": tenant, "api_base": f"https://{tenant}.api.civicclerk.com/v1"})
        return source

    def meeting_listing_url(
        self,
        source: BoardSource,
        since: date | datetime | str | None = None,
    ) -> str:
        start = since_date(since) or parse_date_value(source.metadata.get("date_from"))
        if not start:
            start = (date.today() - timedelta(days=730)).isoformat()
        api_base = str(source.metadata.get("api_base") or "").rstrip("/")
        if not api_base:
            tenant = source.external_source_id or _tenant(source.public_url)
            api_base = f"https://{tenant}.api.civicclerk.com/v1"
        page_size = max(1, min(int(source.metadata.get("page_size") or 100), 250))
        query = {
            "$filter": f"startDateTime ge {start}T00:00:00Z and isDeleted eq false",
            "$orderby": "startDateTime desc",
            "$top": str(page_size),
        }
        return f"{api_base}/Events?{urlencode(query)}"

    def _published_documents(self, record: Mapping[str, Any], api_base: str) -> list[DocumentRef]:
        documents: list[DocumentRef] = []
        values = mapping_value(record, "publishedFiles", default=[])
        if not isinstance(values, list):
            return documents
        for value in values:
            if not isinstance(value, Mapping):
                continue
            file_id = mapping_value(value, "fileId", "id")
            if file_id in (None, 0, "0"):
                continue
            file_type = collapse_ws(mapping_value(value, "type", default=""))
            title = collapse_ws(mapping_value(value, "name", default=file_type)) or f"Meeting file {file_id}"
            stable_url = f"{api_base}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
            documents.append(
                DocumentRef(
                    external_document_id=str(file_id),
                    title=title,
                    url=stable_url,
                    document_type=document_type_from_text(file_type or title, stable_url),
                    content_type="application/pdf",
                    file_name=title,
                    metadata={
                        "file_type": mapping_value(value, "fileType"),
                        "published_on": mapping_value(value, "publishOn"),
                    },
                )
            )
        return dedupe_documents(documents)

    def _meeting_from_event(self, event: Mapping[str, Any], source: BoardSource | None, url: str) -> MeetingRef | None:
        event_id = mapping_value(event, "id", "eventId")
        if event_id is None or parse_bool_value(mapping_value(event, "isDeleted")):
            return None
        publication = collapse_ws(mapping_value(event, "isPublished", default=""))
        if publication and publication.casefold() not in {"published", "true", "1"}:
            return None
        tenant = source.external_source_id if source else _tenant(url)
        portal_base = source.public_url.rstrip("/") if source else f"https://{tenant}.portal.civicclerk.com"
        title = collapse_ws(mapping_value(event, "eventName", "agendaName", "name", default=""))
        start = mapping_value(event, "startDateTime", "eventDate")
        end = mapping_value(event, "meetingEndTime", "endDateTime")
        end_date = parse_date_value(end)
        end_time = None if end_date in {None, "1900-01-01", "0001-01-01"} else parse_time_value(end)
        location_value = mapping_value(event, "eventLocation", "location")
        location = None
        if isinstance(location_value, Mapping):
            location = ", ".join(
                collapse_ws(mapping_value(location_value, key))
                for key in ("address1", "address2", "city", "state", "zipCode")
                if collapse_ws(mapping_value(location_value, key))
            ) or None
        elif location_value:
            location = collapse_ws(location_value) or None
        video_url = mapping_value(event, "externalMediaUrl", "mediaStreamPath", "videoUrl")
        if not isinstance(video_url, str) or not video_url.startswith(("http://", "https://")):
            video_url = None
        has_agenda = parse_bool_value(mapping_value(event, "hasAgenda"))
        published_files = mapping_value(event, "publishedFiles", default=[])
        published_files = published_files if isinstance(published_files, list) else []
        agenda_id = mapping_value(event, "agendaId")
        detail_url = f"{portal_base}/event/{event_id}"
        return MeetingRef(
            external_meeting_id=str(event_id),
            url=detail_url,
            title=title or f"Event {event_id}",
            meeting_date=parse_date_value(start),
            meeting_start_time=parse_time_value(start),
            meeting_end_time=end_time,
            meeting_datetime_text=collapse_ws(start) or None,
            meeting_type=collapse_ws(mapping_value(event, "meetingTypeName", "categoryName", default="")) or None,
            location=location,
            agenda_url=(f"{detail_url}/files" if has_agenda else None),
            minutes_url=(f"{detail_url}/files" if any(
                isinstance(item, Mapping) and "minute" in collapse_ws(mapping_value(item, "type", default="")).casefold()
                for item in published_files
            ) else None),
            video_url=video_url,
            metadata={
                "agenda_id": agenda_id,
                "has_agenda": has_agenda,
                "has_media": parse_bool_value(mapping_value(event, "hasMedia")),
                "publication_status": publication,
                "event_description": _plain_html(mapping_value(event, "eventDescription", default="")),
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
        payload = _payload(content)
        meetings: list[MeetingRef] = []
        if payload is not None:
            for event in _records(payload):
                meeting = self._meeting_from_event(event, normalized_source, url)
                if meeting and meeting_is_on_or_after(meeting, since):
                    meetings.append(meeting)
            return dedupe_meetings(meetings)
        soup = html_soup(content)
        for anchor in soup.select("a[href*='/event/']"):
            path = urlsplit(anchor.get("href") or "").path.rstrip("/").split("/")
            try:
                event_id = path[path.index("event") + 1]
            except (ValueError, IndexError):
                continue
            context = anchor.find_parent(["article", "li", "tr", "div"]) or anchor
            text = collapse_ws(context.get_text(" ", strip=True))
            portal = normalized_source.public_url.rstrip("/") if normalized_source else f"https://{_tenant(url)}.portal.civicclerk.com"
            detail_url = f"{portal}/event/{event_id}"
            meeting = MeetingRef(
                external_meeting_id=event_id,
                url=detail_url,
                title=collapse_ws(anchor.get_text(" ", strip=True)) or text,
                meeting_date=parse_date_value(text),
                meeting_start_time=parse_time_value(text),
                meeting_datetime_text=text,
                agenda_url=f"{detail_url}/files",
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
        return dedupe_meetings(meetings)

    def list_meetings(
        self,
        source: SourceLike,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        normalized_source = source_from_mapping(source)
        next_url: str | None = self.meeting_listing_url(normalized_source, since)
        max_pages = max(1, min(int(normalized_source.metadata.get("max_listing_pages") or 20), 100))
        meetings: list[MeetingRef] = []
        visited: set[str] = set()
        pages = 0
        while next_url and next_url not in visited and pages < max_pages:
            visited.add(next_url)
            response, payload = self.client.get_json(next_url)
            meetings.extend(self.parse_meeting_list(response.content, response.final_url, normalized_source, since))
            next_value = mapping_value(payload, "@odata.nextLink", "odata.nextLink") if isinstance(payload, Mapping) else None
            next_url = urljoin(response.final_url, str(next_value)) if next_value else None
            pages += 1
        return dedupe_meetings(meetings)

    def _agenda_items(
        self,
        values: Any,
        api_base: str,
        *,
        parent_id: str | None = None,
        depth: int = 0,
        order: list[int] | None = None,
    ) -> tuple[list[AgendaItem], list[DocumentRef]]:
        order = order if order is not None else [0]
        items: list[AgendaItem] = []
        documents: list[DocumentRef] = []
        if not isinstance(values, list):
            return items, documents
        for value in values:
            if not isinstance(value, Mapping) or parse_bool_value(mapping_value(value, "isDeleted")):
                continue
            item_id_value = mapping_value(value, "id", "agendaObjectItemId")
            if item_id_value is None:
                continue
            item_id = str(item_id_value)
            actual_parent = mapping_value(value, "parentId")
            if actual_parent in (None, -1, 0, "-1", "0"):
                actual_parent_id = parent_id
            else:
                actual_parent_id = str(actual_parent)
            description_parts = [
                _plain_html(mapping_value(value, "agendaObjectItemDescription")),
                _plain_html(mapping_value(value, "agendaObjectItemHtmlContent")),
            ]
            description = collapse_ws(" ".join(part for part in description_parts if part)) or None
            item_documents: list[DocumentRef] = []
            attachments = mapping_value(value, "attachmentsList", default=[])
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, Mapping):
                        continue
                    if parse_bool_value(mapping_value(attachment, "isDeleted")):
                        continue
                    published = mapping_value(attachment, "isPublished", default=True)
                    if not parse_bool_value(published, default=True):
                        continue
                    attachment_id = mapping_value(attachment, "id")
                    if attachment_id is None:
                        continue
                    is_link = parse_bool_value(mapping_value(attachment, "isLink"))
                    external_link = mapping_value(attachment, "linkToItem")
                    if is_link and isinstance(external_link, str) and external_link.startswith(("http://", "https://")):
                        stable_url = external_link
                    else:
                        stable_url = f"{api_base}/Meetings/GetAttachmentFile(fileId={attachment_id})"
                    file_name = collapse_ws(mapping_value(attachment, "mediaFileName", "fileName", default=""))
                    title = collapse_ws(mapping_value(attachment, "fileName", "description", default=file_name))
                    document = DocumentRef(
                        external_document_id=str(attachment_id),
                        title=title or f"Attachment {attachment_id}",
                        url=stable_url,
                        document_type=document_type_from_text(title, stable_url),
                        agenda_item_external_id=item_id,
                        content_type=collapse_ws(mapping_value(attachment, "contentType")) or None,
                        file_name=file_name or title or None,
                        metadata={
                            "file_size": mapping_value(attachment, "fileSize"),
                            "sort_order": mapping_value(attachment, "sortOrder"),
                        },
                    )
                    item_documents.append(document)
                    documents.append(document)
            item = AgendaItem(
                external_item_id=item_id,
                item_number=collapse_ws(
                    mapping_value(value, "agendaObjectItemOutlineNumber", "agendaObjectItemNumber", default="")
                ) or None,
                title=collapse_ws(mapping_value(value, "agendaObjectItemName", "name", default="")) or f"Agenda item {item_id}",
                description=description,
                parent_external_item_id=actual_parent_id,
                depth=depth,
                order_index=order[0],
                timestamp=collapse_ws(mapping_value(value, "timestamp", "timeStamp", default="")) or None,
                documents=dedupe_documents(item_documents),
                metadata={"is_section": parse_bool_value(mapping_value(value, "isSection"))},
            )
            order[0] += 1
            items.append(item)
            child_items, child_documents = self._agenda_items(
                mapping_value(value, "childItems", default=[]),
                api_base,
                parent_id=item_id,
                depth=depth + 1,
                order=order,
            )
            items.extend(child_items)
            documents.extend(child_documents)
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
            source_external_id=normalized_source.external_source_id if normalized_source else _tenant(url),
        )
        meeting.source_url = url
        payload = _payload(content)
        if not isinstance(payload, Mapping):
            return meeting
        tenant = normalized_source.external_source_id if normalized_source else _tenant(url)
        api_base = str(normalized_source.metadata.get("api_base") if normalized_source else "") or f"https://{tenant}.api.civicclerk.com/v1"

        if mapping_value(payload, "eventName") is not None:
            event_ref = self._meeting_from_event(payload, normalized_source, url)
            if event_ref:
                event_meeting = NormalizedMeeting.from_ref(
                    event_ref,
                    platform=self.platform_name,
                    source_external_id=meeting.source_external_id,
                )
                event_meeting.description = _plain_html(mapping_value(payload, "eventDescription")) or None
                event_meeting.documents = self._published_documents(payload, api_base)
                return event_meeting

        published_files = self._published_documents(payload, api_base)
        agenda_published = parse_bool_value(mapping_value(payload, "agendaIsPublish"))
        packet_published = parse_bool_value(mapping_value(payload, "agendaPacketIsPublish"))
        if agenda_published or packet_published or published_files:
            items, attachment_documents = self._agenda_items(mapping_value(payload, "items", default=[]), api_base)
            meeting.agenda_items = items
            meeting.documents = dedupe_documents([*published_files, *attachment_documents])
        meeting.metadata.update(
            {
                "agenda_is_published": agenda_published,
                "agenda_packet_is_published": packet_published,
            }
        )
        return meeting

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        tenant = normalized_source.external_source_id or _tenant(normalized_source.public_url)
        api_base = str(normalized_source.metadata.get("api_base") or f"https://{tenant}.api.civicclerk.com/v1").rstrip("/")
        event_response = self.fetch_url(f"{api_base}/Events/{normalized_ref.external_meeting_id}")
        event_payload = event_response.json()
        meeting = self.parse_meeting_detail(
            event_response.content,
            event_response.final_url,
            normalized_source,
            normalized_ref,
        )
        agenda_id = mapping_value(event_payload, "agendaId") if isinstance(event_payload, Mapping) else None
        has_agenda = parse_bool_value(mapping_value(event_payload, "hasAgenda")) if isinstance(event_payload, Mapping) else False
        if agenda_id not in (None, 0, "0") and has_agenda:
            agenda_response = self.fetch_url(f"{api_base}/Meetings/{agenda_id}")
            agenda_meeting = self.parse_meeting_detail(
                agenda_response.content,
                agenda_response.final_url,
                normalized_source,
                normalized_ref,
            )
            meeting.agenda_items = agenda_meeting.agenda_items
            meeting.documents = dedupe_documents([*meeting.documents, *agenda_meeting.documents])
            meeting.metadata.update(agenda_meeting.metadata)
            meeting.metadata["agenda_api_url"] = agenda_response.final_url
        meeting.metadata["event_api_url"] = event_response.final_url
        return meeting


__all__ = ["CivicClerkAdapter"]
