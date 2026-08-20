from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

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
_DOWNLOAD_SUFFIXES = (".pdf", ".doc", ".docx", ".txt")
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
_ONE_OFF_DETAIL_SEGMENTS = {
    "article",
    "event",
    "post",
    "post-details",
    "story",
    "~occur-id",
}
_ONE_OFF_COLLECTION_SEGMENTS = {"articles", "events", "news", "posts", "stories"}
_ARCHIVE_SEGMENTS = {"archive", "archives", "index"}
_MAX_DATED_BLOCKS = 500
_MAX_BLOCK_SCAN_NODES = 5_000
_MAX_BLOCK_ANCESTOR_DEPTH = 32
_MAX_LOCAL_CONTEXT_CHARS = 1_200
_MAX_LOCAL_CONTEXT_LINKS = 8
_MAX_LINKS_PER_DATED_BLOCK = 128
_MAX_TOTAL_DATED_BLOCK_LINKS = 4_096
_PUBLIC_DOCUMENT_HOSTS = {
    "campussuite-storage.s3.amazonaws.com",
    "core-docs.s3.amazonaws.com",
    "core-docs.s3.us-east-1.amazonaws.com",
    "docs.google.com",
    "drive.google.com",
    "files-backend.assets.thrillshare.com",
    "resources.finalsite.net",
    "storage.googleapis.com",
}


@dataclass(slots=True)
class _DatedMeetingBlock:
    meeting_date: str
    text: str
    identity_text: str
    identity_qualifiers: tuple[str, ...]
    links: list[tuple[str, str, str]]
    detail_url: str | None = None
    video_url: str | None = None


def _phrase_text(*values: object) -> str:
    """Normalize URL separators so human phrases still match in paths.

    District sites routinely spell ``school board meetings`` as
    ``/school-board/board_meetings`` or similar. Treat punctuation as word
    separators without changing the canonical URL used for fetching.
    """

    text = " ".join(unquote(str(value or "")) for value in values).casefold()
    return collapse_ws(re.sub(r"[^a-z0-9]+", " ", text))


def _public_document_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().strip(".")
    return host in _PUBLIC_DOCUMENT_HOSTS


def _allowed_meeting_document_link(
    link: str,
    base_url: str,
    *,
    text: str = "",
) -> bool:
    """Allow district-hosted or narrowly vetted public meeting documents.

    ``public_absolute_url`` has already rejected local/private literal URLs,
    credentials, and unsupported schemes. Network fetches remain subject to
    ``BoardHTTPClient`` DNS/IP validation; this helper only decides whether an
    already linked public URL is relevant enough to parse.
    """

    if same_host_or_subdomain(link, base_url):
        return True
    if not _public_document_host(link):
        return False
    kind = document_type_from_text(text, link)
    has_document_suffix = urlsplit(link).path.casefold().endswith(_DOCUMENT_SUFFIXES)
    return kind in {"agenda", "minutes", "notice", "packet"} or has_document_suffix


def _anchor_has_meeting_intent(text: str, link: str) -> bool:
    """Require meeting intent on the anchor itself, not a broad parent div."""

    kind = document_type_from_text(text, link)
    if kind in {"agenda", "minutes", "notice", "packet"}:
        return True
    if urlsplit(link).path.casefold().endswith(_DOWNLOAD_SUFFIXES):
        return True
    anchor_text = _phrase_text(text)
    return any(
        phrase in anchor_text
        for phrase in (*_BOARD_PHRASES, *_DOCUMENT_PHRASES)
    )


def _context_date_values(node: Any) -> set[str]:
    """Collect distinct dates from local text nodes to reject broad archives."""

    dates: set[str] = set()
    consumed = 0
    for value in getattr(node, "stripped_strings", ()):
        consumed += len(value) + 1
        if consumed > _MAX_LOCAL_CONTEXT_CHARS:
            break
        parsed = parse_date_value(value)
        if parsed:
            dates.add(parsed)
    parsed = parse_date_value(_bounded_node_text(node))
    if parsed:
        dates.add(parsed)
    return dates


