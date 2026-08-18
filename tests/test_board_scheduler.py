from __future__ import annotations

import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from flask import Flask

import common
from board.scheduler import (
    DuplicateScheduleError,
    calculate_next_run,
    claim_due_schedules,
    create_schedule,
    dispatch_due_schedules,
    materialize_claimed_schedule,
    run_scheduler_loop,
)
from board.storage import upsert_board_source
from board.web import bp as board_blueprint
from common import connect_db, init_db, utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BoardSchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.db_patch.start()
        init_db(self.db_path)
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO districts (
                    source_file, source_row_number, agency_id_nces, agency_name,
                    state, agency_type, total_enrollment_excludes_ae, website,
                    website_normalized, has_searchable_website, raw_json,
                    created_at, updated_at
                ) VALUES ('schedule.csv', 1, '4100777', 'Scheduled District',
                          'OR', '1-Regular local school district', 3100,
                          'https://district.example/', 'https://district.example/',
                          1, '{}', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
            self.district_id = int(cursor.lastrowid)
        self.source = upsert_board_source(
            self.district_id,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": "https://meetings.boardbook.org/Public/Organization/777",
                "organization_external_id": "777",
                "confidence": 99,
            },
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.db_path.unlink(missing_ok=True)

    def test_next_run_calculation_handles_weekly_and_short_months(self):
        self.assertEqual(
            calculate_next_run(
                frequency="weekly",
                weekday=0,
                hour_24=8,
                minute=15,
                after=datetime(2026, 8, 17, 8, 15),  # Monday at the exact run time
            ),
            datetime(2026, 8, 24, 8, 15),
        )
        self.assertEqual(
            calculate_next_run(
                frequency="monthly",
                day_of_month=31,
                hour_24=20,
                minute=5,
                after=datetime(2026, 1, 31, 20, 5),
            ),
            datetime(2026, 2, 28, 20, 5),
        )
        self.assertEqual(
            calculate_next_run(
                frequency="monthly",
                day_of_month=31,
                hour_24=20,
                minute=5,
                after=datetime(2028, 1, 31, 20, 5),
            ),
            datetime(2028, 2, 29, 20, 5),
        )
        self.assertEqual(
            calculate_next_run(
                frequency="monthly",
                day_of_month=31,
                hour_24=7,
                minute=0,
                after=datetime(2026, 4, 1),
            ),
            datetime(2026, 4, 30, 7, 0),
        )

    def test_schedule_is_persistent_and_one_per_source(self):
        schedule_id = create_schedule(
            board_source_id=self.source["id"],
            frequency="monthly",
            day_of_month=31,
            hour_24=8,
            minute=30,
            now=datetime(2026, 1, 30, 9),
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM board_sync_schedules WHERE id = ?", (schedule_id,)).fetchone()
        self.assertEqual(row["next_run_at"], "2026-01-31T08:30:00")
        self.assertEqual((row["frequency"], row["day_of_month"], row["enabled"]), ("monthly", 31, 1))
        with self.assertRaises(DuplicateScheduleError):
            create_schedule(
                board_source_id=self.source["id"],
                frequency="daily",
                hour_24=8,
                minute=0,
                now=datetime(2026, 1, 30),
                db_path=self.db_path,
            )

    def test_due_claim_is_atomic_and_materializes_one_exact_source_run(self):
        schedule_id = create_schedule(
            board_source_id=self.source["id"],
            frequency="daily",
            hour_24=8,
            minute=0,
            now=datetime(2026, 8, 17, 9),
            db_path=self.db_path,
        )
        due = datetime(2026, 8, 18, 8)
        claims = claim_due_schedules(
            now=due,
            db_path=self.db_path,
            token_factory=lambda: "worker-one",
        )
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].schedule_id, schedule_id)
        self.assertEqual(
            claim_due_schedules(now=due, db_path=self.db_path, token_factory=lambda: "worker-two"),
            [],
        )

        run_id = materialize_claimed_schedule(claims[0], now=due, db_path=self.db_path)
        self.assertIsNotNone(run_id)
        self.assertIsNone(materialize_claimed_schedule(claims[0], now=due, db_path=self.db_path))
        with connect_db(self.db_path) as conn:
            schedule = conn.execute("SELECT * FROM board_sync_schedules WHERE id = ?", (schedule_id,)).fetchone()
            run = conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone()
            items = conn.execute("SELECT * FROM board_sync_run_items WHERE run_id = ?", (run_id,)).fetchall()
            events = conn.execute("SELECT * FROM board_sync_schedule_events WHERE schedule_id = ?", (schedule_id,)).fetchall()
        self.assertEqual(schedule["next_run_at"], "2026-08-19T08:00:00")
        self.assertEqual(schedule["last_sync_run_id"], run_id)
        self.assertEqual((run["status"], run["sync_mode"], run["districts_planned"]), ("queued", "monitor", 1))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["board_source_id"], self.source["id"])
        self.assertEqual(len(events), 1)

        next_claim = claim_due_schedules(
            now=datetime(2026, 8, 19, 8),
            db_path=self.db_path,
            token_factory=lambda: "worker-next-day",
        )[0]
        self.assertIsNone(
            materialize_claimed_schedule(
                next_claim,
                now=datetime(2026, 8, 19, 8),
                db_path=self.db_path,
            )
        )
        with connect_db(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM board_sync_runs").fetchone()["count"], 1)
            skipped = conn.execute(
                "SELECT status FROM board_sync_schedule_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            next_run_at = conn.execute(
                "SELECT next_run_at FROM board_sync_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()["next_run_at"]
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(next_run_at, "2026-08-20T08:00:00")

    def test_competing_workers_claim_once_and_expired_lease_can_be_reclaimed(self):
        create_schedule(
            board_source_id=self.source["id"],
            frequency="daily",
            hour_24=8,
            minute=0,
            now=datetime(2026, 8, 17, 9),
            db_path=self.db_path,
        )
        due = datetime(2026, 8, 18, 8)
        barrier = threading.Barrier(2)

        def compete(token: str):
            barrier.wait()
            return claim_due_schedules(
                now=due,
                db_path=self.db_path,
                claim_seconds=60,
                token_factory=lambda: token,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ("process-a", "process-b")))
        claimed = [claim for result in results for claim in result]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claim_due_schedules(now=datetime(2026, 8, 18, 8, 0, 59), db_path=self.db_path), [])
        reclaimed = claim_due_schedules(
            now=datetime(2026, 8, 18, 8, 1, 1),
            db_path=self.db_path,
            token_factory=lambda: "process-c",
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].token, "process-c")

    def test_due_schedule_does_not_overlap_a_manual_run_for_the_same_source(self):
        schedule_id = create_schedule(
            board_source_id=self.source["id"],
            frequency="daily",
            hour_24=8,
            minute=0,
            now=datetime(2026, 8, 17, 9),
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            run_cursor = conn.execute(
                """
                INSERT INTO board_sync_runs (
                    states_json, agency_types_json, platforms_json, source_status,
                    sync_mode, force, max_districts, max_workers, debug_logging,
                    status, districts_matched, districts_planned, queued_at
                ) VALUES ('["OR"]', '[]', '["boardbook"]', 'working',
                          'monitor', 0, 1, 1, 0, 'running', 1, 1, ?)
                """,
                (utc_now_iso(),),
            )
            manual_run_id = int(run_cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO board_sync_run_items (run_id, district_id, board_source_id, status)
                VALUES (?, ?, ?, 'running')
                """,
                (manual_run_id, self.district_id, self.source["id"]),
            )
            conn.commit()

        due = datetime(2026, 8, 18, 8)
        claim = claim_due_schedules(now=due, db_path=self.db_path)[0]
        self.assertIsNone(materialize_claimed_schedule(claim, now=due, db_path=self.db_path))

        with connect_db(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM board_sync_runs").fetchone()["count"], 1)
            event = conn.execute(
                "SELECT * FROM board_sync_schedule_events WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        self.assertEqual(event["status"], "skipped")
        self.assertIn(f"sync #{manual_run_id}", event["error_message"])

    def test_dispatch_skips_missed_intervals_and_calls_queue_once(self):
        schedule_id = create_schedule(
            board_source_id=self.source["id"],
            frequency="monthly",
            day_of_month=31,
            hour_24=8,
            minute=0,
            now=datetime(2026, 1, 1),
            db_path=self.db_path,
        )
        queued: list[int] = []
        run_ids = dispatch_due_schedules(
            queued.append,
            now=datetime(2026, 3, 5, 12),
            db_path=self.db_path,
        )
        self.assertEqual(queued, run_ids)
        self.assertEqual(len(run_ids), 1)
        with connect_db(self.db_path) as conn:
            row = conn.execute("SELECT next_run_at FROM board_sync_schedules WHERE id = ?", (schedule_id,)).fetchone()
        self.assertEqual(row["next_run_at"], "2026-03-31T08:00:00")

    def test_scheduler_loop_accepts_an_injected_clock_and_stops_cleanly(self):
        create_schedule(
            board_source_id=self.source["id"],
            frequency="daily",
            hour_24=8,
            minute=0,
            now=datetime(2026, 8, 17, 9),
            db_path=self.db_path,
        )
        stop_event = threading.Event()
        queued: list[int] = []

        def enqueue(run_id: int) -> None:
            queued.append(run_id)
            stop_event.set()

        run_scheduler_loop(
            enqueue,
            stop_event=stop_event,
            poll_seconds=1,
            clock=lambda: datetime(2026, 8, 18, 8),
            db_path=self.db_path,
        )
        self.assertEqual(len(queued), 1)


class BoardScheduleRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        BoardSchedulerTestCase.setUp(self)
        self.app = Flask(
            "board-schedule-route-tests",
            template_folder=str(PROJECT_ROOT / "templates"),
            static_folder=str(PROJECT_ROOT / "static"),
        )
        self.app.config.update(TESTING=True, SECRET_KEY="board-schedule-tests")
        for endpoint, path in (
            ("index", "/"),
            ("import_page", "/import"),
            ("search_page", "/search"),
            ("search_profiles_page", "/search-profiles"),
            ("contracts_page", "/contracts"),
            ("districts_page", "/districts"),
            ("settings_page", "/settings"),
            ("help_index", "/help"),
        ):
            self.app.add_url_rule(path, endpoint=endpoint, view_func=lambda endpoint=endpoint: endpoint)
        self.app.add_url_rule(
            "/help/<slug>",
            endpoint="help_topic_page",
            view_func=lambda slug: slug,
        )
        self.app.register_blueprint(board_blueprint)
        self.app.jinja_env.filters["fmt_int"] = lambda value: f"{int(value or 0):,}"
        self.app.jinja_env.filters["fmt_dt"] = lambda value: str(value or "")

        @self.app.context_processor
        def template_globals():
            return {"year": 2026, "db_path": str(self.db_path)}

        self.client = self.app.test_client()

    def tearDown(self) -> None:
        BoardSchedulerTestCase.tearDown(self)

    def test_schedule_pages_create_edit_and_pause(self):
        response = self.client.get("/school-boards/schedules/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Scheduled District", response.get_data(as_text=True))
        response = self.client.post(
            "/school-boards/schedules/new",
            data={
                "board_source_id": str(self.source["id"]),
                "frequency": "weekly",
                "weekday": "4",
                "run_hour": "8",
                "run_minute": "45",
                "meridiem": "PM",
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with connect_db(self.db_path) as conn:
            schedule = conn.execute("SELECT * FROM board_sync_schedules").fetchone()
        self.assertEqual((schedule["frequency"], schedule["weekday"], schedule["hour_24"], schedule["minute"]), ("weekly", 4, 20, 45))

        listing = self.client.get("/school-boards/schedules")
        self.assertEqual(listing.status_code, 200)
        body = listing.get_data(as_text=True)
        self.assertIn("Weekly on Friday", body)
        self.assertIn("8:45 PM", body)

        edited = self.client.post(
            f"/school-boards/schedules/{schedule['id']}/edit",
            data={
                "frequency": "monthly",
                "day_of_month": "31",
                "run_hour": "12",
                "run_minute": "5",
                "meridiem": "AM",
                "enabled": "1",
            },
        )
        self.assertEqual(edited.status_code, 302)
        with connect_db(self.db_path) as conn:
            edited_schedule = conn.execute("SELECT * FROM board_sync_schedules").fetchone()
        self.assertEqual(
            (edited_schedule["frequency"], edited_schedule["day_of_month"], edited_schedule["hour_24"]),
            ("monthly", 31, 0),
        )

        paused = self.client.post(f"/school-boards/schedules/{schedule['id']}/toggle", data={"enabled": "0"})
        self.assertEqual(paused.status_code, 302)
        with connect_db(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM board_sync_schedules").fetchone()["enabled"], 0)

    def test_schedule_routes_reject_invalid_values_and_missing_records(self):
        response = self.client.post(
            "/school-boards/schedules/new",
            data={
                "board_source_id": str(self.source["id"]),
                "frequency": "monthly",
                "run_hour": "8",
                "run_minute": "0",
                "meridiem": "AM",
                "enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/school-boards/schedules/999999/edit").status_code, 404)
        self.assertEqual(
            self.client.post("/school-boards/schedules/999999/toggle", data={"enabled": "1"}).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
