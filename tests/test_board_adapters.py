from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from board.adapters import detect_platform
from board.adapters.base import RenderedPage, assess_challenge
from board.adapters.boardbook import BoardBookAdapter, resolve_boardbook_document_url
from board.adapters.boarddocs import BoardDocsAdapter
from board.adapters.civicclerk import CivicClerkAdapter
from board.adapters.diligent_community import DiligentCommunityAdapter
from board.adapters.generic import GenericBoardAdapter
from board.adapters.simbli import SimbliAdapter
from board.discovery import (
    BoardSourceCandidate,
    _outcome_from_adapter_result,
    board_url_allowed,
    discover_board_source,
    extract_board_candidates,
)
from board.http import HTTPResult, RobotsDenied
from board.models import (
    BoardSource,
    BoardSourceResult,
    DetectionResult,
    MeetingRef,
    NormalizedMeeting,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class FakeDiscoveryClient:
    def __init__(self, response: HTTPResult | BaseException):
        self.response = response
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeBoardBookRecoveryAdapter:
    platform_name = "boardbook"

    def __init__(self):
        self.render_calls: list[str] = []
        self.discover_calls: list[str] = []

    def detect(self, url: str, _html=None) -> DetectionResult:
        matched = "meetings.boardbook.org/public/organization/2413" in url.casefold()
        return DetectionResult(
            matched,
            self.platform_name,
            0.99 if matched else 0.0,
            canonical_url=url if matched else None,
        )

    def render_page(self, url: str) -> bytes:
        self.render_calls.append(url)
        return (
            b'<html><title>Bend-La Pine Schools</title><body>'
            b'<a href="https://meetings.boardbook.org/public/Organization/2413">'
            b'BoardBook agendas and minutes</a></body></html>'
        )

    def discover_source(self, district, candidate_url: str, html=None) -> BoardSourceResult:
        del html
        self.discover_calls.append(candidate_url)
        detection = self.detect(candidate_url)
        source = BoardSource(
            platform="boardbook",
            public_url="https://meetings.boardbook.org/Public/Organization/2413",
            external_source_id="2413",
            district_id=int(district["id"]),
            organization_name=district["agency_name"],
            status="working",
        )
        return BoardSourceResult(
            detection=detection,
            source=source,
            status="working",
            candidate_url=candidate_url,
        )


class BoardDiscoveryFixtureTests(unittest.TestCase):
    def test_challenge_classifier_ignores_incidental_access_denied_text(self):
        ordinary = assess_challenge(
            200,
            b"<html><title>District news</title><body>How to resolve an access denied error.</body></html>",
            "https://district.example/",
        )
        challenged = assess_challenge(
            200,
            b"<html><title>Attention Required</title><body>Cloudflare Ray ID: fixture</body></html>",
            "https://district.example/",
        )
        rate_limited = assess_challenge(429, b"Too many requests")

        self.assertFalse(ordinary.is_challenge)
        self.assertTrue(challenged.is_challenge)
        self.assertEqual(challenged.marker, "attention required")
        self.assertTrue(challenged.browser_retry_allowed)
        self.assertTrue(rate_limited.is_challenge)
        self.assertEqual(rate_limited.category, "rate_limited")
        self.assertFalse(rate_limited.browser_retry_allowed)

    def test_challenge_classifier_accepts_only_constrained_short_headings(self):
        challenged = assess_challenge(
            200,
            b"<html><body><h1>Access Denied</h1><p>Request ID 123</p></body></html>",
        )
        ordinary = assess_challenge(
            200,
            (
                b"<html><title>Technology help</title><body><h1>Access Denied</h1>"
                + b"<p>District troubleshooting guidance.</p>" * 200
                + b"</body></html>"
            ),
        )

        self.assertTrue(challenged.is_challenge)
        self.assertEqual(challenged.category, "challenge_heading")
        self.assertFalse(ordinary.is_challenge)

    def test_listing_sync_recovers_401_and_403_once_and_preserves_final_url(self):
        requested_url = "https://district.example/board"
        challenged_url = "https://district.example/board/challenge"
        recovered_url = "https://district.example/board/meetings"
        source = BoardSource(
            platform="generic",
            public_url=requested_url,
            requires_javascript=True,
        )
        for status_code in (401, 403):
            with self.subTest(status_code=status_code):
                adapter = GenericBoardAdapter(allow_browser_fallback=True)
                adapter.fetch_url = Mock(
                    return_value=HTTPResult(
                        requested_url,
                        challenged_url,
                        status_code,
                        {},
                        b"Access denied",
                    )
                )
                adapter.render_page_with_metadata = Mock(
                    return_value=RenderedPage(
                        b"<html><body>No meetings yet</body></html>",
                        recovered_url,
                        200,
                        browser_rendered=True,
                    )
                )
                adapter.parse_meeting_list = Mock(return_value=[])

                self.assertEqual(adapter.list_meetings(source), [])

                adapter.fetch_url.assert_called_once_with(
                    requested_url,
                    raise_for_status=False,
                )
                adapter.render_page_with_metadata.assert_called_once_with(challenged_url)
                parsed_args = adapter.parse_meeting_list.call_args.args
                self.assertEqual(parsed_args[1], recovered_url)
                # An empty JS-backed result must not start a second browser
                # after challenge recovery already rendered the page.
                self.assertEqual(adapter.render_page_with_metadata.call_count, 1)

    def test_meeting_sync_recovers_structural_200_challenge_once(self):
        requested_url = "https://district.example/board/agenda/7"
        challenged_url = "https://district.example/challenge/agenda/7"
        recovered_url = "https://district.example/board/agenda/7?rendered=1"
        source = BoardSource(
            platform="generic",
            public_url="https://district.example/board",
            requires_javascript=True,
        )
        meeting_ref = MeetingRef("7", requested_url, agenda_url=requested_url)
        normalized = NormalizedMeeting("7", recovered_url)
        adapter = GenericBoardAdapter(allow_browser_fallback=True)
        adapter.fetch_url = Mock(
            return_value=HTTPResult(
                requested_url,
                challenged_url,
                200,
                {},
                b"<html><body><h1>Access Denied</h1></body></html>",
            )
        )
        adapter.render_page_with_metadata = Mock(
            return_value=RenderedPage(
                b"<html><body>Rendered agenda</body></html>",
                recovered_url,
                200,
                browser_rendered=True,
            )
        )
        adapter.parse_meeting_detail = Mock(return_value=normalized)

        self.assertIs(adapter.fetch_meeting(source, meeting_ref), normalized)

        adapter.render_page_with_metadata.assert_called_once_with(challenged_url)
        parsed_args = adapter.parse_meeting_detail.call_args.args
        self.assertEqual(parsed_args[1], recovered_url)
        self.assertEqual(adapter.render_page_with_metadata.call_count, 1)

    def test_sync_never_browser_retries_429_or_robots_denial(self):
        url = "https://district.example/board"
        source = BoardSource(platform="generic", public_url=url)

        rate_limited = GenericBoardAdapter(allow_browser_fallback=True)
        rate_limited.fetch_url = Mock(
            return_value=HTTPResult(url, url, 429, {}, b"Too many requests")
        )
        rate_limited.render_page_with_metadata = Mock()
        with self.assertRaises(requests.HTTPError):
            rate_limited.list_meetings(source)
        rate_limited.render_page_with_metadata.assert_not_called()

        denied = GenericBoardAdapter(allow_browser_fallback=True)
        denied.fetch_url = Mock(side_effect=RobotsDenied("robots.txt disallows fixture"))
        denied.render_page_with_metadata = Mock()
        with self.assertRaises(RobotsDenied):
            denied.list_meetings(source)
        denied.render_page_with_metadata.assert_not_called()

    def test_non_javascript_adapter_recovers_challenge_with_browser_once(self):
        url = "https://district.example/board/meetings"
        adapter = GenericBoardAdapter(allow_browser_fallback=True)
        adapter.fetch_url = Mock(
            return_value=HTTPResult(url, url, 403, {}, b"Access denied")
        )
        adapter.render_page = Mock(return_value=fixture_bytes("generic_board_page.html"))

        result = adapter.discover_source({"id": 7, "agency_name": "Example Schools"}, url)

        self.assertEqual(result.status, "working")
        self.assertIsNotNone(result.source)
        adapter.render_page.assert_called_once_with(url)

    def test_adapter_does_not_render_for_rate_limit_or_robots_denial(self):
        url = "https://district.example/board/meetings"
        rate_limited = GenericBoardAdapter(allow_browser_fallback=True)
        rate_limited.fetch_url = Mock(
            return_value=HTTPResult(url, url, 429, {}, b"Too many requests")
        )
        rate_limited.render_page = Mock(return_value=fixture_bytes("generic_board_page.html"))

        result = rate_limited.discover_source(
            {"id": 7, "agency_name": "Example Schools"},
            url,
        )
        self.assertEqual(result.status, "blocked_by_challenge")
        rate_limited.render_page.assert_not_called()

        denied = GenericBoardAdapter(allow_browser_fallback=True)
        denied.fetch_url = Mock(side_effect=RobotsDenied("robots.txt disallows fixture"))
        denied.render_page = Mock(return_value=fixture_bytes("generic_board_page.html"))
        result = denied.discover_source(
            {"id": 7, "agency_name": "Example Schools"},
            url,
        )
        self.assertEqual(result.status, "blocked_by_robots")
        denied.render_page.assert_not_called()

    def test_district_discovery_recovers_403_with_one_browser_render(self):
        district_url = "https://www.bend.k12.or.us/"
        client = FakeDiscoveryClient(
            HTTPResult(district_url, district_url, 403, {}, b"Access denied")
        )
        adapter = FakeBoardBookRecoveryAdapter()
        district = {
            "id": 1445,
            "agency_name": "BEND-LAPINE ADMINISTRATIVE SD 1",
            "website": district_url,
            "website_normalized": district_url,
        }

        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(district, client=client)

        self.assertEqual(outcome.status, "working")
        self.assertEqual(outcome.platform, "boardbook")
        self.assertEqual(outcome.organization_external_id, "2413")
        self.assertEqual(adapter.render_calls, [district_url])
        self.assertEqual(len(outcome.raw["challenge_recoveries"]), 1)
        self.assertTrue(
            outcome.raw["challenge_recoveries"][0]["browser_fallback_attempted"]
        )

    def test_direct_challenged_platform_source_is_rendered_only_once(self):
        source_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeDiscoveryClient(
            HTTPResult(source_url, source_url, 403, {}, b"Access denied")
        )
        adapter = FakeBoardBookRecoveryAdapter()
        district = {
            "id": 1445,
            "agency_name": "BEND-LAPINE ADMINISTRATIVE SD 1",
            "website": source_url,
            "website_normalized": source_url,
        }

        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(district, client=client)

        self.assertEqual(outcome.status, "working")
        self.assertEqual(adapter.render_calls, [source_url])
        self.assertEqual(adapter.discover_calls, [source_url])

    def test_challenged_redirect_keeps_the_final_platform_and_source_url(self):
        district_url = "https://district.example/board"
        source_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeDiscoveryClient(
            HTTPResult(district_url, source_url, 403, {}, b"Access denied")
        )
        adapter = BoardBookAdapter(client, allow_browser_fallback=False)
        district = {
            "id": 1445,
            "agency_name": "BEND-LAPINE ADMINISTRATIVE SD 1",
            "website": district_url,
            "website_normalized": district_url,
        }

        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(
                district,
                client=client,
                allow_browser_fallback=False,
                max_pages=1,
            )

        self.assertEqual(outcome.status, "blocked_by_challenge")
        self.assertEqual(outcome.platform, "boardbook")
        self.assertEqual(outcome.source_url, source_url)

    def test_district_discovery_does_not_render_429_or_robots_denial(self):
        district_url = "https://district.example/"
        district = {
            "id": 9,
            "agency_name": "Example Schools",
            "website": district_url,
            "website_normalized": district_url,
        }

        adapter = FakeBoardBookRecoveryAdapter()
        rate_limited = FakeDiscoveryClient(
            HTTPResult(district_url, district_url, 429, {}, b"Too many requests")
        )
        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(district, client=rate_limited)
        self.assertEqual(outcome.status, "blocked_by_challenge")
        self.assertEqual(adapter.render_calls, [])
        self.assertEqual(outcome.raw["challenges"][0]["category"], "rate_limited")

        denied_adapter = FakeBoardBookRecoveryAdapter()
        robots_denied = FakeDiscoveryClient(
            RobotsDenied("robots.txt disallows https://district.example/")
        )
        with patch("board.discovery.build_adapters", return_value=[denied_adapter]):
            outcome = discover_board_source(district, client=robots_denied)
        self.assertEqual(outcome.status, "blocked_by_robots")
        self.assertEqual(denied_adapter.render_calls, [])

    def test_adapter_confidence_is_normalized_without_rescaling_candidate_scores(self):
        candidate = BoardSourceCandidate(
            url="https://meetings.boardbook.org/Public/Organization/2221",
            text="Board meetings",
            discovered_from_url="https://district.example/board",
            score=73,
            known_platform="boardbook",
        )
        source = BoardSource(
            platform="boardbook",
            public_url=candidate.url,
            external_source_id="2221",
        )

        detected = _outcome_from_adapter_result(
            BoardSourceResult(
                detection=DetectionResult(True, "boardbook", 0.99),
                source=source,
            ),
            candidate,
        )
        candidate_only = _outcome_from_adapter_result(
            BoardSourceResult(
                detection=DetectionResult(False, "boardbook", 0.0),
                source=None,
                status="manual_review",
            ),
            candidate,
        )

        self.assertEqual(detected.confidence, 99)
        self.assertEqual(candidate_only.confidence, 73)

    def test_known_vendor_links_are_allowed_and_unrelated_external_link_is_rejected(self):
        district_url = "https://district.example/"
        known_urls = (
            "https://meetings.boardbook.org/Public/Organization/2221",
            "https://go.boarddocs.com/or/example/Board.nsf/Public",
            "https://rsd407.community.diligentoneplatform.com/Portal/",
            "https://simbli.eboardsolutions.com/Index.aspx?S=36030961",
            "https://usbe.portal.civicclerk.com/",
        )
        for url in known_urls:
            with self.subTest(url=url):
                self.assertTrue(board_url_allowed(url, district_url))

        self.assertFalse(
            board_url_allowed("https://unrelated.example.net/board-meetings", district_url)
        )
        self.assertFalse(board_url_allowed("javascript:alert(1)", district_url))

    def test_discovery_extracts_each_vendor_but_not_unrelated_external_host(self):
        candidates = extract_board_candidates(
            fixture_bytes("discovery_links.html"),
            "https://district.example/board",
            "https://district.example/",
        )
        urls = {candidate.url for candidate in candidates}
        platforms = {candidate.known_platform for candidate in candidates}

        self.assertIn("https://meetings.boardbook.org/Public/Organization/2221", urls)
        self.assertIn("https://go.boarddocs.com/or/example/Board.nsf/Public", urls)
        self.assertIn("https://rsd407.community.diligentoneplatform.com/Portal", urls)
        self.assertIn("https://simbli.eboardsolutions.com/Index.aspx?S=36030961", urls)
        self.assertIn("https://usbe.portal.civicclerk.com", urls)
        self.assertNotIn("https://unrelated.example.net/board-meetings", urls)
        self.assertTrue(
            {"boardbook", "boarddocs", "diligent_community", "simbli", "civicclerk"}
            <= platforms
        )

    def test_registry_and_generic_detection_use_deterministic_markers(self):
        cases = (
            (
                "boardbook",
                "https://meetings.boardbook.org/Public/Organization/2221",
                "boardbook_organization.html",
            ),
            (
                "diligent_community",
                "https://rsd407.community.diligentoneplatform.com/Portal/",
                "diligent_meetings.json",
            ),
            (
                "boarddocs",
                "https://go.boarddocs.com/or/example/Board.nsf/Public",
                "boarddocs_public.html",
            ),
            (
                "simbli",
                "https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=36030961",
                "simbli_listing.html",
            ),
            (
                "civicclerk",
                "https://usbe.portal.civicclerk.com/",
                "civicclerk_events.json",
            ),
            (
                "generic",
                "https://district.example/board/meetings",
                "generic_board_page.html",
            ),
        )
        for expected_platform, url, fixture_name in cases:
            with self.subTest(platform=expected_platform):
                detection = detect_platform(url, fixture_bytes(fixture_name))
                self.assertTrue(detection.matched)
                self.assertEqual(detection.platform, expected_platform)
                self.assertGreater(detection.confidence, 0)

        unrelated = detect_platform(
            "https://unrelated.example.net/products",
            b"<html><title>Unrelated product catalog</title></html>",
        )
        self.assertFalse(unrelated.matched)


class BoardBookFixtureTests(unittest.TestCase):
    listing_url = "https://meetings.boardbook.org/Public/Organization/2221"

    def setUp(self):
        self.adapter = BoardBookAdapter()
        self.source = BoardSource(
            platform="boardbook",
            public_url=self.listing_url,
            external_source_id="2221",
            organization_name="Example Public Schools",
        )

    def test_parses_listing_metadata_minutes_video_and_since_filter(self):
        meetings = self.adapter.parse_meeting_list(
            fixture_bytes("boardbook_organization.html"),
            self.listing_url,
            self.source,
            since="2026-01-01",
        )

        self.assertEqual(len(meetings), 1)
        meeting = meetings[0]
        self.assertEqual(meeting.external_meeting_id, "742314")
        self.assertEqual(meeting.title, "Regular Meeting")
        self.assertEqual(meeting.meeting_date, "2026-08-24")
        self.assertEqual(meeting.meeting_start_time, "18:00:00")
        self.assertEqual(meeting.meeting_type, "Regular")
        self.assertEqual(
            meeting.location,
            "District Office Board Room, 100 School Street, Example, Oregon 97000",
        )
        self.assertEqual(
            meeting.agenda_url,
            "https://meetings.boardbook.org/Public/Agenda/2221?meeting=742314",
        )
        self.assertEqual(
            meeting.minutes_url,
            "https://meetings.boardbook.org/Public/Minutes/2221?meeting=742314",
        )
        self.assertEqual(meeting.video_url, "https://video.example.test/watch/742314")
        self.assertEqual(
            meeting.metadata["public_notice_url"],
            "https://meetings.boardbook.org/Public/Notice/2221?meeting=742314",
        )

    def test_parses_detail_hierarchy_descriptions_and_direct_attachments(self):
        meeting_ref = self.adapter.parse_meeting_list(
            fixture_bytes("boardbook_organization.html"),
            self.listing_url,
            self.source,
        )[0]
        meeting = self.adapter.parse_meeting_detail(
            fixture_bytes("boardbook_agenda.html"),
            meeting_ref.agenda_url,
            self.source,
            meeting_ref,
        )

        self.assertEqual(meeting.external_meeting_id, "742314")
        self.assertEqual(meeting.minutes_url, meeting_ref.minutes_url)
        self.assertEqual(meeting.video_url, meeting_ref.video_url)
        self.assertEqual(
            meeting.public_notice_url,
            "https://meetings.boardbook.org/Public/Notice/2221?meeting=742314",
        )
        self.assertEqual(len(meeting.agenda_items), 4)

        by_id = {item.external_item_id: item for item in meeting.agenda_items}
        self.assertEqual((by_id["100"].parent_external_item_id, by_id["100"].depth), (None, 0))
        self.assertEqual((by_id["101"].parent_external_item_id, by_id["101"].depth), ("100", 1))
        self.assertEqual((by_id["102"].parent_external_item_id, by_id["102"].depth), ("100", 1))
        self.assertEqual((by_id["103"].parent_external_item_id, by_id["103"].depth), ("102", 2))
        self.assertEqual(by_id["103"].item_number, "I.B.1.")
        self.assertEqual(by_id["103"].description, "Approve the public service contracts.")

        attachment = by_id["101"].documents[0]
        self.assertEqual(attachment.external_document_id, "2381887")
        self.assertEqual(attachment.agenda_item_external_id, "101")
        self.assertEqual(attachment.content_type, "application/pdf")
        self.assertEqual(
            attachment.url,
            "https://meetings.boardbook.org/Documents/DownloadPDF/2381887?org=2221",
        )
        self.assertEqual(meeting.metadata["attachment_count"], 1)

    def test_resolves_viewer_and_minutes_pages_to_public_pdf_urls(self):
        self.assertEqual(
            resolve_boardbook_document_url(
                "https://meetings.boardbook.org/Documents/FileViewerOrPublic/2221?file=2381887"
            ),
            "https://meetings.boardbook.org/Documents/DownloadPDF/2381887?org=2221",
        )
        minutes = self.adapter.parse_minutes_document(
            fixture_bytes("boardbook_minutes.html"),
            "https://meetings.boardbook.org/Public/Minutes/2221?meeting=742314",
        )
        self.assertIsNotNone(minutes)
        self.assertEqual(minutes.external_document_id, "2381999")
        self.assertEqual(minutes.document_type, "minutes")
        self.assertEqual(
            minutes.url,
            "https://meetings.boardbook.org/Documents/DownloadPDF/2381999?org=2221",
        )


class OtherPlatformFixtureTests(unittest.TestCase):
    def test_diligent_parses_public_json_listing_and_documents(self):
        adapter = DiligentCommunityAdapter()
        source = BoardSource(
            platform="diligent_community",
            public_url="https://rsd407.community.diligentoneplatform.com/Portal/",
            external_source_id="rsd407",
            metadata={
                "api_base": "https://rsd407.community.diligentoneplatform.com/Services/MeetingsService.svc"
            },
        )
        meetings = adapter.parse_meeting_list(
            fixture_bytes("diligent_meetings.json"),
            f"{source.metadata['api_base']}/meetings",
            source,
        )
        self.assertEqual([meeting.external_meeting_id for meeting in meetings], ["128", "129"])
        self.assertEqual(meetings[0].meeting_date, "2026-08-25")
        self.assertEqual(meetings[0].meeting_start_time, "18:00:00")
        self.assertEqual(meetings[0].location, "Educational Service Center - Board Room")
        self.assertTrue(meetings[0].metadata["published"])

        detail = adapter.parse_meeting_detail(
            fixture_bytes("diligent_meeting_documents.json"),
            f"{source.metadata['api_base']}/meetings/128/meetingDocuments",
            source,
            meetings[0],
        )
        self.assertEqual(detail.platform, "diligent_community")
        self.assertEqual(detail.agenda_items[0].external_item_id, "agenda-1")
        self.assertEqual({document.external_document_id for document in detail.documents}, {
            "6001", "6002", "6003", "ATTACHMENT-1"
        })

    def test_boarddocs_parses_rendered_listing_agenda_and_attachment(self):
        adapter = BoardDocsAdapter()
        source = BoardSource(
            platform="boarddocs",
            public_url="https://go.boarddocs.com/or/example/Board.nsf/Public",
            external_source_id="or/example",
            requires_javascript=True,
        )
        content = fixture_bytes("boarddocs_public.html")
        detection = adapter.detect(source.public_url, content)
        self.assertTrue(detection.matched)
        self.assertTrue(detection.requires_javascript)
        meetings = adapter.parse_meeting_list(content, source.public_url, source)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].external_meeting_id, "2A952AF932AD38E185258CF400744B01")
        self.assertEqual(meetings[0].meeting_date, "2026-08-25")

        detail = adapter.parse_meeting_detail(content, meetings[0].url, source, meetings[0])
        self.assertEqual(len(detail.agenda_items), 2)
        self.assertEqual(detail.agenda_items[1].parent_external_item_id, "CAT-UNID-1")
        self.assertEqual(detail.agenda_items[1].documents[0].title, "Opening Memo")
        self.assertIn("/Board.nsf/pfiles/FILE-1/$file/", detail.documents[0].url)

    def test_simbli_parses_onclick_ids_rendered_hierarchy_and_supporting_document(self):
        adapter = SimbliAdapter()
        source = BoardSource(
            platform="simbli",
            public_url=(
                "https://simbli.eboardsolutions.com/SB_Meetings/"
                "SB_MeetingListing.aspx?S=36030961"
            ),
            external_source_id="36030961",
            requires_javascript=True,
        )
        meetings = adapter.parse_meeting_list(
            fixture_bytes("simbli_listing.html"), source.public_url, source
        )
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].external_meeting_id, "25723")
        self.assertEqual(meetings[0].meeting_date, "2026-08-17")
        self.assertEqual(meetings[0].metadata["site_id"], "36030961")
        self.assertEqual(
            meetings[0].url,
            "https://simbli.eboardsolutions.com/SB_Meetings/ViewMeeting.aspx?MID=25723&S=36030961",
        )

        detail = adapter.parse_meeting_detail(
            fixture_bytes("simbli_detail.html"), meetings[0].url, source, meetings[0]
        )
        by_id = {item.external_item_id: item for item in detail.agenda_items}
        self.assertEqual((by_id["ROOT-1"].parent_external_item_id, by_id["ROOT-1"].depth), (None, 0))
        self.assertEqual((by_id["CHILD-1"].parent_external_item_id, by_id["CHILD-1"].depth), ("ROOT-1", 1))
        self.assertEqual(by_id["CHILD-1"].documents[0].external_document_id, "601320")
        self.assertIn("AID=601320", by_id["CHILD-1"].documents[0].url)

    def test_civicclerk_parses_odata_listing_and_recursive_meeting_items(self):
        adapter = CivicClerkAdapter()
        source = BoardSource(
            platform="civicclerk",
            public_url="https://usbe.portal.civicclerk.com/",
            external_source_id="usbe",
            metadata={"api_base": "https://usbe.api.civicclerk.com/v1"},
        )
        meetings = adapter.parse_meeting_list(
            fixture_bytes("civicclerk_events.json"),
            "https://usbe.api.civicclerk.com/v1/Events",
            source,
        )
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].external_meeting_id, "496")
        self.assertEqual(meetings[0].meeting_date, "2026-01-07")
        self.assertEqual(meetings[0].metadata["agenda_id"], 343)
        self.assertEqual(meetings[0].video_url, "https://video.example.test/usbe/496")

        detail = adapter.parse_meeting_detail(
            fixture_bytes("civicclerk_meeting.json"),
            "https://usbe.api.civicclerk.com/v1/Meetings/343",
            source,
            meetings[0],
        )
        by_id = {item.external_item_id: item for item in detail.agenda_items}
        self.assertEqual((by_id["11843"].parent_external_item_id, by_id["11843"].depth), (None, 0))
        self.assertEqual((by_id["11858"].parent_external_item_id, by_id["11858"].depth), ("11843", 1))
        self.assertEqual(by_id["11858"].documents[0].external_document_id, "25442")
        self.assertEqual(
            by_id["11858"].documents[0].url,
            "https://usbe.api.civicclerk.com/v1/Meetings/GetAttachmentFile(fileId=25442)",
        )

    def test_generic_adapter_conservatively_parses_obvious_public_documents(self):
        adapter = GenericBoardAdapter()
        url = "https://district.example/board/meetings"
        content = fixture_bytes("generic_board_page.html")
        detection = adapter.detect(url, content)
        self.assertTrue(detection.matched)
        meetings = adapter.parse_meeting_list(content, url)
        self.assertEqual(len(meetings), 3)
        self.assertTrue(all(meeting.meeting_date == "2026-08-10" for meeting in meetings))
        self.assertTrue(all(meeting.meeting_start_time == "18:30:00" for meeting in meetings))
        self.assertEqual(sum(meeting.agenda_url is not None for meeting in meetings), 1)
        self.assertEqual(sum(meeting.minutes_url is not None for meeting in meetings), 1)
        self.assertEqual(sum(meeting.packet_url is not None for meeting in meetings), 1)

        detail = adapter.parse_meeting_detail(content, url)
        self.assertEqual(
            {document.document_type for document in detail.documents},
            {"agenda", "minutes", "packet"},
        )
        self.assertEqual(
            detail.video_url,
            "https://www.youtube.com/watch?v=meeting-2026-08-10",
        )


if __name__ == "__main__":
    unittest.main()