def _bounded_node_text(node: Any, limit: int = _MAX_LOCAL_CONTEXT_CHARS) -> str:
    """Read only enough descendant text to classify one local meeting block."""

    parts: list[str] = []
    consumed = 0
    for value in getattr(node, "stripped_strings", ()):
        normalized = collapse_ws(value)
        if not normalized:
            continue
        remaining = limit - consumed
        if remaining <= 0:
            break
        parts.append(normalized[:remaining])
        consumed += len(normalized) + 1
        if consumed >= limit:
            break
    return collapse_ws(" ".join(parts))


def _dated_anchor_context(anchor: Any) -> Any:
    """Choose the nearest small ancestor that supplies one unambiguous date.

    CMS templates often nest an agenda link in an undated ``li`` while the
    enclosing article or section owns the date. Conversely, a site-wide quick
    links div may contain a date plus unrelated search/contact links. Context
    is therefore bounded by text size, link count, and a single distinct date.
    """

    if parse_date_value(collapse_ws(anchor.get_text(" ", strip=True))):
        return anchor
    parents = anchor.find_parents(
        ["article", "li", "tr", "section", "div"],
        limit=8,
    )
    for parent in parents:
        parent_text = _bounded_node_text(parent)
        if len(parent_text) >= _MAX_LOCAL_CONTEXT_CHARS:
            continue
        if len(
            parent.find_all(
                "a",
                href=True,
                limit=_MAX_LOCAL_CONTEXT_LINKS + 1,
            )
        ) > _MAX_LOCAL_CONTEXT_LINKS:
            continue
        if len(_context_date_values(parent)) == 1:
            return parent
    return anchor


def _block_identity_text(
    node: Any,
    fallback: str,
) -> tuple[str, str, tuple[str, ...]]:
    """Build a base row identity plus anchor-only disambiguators.

    Anchor qualifiers are kept separate from the stable base identity. They are
    used only when the page actually contains otherwise-identical same-date
    blocks; a lone row therefore keeps its ID when an agenda link is added.
    """

    parts: list[str] = []
    consumed = 0
    for value in node.find_all(string=True, limit=200):
        if value.find_parent("a") is not None:
            continue
        normalized = collapse_ws(value)
        if not normalized:
            continue
        remaining = _MAX_LOCAL_CONTEXT_CHARS - consumed
        if remaining <= 0:
            break
        parts.append(normalized[:remaining])
        consumed += len(normalized) + 1
    display_text = collapse_ws(" ".join(parts)) or fallback
    stable_text = _phrase_text(display_text)
    stable_text = collapse_ws(
        re.sub(
            r"\b(?:agenda|minutes?|packets?|notices?|documents?|downloads?|view|pdf|docs?|docx)\b",
            " ",
            stable_text,
        )
    )
    # Some CMS cards put the meeting type only inside a document anchor. Keep
    # exceptional types so same-day meetings remain distinct, but treat
    # ``regular`` as the unqualified/default meeting type. Otherwise adding a
    # ``Regular School Board Meeting Agenda`` anchor to an existing generic
    # ``School Board Meeting`` row would churn the meeting's stable ID.
    identity_qualifiers: list[str] = []
    qualifier_tokens = {
        "business",
        "emergency",
        "executive",
        "hearing",
        "retreat",
        "session",
        "special",
        "study",
        "work",
        "workshop",
    }
    base_tokens = set(stable_text.split())
    for anchor in node.find_all("a", href=True, limit=_MAX_LOCAL_CONTEXT_LINKS):
        anchor_text = _phrase_text(anchor.get_text(" ", strip=True))
        anchor_tokens = set(anchor_text.split())
        for token in sorted(anchor_tokens & qualifier_tokens):
            if token not in base_tokens and token not in identity_qualifiers:
                identity_qualifiers.append(token)
    return display_text, stable_text or "meeting", tuple(identity_qualifiers)


