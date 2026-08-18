from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

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
    meeting_is_on_or_after,
    parse_date_value,
    parse_time_value,
    public_absolute_url,
    query_value,
)


_PUBLIC_PATH = re.compile(r"^/([^/]+)/([^/]+)/Board\.nsf/(?:Public)?/?$", re.IGNORECASE)
_ANY_BOARD_PATH = re.compile(r"^/([^/]+)/([^/]+)/Board\.nsf/", re.IGNORECASE)


def _source_parts(url: str) -> tuple[str | None, str | None]:
    match = _ANY_BOARD_PATH.match(urlsplit(url).path)
    return (match.group(1), match.group(2)) if match else (None, None)


def _canonical_source(url: str) -> str:
    state, district = _source_parts(url)
    parsed = urlsplit(url)
    if not state or not district:
        return url
    return f"{parsed.scheme}://{parsed.netloc}/{state}/{district}/Board.nsf/Public"


class BoardDocsAdapter(BoardPlatformAdapter):
    platform_name = "boarddocs"
    requires_javascript = True

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        path_match = _ANY_BOARD_PATH.match(parsed.path)
        folded = (html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else str(html or "")).casefold()
        html_match = any(
            marker in folded
            for marker in ("boarddocs", "btn-view-agenda", "wrap-items", "board.nsf/pfiles")
        )
        host_match = host == "go.boarddocs.com" or host.endswith(".boarddocs.com")
        matched = bool((host_match and path_match) or html_match)
        state, district = _source_parts(url)
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=0.99 if host_match and path_match else 0.9 if html_match else 0.0,
            reason=("Legacy public BoardDocs portal detected." if matched else "No public BoardDocs markers found."),
            canonical_url=_canonical_source(url) if matched else None,
            requires_javascript=True,
            metadata={
                "external_source_id": f"{state}/{district}" if state and district else None,
                "state_slug": state,
                "district_slug": district,
            },
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, object] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        source.public_url = _canonical_source(url)
        source.requires_javascript = True
        return source

    def _goto_url(self, source_url: str, meeting_id: str) -> str:
        return f"{_canonical_source(source_url).rsplit('/Public', 1)[0]}/goto?id={meeting_id}&open="

    def _prepare_rendered_page(self, page: Any, url: str, timeout_ms: int) -> None:
        if "/goto" not in urlsplit(url).path.casefold() and not query_value(url, "id"):
            return
        try:
            button = page.locator("#btn-view-agenda").first
            if button.count() and button.is_visible():
                button.click(timeout=min(timeout_ms, 5000))
                page.wait_for_timeout(500)
        except Exception:
            # Some BoardDocs tenants open the agenda directly from /goto.
            return

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        soup = html_soup(content)
        source_url = source_from_mapping(source).public_url if source else _canonical_source(url)
        meetings: list[MeetingRef] = []
        nodes = soup.select(".meeting[unid], .meeting[data-unid], [data-meeting-id].meeting")
        for node in nodes:
            meeting_id = str(
                node.get("unid") or node.get("data-unid") or node.get("data-meeting-id") or ""
            ).strip()
            if not meeting_id:
                continue
            text = collapse_ws(node.get_text(" ", strip=True))
            title_node = node.select_one(".meeting-name, .meeting-title, .name, h3, h4, strong")
            title = collapse_ws(title_node.get_text(" ", strip=True)) if title_node else text
            date_value = node.get("datenumber") or node.get("data-date") or text
            detail_url = self._goto_url(source_url, meeting_id)
            meeting = MeetingRef(
                external_meeting_id=meeting_id,
                url=detail_url,
                title=title or f"Meeting {meeting_id}",
                meeting_date=parse_date_value(date_value) or parse_date_value(text),
                meeting_start_time=parse_time_value(text),
                meeting_datetime_text=text,
                agenda_url=detail_url,
                is_cancelled="cancel" in text.casefold(),
                metadata={"datenumber": node.get("datenumber")},
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
        if meetings:
            return dedupe_meetings(meetings)

        for anchor in soup.select("a[href*='Board.nsf/goto'], a[href*='goto?id=']"):
            detail_url = public_absolute_url(url, anchor.get("href"))
            meeting_id = query_value(detail_url or "", "id")
            if not detail_url or not meeting_id:
                continue
            context = anchor.find_parent(["li", "tr", "div"]) or anchor
            text = collapse_ws(context.get_text(" ", strip=True))
            meeting = MeetingRef(
                external_meeting_id=meeting_id,
                url=detail_url,
                title=collapse_ws(anchor.get_text(" ", strip=True)) or text,
                meeting_date=parse_date_value(text),
                meeting_start_time=parse_time_value(text),
                meeting_datetime_text=text,
                agenda_url=detail_url,
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
        try:
            response = self.fetch_url(normalized_source.public_url)
            content, final_url = response.content, response.final_url
        except requests.HTTPError:
            if not self.allow_browser_fallback:
                raise
            content, final_url = self.render_page(normalized_source.public_url), normalized_source.public_url
        meetings = self.parse_meeting_list(content, final_url, normalized_source, since)
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
            query_value(url, "id") or "", url
        )
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=normalized_source.external_source_id if normalized_source else None,
        )
        meeting.source_url = url
        soup = html_soup(content)
        heading = soup.select_one(".meeting-title, #meeting-title, h1, h2")
        if heading:
            heading_text = collapse_ws(heading.get_text(" ", strip=True))
            meeting.title = meeting.title or heading_text
            meeting.meeting_date = meeting.meeting_date or parse_date_value(heading_text)
            meeting.meeting_start_time = meeting.meeting_start_time or parse_time_value(heading_text)

        items: list[AgendaItem] = []
        documents: list[DocumentRef] = []
        category_ids: dict[int, str] = {}
        order_index = 0
        for category_index, category in enumerate(soup.select("dt.category.agendaorder, dt.category")):
            category_id = str(category.get("unid") or category.get("data-unid") or f"category-{category_index + 1}")
            category_ids[id(category)] = category_id
            items.append(
                AgendaItem(
                    external_item_id=category_id,
                    title=collapse_ws(category.get_text(" ", strip=True)) or f"Agenda section {category_index + 1}",
                    depth=0,
                    order_index=order_index,
                )
            )
            order_index += 1
            sibling = category.find_next_sibling("dd")
            for node in sibling.select("li.item.agendaorder.public[unid], li.item[unid]") if sibling else []:
                item_id = str(node.get("unid") or node.get("data-unid") or "").strip()
                if not item_id:
                    continue
                title_node = node.select_one(".title, .item-title, h3, h4, strong") or node
                title = collapse_ws(title_node.get_text(" ", strip=True))
                description_node = node.select_one(".description, .item-description")
                item_documents: list[DocumentRef] = []
                for anchor in node.select("a.public-file[href], a[href*='/pfiles/']"):
                    link = public_absolute_url(url, anchor.get("href"))
                    if not link:
                        continue
                    file_id = str(anchor.get("unid") or anchor.get("data-unid") or urlsplit(link).path)
                    document = DocumentRef(
                        external_document_id=file_id,
                        title=collapse_ws(anchor.get_text(" ", strip=True)) or urlsplit(link).path.rsplit("/", 1)[-1],
                        url=link,
                        document_type=document_type_from_text(anchor.get_text(" ", strip=True), link),
                        agenda_item_external_id=item_id,
                        content_type=content_type_from_url(link),
                        file_name=urlsplit(link).path.rsplit("/", 1)[-1],
                    )
                    item_documents.append(document)
                    documents.append(document)
                items.append(
                    AgendaItem(
                        external_item_id=item_id,
                        title=title or f"Agenda item {item_id}",
                        description=(collapse_ws(description_node.get_text(" ", strip=True)) if description_node else None),
                        parent_external_item_id=category_id,
                        depth=1,
                        order_index=order_index,
                        documents=dedupe_documents(item_documents),
                    )
                )
                order_index += 1
        if not items:
            for index, node in enumerate(soup.select("li.item.agendaorder.public[unid], li.item[unid]")):
                item_id = str(node.get("unid") or node.get("data-unid") or f"item-{index + 1}")
                items.append(
                    AgendaItem(
                        external_item_id=item_id,
                        title=collapse_ws(node.get_text(" ", strip=True)),
                        order_index=index,
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
            elif any(word in text for word in ("video", "recording", "stream")):
                meeting.video_url = meeting.video_url or link
        meeting.metadata["requires_javascript"] = not bool(items)
        return meeting

    def fetch_meeting(self, source: SourceLike, meeting_ref: MeetingLike) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source)
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        url = normalized_ref.agenda_url or normalized_ref.url
        try:
            response = self.fetch_url(url)
            content, final_url = response.content, response.final_url
        except requests.HTTPError:
            if not self.allow_browser_fallback:
                raise
            content, final_url = self.render_page(url), url
        meeting = self.parse_meeting_detail(content, final_url, normalized_source, normalized_ref)
        if not meeting.agenda_items and self.allow_browser_fallback:
            meeting = self.parse_meeting_detail(self.render_page(url), url, normalized_source, normalized_ref)
        return meeting


__all__ = ["BoardDocsAdapter"]
