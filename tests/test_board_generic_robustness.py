from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import board.adapters.generic as generic_module
from board.adapters.generic import GenericBoardAdapter
from board.models import BoardSource


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class GenericBoardRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = GenericBoardAdapter()

    def test_hyphenated_board_meeting_path_marks_a_durable_hub(self):
        url = "https://district.example/school-board/board-meetings"
        content = fixture_bytes("generic_hyphenated_hub.html")

        detection = self.adapter.detect(url, content)

        self.assertTrue(detection.matched)
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(detection.metadata["dated_meeting_link_count"], 2)
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_vetted_external_documents_count_but_untrusted_and_private_links_do_not(self):
        url = "https://district.example/school-board/meetings"
        content = fixture_bytes("generic_external_documents.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(
            {meeting.url for meeting in meetings},
            {
                "https://drive.google.com/file/d/public-agenda/view",
                "https://resources.finalsite.net/images/v1/district/board-minutes-2026-09-09.pdf",
            },
        )
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(detection.metadata["dated_meeting_link_count"], 2)

        detail = self.adapter.parse_meeting_detail(content, url)
        self.assertEqual(
            {document.url for document in detail.documents},
            {
                "https://drive.google.com/file/d/public-agenda/view",
                "https://resources.finalsite.net/images/v1/district/board-minutes-2026-09-09.pdf",
            },
        )

    def test_repeated_dated_schedule_rows_are_a_durable_hub_without_links(self):
        url = "https://district.example/school-board/meeting-schedule"
        content = fixture_bytes("generic_schedule_rows.html")

        detection = self.adapter.detect(url, content)
        meetings = self.adapter.parse_meeting_list(content, url)

        self.assertEqual(len(meetings), 3)
        self.assertEqual(
            {meeting.meeting_date for meeting in meetings},
            {"2026-08-18", "2026-09-15", "2026-10-20"},
        )
        self.assertTrue(all(meeting.url == url for meeting in meetings))
        self.assertTrue(
            all(meeting.metadata["is_hub_schedule_row"] for meeting in meetings)
        )
        self.assertEqual(
            [meeting.external_meeting_id for meeting in meetings],
            [
                meeting.external_meeting_id
                for meeting in self.adapter.parse_meeting_list(content, url)
            ],
        )
        self.assertEqual(detection.metadata["dated_meeting_link_count"], 0)
        self.assertEqual(detection.metadata["dated_meeting_block_count"], 3)
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_one_off_news_page_remains_manual_even_with_multiple_meetings(self):
        url = "https://district.example/news/board-meeting-roundup"
        content = fixture_bytes("generic_one_off_multi_meeting_news.html")

        detection = self.adapter.detect(url, content)

        self.assertTrue(detection.matched)
        self.assertTrue(detection.metadata["one_off_content_url"])
        self.assertGreaterEqual(detection.metadata["dated_meeting_link_count"], 2)
        self.assertFalse(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "manual_review")

    def test_rejected_document_rows_cannot_make_an_empty_source_working(self):
        url = "https://district.example/school-board/meetings"
        content = fixture_bytes("generic_rejected_document_rows.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(meetings, [])
        self.assertEqual(detection.metadata["dated_meeting_link_count"], 0)
        self.assertEqual(detection.metadata["dated_meeting_block_count"], 0)
        self.assertFalse(detection.metadata["durable_meeting_hub"])
        self.assertNotEqual(self.adapter.parse_source(content, url).status, "working")

    def test_nested_date_blocks_count_as_one_logical_meeting(self):
        url = "https://district.example/school-board/meeting-schedule"
        content = fixture_bytes("generic_nested_duplicate_block.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].meeting_date, "2026-08-18")
        self.assertEqual(detection.metadata["dated_meeting_block_count"], 1)
        self.assertFalse(detection.metadata["durable_meeting_hub"])
        self.assertNotEqual(self.adapter.parse_source(content, url).status, "working")

    def test_undated_document_list_items_use_their_dated_article_context(self):
        url = "https://district.example/school-board/meetings"
        content = fixture_bytes("generic_dated_ancestor_documents.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(
            {meeting.meeting_date for meeting in meetings},
            {"2026-08-18", "2026-09-15"},
        )
        self.assertEqual(detection.metadata["dated_meeting_link_count"], 2)
        self.assertEqual(detection.metadata["dated_meeting_block_count"], 2)
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_occurrence_and_post_detail_routes_remain_manual_with_multiple_docs(self):
        content = fixture_bytes("generic_one_off_multi_meeting_news.html")
        urls = (
            "https://district.example/families/calendar/event/~occur-id/4192",
            "https://district.example/news/post-details/~board/posts/post/board-update",
        )

        for url in urls:
            with self.subTest(url=url):
                detection = self.adapter.detect(url, content)

                self.assertGreaterEqual(
                    detection.metadata["dated_meeting_link_count"],
                    2,
                )
                self.assertTrue(detection.metadata["one_off_content_url"])
                self.assertFalse(detection.metadata["durable_meeting_hub"])
                self.assertEqual(
                    self.adapter.parse_source(content, url).status,
                    "manual_review",
                )

    def test_durable_board_calendar_hub_is_not_treated_as_one_off(self):
        url = "https://district.example/school-board/calendar"
        content = fixture_bytes("generic_schedule_rows.html")

        detection = self.adapter.detect(url, content)

        self.assertFalse(detection.metadata["one_off_content_url"])
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_quick_link_context_does_not_contaminate_unrelated_anchors(self):
        url = "https://www.canby.k12.or.us/school-board/meetings"
        content = fixture_bytes("generic_quick_links_context.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(
            {meeting.url for meeting in meetings},
            {
                "https://drive.google.com/file/d/canby-may-18-agenda/view",
                "https://drive.google.com/file/d/canby-may-4-agenda/view",
            },
        )
        self.assertEqual(
            {meeting.meeting_date for meeting in meetings},
            {"2026-05-04", "2026-05-18"},
        )
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_synthetic_hub_row_never_inherits_unscoped_hub_documents(self):
        url = "https://district.example/school-board/meeting-schedule"
        content = fixture_bytes("generic_synthetic_mixed_hub.html")
        meeting_ref = self.adapter.parse_meeting_list(content, url)[0]
        source = BoardSource(platform="generic", public_url=url)

        parsed = self.adapter.parse_meeting_detail(
            content,
            url,
            source,
            meeting_ref,
        )

        self.assertTrue(meeting_ref.metadata["is_hub_schedule_row"])
        self.assertEqual(parsed.documents, [])
        self.assertTrue(parsed.metadata["hub_documents_omitted"])

        class NoFetchClient:
            def get(self, *_args, **_kwargs):
                raise AssertionError("synthetic hub rows must not be fetched")

        fetch_adapter = GenericBoardAdapter(client=NoFetchClient())
        fetched = fetch_adapter.fetch_meeting(source, meeting_ref)
        self.assertEqual(fetched.documents, [])
        self.assertTrue(fetched.metadata["hub_documents_omitted"])

    def test_schedule_row_identity_survives_a_later_agenda_link(self):
        url = "https://district.example/school-board/meetings"
        before = self.adapter.parse_meeting_list(
            fixture_bytes("generic_row_before_document.html"),
            url,
        )
        after = self.adapter.parse_meeting_list(
            fixture_bytes("generic_row_after_document.html"),
            url,
        )

        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(
            before[0].external_meeting_id,
            after[0].external_meeting_id,
        )
        self.assertTrue(before[0].metadata["is_hub_schedule_row"])
        self.assertFalse(after[0].metadata["is_hub_schedule_row"])
        self.assertEqual(
            after[0].agenda_url,
            "https://district.example/documents/2026-08-18-board-agenda.pdf",
        )

    def test_unqualified_row_identity_survives_later_typed_agenda_anchor(self):
        url = "https://district.example/school-board/meetings"
        before_content = b"""
            <html><head><title>School Board Meetings</title></head><body>
              <article>August 18, 2026 School Board Meeting</article>
            </body></html>
        """
        before = self.adapter.parse_meeting_list(before_content, url)
        self.assertEqual(len(before), 1)
        self.assertIsNone(before[0].agenda_url)

        for slug, label in (
            ("regular", "Regular School Board Meeting Agenda"),
            ("work-session", "Work Session School Board Meeting Agenda"),
            ("special", "Special School Board Meeting Agenda"),
        ):
            with self.subTest(label=label):
                after_content = f"""
                    <html><head><title>School Board Meetings</title></head><body>
                      <article>August 18, 2026 School Board Meeting
                        <a href="/docs/{slug}-agenda.pdf">{label}</a>
                      </article>
                    </body></html>
                """.encode()
                after = self.adapter.parse_meeting_list(after_content, url)

                self.assertEqual(len(after), 1)
                self.assertEqual(
                    before[0].external_meeting_id,
                    after[0].external_meeting_id,
                )
                self.assertEqual(
                    after[0].agenda_url,
                    f"https://district.example/docs/{slug}-agenda.pdf",
                )

    def test_row_identity_ignores_later_time_status_and_location_enrichment(self):
        url = "https://district.example/school-board/meetings"
        snapshots = (
            "August 18, 2026 School Board Meeting",
            "August 18, 2026 School Board Meeting - 6:00 PM",
            (
                "CANCELLED - August 18, 2026 School Board Meeting - 6:00 PM, "
                "District Office"
            ),
        )

        meetings = []
        for row_text in snapshots:
            content = (
                "<html><head><title>School Board Meetings</title></head><body>"
                f"<article>{row_text}</article>"
                "</body></html>"
            ).encode()
            parsed = self.adapter.parse_meeting_list(content, url)
            self.assertEqual(len(parsed), 1)
            meetings.append(parsed[0])

        self.assertEqual(
            len({meeting.external_meeting_id for meeting in meetings}),
            1,
        )
        self.assertIsNone(meetings[0].meeting_start_time)
        self.assertEqual(meetings[1].meeting_start_time, "18:00:00")
        self.assertTrue(meetings[2].is_cancelled)

    def test_concise_rows_require_an_explicit_board_hub(self):
        content = fixture_bytes("generic_concise_schedule_rows.html")
        board_url = "https://district.example/school-board/calendar"

        meetings = self.adapter.parse_meeting_list(content, board_url)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(self.adapter.parse_source(content, board_url).status, "working")

        unmarked = content.replace(b"School Board", b"District Event")
        self.assertEqual(
            self.adapter.parse_meeting_list(
                unmarked,
                "https://district.example/calendar",
            ),
            [],
        )

    def test_working_generic_fixtures_always_emit_a_meeting(self):
        url = "https://district.example/school-board/meetings"
        for fixture_path in sorted(FIXTURE_DIR.glob("generic_*.html")):
            with self.subTest(fixture=fixture_path.name):
                content = fixture_path.read_bytes()
                source = self.adapter.parse_source(content, url)
                meetings = self.adapter.parse_meeting_list(content, url)
                if source.status == "working":
                    self.assertGreaterEqual(len(meetings), 1)

    def test_multiple_documents_enrich_one_dated_meeting(self):
        url = "https://district.example/board/meetings"
        content = fixture_bytes("generic_board_page.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 1)
        self.assertIsNotNone(meetings[0].agenda_url)
        self.assertIsNotNone(meetings[0].minutes_url)
        self.assertIsNotNone(meetings[0].packet_url)
        self.assertEqual(meetings[0].metadata["vetted_document_link_count"], 3)
        self.assertEqual(detection.metadata["vetted_document_link_count"], 3)
        self.assertTrue(detection.metadata["durable_meeting_hub"])
        self.assertEqual(self.adapter.parse_source(content, url).status, "working")

    def test_all_consolidated_documents_are_materialized_for_download(self):
        hub_url = "https://district.example/school-board/meetings"
        content = fixture_bytes("generic_consolidated_documents.html")
        meeting_ref = self.adapter.parse_meeting_list(content, hub_url)[0]
        source = BoardSource(platform="generic", public_url=hub_url)

        meeting = self.adapter.parse_meeting_detail(
            b"%PDF fixture",
            meeting_ref.agenda_url,
            source,
            meeting_ref,
        )

        self.assertEqual(meeting_ref.metadata["vetted_document_link_count"], 3)
        self.assertEqual(
            {document.url for document in meeting.documents},
            {
                "https://district.example/documents/2026-08-18-agenda.pdf",
                "https://district.example/documents/2026-08-18-minutes.pdf",
                "https://district.example/documents/2026-08-18-budget.pdf",
            },
        )

    def test_responsive_duplicate_links_count_as_one_vetted_document(self):
        url = "https://district.example/school-board/meetings"
        content = fixture_bytes("generic_responsive_duplicate_link.html")

        meetings = self.adapter.parse_meeting_list(content, url)
        detection = self.adapter.detect(url, content)

        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].metadata["vetted_document_link_count"], 1)
        self.assertEqual(
            meetings[0].agenda_url,
            "https://district.example/documents/2026-08-18-meeting-document.pdf",
        )
        self.assertEqual(detection.metadata["vetted_document_link_count"], 1)
        self.assertFalse(detection.metadata["durable_meeting_hub"])
        self.assertNotEqual(self.adapter.parse_source(content, url).status, "working")

    def test_dated_block_processing_is_capped(self):
        rows = "".join(
            f"<li>January 1, 2026 Regular School Board Meeting Session {index}</li>"
            for index in range(750)
        )
        content = (
            "<html><head><title>School Board Meetings</title></head>"
            f"<body><h1>School Board Meetings</h1><ul>{rows}</ul></body></html>"
        )

        meetings = self.adapter.parse_meeting_list(
            content,
            "https://district.example/school-board/meetings",
        )

        self.assertEqual(len(meetings), 500)

    def test_dated_block_anchor_processing_is_strictly_capped(self):
        anchors = "".join(
            f'<a href="/navigation/{index}">Navigation {index}</a>'
            for index in range(25_000)
        )
        content = (
            "<html><head><title>School Board Meetings</title></head><body>"
            f"<article>August 18, 2026 School Board Meeting {anchors}</article>"
            "</body></html>"
        ).encode()

        with patch(
            "board.adapters.generic._allowed_meeting_document_link",
            wraps=generic_module._allowed_meeting_document_link,
        ) as inspected_link:
            meetings = self.adapter.parse_meeting_list(
                content,
                "https://district.example/school-board/meetings",
            )

        self.assertEqual(meetings, [])
        self.assertEqual(
            inspected_link.call_count,
            generic_module._MAX_LINKS_PER_DATED_BLOCK,
        )
        self.assertLessEqual(
            inspected_link.call_count,
            generic_module._MAX_TOTAL_DATED_BLOCK_LINKS,
        )

    def test_same_site_meeting_detail_cards_emit_scoped_refs(self):
        url = "https://district.example/school-board/meetings"
        content = b"""
            <html><head><title>School Board Meetings</title></head><body>
              <h1>School Board Meetings</h1>
              <article>August 18, 2026 Regular School Board Meeting
                <a href="/meetings/august-18.html">Details</a></article>
              <article>September 15, 2026 Regular School Board Meeting
                <a href="/meetings/september-15.html">View details</a></article>
            </body></html>
        """

        meetings = self.adapter.parse_meeting_list(content, url)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(
            {meeting.url for meeting in meetings},
            {
                "https://district.example/meetings/august-18.html",
                "https://district.example/meetings/september-15.html",
            },
        )
        self.assertTrue(
            all(meeting.metadata["is_scoped_detail_page"] for meeting in meetings)
        )
        self.assertTrue(self.adapter.detect(url, content).metadata["durable_meeting_hub"])

    def test_same_day_meeting_types_do_not_collapse(self):
        url = "https://district.example/school-board/meetings"
        content = b"""
            <html><head><title>School Board Meetings</title></head><body>
              <h1>School Board Meetings</h1>
              <article>August 18, 2026
                <a href="/docs/regular-agenda.pdf">Regular School Board Meeting Agenda</a>
              </article>
              <article>August 18, 2026
                <a href="/docs/special-agenda.pdf">Special School Board Meeting Agenda</a>
              </article>
            </body></html>
        """

        meetings = self.adapter.parse_meeting_list(content, url)

        self.assertEqual(len(meetings), 2)
        self.assertEqual(len({meeting.external_meeting_id for meeting in meetings}), 2)
        self.assertEqual(
            {meeting.agenda_url for meeting in meetings},
            {
                "https://district.example/docs/regular-agenda.pdf",
                "https://district.example/docs/special-agenda.pdf",
            },
        )

    def test_row_scoped_video_survives_primary_document_fetch(self):
        hub_url = "https://district.example/board/meetings"
        content = fixture_bytes("generic_board_page.html")
        meeting_ref = self.adapter.parse_meeting_list(content, hub_url)[0]
        source = BoardSource(platform="generic", public_url=hub_url)

        meeting = self.adapter.parse_meeting_detail(
            b"%PDF fixture",
            meeting_ref.agenda_url,
            source,
            meeting_ref,
        )

        self.assertEqual(
            meeting_ref.video_url,
            "https://www.youtube.com/watch?v=meeting-2026-08-10",
        )
        self.assertEqual(meeting.video_url, meeting_ref.video_url)


if __name__ == "__main__":
    unittest.main()