def _meeting_type_identity(block: _DatedMeetingBlock) -> str:
    """Return a normalized type only when same-date rows need separation."""

    tokens = set(
        _phrase_text(block.identity_text, *block.identity_qualifiers).split()
    )
    # ``regular`` is the default identity. This lets a previously unqualified
    # row retain its ID when the site later labels it a regular meeting.
    type_tokens = sorted(
        tokens
        & {
            "business",
            "emergency",
            "executive",
            "hearing",
            "retreat",
            "session",
            "special",
            "study",
            "work",
            "workshop",
        }
    )
    return collapse_ws(f"meeting {' '.join(type_tokens)}")


def _meeting_collision_disambiguator(block: _DatedMeetingBlock) -> str:
    """Extract a non-time/status suffix for otherwise identical same-day types."""

    text = _phrase_text(block.identity_text)
    months = (
        "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        "jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        "nov(?:ember)?|dec(?:ember)?"
    )
    text = re.sub(rf"\b(?:{months})\s+\d{{1,2}}(?:\s+\d{{4}})?\b", " ", text)
    text = re.sub(rf"\b\d{{1,2}}\s+(?:{months})\s+\d{{4}}\b", " ", text)
    text = re.sub(r"\b(?:\d{4}\s+\d{1,2}\s+\d{1,2}|\d{1,2}\s+\d{1,2}\s+\d{4})\b", " ", text)
    text = re.sub(r"\b\d{1,2}\s+\d{2}\s*(?:am|pm)\b", " ", text)
    text = re.sub(
        r"\b(?:cancelled|canceled|postponed|rescheduled)\b",
        " ",
        text,
    )
    text = re.sub(
        r"\b(?:school board|board of education|board of directors|board|meeting|"
        r"business|emergency|executive|hearing|regular|retreat|session|special|"
        r"study|work|workshop)\b",
        " ",
        text,
    )
    # A trailing venue is enrichment, not identity. Explicit sequence labels
    # (for example ``Session 2``) have already survived before this boundary.
    text = re.sub(r"\b(?:at|location|room)\b.*$", " ", text)
    return collapse_ws(text)[:120]


