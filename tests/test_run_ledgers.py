from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import common
import profile_runs
import run_workers
import search_engine
from common import connect_db, init_db, utc_now_iso
from profile_runs import create_profile_discovery_run, execute_profile_discovery_run
from search_engine import SearchSettings, create_search_run, execute_search_run


class RunLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        init_db(self.db_path)
        self.district_ids = self._insert_districts(["Alpha", "Bravo", "Charlie", "Delta"])

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def _insert_districts(self, names: list[str]) -> list[int]:
        now = utc_now_iso()
        district_ids: list[int] = []
        with connect_db(self.db_path) as conn:
            for ordinal, name in enumerate(names, start=1):
                cursor = conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('ledger.csv', ?, ?, ?, 'OR', 'Regular', ?, ?, ?, 1, '{}', ?, ?)
                    """,
                    (
                        ordinal,
                        f"ledger-{ordinal}",
                        name,
                        ordinal * 100,
                        f"https://{name.casefold()}.example",
                        f"https://{name.casefold()}.example",
                        now,
                        now,
                    ),
                )
                district_ids.append(int(cursor.lastrowid))
            conn.commit()
        return district_ids

    @staticmethod
    def _result_for(district: dict[str, object]) -> list[dict[str, object]]:
        name = str(district["agency_name"]).casefold()
        return [
            {
                "url": f"https://{name}.example/community-schools",
                "title": f"{district['agency_name']} Community Schools",
                "content_type": "text/html",
                "status_code": 200,
                "search_source": "test",
                "score": 50,
                "snippet": "Community schools evidence.",
                "matched_terms": ["community schools"],
            }
        ]

    def test_search_run_freezes_only_explicit_district_ids(self) -> None:
        selected = [self.district_ids[2], self.district_ids[0]]
        run_id = create_search_run(
            "community schools",
            max_districts=4,
            max_workers=1,
            district_ids=selected,
            db_path=self.db_path,
            settings=SearchSettings(max_total_districts_per_run=4, delay_seconds=0),
        )
        with connect_db(self.db_path) as conn:
            frozen = [
                int(row["district_id"])
                for row in conn.execute(
                    "SELECT district_id FROM search_run_items WHERE run_id = ? ORDER BY ordinal",
                    (run_id,),
                )
            ]
        self.assertEqual(set(frozen), set(selected))
        self.assertEqual(len(frozen), 2)

    def test_search_resume_preserves_completed_results_and_retries_only_queued_item(self) -> None:
        first, second = self.district_ids[:2]
        run_id = create_search_run(
            "community schools",
            max_districts=2,
            max_workers=2,
            district_ids=[first, second],
            db_path=self.db_path,
            settings=SearchSettings(max_total_districts_per_run=2, delay_seconds=0),
        )
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            conn.execute(
                """
                UPDATE search_run_items
                SET status = 'completed', result_count = 1, finished_at = ?
                WHERE run_id = ? AND district_id = ?
                """,
                (now, run_id, first),
            )
            conn.execute(
                """
                INSERT INTO search_results (
                    search_run_id, district_id, district_name, result_rank,
                    url, score, created_at
                ) VALUES (?, ?, 'Alpha', 1, 'https://alpha.example/saved', 50, ?)
                """,
                (run_id, first, now),
            )
            conn.execute(
                """
                INSERT INTO search_results (
                    search_run_id, district_id, district_name, result_rank,
                    url, score, created_at
                ) VALUES (?, ?, 'Bravo', 1, 'https://bravo.example/stale-attempt', 5, ?)
                """,
                (run_id, second, now),
            )
            conn.commit()

        called: list[int] = []

        def fake_search(district, *_args, **_kwargs):
            called.append(int(district["id"]))
            return self._result_for(district)

        with patch.object(search_engine, "search_district", side_effect=fake_search):
            execute_search_run(
                run_id,
                db_path=self.db_path,
                settings=SearchSettings(delay_seconds=0),
            )

        with connect_db(self.db_path) as conn:
            run = conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
            items = conn.execute(
                "SELECT district_id, status, attempt FROM search_run_items WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
            urls = {
                row["url"]
                for row in conn.execute(
                    "SELECT url FROM search_results WHERE search_run_id = ?",
                    (run_id,),
                )
            }
        self.assertEqual(called, [second])
        self.assertEqual([row["status"] for row in items], ["completed", "completed"])
        self.assertEqual([row["attempt"] for row in items], [0, 1])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["districts_searched"], 2)
        self.assertIn("https://alpha.example/saved", urls)
        self.assertIn("https://bravo.example/community-schools", urls)
        self.assertNotIn("https://bravo.example/stale-attempt", urls)

    def test_adaptive_dispatcher_never_exceeds_current_target(self) -> None:
        run_id = create_search_run(
            "community schools",
            max_districts=4,
            max_workers=4,
            district_ids=self.district_ids,
            adaptive_enabled=True,
            resource_policy={"initial_workers": 1, "max_workers": 4},
            db_path=self.db_path,
            settings=SearchSettings(max_total_districts_per_run=4, delay_seconds=0),
        )

        class FixedController:
            target_workers = 1
            current_delay_seconds = 0.0

            def observe_district_completion(self, **_kwargs):
                return None

        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def fake_search(district, *_args, **_kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return []

        with patch.object(search_engine, "search_district", side_effect=fake_search):
            execute_search_run(
                run_id,
                db_path=self.db_path,
                settings=SearchSettings(delay_seconds=0),
                resource_controller=FixedController(),
            )
        self.assertEqual(maximum_active, 1)

    def test_manual_search_run_retains_fixed_nonadaptive_settings(self) -> None:
        run_id = create_search_run(
            "community schools",
            max_districts=2,
            max_workers=2,
            district_ids=self.district_ids[:2],
            db_path=self.db_path,
            settings=SearchSettings(
                max_total_districts_per_run=2,
                delay_seconds=0.25,
            ),
        )

        with patch.object(common, "DB_PATH", self.db_path):
            self.assertIsNone(run_workers._resource_controller_for_run(run_id))

        with connect_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT adaptive_enabled, max_workers, current_delay_seconds FROM search_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row["adaptive_enabled"], 0)
        self.assertEqual(row["max_workers"], 2)
        self.assertEqual(row["current_delay_seconds"], 0.25)

    def test_profile_run_exact_cohort_resumes_only_queued_items(self) -> None:
        first, _second, third = self.district_ids[:3]
        run_id = create_profile_discovery_run(
            [],
            [],
            None,
            None,
            [],
            "",
            2,
            2,
            "calendar",
            False,
            district_ids=[first, third],
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            conn.execute(
                """
                UPDATE profile_discovery_run_items
                SET status = 'completed', profile_status = 'working', finished_at = ?
                WHERE run_id = ? AND district_id = ?
                """,
                (utc_now_iso(), run_id, first),
            )
            conn.commit()

        called: list[int] = []

        def fake_discovery(district, **_kwargs):
            called.append(int(district["id"]))
            return {"id": None, "profile_status": "working", "provider_guess": "Test"}

        with patch.object(profile_runs, "discover_district_search_profile", side_effect=fake_discovery):
            execute_profile_discovery_run(run_id, db_path=self.db_path)

        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM profile_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            items = conn.execute(
                "SELECT district_id, status, attempt FROM profile_discovery_run_items WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        self.assertEqual(called, [third])
        self.assertEqual({int(row["district_id"]) for row in items}, {first, third})
        self.assertEqual([row["status"] for row in items], ["completed", "completed"])
        self.assertEqual([row["attempt"] for row in items], [0, 1])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["districts_processed"], 2)
        self.assertEqual(run["profiles_working"], 2)

    def test_worker_recovery_resets_only_running_items(self) -> None:
        run_id = create_search_run(
            "community schools",
            max_districts=3,
            max_workers=1,
            district_ids=self.district_ids[:3],
            db_path=self.db_path,
            settings=SearchSettings(max_total_districts_per_run=3, delay_seconds=0),
        )
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT district_id FROM search_run_items WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
            conn.execute(
                "UPDATE search_run_items SET status = 'completed' WHERE run_id = ? AND district_id = ?",
                (run_id, rows[0]["district_id"]),
            )
            conn.execute(
                "UPDATE search_run_items SET status = 'running' WHERE run_id = ? AND district_id = ?",
                (run_id, rows[1]["district_id"]),
            )
            conn.execute(
                "UPDATE search_run_items SET status = 'failed' WHERE run_id = ? AND district_id = ?",
                (run_id, rows[2]["district_id"]),
            )
            conn.execute("UPDATE search_runs SET status = 'running' WHERE id = ?", (run_id,))
            conn.commit()

        with patch.object(common, "DB_PATH", self.db_path):
            recovered = run_workers._recover_search_runs()

        with connect_db(self.db_path) as conn:
            statuses = [
                row["status"]
                for row in conn.execute(
                    "SELECT status FROM search_run_items WHERE run_id = ? ORDER BY ordinal",
                    (run_id,),
                )
            ]
            run_status = conn.execute(
                "SELECT status FROM search_runs WHERE id = ?",
                (run_id,),
            ).fetchone()["status"]
        self.assertIn(run_id, recovered)
        self.assertEqual(statuses, ["completed", "queued", "failed"])
        self.assertEqual(run_status, "queued")

    def test_duplicate_delivery_cannot_claim_an_already_running_search(self) -> None:
        run_id = create_search_run(
            "community schools",
            max_districts=1,
            max_workers=1,
            district_ids=self.district_ids[:1],
            db_path=self.db_path,
            settings=SearchSettings(max_total_districts_per_run=1, delay_seconds=0),
        )
        with connect_db(self.db_path) as conn:
            conn.execute("UPDATE search_runs SET status = 'running' WHERE id = ?", (run_id,))
            conn.commit()

        with patch.object(search_engine, "search_district") as search:
            execute_search_run(run_id, db_path=self.db_path, settings=SearchSettings(delay_seconds=0))

        search.assert_not_called()
        with connect_db(self.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM search_run_items WHERE run_id = ?",
                (run_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_duplicate_delivery_cannot_claim_an_already_running_profile_run(self) -> None:
        run_id = create_profile_discovery_run(
            [],
            [],
            None,
            None,
            [],
            "",
            1,
            1,
            "calendar",
            False,
            district_ids=self.district_ids[:1],
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE profile_discovery_runs SET status = 'running' WHERE id = ?",
                (run_id,),
            )
            conn.commit()

        with patch.object(profile_runs, "discover_district_search_profile") as discover:
            execute_profile_discovery_run(run_id, db_path=self.db_path)

        discover.assert_not_called()
        with connect_db(self.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM profile_discovery_run_items WHERE run_id = ?",
                (run_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "queued")


if __name__ == "__main__":
    unittest.main()
