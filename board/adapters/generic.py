from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Mapping
from urllib.parse import urlsplit

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
    same_host_or_subdomain,
)


_BOARD_PHRASES = (
    "school board",
    "board of education",
    "board of directors",
    "board meetings",
    "board meeting",
)
_DOCUMENT_PHRASES = (
    "board agenda",
    "meeting agenda",
    "board minutes",
    "meeting minutes",
    "board packet",
    "meeting packet",
)
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".txt", ".html", ".htm")
_VIDEO_HOST_SUFFIXES = (
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "vimeo.com",
    "zoom.us",
    "granicus.com",
    "livestream.com",
    "streamable.com",
    "boxcast.tv",
)


def _stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _board_score(text: str, url: str) -> int:
    combined = f"{text} {url}".casefold()
    score = 0
    if any(phrase in combined for phrase in _BOARD_PHRASES):
        score += 3
    if any(phrase in combined for phrase in _DOCUMENT_PHRASES):
        score += 3
    if "agenda" in combined:
        score += 1
    if "minutes" in combined:
        score += 1
    if "packet" in combined:
        score += 1
    if "meeting" in combined:
        score += 1
    return score


def _video_url(url: str, text: str = "") -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    combined = f"{text} {url}".casefold()
    known_host = any(host == suffix or host.endswith(f".{suffix}") for suffix in _VIDEO_HOST_SUFFIXES)
    return known_host and any(
        marker in combined for marker in ("video", "watch", "stream", "record", "zoom")
    )


class GenericBoardAdapter(BoardPlatformAdapter):
    platform_name = "generic"

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        url_score = _board_score("", url)
        score = url_score
        strong_links = 0
        organization_name = None
        if html:
            soup = html_soup(html)
            title = soup.find(["h1", "h2"]) or soup.title
            title_text = collapse_ws(title.get_text(" ", strip=True)) if title else ""
            organization_name = title_text or None
            if any(phrase in title_text.casefold() for phrase in _BOARD_PHRASES):
                score += 4
            for anchor in soup.find_all("a", href=True):
                if _board_score(collapse_ws(anchor.get_text(" ", strip=True)), str(anchor.get("href"))) >= 3:
                    strong_links += 1
            score += min(strong_links, 4)
        matched = score >= 4
        confidence = min(0.78, 0.38 + score * 0.05) if matched else 0.0
        host = (urlsplit(url).hostname or "").casefold()
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=confidence,
            reason=(
                f"Generic board page matched {score} points and {strong_links} strong links."
                if matched
                else "Generic page lacks enough explicit school-board evidence."
            ),
            canonical_url=url if matched else None,
            metadata={
                "external_source_id": host or None,
                "organization_name": organization_name,
                "evidence_score": score,
                "strong_link_count": strong_links,
            },
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, object] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        source.status = "working" if self.detect(url, content).confidence >= 0.72 else "manual_review"
        return source

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        soup = html_soup(content)
        meetings: list[MeetingRef] = []
        for anchor in soup.find_all("a", href=True):
            link = public_absolute_url(url, anchor.get("href"))
            if not link or not same_host_or_subdomain(link, url):
                continue
            text = collapse_ws(anchor.get_text(" ", strip=True))
            context = anchor.find_parent(["article", "li", "tr", "section", "div"]) or anchor
            context_text = collapse_ws(context.get_text(" ", strip=True))
            score = _board_score(f"{text} {context_text}", link)
            meeting_date = parse_date_value(context_text) or parse_date_value(text)
            if score < 3 or not meeting_date:
                continue
            meeting_id = (
                anchor.get("data-meeting-id")
                or query_value(link, "meeting", "meetingid", "id")
                or _stable_id(link)
            )
            document_type = document_type_from_text(text, link)
            is_document = document_type != "attachment" or urlsplit(link).path.casefold().endswith(_DOCUMENT_SUFFIXES)
            agenda_url = link if document_type == "agenda" or (is_document and "agenda" in text.casefold()) else None
            minutes_url = link if document_type == "minutes" else None
            packet_url = link if document_type == "packet" else None
            meeting = MeetingRef(
                external_meeting_id=str(meeting_id),
                url=link,
                title=text or context_text,
                meeting_date=meeting_date,
                meeting_start_time=parse_time_value(context_text),
                meeting_datetime_text=context_text,
                agenda_url=agenda_url,
                minutes_url=minutes_url,
                packet_url=packet_url,
                is_cancelled="cancel" in context_text.casefold(),
                metadata={"generic_evidence_score": score, "is_direct_document": is_document},
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
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
            query_value(url, "meeting", "meetingid", "id") or _stable_id(url), url
        )
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=normalized_source.external_source_id if normalized_source else None,
        )
        meeting.source_url = url
        content_type = content_type_from_url(url)
        if content_type and content_type != "text/html":
            meeting.documents = [
                DocumentRef(
                    title=meeting.title or urlsplit(url).path.rsplit("/", 1)[-1],
                    url=url,
                    document_type=document_type_from_text(meeting.title, url),
                    content_type=content_type,
                    file_name=urlsplit(url).path.rsplit("/", 1)[-1],
                )
            ]
            return meeting

        soup = html_soup(content)
        heading = soup.find(["h1", "h2"])
        if heading:
            heading_text = collapse_ws(heading.get_text(" ", strip=True))
            meeting.title = meeting.title or heading_text
            meeting.meeting_date = meeting.meeting_date or parse_date_value(heading_text)
            meeting.meeting_start_time = meeting.meeting_start_time or parse_time_value(heading_text)

        documents: list[DocumentRef] = []
        for anchor in soup.find_all("a", href=True):
            link = public_absolute_url(url, anchor.get("href"))
            if not link:
                continue
            text = collapse_ws(anchor.get_text(" ", strip=True))
            if _video_url(link, text):
                meeting.video_url = meeting.video_url or link
                continue
            if not same_host_or_subdomain(link, url):
                continue
            kind = document_type_from_text(text, link)
            suffix_match = urlsplit(link).path.casefold().endswith(_DOCUMENT_SUFFIXES)
            if kind == "attachment" and not suffix_match:
                continue
            document = DocumentRef(
                external_document_id=query_value(link, "document", "documentid", "file", "id"),
                title=text or urlsplit(link).path.rsplit("/", 1)[-1],
                url=link,
                document_type=kind,
                content_type=content_type_from_url(link),
                file_name=urlsplit(link).path.rsplit("/", 1)[-1] or None,
            )
            documents.append(document)
            if kind == "agenda":
                meeting.agenda_url = meeting.agenda_url or link
            elif kind == "minutes":
                meeting.minutes_url = meeting.minutes_url or link
            elif kind == "packet":
                meeting.packet_url = meeting.packet_url or link
        meeting.documents = dedupe_documents(documents)

        agenda_container = soup.select_one(
            "main .agenda, #agenda, .meeting-agenda, [aria-label*='agenda' i]"
        )
        if agenda_container:
            nodes = agenda_container.find_all(["h2", "h3", "h4", "li"], recursive=True)
            items: list[AgendaItem] = []
            for index, node in enumerate(nodes[:500]):
                title = collapse_ws(node.get_text(" ", strip=True))
                if not title or _board_score(title, "") < 1:
                    continue
                items.append(
                    AgendaItem(
                        external_item_id=str(node.get("id") or f"generic-{index + 1}"),
                        title=title,
                        depth=max(0, int(node.name[-1]) - 2) if node.name.startswith("h") else 1,
                        order_index=index,
                    )
                )
            meeting.agenda_items = items
        meeting.metadata["manual_review_recommended"] = True
        return meeting


__all__ = ["GenericBoardAdapter"]