def _dedupe_block_links(
    links: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Deduplicate responsive/mobile copies by their canonical public URL."""

    output: list[tuple[str, str, str]] = []
    positions: dict[str, int] = {}
    kind_priority = {
        "attachment": 0,
        "notice": 1,
        "packet": 2,
        "minutes": 3,
        "agenda": 4,
    }
    for link in links:
        position = positions.get(link[0])
        if position is not None:
            existing = output[position]
            if kind_priority.get(link[2], 0) > kind_priority.get(existing[2], 0):
                output[position] = link
            continue
        positions[link[0]] = len(output)
        output.append(link)
    return output


def _metadata_documents(
    meeting_ref: MeetingRef,
    hub_url: str,
) -> list[DocumentRef]:
    """Revalidate and materialize every vetted block link from ref metadata."""

    values = meeting_ref.metadata.get("vetted_document_links")
    if not isinstance(values, list):
        return []
    documents: list[DocumentRef] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        title = collapse_ws(value.get("title"))
        link = public_absolute_url(hub_url, str(value.get("url") or ""))
        if (
            not link
            or not _allowed_meeting_document_link(link, hub_url, text=title)
            or not _anchor_has_meeting_intent(title, link)
        ):
            continue
        kind = document_type_from_text(title, link)
        documents.append(
            DocumentRef(
                external_document_id=query_value(
                    link,
                    "document",
                    "documentid",
                    "file",
                    "id",
                ),
                title=title or urlsplit(link).path.rsplit("/", 1)[-1],
                url=link,
                document_type=kind,
                content_type=content_type_from_url(link),
                file_name=urlsplit(link).path.rsplit("/", 1)[-1] or None,
            )
        )
    return dedupe_documents(documents)


def _merge_documents_by_url(*groups: list[DocumentRef]) -> list[DocumentRef]:
    """Merge generic block/detail documents without duplicating one URL."""

    output: list[DocumentRef] = []
    seen: set[str] = set()
    for group in groups:
        for document in group:
            if document.url in seen:
                continue
            seen.add(document.url)
            output.append(document)
    return output


def _dated_meeting_blocks(
    content: Content,
    url: str,
) -> list[_DatedMeetingBlock]:
    """Return distinct dated rows that can produce a meeting reference.

    The boolean indicates that a vetted link represents the row. Otherwise the
    row is safe to emit as a synthetic, hub-backed meeting. A block containing
    only rejected document links is neither: counting it would allow a source
    to validate as durable while synchronization returns no meetings.
    """

    soup = html_soup(content)
    title = soup.find(["h1", "h2"]) or soup.title
    title_text = collapse_ws(title.get_text(" ", strip=True)) if title else ""
    hub_context = _phrase_text(title_text, url)
    hub_marked = any(
        phrase in hub_context
        for phrase in (*_BOARD_PHRASES, *_DOCUMENT_PHRASES)
    )
    candidates: list[tuple[Any, str, str]] = []
    candidate_node_ids: set[int] = set()
    for node in soup.find_all(
        ["article", "li", "tr"],
        limit=_MAX_BLOCK_SCAN_NODES,
    ):
        text = _bounded_node_text(node)
        meeting_date = parse_date_value(text)
        row_context = _phrase_text(text)
        concise_meeting_row = hub_marked and any(
            marker in row_context
            for marker in ("meeting", "agenda", "minutes", "packet", "notice")
        )
        if not meeting_date or not (
            _board_score(text, "") >= 3 or concise_meeting_row
        ):
            continue
        candidates.append((node, meeting_date, text))
        candidate_node_ids.add(id(node))
        if len(candidates) >= _MAX_DATED_BLOCKS:
            break

    # Mark nested wrappers with bounded parent walks. This replaces a
    # descendant-tree scan for every candidate, which becomes quadratic on
    # deeply nested CMS markup.
    candidates_with_dated_children: set[int] = set()
    for node, _meeting_date, _text in candidates:
        for depth, parent in enumerate(node.parents):
            if depth >= _MAX_BLOCK_ANCESTOR_DEPTH:
                break
            parent_id = id(parent)
            if parent_id in candidate_node_ids:
                candidates_with_dated_children.add(parent_id)
                break

    found: dict[tuple[str, str], _DatedMeetingBlock] = {}
    inspected_anchors = 0
    for node, meeting_date, text in candidates:
        # Prefer the innermost dated block. CMS templates frequently wrap a
        # meeting <li> in a dated <article>; treating both as meetings can make
        # one logical row look like a durable two-meeting archive.
        if id(node) in candidates_with_dated_children:
            continue

        display_text, identity_text, identity_qualifiers = _block_identity_text(
            node,
            text,
        )
        qualified_identity = collapse_ws(
            f"{identity_text} {' '.join(identity_qualifiers)}"
        )
        key = (meeting_date, qualified_identity[:300])
        has_document_intent = False
        allowed_links: list[tuple[str, str, str]] = []
        detail_url: str | None = None
        video_url: str | None = None
        remaining_anchor_budget = _MAX_TOTAL_DATED_BLOCK_LINKS - inspected_anchors
        if remaining_anchor_budget <= 0:
            break
        block_anchor_budget = min(
            _MAX_LINKS_PER_DATED_BLOCK,
            remaining_anchor_budget,
        )
        block_anchors = node.find_all(
            "a",
            href=True,
            limit=block_anchor_budget + 1,
        )
        block_anchor_overflow = len(block_anchors) > block_anchor_budget
        block_anchors = block_anchors[:block_anchor_budget]
        inspected_anchors += len(block_anchors)
        for anchor in block_anchors:
            raw_link = str(anchor.get("href") or "")
            link_text = collapse_ws(anchor.get_text(" ", strip=True))
            kind = document_type_from_text(link_text, raw_link)
            has_document_suffix = urlsplit(raw_link).path.casefold().endswith(
                _DOWNLOAD_SUFFIXES
            )
            if kind in {"agenda", "minutes", "notice", "packet"} or has_document_suffix:
                has_document_intent = True
            link = public_absolute_url(url, raw_link)
            if not link:
                continue
            context = _dated_anchor_context(anchor)
            context_text = _bounded_node_text(context)
            same_dated_context = parse_date_value(context_text) == meeting_date
            if _video_url(link, link_text) and same_dated_context:
                video_url = video_url or link
                continue
            if not _allowed_meeting_document_link(
                link,
                url,
                text=link_text,
            ):
                continue
            if (
                _anchor_has_meeting_intent(link_text, link)
                and same_dated_context
            ):
                allowed_links.append((link, link_text, kind))
                continue
            detail_label = _phrase_text(link_text)
            detail_path = _phrase_text(urlsplit(link).path)
            if (
                same_dated_context
                and same_host_or_subdomain(link, url)
                and _board_score(text, "") >= 3
                and (
                    detail_label in {
                        "details",
                        "meeting details",
                        "more information",
                        "read more",
                        "view details",
                    }
                    or any(
                        marker in detail_path
                        for marker in ("board meeting", "meeting details")
                    )
                )
            ):
                detail_url = detail_url or link

        # A huge card is usually a broad archive wrapper or adversarial markup,
        # not one meeting. Do not classify it from a partial link sample. The
        # capped scan protects both discovery and the repeated detect/validate
        # path while failing closed on unseen document intent.
        if block_anchor_overflow:
            continue

        # Do not turn a private or untrusted document into a synthetic public
        # meeting. If at least one vetted link can represent the row, the
        # normal anchor parser emits it; genuinely linkless rows use the hub.
        if has_document_intent and not allowed_links:
            continue
        existing = found.get(key)
        if existing is None:
            found[key] = _DatedMeetingBlock(
                meeting_date=meeting_date,
                text=display_text,
                identity_text=identity_text,
                identity_qualifiers=identity_qualifiers,
                links=_dedupe_block_links(allowed_links),
                detail_url=detail_url,
                video_url=video_url,
            )
            continue
        known_links = {link[0] for link in existing.links}
        for link in allowed_links:
            if link[0] in known_links:
                continue
            existing.links.append(link)
            known_links.add(link[0])
        existing.detail_url = existing.detail_url or detail_url
        existing.video_url = existing.video_url or video_url
    blocks = list(found.values())
    date_counts: dict[str, int] = {}
    for block in blocks:
        date_counts[block.meeting_date] = date_counts.get(block.meeting_date, 0) + 1
    type_identities = [_meeting_type_identity(block) for block in blocks]
    type_counts: dict[tuple[str, str], int] = {}
    for block, type_identity in zip(blocks, type_identities):
        type_key = (block.meeting_date, type_identity)
        type_counts[type_key] = type_counts.get(type_key, 0) + 1
    type_occurrences: dict[tuple[str, str], int] = {}
    for block, type_identity in zip(blocks, type_identities):
        # Hub and date form the durable identity for an unambiguous row. Times,
        # cancellation labels, locations, and newly published document titles
        # are mutable enrichment and must not create a second meeting. Only an
        # actual same-date collision activates the normalized type qualifier.
        if date_counts[block.meeting_date] == 1:
            block.identity_text = "meeting"
            continue
        type_key = (block.meeting_date, type_identity)
        if type_counts[type_key] == 1:
            block.identity_text = type_identity
            continue
        disambiguator = _meeting_collision_disambiguator(block)
        type_occurrences[type_key] = type_occurrences.get(type_key, 0) + 1
        block.identity_text = collapse_ws(
            f"{type_identity} {disambiguator or type_occurrences[type_key]}"
        )
    return blocks


def _stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _board_score(text: str, url: str) -> int:
    combined = _phrase_text(text, url)
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


def _is_one_off_content_url(url: str) -> bool:
    segments = [
        segment.casefold()
        for segment in urlsplit(url).path.split("/")
        if segment
    ]
    if any(
        segment in _ONE_OFF_DETAIL_SEGMENTS or segment.startswith("~occur-id")
        for segment in segments
    ):
        return True

    # Collection roots such as /news, /posts, and /calendar/events can be
    # durable hubs. A following slug/id marks an individual detail route; an
    # explicit archive/index suffix remains a collection rather than a post.
    for index, segment in enumerate(segments[:-1]):
        if (
            segment in _ONE_OFF_COLLECTION_SEGMENTS
            and segments[index + 1] not in _ARCHIVE_SEGMENTS
        ):
            return True
    return False


class GenericBoardAdapter(BoardPlatformAdapter):
    platform_name = "generic"

    def detect(self, url: str, html: Content | None = None) -> DetectionResult:
        url_score = _board_score("", url)
        score = url_score
        strong_links = 0
        dated_meeting_links = 0
        durable_hub = False
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
            parsed_meetings = self.parse_meeting_list(html, url)
            vetted_document_links = sum(
                int(meeting.metadata.get("vetted_document_link_count") or 0)
                for meeting in parsed_meetings
            )
            dated_meeting_links = vetted_document_links
            # Count only references the adapter can actually emit. This keeps
            # validation coupled to synchronization and avoids a second block
            # traversal over large pages.
            dated_meeting_blocks = len(parsed_meetings)
            hub_context = _phrase_text(title_text, url)
            hub_marked = any(
                phrase in hub_context
                for phrase in (*_BOARD_PHRASES, *_DOCUMENT_PHRASES)
            )
            durable_hub = (
                bool(parsed_meetings)
                and (len(parsed_meetings) >= 2 or vetted_document_links >= 2)
                and hub_marked
                and not _is_one_off_content_url(url)
            )
        else:
            dated_meeting_blocks = 0
        matched = score >= 4
        confidence = (
            0.78
            if matched and durable_hub
            else min(0.68, 0.38 + score * 0.05) if matched else 0.0
        )
        host = (urlsplit(url).hostname or "").casefold()
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=confidence,
            reason=(
                f"Generic board page matched {score} points, {strong_links} strong links, "
                f"and {dated_meeting_links} dated meeting/document links."
                if matched
                else "Generic page lacks enough explicit school-board evidence."
            ),
            canonical_url=url if matched else None,
            metadata={
                "external_source_id": host or None,
                "organization_name": organization_name,
                "evidence_score": score,
                "strong_link_count": strong_links,
                "dated_meeting_link_count": dated_meeting_links,
                "vetted_document_link_count": dated_meeting_links,
                "dated_meeting_block_count": dated_meeting_blocks,
                "durable_meeting_hub": durable_hub,
                "one_off_content_url": _is_one_off_content_url(url),
            },
        )

    def parse_source(
        self,
        content: Content,
        url: str,
        district: Mapping[str, object] | None = None,
    ) -> BoardSource:
        source = super().parse_source(content, url, district)
        detection = self.detect(url, content)
        source.status = (
            "working"
            if detection.metadata.get("durable_meeting_hub") is True
            else "manual_review"
        )
        return source

    def parse_meeting_list(
        self,
        content: Content,
        url: str,
        source: SourceLike | None = None,
        since: date | datetime | str | None = None,
    ) -> list[MeetingRef]:
        meetings: list[MeetingRef] = []
        for block in _dated_meeting_blocks(content, url):
            by_kind: dict[str, str] = {}
            for link, _link_text, kind in block.links:
                by_kind.setdefault(kind, link)
            primary_link = (
                by_kind.get("agenda")
                or by_kind.get("packet")
                or by_kind.get("minutes")
                or (block.links[0][0] if block.links else None)
                or block.detail_url
                or url
            )
            direct_document = bool(block.links)
            scoped_detail = bool(block.detail_url)
            meeting = MeetingRef(
                external_meeting_id=_stable_id(
                    f"{url}\n{block.meeting_date}\n{block.identity_text}"
                ),
                url=primary_link,
                title=block.text,
                meeting_date=block.meeting_date,
                meeting_start_time=parse_time_value(block.text),
                meeting_datetime_text=block.text,
                agenda_url=by_kind.get("agenda"),
                minutes_url=by_kind.get("minutes"),
                packet_url=by_kind.get("packet"),
                public_notice_url=by_kind.get("notice"),
                video_url=block.video_url,
                is_cancelled="cancel" in block.text.casefold(),
                metadata={
                    "generic_evidence_score": max(
                        _board_score(block.text, url),
                        3 if direct_document else 0,
                    ),
                    "is_direct_document": direct_document,
                    "is_hub_schedule_row": not direct_document and not scoped_detail,
                    "is_scoped_detail_page": scoped_detail,
                    "vetted_document_link_count": len(block.links),
                    "hub_url": url,
                    "vetted_document_links": [
                        {
                            "url": link,
                            "title": title,
                            "document_type": kind,
                        }
                        for link, title, kind in block.links
                    ],
                },
            )
            if meeting_is_on_or_after(meeting, since):
                meetings.append(meeting)
        return dedupe_meetings(meetings)

    def _normalized_from_ref(
        self,
        source: SourceLike | None,
        meeting_ref: MeetingLike,
    ) -> NormalizedMeeting:
        normalized_source = source_from_mapping(source) if source else None
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        meeting = NormalizedMeeting.from_ref(
            normalized_ref,
            platform=self.platform_name,
            source_external_id=(
                normalized_source.external_source_id if normalized_source else None
            ),
        )
        if normalized_ref.metadata.get("is_hub_schedule_row"):
            meeting.metadata["hub_documents_omitted"] = True
        return meeting

    def fetch_meeting(
        self,
        source: SourceLike,
        meeting_ref: MeetingLike,
    ) -> NormalizedMeeting:
        normalized_ref = meeting_ref_from_mapping(meeting_ref)
        if normalized_ref.metadata.get("is_hub_schedule_row"):
            # The ref points at a multi-meeting listing page, not a detail
            # document. Fetching it would attach every document on the hub to
            # this one schedule row. Return the scoped row as-is instead.
            return self._normalized_from_ref(source, normalized_ref)
        return super().fetch_meeting(source, normalized_ref)

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
        if normalized_ref.metadata.get("is_hub_schedule_row"):
            # Direct parser callers receive the same safe result as
            # ``fetch_meeting``: no cross-meeting hub documents are attached.
            meeting.metadata["hub_documents_omitted"] = True
            return meeting
        meeting.source_url = url
        hub_url = str(
            (normalized_source.public_url if normalized_source else None)
            or normalized_ref.metadata.get("hub_url")
            or url
        )
        carried_documents = _metadata_documents(normalized_ref, hub_url)
        content_type = content_type_from_url(url)
        if content_type and content_type != "text/html":
            meeting.documents = _merge_documents_by_url(
                carried_documents,
                [
                    DocumentRef(
                        title=meeting.title or urlsplit(url).path.rsplit("/", 1)[-1],
                        url=url,
                        document_type=document_type_from_text(meeting.title, url),
                        content_type=content_type,
                        file_name=urlsplit(url).path.rsplit("/", 1)[-1],
                    ),
                ],
            )
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
            if not _allowed_meeting_document_link(link, url, text=text):
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
        meeting.documents = _merge_documents_by_url(carried_documents, documents)

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
