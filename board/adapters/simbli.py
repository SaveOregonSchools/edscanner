from __future__ import annotations

import re
from datetime import date, datetime
from typing import Mapping
from urllib.parse import urlencode, urlsplit

from board.models import AgendaItem, BoardSource, DetectionResult, DocumentRef, MeetingRef, NormalizedMeeting
from board.models import meeting_ref_from_mapping, source_from_mapping

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
    looks_like_blocked_page,
    meeting_is_on_or_after,
    origin_for,
    parse_date_value,
    parse_time_value,
    public_absolute_url,
    query_value,
)


_VIEW_MEETING = re.compile(
    r"ViewMeeting\(\s*['\"](?P<site>[^'\"]+)['\"]\s*,\s*['\"](?P<meeting>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _site_id(url: str) -> str | None:
    value = query_value(url, "S", "site", "siteid")
    return value if value and _SITE_ID.fullmatch(value) else None


class SimbliAdapter(BoardPlatformAdapter):
    platform_name = "simbli"
    requires_javascript = True

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        host_match = host == "simbli.eboardsolutions.com"
        path_match = any(
            marker in parsed.path.casefold()
            for marker in ("/sb_meetings/", "/index.aspx", "/viewmeeting.aspx")
        )
        folded = (html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else str(html or "")).casefold()
        html_match = any(
            marker in folded
            for marker in (
                "contentplaceholder1_meetinggrid",
                "viewmeeting(",
                "agenda-wrapper",
                "agendaitellist",
                "eboardsolutions",
                "simbli",
            )
        )
        site_id = _site_id(url)
        matched = bool(host_match and path_match and site_id)
        canonical = (
            f"https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?{urlencode({'S': site_id})}"
            if matched
            else None
        )
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=0.98 if matched else 0.0,
            reason=(
                "Public Simbli meeting portal and site ID detected."
                if matched
                else (
                    "Simbli branding was found without a canonical meeting URL and site ID."
                    if html_match
                    else "No canonical Simbli meeting URL and site ID found."
                )
            ),
            canonical_url=canonical,
            requires_javascript=True,
            metadata={"external_source_id": site_id, "site_id": site_id},
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, object] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        site_id = source.external_source_id or _site_id(url)
        if site_id:
            source.public_url = f"https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?{urlencode({'S': site_id})}"
            source.metadata["site_id"] = site_id
        source.requires_javascript = True
        return source

    def _detail_url(self, base_url: str, site_id: str, meeting_id: str) -> str:
        return f"{origin_for(base_url)}/SB_Meetings/ViewMeeting.aspx?{urlencode({'MID': meeting_id, 'S': site_id})}"

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        soup = html_soup(content)
        normalized_source = source_from_mapping(source) if source else None
        default_site_id = (
            normalized_source.external_source_id if normalized_source else None
        ) or _site_id(url)
        meetings: list[MeetingRef] = []
        rows = soup.select("#ContentPlaceHolder1_MeetingGrid tr, table[id*='MeetingGrid'] tr")
        for row in rows:
            onclick_values = " ".join(
                str(node.get("onclick") or "") for node in row.find_all(attrs={"onclick": True})
            )
            onclick_values = f"{row.get('onclick') or ''} {onclick_values}"
            match = _VIEW_MEETING.search(onclick_values)
            detail_anchor = row.select_one("a[href*='ViewMeeting.aspx']")
            detail_url = public_absolute_url(url, detail_anchor.get("href")) if detail_anchor else None
            site_id = match.group("site") if match else _site_id(detail_url or "") or default_site_id
            meeting_id = match.group("meeting") if match else query_value(detail_url or "", "MID", "meetingid")
            if not site_id or not meeting_id:
                continue
            detail_url = detail_url or self._detail_url(url, site_id, meeting_id)
            text = collapse_ws(row.get_text(" ", strip=True))
            cells = row.find_all("td")
            title = ""
            if detail_anchor:
                title = collapse_ws(detail_anchor.get_text(" ", strip=True))
            if not title and cells:
                title = max((collapse_ws(cell.get_text(" ", strip=True)) for cell in cells), key=len, default="")
            meeting = MeetingRef(
                external_meeting_id=str(meeting_id),
                url=detail_url,
                title=title or f"Meeting {meeting_id}",
                meeting_date=parse_date_value(text),
                meeting_start_time=parse_time_value(text),
                meeting_datetime_text=text,
                agenda_url=detail_url,
                is_cancelled="cancel" in text.casefold(),
                metadata={"site_id": str(site_id)},
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
        response = self.fetch_url(normalized_source.public_url, raise_for_status=False)
        content = response.content
        meetings = [] if looks_like_blocked_page(content) else self.parse_meeting_list(
            content, response.final_url, normalized_source, since
        )
        if not meetings and self.allow_browser_fallback:
            rendered = self.render_page(normalized_source.public_url)
            meetings = self.parse_meeting_list(rendered, normalized_source.public_url, normalized_source, since)
        return dedupe_meetings(meetings)

    def parse_meeting_detail(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        meeting_ref: MeetingLike | None = None,
    ) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source) if source else None
        normalized_ref = meeting_ref_from_mapping(meeting_ref) if meeting_ref else MeetingRef(
            query_value(url, "MID") or "", url
        )
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=normalized_source.external_source_id if normalized_source else _site_id(url),
        )
        meeting.source_url = url
        soup = html_soup(content)
        heading = soup.select_one("#tab1 h1, #tab1 h2, .meeting-title, h1, h2")
        if heading:
            heading_text = collapse_ws(heading.get_text(" ", strip=True))
            meeting.title = meeting.title or heading_text
            meeting.meeting_date = meeting.meeting_date or parse_date_value(heading_text)
            meeting.meeting_start_time = meeting.meeting_start_time or parse_time_value(heading_text)

        nodes = soup.select(
            "#tab1 .node-item[data-id], #agendaItelList .node-item[data-id], .agenda-wrapper .node-item[data-id]"
        )
        level_stack: dict[int, str] = {}
        items: list[AgendaItem] = []
        documents: list[DocumentRef] = []
        for index, node in enumerate(nodes):
            deleted = str(node.get("isdeleteditem") or node.get("data-isdeleteditem") or "").casefold()
            if deleted in {"1", "true", "yes"}:
                continue
            item_id = str(node.get("data-id") or node.get("id") or "").strip()
            if not item_id:
                continue
            try:
                level = max(0, int(node.get("level") or node.get("data-level") or 0))
            except (TypeError, ValueError):
                level = 0
            parent_id = level_stack.get(level - 1) if level > 0 else None
            level_stack[level] = item_id
            for old_level in [key for key in level_stack if key > level]:
                level_stack.pop(old_level, None)
            title_node = node.select_one(".node-title, .agenda-title, .title, h3, h4, strong") or node
            title = collapse_ws(title_node.get_text(" ", strip=True))
            description_node = node.select_one(".description, .agenda-description, .node-description")
            item_documents: list[DocumentRef] = []
            attachment_anchors = node.select(
                "a.supportingDocText[href], .supportingDocText a[href], a[href*='Attachment.aspx']"
            )
            for anchor in attachment_anchors:
                link = public_absolute_url(url, anchor.get("href"))
                if not link:
                    continue
                attachment_id = query_value(link, "AID", "attachmentid", "id")
                document = DocumentRef(
                    external_document_id=attachment_id,
                    title=collapse_ws(anchor.get_text(" ", strip=True)) or f"Attachment {attachment_id or ''}".strip(),
                    url=link,
                    document_type=document_type_from_text(anchor.get_text(" ", strip=True), link),
                    agenda_item_external_id=item_id,
                    content_type=content_type_from_url(link),
                    metadata={"site_id": _site_id(link), "meeting_id": query_value(link, "MID")},
                )
                item_documents.append(document)
                documents.append(document)
            items.append(
                AgendaItem(
                    external_item_id=item_id,
                    title=title or f"Agenda item {item_id}",
                    description=(collapse_ws(description_node.get_text(" ", strip=True)) if description_node else None),
                    parent_external_item_id=parent_id,
                    depth=level,
                    order_index=index,
                    documents=dedupe_documents(item_documents),
                )
            )
        meeting.agenda_items = items
        meeting.documents = dedupe_documents(documents)
        for anchor in soup.find_all("a", href=True):
            text = collapse_ws(anchor.get_text(" ", strip=True)).casefold()
            link = public_absolute_url(url, anchor.get("href"))
            if not link:
                continue
            if "minute" in text:
                meeting.minutes_url = meeting.minutes_url or link
            elif "packet" in text:
                meeting.packet_url = meeting.packet_url or link
            elif any(term in text for term in ("video", "recording", "stream")):
                meeting.video_url = meeting.video_url or link
        meeting.metadata["requires_javascript"] = not bool(items)
        return meeting

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        url = normalized_ref.agenda_url or normalized_ref.url
        response = self.fetch_url(url, raise_for_status=False)
        meeting = (
            self.parse_meeting_detail(response.content, response.final_url, normalized_source, normalized_ref)
            if not looks_like_blocked_page(response.content)
            else NormalizedMeeting.from_ref(
                normalized_ref,
                platform=self.platform_name,
                source_external_id=normalized_source.external_source_id,
            )
        )
        if not meeting.agenda_items and self.allow_browser_fallback:
            meeting = self.parse_meeting_detail(self.render_page(url), url, normalized_source, normalized_ref)
        return meeting


__all__ = ["SimbliAdapter"]
