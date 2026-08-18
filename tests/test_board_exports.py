from __future__ import annotations

import csv
import io
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from flask import Flask

import common
from board.documents import store_board_document
from board.exports import (
    DISCOVERY_LEDGER_FIELDS,
    MEETING_FIELDS,
    SEARCH_FIELDS,
    SOURCE_FIELDS,
    SYNC_LEDGER_FIELDS,
    export_board_sources_csv,
    safe_csv_cell,
)
from board.models import AgendaItem, NormalizedMeeting
from board.runs import create_board_discovery_run, create_board_sync_run
from board.storage import persist_meeting_bundle, upsert_board_source
from board.web import bp as board_blueprint
from common import connect_db, init_db, utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BoardExportTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.storage_temp = TemporaryDirectory()
        self.exports_temp = TemporaryDirectory()
        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.exports_patch = patch.object(
            common, "EXPORTS_DIR", Path(self.exports_temp.name)
        )
        self.db_patch.start()
        self.exports_patch.start()
        self.csv_responses = []
        init_db(self.db_path)
        self.district_id = self._add_district()
        self.source = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": "https://meetings.boardbook.org/Public/Organization/2221",
                "discovered_from_url": "https://district.example/board",
                "organization_external_id": "2221",
                "confidence": 0.99,
            },
            db_path=self.db_path,
        )
        self.meeting = persist_meeting_bundle(
            self.district_id,
            self.source["id"],
            NormalizedMeeting(
                external_meeting_id="export-meeting-1",
                source_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=1",
                title="Export Test Board Meeting",
                platform="boardbook",
                meeting_date="2026-08-20",
                meeting_type="Regular",
                description="Discuss the student services budget.",
                agenda_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=1",
                minutes_url="https://meetings.boardbook.org/Public/Minutes/2221?meeting=1",
                video_url="https://video.example.test/meeting/1",
                agenda_items=[
                    AgendaItem(
                        external_item_id="export-item-1",
                        title="Student services budget",
                        item_number="1.A",
                        description="Approve the counseling services budget.",
                    )
                ],
            ),
            db_path=self.db_path,
        )
        self.document = store_board_document(
            self.district_id,
            self.meeting["id"],
            {
                "external_document_id": "export-doc-1",
                "document_type": "packet",
                "title": "Student Services Budget Packet",
                "source_url": "https://district.example/board/student-services.txt",
                "filename": "student-services.txt",
                "mime_type": "text/plain",
            },
            b"Student services budget and counseling allocations.",
            storage_root=Path(self.storage_temp.name),
            db_path=self.db_path,
        )
        self.discovery_run_id = create_board_discovery_run(
            states=["OR"],
            force=True,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        self.sync_run_id = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE board_discovery_run_items SET board_source_id = ?, status = 'completed' WHERE run_id = ?",
                (self.source["id"], self.discovery_run_id),
            )
            conn.commit()
        self.app = self._make_app()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        for response in self.csv_responses:
            response.close()
        self.exports_patch.stop()
        self.db_patch.stop()
        self.exports_temp.cleanup()
        self.storage_temp.cleanup()
        self.db_path.unlink(missing_ok=True)

    def _add_district(self) -> int:
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO districts (
                    source_file, source_row_number, agency_id_nces, agency_name,
                    state, agency_type, total_enrollment_excludes_ae, website,
                    website_normalized, has_searchable_website, raw_json,
                    created_at, updated_at
                ) VALUES ('export-fixture.csv', 1, '4100999', 'Export Test District',
                          'OR', '1-Regular local school district', 2400,
                          'https://district.example/', 'https://district.example/',
                          1, '{}', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _make_app(self) -> Flask:
        app = Flask(
            "board-export-tests",
            template_folder=str(PROJECT_ROOT / "templates"),
            static_folder=str(PROJECT_ROOT / "static"),
        )
        app.config.update(TESTING=True, SECRET_KEY="board-export-tests")
        for endpoint, path in (
            ("index", "/"),
            ("import_page", "/import"),
            ("search_page", "/search"),
            ("search_profiles_page", "/search-profiles"),
            ("contracts_page", "/contracts"),
            ("districts_page", "/districts"),
            ("settings_page", "/settings"),
        ):
            app.add_url_rule(path, endpoint=endpoint, view_func=lambda: "stub")
        app.add_url_rule("/help", endpoint="help_index", view_func=lambda: "help")
        app.add_url_rule(
            "/help/<slug>", endpoint="help_topic_page", view_func=lambda slug: slug
        )
        app.register_blueprint(board_blueprint)
        app.jinja_env.filters["fmt_int"] = lambda value: f"{int(value or 0):,}"
        app.jinja_env.filters["fmt_dt"] = lambda value: str(value or "")

        @app.context_processor
        def globals_for_templates():
            return {"year": datetime.now().year, "db_path": str(self.db_path)}

        return app

    @staticmethod
    def _csv_rows(response) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))

    def assert_csv_response(self, response) -> None:
        self.csv_responses.append(response)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.mimetype == "text/csv")
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))

    def test_filtered_exports_have_stable_columns_and_public_provenance(self):
        source_response = self.client.get(
            "/school-boards/sources/export.csv?state=OR&platform=boardbook"
        )
        self.assert_csv_response(source_response)
        source_reader = csv.DictReader(io.StringIO(source_response.get_data(as_text=True)))
        self.assertEqual(tuple(source_reader.fieldnames or ()), SOURCE_FIELDS)
        source_rows = list(source_reader)
        self.assertEqual(len(source_rows), 1)
        self.assertEqual(source_rows[0]["district_name"], "Export Test District")
        self.assertEqual(
            source_rows[0]["original_source_url"], self.source["source_url"]
        )
        self.assertEqual(
            source_rows[0]["discovered_from_url"], "https://district.example/board"
        )

        excluded_sources = self.client.get(
            "/school-boards/sources/export.csv?state=WA&platform=boardbook"
        )
        self.assert_csv_response(excluded_sources)
        self.assertEqual(self._csv_rows(excluded_sources), [])

        meeting_response = self.client.get(
            "/school-boards/meetings/export.csv?state=OR&platform=boardbook"
            "&has_minutes=1&date_from=2026-08-01&date_to=2026-08-31"
        )
        self.assert_csv_response(meeting_response)
        meeting_reader = csv.DictReader(io.StringIO(meeting_response.get_data(as_text=True)))
        self.assertEqual(tuple(meeting_reader.fieldnames or ()), MEETING_FIELDS)
        meeting_rows = list(meeting_reader)
        self.assertEqual(len(meeting_rows), 1)
        self.assertEqual(
            meeting_rows[0]["original_source_url"], self.meeting["source_url"]
        )
        self.assertEqual(meeting_rows[0]["document_count"], "1")

        excluded_meetings = self.client.get(
            "/school-boards/meetings/export.csv?state=OR&has_minutes=0"
        )
        self.assert_csv_response(excluded_meetings)
        self.assertEqual(self._csv_rows(excluded_meetings), [])

        search_response = self.client.get(
            "/school-boards/search/export.csv?q=counseling&state=OR"
            "&platform=boardbook&document_type=packet"
        )
        self.assert_csv_response(search_response)
        search_reader = csv.DictReader(io.StringIO(search_response.get_data(as_text=True)))
        self.assertEqual(tuple(search_reader.fieldnames or ()), SEARCH_FIELDS)
        search_rows = list(search_reader)
        self.assertEqual(len(search_rows), 1)
        self.assertEqual(search_rows[0]["document_id"], str(self.document["id"]))
        self.assertEqual(
            search_rows[0]["original_source_url"],
            "https://district.example/board/student-services.txt",
        )
        self.assertEqual(search_rows[0]["meeting_source_url"], self.meeting["source_url"])

        discovery_response = self.client.get(
            f"/school-boards/discovery-runs/{self.discovery_run_id}/export.csv"
        )
        self.assert_csv_response(discovery_response)
        discovery_reader = csv.DictReader(
            io.StringIO(discovery_response.get_data(as_text=True))
        )
        self.assertEqual(tuple(discovery_reader.fieldnames or ()), DISCOVERY_LEDGER_FIELDS)
        discovery_rows = list(discovery_reader)
        self.assertEqual(len(discovery_rows), 1)
        self.assertEqual(discovery_rows[0]["run_id"], str(self.discovery_run_id))
        self.assertEqual(discovery_rows[0]["original_source_url"], self.source["source_url"])

        sync_response = self.client.get(
            f"/school-boards/sync-runs/{self.sync_run_id}/export.csv"
        )
        self.assert_csv_response(sync_response)
        sync_reader = csv.DictReader(io.StringIO(sync_response.get_data(as_text=True)))
        self.assertEqual(tuple(sync_reader.fieldnames or ()), SYNC_LEDGER_FIELDS)
        sync_rows = list(sync_reader)
        self.assertEqual(len(sync_rows), 1)
        self.assertEqual(sync_rows[0]["run_id"], str(self.sync_run_id))
        self.assertEqual(sync_rows[0]["original_source_url"], self.source["source_url"])

        archived = sorted(Path(self.exports_temp.name).glob("*.csv"))
        self.assertEqual(len(archived), 7)
        self.assertTrue(all(path.name.startswith("edscanner-board-") for path in archived))

    def test_export_links_preserve_active_filters_but_not_pagination(self):
        source_body = self.client.get(
            "/school-boards/sources?state=OR&platform=boardbook&page=2"
        ).get_data(as_text=True)
        self.assertIn(
            "/school-boards/sources/export.csv?state=OR&amp;platform=boardbook",
            source_body,
        )
        self.assertNotIn("sources/export.csv?state=OR&amp;platform=boardbook&amp;page=2", source_body)

        meeting_body = self.client.get(
            "/school-boards/meetings?state=OR&has_minutes=1&page=2"
        ).get_data(as_text=True)
        self.assertIn(
            "/school-boards/meetings/export.csv?state=OR&amp;has_minutes=1",
            meeting_body,
        )

        search_body = self.client.get(
            "/school-boards/search?q=counseling&state=OR&page=2"
        ).get_data(as_text=True)
        self.assertIn(
            "/school-boards/search/export.csv?q=counseling&amp;state=OR",
            search_body,
        )
        self.assertIn("Export results CSV", search_body)

    def test_exports_reject_invalid_requests_and_escape_spreadsheet_formulas(self):
        self.assertEqual(
            self.client.get("/school-boards/search/export.csv").status_code, 400
        )
        self.assertEqual(
            self.client.get(
                "/school-boards/meetings/export.csv?date_from=2026-12-31&date_to=2026-01-01"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/school-boards/discovery-runs/999999/export.csv").status_code,
            404,
        )
        self.assertEqual(safe_csv_cell("=2+2"), "'=2+2")
        self.assertEqual(safe_csv_cell("  @SUM(A1:A2)"), "'  @SUM(A1:A2)")
        self.assertEqual(safe_csv_cell(-1.25), "-1.25")
        self.assertEqual(safe_csv_cell("https://example.test"), "https://example.test")

        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE districts SET agency_name = '=HYPERLINK(\"https://bad.invalid\")' WHERE id = ?",
                (self.district_id,),
            )
            conn.commit()
        artifact = export_board_sources_csv(
            state="OR", db_path=self.db_path, export_dir=self.exports_temp.name
        )
        with artifact.path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertTrue(row["district_name"].startswith("'="))


if __name__ == "__main__":
    unittest.main()
