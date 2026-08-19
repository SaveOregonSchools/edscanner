from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from flask import Flask

import common
from board.http import BoardHTTPClient, HTTPResult
from board.manual_sources import (
    ManualSourceValidation,
    ManualSourceValidationError,
    validate_manual_board_source,
)
from board.scheduler import create_schedule
from board.storage import upsert_board_source
from board.web import bp as board_blueprint
from common import connect_db, init_db, utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOARDBOOK_ORGANIZATION_PAGE = (
    b"<html><title>Bend-La Pine Schools - BoardBook Premier</title>"
    b'<div id="DisplayHeader"><h1>Bend-La Pine Schools Public View</h1></div></html>'
)


class _FixtureClient:
    def __init__(self, result: HTTPResult, *, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.close_calls = 0

    def get(self, url: str, **kwargs: object) -> HTTPResult:
        self.calls.append((url, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.close_calls += 1


def _http_result(*, status: int = 200, content: bytes = b"") -> HTTPResult:
    url = "https://meetings.boardbook.org/Public/Organization/2413"
    return HTTPResult(
        requested_url=url,
        final_url=url,
        status_code=status,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content=content,
    )


class ManualSourceValidationTests(unittest.TestCase):
    def test_auto_detected_public_boardbook_source_is_verified_working(self):
        client = _FixtureClient(_http_result(content=BOARDBOOK_ORGANIZATION_PAGE))

        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            "https://meetings.boardbook.org/Public/Organization/2413",
            "auto",
            operator_confirmed=True,
            client=client,  # type: ignore[arg-type]
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.source_status, "working")
        self.assertEqual(result.platform, "boardbook")
        self.assertEqual(result.organization_external_id, "2413")
        self.assertEqual(result.confidence, 99.0)
        self.assertEqual(result.raw_discovery_json["discovery_method"], "manual_entry")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][1]["check_robots"])
        self.assertFalse(client.calls[0][1]["raise_for_status"])
        self.assertEqual(client.close_calls, 0, "an injected client remains caller-owned")

    def test_internally_created_client_is_closed_after_success(self):
        client = _FixtureClient(_http_result(content=BOARDBOOK_ORGANIZATION_PAGE))

        with patch("board.manual_sources.BoardHTTPClient", return_value=client):
            result = validate_manual_board_source(
                {"id": 17, "agency_name": "Bend-La Pine Schools"},
                "https://meetings.boardbook.org/Public/Organization/2413",
                "auto",
                operator_confirmed=True,
            )

        self.assertTrue(result.verified)
        self.assertEqual(client.close_calls, 1)

    def test_internally_created_client_is_closed_after_fetch_failure(self):
        client = _FixtureClient(
            _http_result(),
            error=RuntimeError("fixture connection failed"),
        )

        with patch("board.manual_sources.BoardHTTPClient", return_value=client):
            with self.assertRaises(ManualSourceValidationError):
                validate_manual_board_source(
                    {"id": 17, "agency_name": "Bend-La Pine Schools"},
                    "https://meetings.boardbook.org/Public/Organization/2413",
                    "auto",
                )

        self.assertEqual(client.close_calls, 1)

    def test_internally_created_client_is_closed_after_detection_failure(self):
        client = _FixtureClient(_http_result(content=BOARDBOOK_ORGANIZATION_PAGE))

        with (
            patch("board.manual_sources.BoardHTTPClient", return_value=client),
            patch(
                "board.manual_sources.detect_platform",
                side_effect=RuntimeError("fixture detection failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture detection failure"):
                validate_manual_board_source(
                    {"id": 17, "agency_name": "Bend-La Pine Schools"},
                    "https://meetings.boardbook.org/Public/Organization/2413",
                    "auto",
                    operator_confirmed=True,
                )

        self.assertEqual(client.close_calls, 1)

    def test_challenge_response_is_saved_for_review_not_marked_working(self):
        client = _FixtureClient(
            _http_result(
                status=403,
                content=b"<html><title>Verify you are human</title>Cloudflare Ray ID</html>",
            )
        )

        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            "https://meetings.boardbook.org/Public/Organization/2413",
            "boardbook",
            client=client,  # type: ignore[arg-type]
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")
        self.assertIn("challenge", result.error_message or "")
        self.assertEqual(result.raw_discovery_json["challenge_category"], "http_access_denied")
        self.assertTrue(result.raw_discovery_json["browser_retry_allowed"])

    def test_tentative_generic_match_is_not_promoted_to_working(self):
        url = "https://district.example/board"
        client = _FixtureClient(
            HTTPResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=(
                    b"<html><h1>School Board</h1>"
                    b'<a href="/minutes.pdf">Meeting minutes</a></html>'
                ),
            )
        )

        result = validate_manual_board_source(
            {"id": 18, "agency_name": "Example District"},
            url,
            "auto",
            client=client,  # type: ignore[arg-type]
        )

        self.assertEqual(result.platform, "generic")
        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")
        self.assertIn("tentative match", result.error_message or "")

    def test_private_network_url_is_rejected_by_existing_http_boundary(self):
        with self.assertRaises(ManualSourceValidationError) as raised:
            validate_manual_board_source(
                {"id": 17, "agency_name": "Test District"},
                "http://127.0.0.1/admin",
                "auto",
                client=BoardHTTPClient(),
            )
        self.assertIn("public URL", str(raised.exception))

    def test_valid_platform_page_stays_manual_review_without_district_confirmation(self):
        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            "https://meetings.boardbook.org/Public/Organization/2413",
            "auto",
            client=_FixtureClient(_http_result(content=BOARDBOOK_ORGANIZATION_PAGE)),  # type: ignore[arg-type]
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")
        self.assertIn("Confirm", result.error_message or "")

    def test_boardbook_directory_redirect_cannot_be_activated_as_an_organization(self):
        requested = "https://meetings.boardbook.org/Public/Organization/not-real"
        redirected = HTTPResult(
            requested_url=requested,
            final_url="https://meetings.boardbook.org/Public",
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html><title>BoardBook Premier</title><h1>Public Organizations</h1></html>",
        )

        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            requested,
            "auto",
            operator_confirmed=True,
            client=_FixtureClient(redirected),  # type: ignore[arg-type]
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")
        self.assertIsNone(result.organization_external_id)
        self.assertIn("supported board platform", result.error_message or "")

    def test_empty_success_response_cannot_be_activated(self):
        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            "https://meetings.boardbook.org/Public/Organization/2413",
            "boardbook",
            operator_confirmed=True,
            client=_FixtureClient(_http_result(content=b"")),  # type: ignore[arg-type]
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")

    def test_markup_only_success_response_cannot_be_activated(self):
        result = validate_manual_board_source(
            {"id": 17, "agency_name": "Bend-La Pine Schools"},
            "https://meetings.boardbook.org/Public/Organization/2413",
            "boardbook",
            operator_confirmed=True,
            client=_FixtureClient(
                _http_result(content=b"<html><body><div id='DisplayHeader'></div></body></html>")
            ),  # type: ignore[arg-type]
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.source_status, "manual_review")
        self.assertIn("enough public source evidence", result.error_message or "")


class ManualSourceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.db_patch.start()
        init_db(self.db_path)
        self.district_id = self._add_district()
        self.original = upsert_board_source(
            self.district_id,
            {
                "platform": "generic",
                "source_status": "working",
                "source_url": "https://district.example/board",
                "confidence": 82,
            },
            db_path=self.db_path,
        )
        self.app = self._make_app()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.db_patch.stop()
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
                ) VALUES ('manual-fixture.csv', 1, '4100179', 'Bend-La Pine Schools',
                          'OR', '1-Regular local school district', 17000,
                          'https://www.bend.k12.or.us/', 'https://www.bend.k12.or.us/',
                          1, '{}', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _make_app(self) -> Flask:
        app = Flask(
            "manual-board-source-tests",
            template_folder=str(PROJECT_ROOT / "templates"),
            static_folder=str(PROJECT_ROOT / "static"),
        )
        app.config.update(TESTING=True, SECRET_KEY="manual-board-source-tests")
        for endpoint, path in (
            ("index", "/"),
            ("import_page", "/import"),
            ("search_page", "/search"),
            ("search_profiles_page", "/search-profiles"),
            ("contracts_page", "/contracts"),
            ("districts_page", "/districts"),
            ("settings_page", "/settings"),
        ):
            app.add_url_rule(path, endpoint=endpoint, view_func=lambda endpoint=endpoint: endpoint)
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
    def _validation_result(*, verified: bool, url: str, platform: str) -> ManualSourceValidation:
        return ManualSourceValidation(
            platform=platform,
            source_url=url,
            source_status="working" if verified else "manual_review",
            confidence=99.0 if verified else 35.0,
            requires_javascript=False,
            organization_external_id="2413" if platform == "boardbook" else None,
            platform_tenant=None,
            error_message=None if verified else "Adapter did not match.",
            raw_discovery_json={
                "discovery_method": "manual_entry",
                "verified": verified,
                "operator_confirmed_district_identity": True,
            },
            verified=verified,
        )

    def _manual_post_data(self, **values: object) -> dict[str, object]:
        with self.client.session_transaction() as flask_session:
            token = "manual-source-test-csrf-token-0123456789"
            flask_session["board_manual_source_csrf"] = token
        return {
            "district_id": self.district_id,
            "csrf_token": token,
            "confirm_district_identity": "1",
            **values,
        }

    def test_verified_correction_becomes_active_and_preserves_previous_source_history(self):
        new_url = "https://meetings.boardbook.org/Public/Organization/2413"
        outcome = self._validation_result(verified=True, url=new_url, platform="boardbook")
        with patch("board.web.validate_manual_board_source", return_value=outcome):
            response = self.client.post(
                "/school-boards/sources/manual",
                data=self._manual_post_data(
                    source_url=new_url,
                    platform="auto",
                ),
            )

        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM board_sources WHERE district_id = ? ORDER BY id",
                (self.district_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], self.original["id"])
        self.assertEqual(rows[0]["is_active"], 0)
        self.assertIsNotNone(rows[0]["superseded_at"])
        self.assertEqual(rows[1]["source_status"], "working")
        self.assertEqual(rows[1]["is_active"], 1)

        page = self.client.get(
            f"/school-boards/sources/manual?district_id={self.district_id}"
        )
        body = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Source History", body)
        self.assertIn(new_url, body)
        self.assertIn("https://district.example/board", body)

    def test_unverified_link_cannot_displace_working_source_or_spoof_status(self):
        review_url = "https://unknown.example/public-board"
        outcome = self._validation_result(verified=False, url=review_url, platform="generic")
        with patch("board.web.validate_manual_board_source", return_value=outcome):
            response = self.client.post(
                "/school-boards/sources/manual",
                data=self._manual_post_data(
                    source_url=review_url,
                    platform="generic",
                    source_status="working",
                ),
            )

        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            active = conn.execute(
                "SELECT * FROM board_sources WHERE district_id = ? AND is_active = 1",
                (self.district_id,),
            ).fetchone()
            review = conn.execute(
                "SELECT * FROM board_sources WHERE source_url = ?", (review_url,)
            ).fetchone()
        self.assertEqual(active["id"], self.original["id"])
        self.assertEqual(active["source_status"], "working")
        self.assertEqual(review["source_status"], "manual_review")
        self.assertEqual(review["is_active"], 0)

    def test_unsafe_or_unreachable_url_rerenders_form_and_is_not_saved(self):
        with patch(
            "board.web.validate_manual_board_source",
            side_effect=ManualSourceValidationError("Only public HTTP(S) URLs are supported."),
        ):
            response = self.client.post(
                "/school-boards/sources/manual",
                data=self._manual_post_data(
                    source_url="http://127.0.0.1/admin",
                    platform="auto",
                ),
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("The source was not saved", response.get_data(as_text=True))
        with connect_db(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS count FROM board_sources").fetchone()["count"]
        self.assertEqual(count, 1)

    def test_post_requires_csrf_and_explicit_district_confirmation(self):
        validator = patch("board.web.validate_manual_board_source")
        with validator as mocked:
            missing_csrf = self.client.post(
                "/school-boards/sources/manual",
                data={
                    "district_id": self.district_id,
                    "source_url": "https://meetings.boardbook.org/Public/Organization/2413",
                    "platform": "auto",
                    "confirm_district_identity": "1",
                },
            )
            self.assertEqual(missing_csrf.status_code, 400)

            data = self._manual_post_data(
                source_url="https://meetings.boardbook.org/Public/Organization/2413",
                platform="auto",
            )
            data.pop("confirm_district_identity")
            missing_confirmation = self.client.post(
                "/school-boards/sources/manual", data=data
            )
            self.assertEqual(missing_confirmation.status_code, 400)

        mocked.assert_not_called()
        with connect_db(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS count FROM board_sources").fetchone()["count"]
        self.assertEqual(count, 1)

    def test_route_rejects_district_id_outside_sqlite_integer_range(self):
        response = self.client.get(
            "/school-boards/sources/manual?district_id=9223372036854775808"
        )
        self.assertEqual(response.status_code, 400)

    def test_get_prefills_url_but_returns_platform_to_auto_detect(self):
        response = self.client.get(
            f"/school-boards/sources/manual?district_id={self.district_id}"
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="https://district.example/board"', body)
        self.assertIn('<option value="auto" selected>', body)
        self.assertNotIn('<option value="generic" selected>', body)

    def test_enabled_schedule_moves_to_verified_replacement(self):
        schedule_id = create_schedule(
            board_source_id=self.original["id"],
            frequency="weekly",
            weekday=2,
            hour_24=9,
            minute=15,
            db_path=self.db_path,
        )
        new_url = "https://meetings.boardbook.org/Public/Organization/2413"
        outcome = self._validation_result(verified=True, url=new_url, platform="boardbook")
        with patch("board.web.validate_manual_board_source", return_value=outcome):
            response = self.client.post(
                "/school-boards/sources/manual",
                data=self._manual_post_data(source_url=new_url, platform="auto"),
            )

        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            replacement = conn.execute(
                "SELECT id FROM board_sources WHERE source_url = ?", (new_url,)
            ).fetchone()
            schedule = conn.execute(
                "SELECT * FROM board_sync_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        self.assertEqual(schedule["board_source_id"], replacement["id"])
        self.assertEqual(schedule["enabled"], 1)
        self.assertEqual((schedule["frequency"], schedule["weekday"]), ("weekly", 2))

    def test_confirmed_manual_source_is_not_superseded_by_automated_discovery(self):
        manual_url = "https://meetings.boardbook.org/Public/Organization/2413"
        manual = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": manual_url,
                "raw_discovery_json": {
                    "discovery_method": "manual_entry",
                    "verified": True,
                    "operator_confirmed_district_identity": True,
                },
            },
            db_path=self.db_path,
        )
        refreshed = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": manual_url,
                "raw_discovery_json": {"discovery_method": "district_homepage"},
            },
            db_path=self.db_path,
        )
        self.assertIn(
            '"operator_confirmed_district_identity":true',
            refreshed["raw_discovery_json"],
        )
        automated = upsert_board_source(
            self.district_id,
            {
                "platform": "boarddocs",
                "source_status": "working",
                "source_url": "https://go.boarddocs.com/or/example/Board.nsf/Public",
                "raw_discovery_json": {"discovery_method": "district_homepage"},
            },
            db_path=self.db_path,
        )

        with connect_db(self.db_path) as conn:
            active = conn.execute(
                "SELECT id FROM board_sources WHERE district_id = ? AND is_active = 1",
                (self.district_id,),
            ).fetchone()
        self.assertEqual(active["id"], manual["id"])
        self.assertEqual(automated["is_active"], 0)

        confirmed_replacement = upsert_board_source(
            self.district_id,
            {
                "platform": "boarddocs",
                "source_status": "working",
                "source_url": "https://go.boarddocs.com/or/example/Board.nsf/Public",
                "raw_discovery_json": {
                    "discovery_method": "manual_entry",
                    "verified": True,
                    "operator_confirmed_district_identity": True,
                },
            },
            db_path=self.db_path,
        )
        self.assertEqual(confirmed_replacement["is_active"], 1)

    def test_conflicting_schedule_on_replacement_disables_old_schedule_with_reason(self):
        old_schedule_id = create_schedule(
            board_source_id=self.original["id"],
            frequency="daily",
            hour_24=7,
            minute=30,
            db_path=self.db_path,
        )
        replacement_url = "https://meetings.boardbook.org/Public/Organization/2413"
        candidate = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "manual_review",
                "source_url": replacement_url,
            },
            db_path=self.db_path,
        )
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            replacement_schedule_id = int(
                conn.execute(
                    """
                    INSERT INTO board_sync_schedules (
                        board_source_id, frequency, hour_24, minute, enabled,
                        next_run_at, created_at, updated_at
                    ) VALUES (?, 'weekly', 9, 0, 1, ?, ?, ?)
                    """,
                    (candidate["id"], now, now, now),
                ).lastrowid
            )
            conn.commit()

        replacement = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": replacement_url,
                "raw_discovery_json": {
                    "discovery_method": "manual_entry",
                    "verified": True,
                    "operator_confirmed_district_identity": True,
                },
            },
            db_path=self.db_path,
        )

        self.assertEqual(replacement["_schedule_action"], "disabled_conflict")
        with connect_db(self.db_path) as conn:
            old_schedule = conn.execute(
                "SELECT * FROM board_sync_schedules WHERE id = ?", (old_schedule_id,)
            ).fetchone()
            replacement_schedule = conn.execute(
                "SELECT * FROM board_sync_schedules WHERE id = ?",
                (replacement_schedule_id,),
            ).fetchone()
        self.assertEqual(old_schedule["enabled"], 0)
        self.assertIn("replacement source already had a schedule", old_schedule["last_error"])
        self.assertEqual(replacement_schedule["enabled"], 1)

    def test_board_sources_table_links_each_district_to_manual_correction(self):
        response = self.client.get("/school-boards/sources?state=OR")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Correct source", body)
        self.assertIn(
            f"/school-boards/sources/manual?district_id={self.district_id}", body
        )


if __name__ == "__main__":
    unittest.main()
