from __future__ import annotations

from contextlib import closing
import sqlite3
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile

from common import connect_db, init_db, utc_now_iso
from guided_search.storage import (
    CohortAlreadyFrozenError,
    ConcurrentUpdateError,
    InvalidStatusTransitionError,
    add_step,
    cancellation_requested,
    claim_session,
    create_session,
    finish_step,
    freeze_cohort,
    freeze_profile_discovery_run_cohort,
    freeze_search_run_cohort,
    get_cohort_ids,
    get_session,
    heartbeat_session_claim,
    link_child_run,
    link_evidence_source,
    list_child_runs,
    list_evidence,
    list_evidence_sources,
    list_model_calls,
    list_profile_discovery_run_items,
    list_search_run_items,
    list_steps,
    record_model_call,
    release_session_claim,
    request_cancellation,
    transition_session,
    update_profile_discovery_run_item,
    update_search_run_item,
    update_session,
    upsert_evidence,
)


class GuidedSearchStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        init_db(self.db_path)
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            for district_id, name in enumerate(("Alpha", "Beta", "Gamma"), start=1):
                conn.execute(
                    """
                    INSERT INTO districts (
                        id, agency_name, state, agency_type,
                        total_enrollment_excludes_ae, website, website_normalized,
                        has_searchable_website, created_at, updated_at
                    ) VALUES (?, ?, 'OR', 'Regular', ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        district_id,
                        f"{name} District",
                        district_id * 100,
                        f"https://{name.casefold()}.example",
                        f"https://{name.casefold()}.example",
                        now,
                        now,
                    ),
                )

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def _session(self) -> int:
        return create_session(
            "  Find community-school programs exactly as described.  ",
            example_text="Example language",
            scope={"states": ["OR"]},
            strategy_mode="balanced",
            resource_policy={"initial_workers": 2, "max_workers": 4},
            max_rounds=3,
            max_child_search_runs=6,
            db_path=self.db_path,
        )

    def _runs(self) -> tuple[int, int]:
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            search_run_id = int(
                conn.execute(
                    "INSERT INTO search_runs (query_text, status, started_at) "
                    "VALUES ('community schools', 'queued', ?)",
                    (now,),
                ).lastrowid
            )
            profile_run_id = int(
                conn.execute(
                    "INSERT INTO profile_discovery_runs (status) VALUES ('queued')"
                ).lastrowid
            )
        return search_run_id, profile_run_id

    def test_schema_is_additive_and_idempotent(self):
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            search_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(search_runs)")
            }
            session_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(guided_search_sessions)")
            }

        self.assertTrue(
            {
                "search_run_items",
                "profile_discovery_run_items",
                "guided_search_sessions",
                "guided_search_session_districts",
                "guided_search_steps",
                "guided_search_child_runs",
                "guided_search_model_calls",
                "guided_search_evidence",
                "guided_search_evidence_sources",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "adaptive_enabled",
                "resource_policy_json",
                "current_workers",
                "current_delay_seconds",
            }.issubset(search_columns)
        )
        self.assertTrue(
            {
                "scope_json",
                "latest_plan_json",
                "latest_evaluation_json",
                "resource_policy_json",
                "clarification_questions_json",
                "cancel_requested",
                "claim_token",
                "claim_expires_at",
                "review_reason",
                "debug_log_path",
            }.issubset(session_columns)
        )

    def test_existing_search_runs_table_receives_adaptive_columns(self):
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        legacy_path = Path(handle.name)
        handle.close()
        try:
            with closing(sqlite3.connect(legacy_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE search_runs (
                        id INTEGER PRIMARY KEY,
                        query_text TEXT NOT NULL,
                        states_json TEXT,
                        agency_types_json TEXT,
                        min_enrollment INTEGER,
                        max_enrollment INTEGER,
                        max_districts INTEGER,
                        max_pages_per_district INTEGER,
                        search_method TEXT NOT NULL DEFAULT 'crawler',
                        search_provider TEXT,
                        api_results_per_district INTEGER,
                        follow_depth INTEGER NOT NULL DEFAULT 0,
                        max_workers INTEGER,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        debug_logging INTEGER NOT NULL DEFAULT 0,
                        debug_log_path TEXT,
                        status TEXT NOT NULL,
                        districts_matched INTEGER NOT NULL DEFAULT 0,
                        districts_searched INTEGER NOT NULL DEFAULT 0,
                        districts_failed INTEGER NOT NULL DEFAULT 0,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        error_message TEXT
                    )
                    """
                )
                conn.commit()
            init_db(legacy_path)
            with connect_db(legacy_path) as conn:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(search_runs)")
                }
            self.assertTrue(
                {
                    "adaptive_enabled",
                    "resource_policy_json",
                    "current_workers",
                    "current_delay_seconds",
                }.issubset(columns)
            )
        finally:
            legacy_path.unlink(missing_ok=True)

    def test_session_updates_transitions_claims_and_cancellation(self):
        session_id = self._session()
        session = get_session(session_id, self.db_path)
        self.assertEqual(
            session["original_objective"],
            "  Find community-school programs exactly as described.  ",
        )
        self.assertEqual(session["scope"], {"states": ["OR"]})
        self.assertEqual(session["resource_policy"]["max_workers"], 4)

        session = update_session(
            session_id,
            latest_plan={"queries": [{"query_text": '"community schools"'}]},
            clarification_questions=["Must implementation be explicit?"],
            expected_version=session["version"],
            db_path=self.db_path,
        )
        self.assertEqual(session["latest_plan"]["queries"][0]["query_text"], '"community schools"')
        with self.assertRaises(ConcurrentUpdateError):
            update_session(
                session_id,
                expected_version=0,
                final_summary="stale",
                db_path=self.db_path,
            )

        session = transition_session(
            session_id,
            "planning",
            expected_status="draft",
            db_path=self.db_path,
        )
        self.assertEqual(session["status"], "planning")
        self.assertIsNotNone(session["started_at"])
        with self.assertRaises(InvalidStatusTransitionError):
            transition_session(session_id, "completed", db_path=self.db_path)

        token = claim_session(
            session_id,
            statuses=["planning"],
            claim_seconds=60,
            db_path=self.db_path,
        )
        self.assertTrue(token)
        self.assertIsNone(
            claim_session(
                session_id,
                statuses=["planning"],
                claim_seconds=60,
                db_path=self.db_path,
            )
        )
        self.assertTrue(
            heartbeat_session_claim(
                session_id, token, claim_seconds=60, db_path=self.db_path
            )
        )
        self.assertTrue(
            release_session_claim(
                session_id,
                token,
                next_wake_at=utc_now_iso(),
                db_path=self.db_path,
            )
        )

        request_cancellation(
            session_id, reason="Cancelled in storage test.", db_path=self.db_path
        )
        self.assertTrue(cancellation_requested(session_id, self.db_path))
        with self.assertRaises(InvalidStatusTransitionError):
            transition_session(session_id, "ready", db_path=self.db_path)
        self.assertEqual(
            transition_session(session_id, "cancelled", db_path=self.db_path)["status"],
            "cancelled",
        )

    def test_profiling_can_stop_directly_for_summary(self):
        session_id = self._session()
        for status in ("planning", "ready", "queued", "profiling", "summarizing"):
            session = transition_session(session_id, status, db_path=self.db_path)
        self.assertEqual(session["status"], "summarizing")

    def test_frozen_cohorts_and_item_progress_are_exact_and_idempotent(self):
        session_id = self._session()
        search_run_id, profile_run_id = self._runs()

        self.assertEqual(freeze_cohort(session_id, [2, 1, 2], db_path=self.db_path), 2)
        self.assertEqual(get_cohort_ids(session_id, self.db_path), [2, 1])
        self.assertEqual(freeze_cohort(session_id, [2, 1], db_path=self.db_path), 2)
        with self.assertRaises(CohortAlreadyFrozenError):
            freeze_cohort(session_id, [1, 2], db_path=self.db_path)

        self.assertEqual(
            freeze_search_run_cohort(
                search_run_id, [2, 1], db_path=self.db_path
            ),
            2,
        )
        self.assertEqual(
            freeze_profile_discovery_run_cohort(
                profile_run_id, [2, 1], db_path=self.db_path
            ),
            2,
        )
        self.assertEqual(
            freeze_search_run_cohort(
                search_run_id, [1, 2], db_path=self.db_path
            ),
            2,
        )
        with self.assertRaises(CohortAlreadyFrozenError):
            freeze_search_run_cohort(
                search_run_id, [1, 3], db_path=self.db_path
            )
        running = update_search_run_item(
            search_run_id, 2, status="running", db_path=self.db_path
        )
        self.assertEqual(running["attempt"], 1)
        completed = update_search_run_item(
            search_run_id,
            2,
            status="completed",
            result_count=3,
            profile_status="working",
            db_path=self.db_path,
        )
        self.assertEqual(completed["result_count"], 3)
        self.assertIsNotNone(completed["finished_at"])

        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            profile_id = int(
                conn.execute(
                    """
                    INSERT INTO district_search_profiles (
                        district_id, profile_status, created_at, updated_at
                    ) VALUES (2, 'working', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
            )
        profile_item = update_profile_discovery_run_item(
            profile_run_id,
            2,
            status="completed",
            profile_id=profile_id,
            profile_status="working",
            result_count=5,
            db_path=self.db_path,
        )
        self.assertEqual(profile_item["profile_id"], profile_id)
        self.assertEqual(
            [row["district_id"] for row in list_search_run_items(search_run_id, db_path=self.db_path)],
            [2, 1],
        )
        self.assertEqual(
            [row["district_id"] for row in list_profile_discovery_run_items(profile_run_id, db_path=self.db_path)],
            [2, 1],
        )

    def test_steps_children_model_audit_and_evidence_provenance(self):
        session_id = self._session()
        search_run_id, profile_run_id = self._runs()
        step_id = add_step(
            session_id,
            "planning",
            short_description="Create a validated plan",
            input_data={"objective": "community schools"},
            db_path=self.db_path,
        )
        finish_step(
            step_id,
            output_data={"queries": ['"community schools"']},
            db_path=self.db_path,
        )
        self.assertEqual(list_steps(session_id, db_path=self.db_path)[0]["output"]["queries"], ['"community schools"'])

        search_link = link_child_run(
            session_id,
            "search",
            search_run_id,
            step_id=step_id,
            round_number=1,
            query_text='"community schools"',
            purpose="Find explicit references",
            db_path=self.db_path,
        )
        link_child_run(
            session_id,
            "profile_discovery",
            profile_run_id,
            round_number=0,
            purpose="Fill missing profile coverage",
            db_path=self.db_path,
        )
        self.assertEqual(len(list_child_runs(session_id, db_path=self.db_path)), 2)

        record_model_call(
            session_id,
            step_id=step_id,
            task_type="plan",
            model="qwen3.5:9b",
            endpoint_identity="http://user:password@ollama.test:11434/api/chat?token=secret",
            prompt_version="guided-search-plan-v1",
            success=True,
            validation_status="valid",
            latency_ms=120,
            metadata={"eval_count": 40, "authorization": "Bearer secret"},
            db_path=self.db_path,
        )
        calls = list_model_calls(session_id, db_path=self.db_path)
        self.assertTrue(calls[0]["success"])
        self.assertEqual(calls[0]["endpoint_identity"], "http://ollama.test:11434/api/chat")
        self.assertEqual(calls[0]["metadata"]["eval_count"], 40)
        self.assertEqual(calls[0]["metadata"]["authorization"], "[redacted]")

        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            result_one = int(
                conn.execute(
                    """
                    INSERT INTO search_results (
                        search_run_id, district_id, result_rank, url, title,
                        score, snippet, created_at
                    ) VALUES (?, 1, 1, ?, 'Community Schools', 42, 'Program page', ?)
                    """,
                    (search_run_id, "https://alpha.example/program", now),
                ).lastrowid
            )
            result_two = int(
                conn.execute(
                    """
                    INSERT INTO search_results (
                        search_run_id, district_id, result_rank, url, title,
                        score, snippet, created_at
                    ) VALUES (?, 1, 2, ?, 'Community Schools Update', 55, 'New evidence', ?)
                    """,
                    (search_run_id, "https://alpha.example/program#details", now),
                ).lastrowid
            )

        evidence_id = upsert_evidence(
            session_id,
            "https://alpha.example/program",
            district_id=1,
            score=42,
            round_number=1,
            title="Community Schools",
            db_path=self.db_path,
        )
        self.assertEqual(
            evidence_id,
            upsert_evidence(
                session_id,
                "https://alpha.example/program",
                district_id=1,
                score=55,
                round_number=2,
                classification="useful",
                confidence=0.9,
                evaluation={"reason": "Explicit implementation evidence"},
                db_path=self.db_path,
            ),
        )
        link_evidence_source(
            evidence_id,
            search_run_id=search_run_id,
            search_result_id=result_one,
            child_run_link_id=search_link,
            round_number=1,
            db_path=self.db_path,
        )
        link_evidence_source(
            evidence_id,
            search_run_id=search_run_id,
            search_result_id=result_two,
            child_run_link_id=search_link,
            round_number=2,
            db_path=self.db_path,
        )
        evidence = list_evidence(session_id, db_path=self.db_path)[0]
        self.assertEqual(evidence["best_score"], 55)
        self.assertEqual(evidence["first_round"], 1)
        self.assertEqual(evidence["last_round"], 2)
        self.assertEqual(evidence["source_count"], 2)
        self.assertEqual(evidence["evaluation"]["reason"], "Explicit implementation evidence")
        self.assertEqual(
            len(list_evidence_sources(evidence_id=evidence_id, db_path=self.db_path)),
            2,
        )

    def test_evidence_keeps_best_representative_and_classification_preserves_rounds(self):
        session_id = self._session()
        evidence_id = upsert_evidence(
            session_id,
            "https://alpha.example/program",
            district_id=1,
            title="High-confidence page",
            snippet="Strong evidence from the best result.",
            content_fingerprint="high",
            score=100,
            round_number=2,
            db_path=self.db_path,
        )
        self.assertEqual(
            upsert_evidence(
                session_id,
                "https://alpha.example/program",
                district_id=2,
                title="Low-confidence duplicate",
                snippet="Weak duplicate text.",
                content_fingerprint="low",
                score=1,
                round_number=3,
                db_path=self.db_path,
            ),
            evidence_id,
        )
        upsert_evidence(
            session_id,
            "https://alpha.example/program",
            classification="useful",
            confidence=0.9,
            db_path=self.db_path,
        )

        evidence = list_evidence(session_id, db_path=self.db_path)[0]
        self.assertEqual(evidence["best_score"], 100)
        self.assertEqual(evidence["title"], "High-confidence page")
        self.assertEqual(evidence["snippet"], "Strong evidence from the best result.")
        self.assertEqual(evidence["content_fingerprint"], "high")
        self.assertEqual(evidence["district_id"], 1)
        self.assertEqual(evidence["first_round"], 2)
        self.assertEqual(evidence["last_round"], 3)
        self.assertEqual(evidence["classification"], "useful")


if __name__ == "__main__":
    unittest.main()
