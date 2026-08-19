from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from flask import Flask

import common
from board.documents import store_board_document
from board.models import AgendaItem, NormalizedMeeting
from board.runs import create_board_discovery_run, create_board_sync_run
from board.storage import persist_meeting_bundle, upsert_board_source
from board.web import bp as board_blueprint
from common import connect_db, init_db, utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BoardRouteSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.storage_temp = TemporaryDirectory()
        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.db_patch.start()
        init_db(self.db_path)
        self.district_id = self._add_district()
        self.source = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": "https://meetings.boardbook.org/Public/Organization/2221",
                "organization_external_id": "2221",
                "confidence": 99,
            },
            db_path=self.db_path,
        )
        self.meeting = persist_meeting_bundle(
            self.district_id,
            self.source["id"],
            NormalizedMeeting(
                external_meeting_id="route-meeting-1",
                source_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=1",
                title="Route Test Board Meeting",
                platform="boardbook",
                meeting_date="2026-08-20",
                meeting_type="Regular",
                description="Discuss the student services budget.",
                agenda_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=1",
                minutes_url="https://meetings.boardbook.org/Public/Minutes/2221?meeting=1",
                video_url="https://video.example.test/meeting/1",
                agenda_items=[
                    AgendaItem(
                        external_item_id="route-item-1",
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
                "external_document_id": "route-doc-1",
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
        with connect_db(self.db_path) as conn:
            conn.execute(
                """
                UPDATE board_discovery_runs
                SET website_moves_accepted = 1
                WHERE id = ?
                """,
                (self.discovery_run_id,),
            )
            conn.execute(
                """
                UPDATE board_discovery_run_items
                SET website_original_url = ?, website_final_url = ?
                WHERE run_id = ?
                """,
                (
                    "https://old-district.example/",
                    "https://district.example/",
                    self.discovery_run_id,
                ),
            )
            conn.commit()
        self.sync_run_id = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        self.app = self._make_app()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db_patch.stop()
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
                ) VALUES ('route-fixture.csv', 1, '4100999', 'Route Test District',
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
            "board-route-tests",
            template_folder=str(PROJECT_ROOT / "templates"),
            static_folder=str(PROJECT_ROOT / "static"),
        )
        app.config.update(TESTING=True, SECRET_KEY="board-route-tests")

        # base.html links to these existing EdScanner endpoints. Small stubs keep
        # this a direct-blueprint test instead of importing and starting the app.
        for endpoint, path in (
            ("index", "/"),
            ("import_page", "/import"),
            ("search_page", "/search"),
            ("search_profiles_page", "/search-profiles"),
            ("contracts_page", "/contracts"),
            ("districts_page", "/districts"),
            ("settings_page", "/settings"),
        ):
            app.add_url_rule(
                path,
                endpoint=endpoint,
                view_func=lambda endpoint=endpoint: endpoint,
            )
        app.add_url_rule("/help", endpoint="help_index", view_func=lambda: "help")
        app.add_url_rule(
            "/help/<slug>",
            endpoint="help_topic_page",
            view_func=lambda slug: slug,
        )
        app.register_blueprint(board_blueprint)
        app.jinja_env.filters["fmt_int"] = lambda value: f"{int(value or 0):,}"
        app.jinja_env.filters["fmt_dt"] = lambda value: str(value or "")

        @app.context_processor
        def globals_for_templates():
            return {"year": datetime.now().year, "db_path": str(self.db_path)}

        return app

    def assert_page(self, path: str, *expected: str) -> None:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_data(as_text=True)
        for text in expected:
            self.assertIn(text, body)

    def test_all_board_read_routes_render_with_real_temp_database_rows(self):
        cases = (
            ("/school-boards", "School Boards", "Working sources"),
            ("/school-boards/sources?state=OR", "Board Sources", "Route Test District"),
            (
                "/school-boards/discover",
                "Discover Board Sources",
                "Matching districts",
                "Discovery scope",
            ),
            ("/school-boards/sync", "Sync Board Meetings", "Working sources"),
            ("/school-boards/meetings", "Board Meetings", "Route Test Board Meeting"),
            (
                f"/school-boards/meetings/{self.meeting['id']}",
                "Route Test Board Meeting",
                "Student services budget",
            ),
            (
                "/school-boards/search?q=counseling&state=OR&platform=boardbook",
                "Search Board Records",
                "counseling",
            ),
            ("/school-boards/runs", "Board Runs", f"#{self.discovery_run_id}"),
            (
                f"/school-boards/discovery-runs/{self.discovery_run_id}",
                f"Board Source Discovery #{self.discovery_run_id}",
                "Route Test District",
                "Rediscover district sites, including districts with saved sources",
                "Website moves accepted",
                "old-district.example",
                "district.example",
            ),
            (
                f"/school-boards/sync-runs/{self.sync_run_id}",
                f"Board Sync #{self.sync_run_id}",
                "Route Test District",
            ),
        )
        for path, *expected in cases:
            with self.subTest(path=path):
                self.assert_page(path, *expected)

    def test_queued_run_cancellation_is_persistent_and_preserves_collected_data(self):
        discovery_response = self.client.post(
            f"/school-boards/discovery-runs/{self.discovery_run_id}/cancel"
        )
        sync_response = self.client.post(
            f"/school-boards/sync-runs/{self.sync_run_id}/cancel"
        )
        self.assertEqual(discovery_response.status_code, 302)
        self.assertEqual(sync_response.status_code, 302)

        with connect_db(self.db_path) as conn:
            discovery = conn.execute(
                "SELECT status, cancel_requested FROM board_discovery_runs WHERE id = ?",
                (self.discovery_run_id,),
            ).fetchone()
            discovery_items = [
                row["status"]
                for row in conn.execute(
                    "SELECT status FROM board_discovery_run_items WHERE run_id = ?",
                    (self.discovery_run_id,),
                )
            ]
            sync = conn.execute(
                "SELECT status, cancel_requested FROM board_sync_runs WHERE id = ?",
                (self.sync_run_id,),
            ).fetchone()
            sync_items = [
                row["status"]
                for row in conn.execute(
                    "SELECT status FROM board_sync_run_items WHERE run_id = ?",
                    (self.sync_run_id,),
                )
            ]
            meeting_count = conn.execute(
                "SELECT COUNT(*) AS count FROM board_meetings"
            ).fetchone()["count"]
            document_count = conn.execute(
                "SELECT COUNT(*) AS count FROM board_documents"
            ).fetchone()["count"]

        self.assertEqual((discovery["status"], discovery["cancel_requested"]), ("cancelled", 1))
        self.assertEqual(discovery_items, ["cancelled"])
        self.assertEqual((sync["status"], sync["cancel_requested"]), ("cancelled", 1))
        self.assertEqual(sync_items, ["cancelled"])
        self.assertEqual(meeting_count, 1)
        self.assertEqual(document_count, 1)

    def test_discovery_only_sync_queues_source_discovery_not_an_empty_sync(self):
        with patch("board.web.enqueue_discovery_run") as enqueue:
            response = self.client.post(
                "/school-boards/sync",
                data={
                    "sync_mode": "discovery_only",
                    "states": "OR",
                    "force": "1",
                    "max_workers": "1",
                    "max_districts": "10",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/school-boards/discovery-runs/", response.headers["Location"])
        enqueue.assert_called_once()
        run_id = int(response.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
        self.assertEqual(enqueue.call_args.args, (run_id,))
        with connect_db(self.db_path) as conn:
            discovery = conn.execute(
                "SELECT status, force, districts_planned FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            accidental_sync = conn.execute(
                "SELECT COUNT(*) AS count FROM board_sync_runs WHERE id > ?",
                (self.sync_run_id,),
            ).fetchone()["count"]
        self.assertEqual((discovery["status"], discovery["force"]), ("queued", 1))
        self.assertEqual(discovery["districts_planned"], 1)
        self.assertEqual(accidental_sync, 0)

    def test_discovery_scope_control_submits_an_explicit_default_value(self):
        response = self.client.get("/school-boards/discover")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('<select id="force" name="force">', body)
        self.assertIn('<option value="0" selected>', body)
        self.assertIn('<option value="1"', body)
        self.assertNotIn('name="force" type="checkbox"', body)

    def test_rediscovery_scope_persists_force_and_plans_existing_sources(self):
        with patch("board.web.enqueue_discovery_run") as enqueue:
            response = self.client.post(
                "/school-boards/discover",
                data={
                    "states": "OR",
                    "status_filter": "__unchecked__",
                    "force": "1",
                    "max_districts": "10",
                },
            )
        self.assertEqual(response.status_code, 302)
        run_id = enqueue.call_args.args[0]
        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT status_filter, force, districts_planned FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        self.assertIsNone(run["status_filter"])
        self.assertEqual(run["force"], 1)
        self.assertEqual(run["districts_planned"], 1)

        detail = self.client.get(f"/school-boards/discovery-runs/{run_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Discovery scope", detail.get_data(as_text=True))
        self.assertIn(
            "Rediscover district sites, including districts with saved sources",
            detail.get_data(as_text=True),
        )

    def test_default_scope_explicit_force_zero_excludes_existing_sources(self):
        with patch("board.web.enqueue_discovery_run") as enqueue:
            response = self.client.post(
                "/school-boards/discover",
                data={
                    "states": "OR",
                    "status_filter": "__unchecked__",
                    "force": "0",
                    "max_districts": "10",
                },
            )
        self.assertEqual(response.status_code, 302)
        run_id = enqueue.call_args.args[0]
        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT status_filter, force, districts_matched, districts_planned "
                "FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        self.assertEqual(run["status_filter"], "__unchecked__")
        self.assertEqual(run["force"], 0)
        self.assertEqual(run["districts_matched"], 0)
        self.assertEqual(run["districts_planned"], 0)

    def test_specific_status_selects_saved_sources_without_rediscovery_mode(self):
        with patch("board.web.enqueue_discovery_run") as enqueue:
            response = self.client.post(
                "/school-boards/discover",
                data={
                    "states": "OR",
                    "status_filter": "working",
                    "force": "0",
                    "max_districts": "10",
                },
            )
        self.assertEqual(response.status_code, 302)
        run_id = enqueue.call_args.args[0]
        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT status_filter, force, districts_planned "
                "FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        self.assertEqual(run["status_filter"], "working")
        self.assertEqual(run["force"], 0)
        self.assertEqual(run["districts_planned"], 1)

    def test_platform_filter_needs_rediscovery_when_status_is_unchecked(self):
        planned_by_force = {}
        for force in ("0", "1"):
            with self.subTest(force=force):
                with patch("board.web.enqueue_discovery_run") as enqueue:
                    response = self.client.post(
                        "/school-boards/discover",
                        data={
                            "states": "OR",
                            "status_filter": "__unchecked__",
                            "platform_filter": "boardbook",
                            "force": force,
                            "max_districts": "10",
                        },
                    )
                self.assertEqual(response.status_code, 302)
                run_id = enqueue.call_args.args[0]
                with connect_db(self.db_path) as conn:
                    run = conn.execute(
                        "SELECT status_filter, platform_filter, force, districts_planned "
                        "FROM board_discovery_runs WHERE id = ?",
                        (run_id,),
                    ).fetchone()
                planned_by_force[force] = run["districts_planned"]
                self.assertEqual(run["platform_filter"], "boardbook")

        self.assertEqual(planned_by_force, {"0": 0, "1": 1})

    def test_route_validation_and_missing_records_return_clear_http_errors(self):
        self.assertEqual(self.client.get("/school-boards/meetings/999999").status_code, 404)
        self.assertEqual(self.client.get("/school-boards/sync-runs/999999").status_code, 404)
        self.assertEqual(
            self.client.get("/school-boards/sync?date_from=2026-12-31&date_to=2026-01-01").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/school-boards/sync?date_from=not-a-date").status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
