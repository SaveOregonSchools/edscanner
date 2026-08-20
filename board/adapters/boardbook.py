from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from board.http import BoardHTTPError
from board.models import (
    AgendaItem,
    DocumentRef,
    MeetingRef,
    NormalizedMeeting,
    meeting_ref_from_mapping,
    source_from_mapping,
)

from .base import (
    BoardPlatformAdapter,
    Content,
    MeetingLike,
    SourceLike,
    collapse_ws,
    content_type_from_url,
    dedupe_documents,
    dedupe_meetings,
    document_type_from_text,
    html_soup,
    meeting_is_on_or_after,
    origin_for,
    parse_meeting_datetime_text,
    public_absolute_url,
    query_value,
)
from board.models import DetectionResult


_ORGANIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SOURCE_PATH = re.compile(
    r"/Public/(?:Organization|Agenda|Minutes|PublicNotice)/([A-Za-z0-9][A-Za-z0-9_-]{0,127})(?:/|$)",
    re.IGNORECASE,
)
_MEETING_LABEL = re.compile(
    r"^(?:Cancelled\s+)?(.+?)(?:\s+-\s+(.+))?$",
    re.IGNORECASE,
)
_AGENDA_NUMBER = re.compile(
    r"^((?:\d+|[IVXLCDM]+)(?:\.[A-Za-z0-9]+)*\.?)\s+(.+)$",
    re.IGNORECASE,
)
_PARENT_CLASS = re.compile(r"^agenda-item-children-of-(\d+)$", re.IGNORECASE)


def _organization_id(url: str) -> str | None:
    match = _SOURCE_PATH.search(urlsplit(url).path)
    return match.group(1) if match else None


def _meeting_id(url: str) -> str | None:
    return query_value(url, "meeting", "meetingid", "id")


def _video_link(text: str, url: str) -> bool:
    combined = f"{text} {url}".casefold()
    host = (urlsplit(url).hostname or "").casefold()
    return any(
        marker in combined
        for marker in ("video", "livestream", "live stream", "recording", "watch meeting")
    ) or any(
        marker in host
        for marker in ("youtube.com", "youtu.be", "vimeo.com", "zoom.us", "stream", "granicus")
    )


def resolve_boardbook_document_url(viewer_url: str, organization_id: str | None = None) -> str:
    """Resolve BoardBook's public viewer link to its anonymous stable PDF URL."""

    parsed = urlsplit(viewer_url)
    values = parse_qs(parsed.query)
    document_id = (values.get("file") or values.get("documentid") or [None])[0]
    organization_id = organization_id or _organization_id(viewer_url)
    if organization_id is None:
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts and _ORGANIZATION_ID.fullmatch(path_parts[-1]):
            organization_id = path_parts[-1]
    if not document_id or not organization_id:
        return viewer_url
    origin = urlunsplit((parsed.scheme or "https", parsed.netloc or "meetings.boardbook.org", "", "", ""))
    return f"{origin}/Documents/DownloadPDF/{document_id}?org={organization_id}"


class BoardBookAdapter(BoardPlatformAdapter):
    platform_name = "boardbook"

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        path_match = _SOURCE_PATH.search(parsed.path)
        text = (html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else str(html or ""))
        folded = text.casefold()
        html_match = any(
            marker in folded
            for marker in (
                "boardbook premier",
                "sparq data solutions",
                "row-for-board",
                "agenda-item-information",
            )
        )
        host_match = host == "meetings.boardbook.org"
        organization_id = path_match.group(1) if path_match else None
        if organization_id and not _ORGANIZATION_ID.fullmatch(organization_id):
            organization_id = None
        matched = bool(host_match and organization_id)
        confidence = 0.99 if matched else 0.0
        canonical_url = (
            f"https://meetings.boardbook.org/Public/Organization/{organization_id}"
            if matched
            else None
        )
        organization_name = None
        evidence_metadata: dict[str, Any] = {}
        if html and organization_id:
            # Reuse the same bounded, deterministic evidence parser used by
            # the opt-in directory matcher. These fields let independent
            # search evidence be checked against the district before a source
            # is activated; they do not enumerate provider organizations.
            from board.provider_directories import (
                BoardBookDirectoryEntry,
                parse_boardbook_organization_evidence,
            )

            entry = BoardBookDirectoryEntry(
                external_id=organization_id,
                organization_name="",
                public_url=canonical_url or url,
            )
            evidence = parse_boardbook_organization_evidence(html, entry)
            organization_name = evidence.organization_name or None
            evidence_metadata = {
                "states": sorted(evidence.states),
                "cities": sorted(evidence.cities),
                "meeting_count": evidence.meeting_count,
                "homepage_urls": list(evidence.homepage_urls),
            }
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=confidence,
            reason=(
                "BoardBook public organization URL detected."
                if matched
                else (
                    "BoardBook branding was found without a canonical public organization URL."
                    if html_match
                    else "No canonical BoardBook public organization URL found."
                )
            ),
            canonical_url=canonical_url,
            metadata={
                "external_source_id": organization_id,
                "organization_name": organization_name,
                **evidence_metadata,
            },
        )

    def _location(self, cell: Tag) -> str | None:
        parts = [
            collapse_ws(span.get_text(" ", strip=True))
            for span in cell.find_all("span", id=re.compile(r"^location.*-(?:description|line1|csz)$", re.IGNORECASE))
        ]
        parts = [part for part in parts if part]
        if not parts:
            cloned = BeautifulSoup(str(cell), "lxml")
            for anchor in cloned.find_all("a"):
                anchor.decompose()
            value = collapse_ws(cloned.get_text(" ", strip=True)).strip("[] ")
            return value or None
        return ", ".join(dict.fromkeys(parts))

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        soup = html_soup(content)
        organization_id = _organization_id(url)
        if source:
            organization_id = source_from_mapping(source).external_source_id or organization_id
        meetings: list[MeetingRef] = []
        for row in soup.select("#PublicMeetingsTable tr.row-for-board, tr.row-for-board"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            first_div = cells[0].find("div", recursive=False)
            heading = collapse_ws((first_div or cells[0]).get_text(" ", strip=True))
            meeting_date, start_time = parse_meeting_datetime_text(heading)
            title = ""
            label_match = _MEETING_LABEL.match(heading)
            if label_match:
                title = collapse_ws(label_match.group(2) or label_match.group(1))
            meeting_type = None
            for label in cells[0].select("b.important-page-text, strong"):
                if "meeting type" in collapse_ws(label.get_text(" ", strip=True)).casefold():
                    parent_text = collapse_ws(label.parent.get_text(" ", strip=True))
                    meeting_type = re.sub(r"^Meeting Type:\s*", "", parent_text, flags=re.IGNORECASE) or None
                    break

            agenda_url = minutes_url = packet_url = video_url = None
            public_notice_url = projector_url = None
            extra_links: list[dict[str, str]] = []
            meeting_id = None
            for anchor in row.find_all("a", href=True):
                link = public_absolute_url(url, anchor.get("href"))
                if not link:
                    continue
                text = collapse_ws(anchor.get_text(" ", strip=True))
                folded = text.casefold()
                link_meeting_id = _meeting_id(link)
                meeting_id = meeting_id or link_meeting_id
                if folded == "agenda" or "/public/agenda/" in link.casefold():
                    agenda_url = link
                elif "minute" in folded or "/public/minutes/" in link.casefold():
                    minutes_url = link
                elif "public notice" in folded or "/public/publicnotice/" in link.casefold():
                    public_notice_url = link
                elif "packet" in folded:
                    packet_url = link
                elif "projector" in folded:
                    projector_url = link
                elif _video_link(text, link):
                    video_url = video_url or link
                elif "maps.google" not in link.casefold() and folded != "map it":
                    extra_links.append({"title": text or link, "url": link})
            if not meeting_id:
                meeting_id = str(row.get("data-meetingid") or row.get("data-id") or "") or None
            if not meeting_id:
                continue
            if not agenda_url and organization_id:
                agenda_url = f"{origin_for(url)}/Public/Agenda/{organization_id}?meeting={meeting_id}"
            detail_url = agenda_url or minutes_url or f"{url}#meeting-{meeting_id}"

            cell_text = collapse_ws(cells[0].get_text(" ", strip=True))
            note = cell_text
            for value in (heading, f"Meeting Type: {meeting_type}" if meeting_type else ""):
                if value:
                    note = note.replace(value, "", 1).strip()
            metadata: dict[str, Any] = {}
            if public_notice_url:
                metadata["public_notice_url"] = public_notice_url
            if projector_url:
                metadata["projector_url"] = projector_url
            if extra_links:
                metadata["extra_links"] = extra_links
            if note:
                metadata["listing_note"] = note
            meeting = MeetingRef(
                external_meeting_id=str(meeting_id),
                url=detail_url,
                title=title or meeting_type or heading,
                meeting_date=meeting_date,
                meeting_start_time=start_time,
                meeting_datetime_text=heading,
                meeting_type=meeting_type,
                location=self._location(cells[1]),
                agenda_url=agenda_url,
                minutes_url=minutes_url,
                packet_url=packet_url,
                video_url=video_url,
                is_cancelled="cancelled" in heading.casefold(),
                metadata=metadata,
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
        return dedupe_meetings(meetings)

    def _agenda_title(self, row: Tag) -> tuple[str | None, str]:
        title_node = row.select_one(".form-check") or row.find("td") or row
        title = collapse_ws(title_node.get_text(" ", strip=True))
        number_match = _AGENDA_NUMBER.match(title)
        if not number_match:
            return None, title
        return number_match.group(1), collapse_ws(number_match.group(2))

    def _agenda_description(self, row: Tag) -> str | None:
        description = row.select_one(".Description, .description")
        if not description:
            return None
        text = collapse_ws(description.get_text(" ", strip=True))
        text = re.sub(r"^Description:\s*", "", text, flags=re.IGNORECASE)
        return text or None

    def _agenda_documents(self, row: Tag, url: str, organization_id: str | None) -> list[DocumentRef]:
        documents: list[DocumentRef] = []
        item_id = str(row.get("data-agendaitemid") or "") or None
        for anchor in row.select("a[data-documentid]"):
            viewer_url = public_absolute_url(url, anchor.get("href"))
            document_id = str(anchor.get("data-documentid") or "") or None
            if not viewer_url or not document_id:
                continue
            title_node = anchor.select_one(".fileNameValue")
            title = collapse_ws((title_node or anchor).get_text(" ", strip=True)) or f"Document {document_id}"
            icon = anchor.find("img")
            original_type = collapse_ws(icon.get("alt") if icon else "")
            direct_url = resolve_boardbook_document_url(viewer_url, organization_id)
            documents.append(
                DocumentRef(
                    external_document_id=document_id,
                    title=title,
                    url=direct_url,
                    document_type=document_type_from_text(title, viewer_url),
                    agenda_item_external_id=(
                        str(anchor.get("data-foragendaitem") or "").strip() or item_id
                    ),
                    content_type="application/pdf",
                    file_name=title,
                    metadata={
                        "viewer_url": viewer_url,
                        "original_file_type": original_type or None,
                    },
                )
            )
        return dedupe_documents(documents)

    def parse_meeting_detail(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        meeting_ref: MeetingLike | None = None,
    ) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source) if source else None
        normalized_ref = meeting_ref_from_mapping(meeting_ref) if meeting_ref else MeetingRef(
            external_meeting_id=_meeting_id(url) or "",
            url=url,
            agenda_url=url,
        )
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=(normalized_source.external_source_id if normalized_source else _organization_id(url)),
        )
        meeting.source_url = url
        meeting.agenda_url = normalized_ref.agenda_url or url
        soup = html_soup(content)
        heading = soup.select_one("#MeetingHeader")
        if heading:
            heading_text = collapse_ws(heading.get_text(" ", strip=True))
            meeting.meeting_datetime_text = meeting.meeting_datetime_text or heading_text
            parsed_date, parsed_time = parse_meeting_datetime_text(heading_text)
            meeting.meeting_date = meeting.meeting_date or parsed_date
            meeting.meeting_start_time = meeting.meeting_start_time or parsed_time
            label_match = _MEETING_LABEL.match(heading_text)
            parsed_title = collapse_ws(label_match.group(2) if label_match and label_match.group(2) else "")
            meeting.title = meeting.title or parsed_title or heading_text
            meeting.is_cancelled = meeting.is_cancelled or "cancelled" in heading_text.casefold()

        organization_id = (
            normalized_source.external_source_id if normalized_source else None
        ) or _organization_id(url)
        depths: dict[str, int] = {}
        items: list[AgendaItem] = []
        all_documents: list[DocumentRef] = []
        for order_index, row in enumerate(soup.select("tr.agenda-item-information")):
            item_id = str(row.get("data-agendaitemid") or "").strip()
            if not item_id:
                continue
            parent_id = None
            for class_name in row.get("class", []):
                parent_match = _PARENT_CLASS.match(str(class_name))
                if parent_match:
                    parent_id = parent_match.group(1)
                    break
            if parent_id == "0":
                parent_id = None
            depth = 0 if parent_id is None else depths.get(parent_id, 0) + 1
            depths[item_id] = depth
            item_number, title = self._agenda_title(row)
            timestamp_node = row.find(
                class_=lambda value: bool(value and "timestamp" in " ".join(value if isinstance(value, list) else [value]).casefold())
            )
            documents = self._agenda_documents(row, url, organization_id)
            all_documents.extend(documents)
            items.append(
                AgendaItem(
                    external_item_id=item_id,
                    item_number=item_number,
                    title=title or f"Agenda item {item_id}",
                    description=self._agenda_description(row),
                    parent_external_item_id=parent_id,
                    depth=depth,
                    order_index=order_index,
                    timestamp=(collapse_ws(timestamp_node.get_text(" ", strip=True)) if timestamp_node else None),
                    documents=documents,
                )
            )
        meeting.agenda_items = items

        if normalized_ref.metadata.get("public_notice_url"):
            notice_url = str(normalized_ref.metadata["public_notice_url"])
            all_documents.append(
                DocumentRef(
                    title="Public Notice",
                    url=notice_url,
                    document_type="notice",
                    content_type=content_type_from_url(notice_url),
                )
            )
        if normalized_ref.packet_url:
            all_documents.append(
                DocumentRef(
                    title="Meeting Packet",
                    url=normalized_ref.packet_url,
                    document_type="packet",
                    content_type=content_type_from_url(normalized_ref.packet_url),
                )
            )
        meeting.documents = dedupe_documents(all_documents)

        for anchor in soup.find_all("a", href=True):
            absolute = public_absolute_url(url, anchor.get("href"))
            if absolute and _video_link(collapse_ws(anchor.get_text(" ", strip=True)), absolute):
                meeting.video_url = meeting.video_url or absolute
                break
        meeting.metadata.update(
            {
                "agenda_item_count": len(meeting.agenda_items),
                "attachment_count": len([doc for doc in meeting.documents if doc.agenda_item_external_id]),
            }
        )
        return meeting

    def parse_minutes_document(
        self,
        content: Content,
        url: str,
        organization_id: str | None = None,
    ) -> DocumentRef | None:
        soup = html_soup(content)
        document_id_node = soup.select_one("#NewDocumentViewerDocumentID")
        document_id = str(document_id_node.get("value") or "").strip() if document_id_node else ""
        organization_id = organization_id or _organization_id(url)
        if not organization_id:
            match = re.search(r"/CustomMinutesForMeeting/(\d+)", urlsplit(url).path, re.IGNORECASE)
            organization_id = match.group(1) if match else None
        if not document_id or not organization_id:
            return None
        title_node = soup.select_one("main h3, #MainPage h3, #NewDocumentViewerDisplayName")
        if title_node and title_node.name == "input":
            title = collapse_ws(title_node.get("value"))
        else:
            title = collapse_ws(title_node.get_text(" ", strip=True)) if title_node else "Approved Minutes"
        direct_url = f"{origin_for(url)}/Documents/DownloadPDF/{document_id}?org={organization_id}"
        return DocumentRef(
            external_document_id=document_id,
            title=title or "Approved Minutes",
            url=direct_url,
            document_type="minutes",
            content_type="application/pdf",
            file_name=title or "Approved Minutes",
            metadata={"minutes_page_url": url},
        )

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        meeting = super().fetch_meeting(normalized_source, normalized_ref)
        if normalized_ref.minutes_url:
            meeting.minutes_url = normalized_ref.minutes_url
            try:
                response = self.fetch_url(normalized_ref.minutes_url)
                minutes_document = self.parse_minutes_document(
                    response.content,
                    response.final_url,
                    normalized_source.external_source_id,
                )
                if minutes_document:
                    meeting.documents = dedupe_documents([*meeting.documents, minutes_document])
                    meeting.metadata["minutes_document_id"] = minutes_document.external_document_id
                    meeting.metadata["minutes_final_url"] = response.final_url
            except (requests.RequestException, BoardHTTPError) as exc:
                meeting.metadata["minutes_fetch_error"] = str(exc)
        return meeting


__all__ = ["BoardBookAdapter", "resolve_boardbook_document_url"]
