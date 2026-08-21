from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from common import connect_db, init_db, utc_now_iso
from guided_search import storage
from guided_search.ai import AIResult, GuidedAIError, ModelCallMetadata
from guided_search.models import GuidedSummary, SearchEvaluation, SearchPlan
from guided_search.orchestrator import (
    GuidedSearchOrchestrator,
    continue_last_plan,
    retry_ai_stage,
)


def _call(prompt_version: str, *, success: bool = True) -> ModelCallMetadata:
    return ModelCallMetadata(
        endpoint="http://local-ollama:11434",
        model="test-model",
        prompt_version=prompt_version,
        attempt=1,
        repair=False,
        success=success,
        validation_status="valid" if success else "transport_error",
        latency_seconds=0.01,
        error=None if success else "offline",
    )


def _plan(*, profile_policy: str = "discover_missing", max_rounds: int = 1) -> SearchPlan:
    return SearchPlan.from_dict(
        {
            "objective": "Find direct evidence of active community-school partnerships.",
            "needs_clarification": False,
            "clarification_questions": [],
            "search_concepts": [
                {
                    "name": "community school partnership",
                    "required": True,
                    "synonyms": ["full-service community school"],
                    "exclude_terms": ["job posting"],
                }
            ],
            "queries": [
                {
                    "query_text": '"community school" AND partnership',
                    "purpose": "Find district-owned program evidence.",
                    "priority": 1,
                }
            ],
            "profile_policy": profile_policy,
            "preferred_search_method": "crawler",
            "evaluation_criteria": {
                "positive_signals": ["named partner"],
                "negative_signals": ["generic policy mention"],
                "minimum_evidence": "A district page describing an active program.",
            },
            "stop_policy": {"max_rounds": max_rounds, "max_child_search_runs": 3},
            "resource_policy": {"initial_workers": 2, "max_workers": 3},
            "short_explanation": "Start with direct district evidence and a bounded cohort.",
        },
        mode="balanced",
    )


class GuidedOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        init_db(self.db_path)
        self.district_ids = self._insert_districts()

    def tearDown(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def _insert_districts(self) -> list[int]:
        now = utc_now_iso()
        ids: list[int] = []
        with connect_db(self.db_path) as conn:
            for ordinal, name in enumerate(("Alpha", "Bravo", "Charlie"), start=1):
                cursor = conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('guided.csv', ?, ?, ?, 'OR', 'Regular', ?, ?, ?, 1, '{}', ?, ?)
                    """,
                    (
                        ordinal,
                        f"guided-{ordinal}",
                        name,
                        ordinal * 100,
                        f"https://{name.casefold()}.example",
                        f"https://{name.casefold()}.example",
                        now,
                        now,
                    ),
                )
                ids.append(int(cursor.lastrowid))
            conn.commit()
        return ids

    def _session(self, *, status: str = "planning") -> int:
        return storage.create_session(
            "Find direct evidence of community-school partnerships; exclude job postings.",
            scope={
                "states": ["OR"],
                "agency_types": ["Regular"],
                "min_enrollment": None,
                "max_enrollment": None,
                "max_districts": 2,
            },
            strategy_mode="balanced",
            brave_allowed=False,
            status=status,
            stage=status,
            max_rounds=1,
            max_child_search_runs=3,
            db_path=self.db_path,
        )

    def test_profile_first_exact_cohort_then_search_evaluate_and_summarize(self) -> None:
        session_id = self._session()
        search_queue: list[int] = []
        profile_queue: list[int] = []
        orchestrator = GuidedSearchOrchestrator(
            db_path=self.db_path,
            enqueue_search=search_queue.append,
            enqueue_profile=profile_queue.append,
        )
        plan = _plan()
        evaluation = SearchEvaluation.from_dict(
            {
                "assessment": "mixed",
                "confidence": 0.7,
                "appears_to_match_user_intent": True,
                "useful_result_ids": [1],
                "likely_false_positive_ids": [],
                "positive_patterns": ["named partner"],
                "false_positive_patterns": [],
                "missing_concepts": ["implementation detail"],
                "new_information_gain": 0.5,
                "recommended_action": "run_additional_query",
                "proposed_queries": [
                    {
                        "query_text": '"community school" AND implementation',
                        "purpose": "Try to locate implementation evidence.",
                        "priority": 1,
                    }
                ],
                "recommended_search_method": None,
                "short_reason": "Useful evidence exists, but one concept remains thin.",
            },
            mode="balanced",
            available_result_ids=[1],
        )
        summary = GuidedSummary.from_dict(
            {
                "overview": "One district-owned page matched the requested concept.",
                "what_was_searched": "Two frozen Oregon districts were searched with one query.",
                "what_was_found": "Alpha described an active community-school partnership.",
                "how_search_evolved": "The hard one-round budget stopped further refinement.",
                "limitations": ["Absence from results is not proof of absence."],
                "highest_confidence_result_ids": [1],
                "uncertain_result_ids": [],
            },
            available_result_ids=[1],
        )

        with (
            patch(
                "guided_search.orchestrator.plan_search",
                return_value=AIResult(plan, (_call("guided-plan-v1"),)),
            ),
            patch(
                "guided_search.orchestrator.evaluate_search_results",
                return_value=AIResult(evaluation, (_call("guided-evaluate-v1"),)),
            ),
            patch(
                "guided_search.orchestrator.summarize_guided_search",
                return_value=AIResult(summary, (_call("guided-summary-v1"),)),
            ),
        ):
            self.assertEqual(orchestrator.advance(session_id).status, "ready")
            frozen = storage.get_cohort_ids(session_id, self.db_path)
            self.assertEqual(len(frozen), 2)
            self.assertEqual(set(frozen), set(self.district_ids[:2]))

            storage.transition_session(session_id, "queued", stage="queued", db_path=self.db_path)
            self.assertEqual(orchestrator.advance(session_id).status, "profiling")
            self.assertEqual(len(profile_queue), 1)
            profile_run_id = profile_queue[0]
            with connect_db(self.db_path) as conn:
                profile_items = conn.execute(
                    "SELECT district_id FROM profile_discovery_run_items WHERE run_id = ? ORDER BY ordinal",
                    (profile_run_id,),
                ).fetchall()
                conn.execute(
                    "UPDATE profile_discovery_runs SET status = 'completed', districts_processed = districts_planned, finished_at = ? WHERE id = ?",
                    (utc_now_iso(), profile_run_id),
                )
                conn.commit()
            self.assertEqual({int(row["district_id"]) for row in profile_items}, set(frozen))

            self.assertEqual(orchestrator.advance(session_id).status, "searching")
            self.assertEqual(len(search_queue), 1)
            search_run_id = search_queue[0]
            with connect_db(self.db_path) as conn:
                search_items = conn.execute(
                    "SELECT district_id FROM search_run_items WHERE run_id = ? ORDER BY ordinal",
                    (search_run_id,),
                ).fetchall()
                conn.execute(
                    "UPDATE search_run_items SET status = 'completed', result_count = CASE WHEN district_id = ? THEN 1 ELSE 0 END, finished_at = ? WHERE run_id = ?",
                    (frozen[0], utc_now_iso(), search_run_id),
                )
                conn.execute(
                    "UPDATE search_runs SET status = 'completed', districts_searched = 2, finished_at = ? WHERE id = ?",
                    (utc_now_iso(), search_run_id),
                )
                conn.execute(
                    """
                    INSERT INTO search_results (
                        search_run_id, district_id, district_name, state, agency_type,
                        result_rank, url, title, content_type, status_code,
                        search_source, score, snippet, matched_terms_json, created_at
                    ) VALUES (?, ?, 'Alpha', 'OR', 'Regular', 1,
                              'https://alpha.example/community-schools?utm_source=test',
                              'Community Schools', 'text/html', 200, 'test', 55,
                              'Alpha names a partner for its active program.',
                              '["community school", "partner"]', ?)
                    """,
                    (search_run_id, frozen[0], utc_now_iso()),
                )
                conn.commit()
            self.assertEqual({int(row["district_id"]) for row in search_items}, set(frozen))

            self.assertEqual(orchestrator.advance(session_id).status, "evaluating")
            # The evaluator asks for another query, but deterministic round limits win.
            self.assertEqual(orchestrator.advance(session_id).status, "summarizing")
            self.assertEqual(orchestrator.advance(session_id).status, "completed")

        session = storage.get_session(session_id, self.db_path)
        self.assertEqual(session["status"], "completed")
        self.assertIn("One district-owned page", session["final_summary"])
        self.assertEqual(len(storage.list_evidence(session_id, db_path=self.db_path)), 1)
        sources = storage.list_evidence_sources(session_id=session_id, db_path=self.db_path)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["search_result_id"], 1)
        self.assertEqual(len(storage.list_model_calls(session_id, db_path=self.db_path)), 3)

    def test_ai_outage_moves_to_review_and_preserves_call_audit(self) -> None:
        session_id = self._session()
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)
        failure = GuidedAIError(
            "All configured Ollama endpoints failed.",
            calls=(_call("guided-plan-v1", success=False),),
        )
        with patch("guided_search.orchestrator.plan_search", side_effect=failure):
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "needs_review")
        session = storage.get_session(session_id, self.db_path)
        self.assertEqual(session["status"], "needs_review")
        self.assertIn("Ollama", session["review_reason"])
        calls = storage.list_model_calls(session_id, db_path=self.db_path)
        self.assertEqual(len(calls), 1)
        self.assertFalse(bool(calls[0]["success"]))

    def test_cancellation_waits_for_child_terminal_state_and_preserves_it(self) -> None:
        session_id = self._session(status="searching")
        storage.freeze_cohort(session_id, self.district_ids[:1], db_path=self.db_path)
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO search_runs (
                    query_text, states_json, agency_types_json, max_districts,
                    max_pages_per_district, search_method, search_provider,
                    cancel_requested, debug_logging, status, districts_matched,
                    districts_searched, districts_failed, started_at
                ) VALUES ('community schools', '[]', '[]', 1, 10, 'crawler',
                          'crawler', 0, 0, 'running', 1, 0, 0, ?)
                """,
                (now,),
            )
            run_id = int(cursor.lastrowid)
            conn.commit()
        step_id = storage.add_step(session_id, "search_round", db_path=self.db_path)
        storage.link_child_run(
            session_id,
            "search",
            run_id,
            step_id=step_id,
            round_number=1,
            query_text="community schools",
            db_path=self.db_path,
        )
        storage.request_cancellation(session_id, db_path=self.db_path)
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)

        self.assertEqual(orchestrator.advance(session_id).status, "searching")
        with connect_db(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT cancel_requested FROM search_runs WHERE id = ?", (run_id,)).fetchone()["cancel_requested"],
                1,
            )
            conn.execute(
                "UPDATE search_runs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (utc_now_iso(), run_id),
            )
            conn.commit()
        self.assertEqual(orchestrator.advance(session_id).status, "cancelled")
        self.assertEqual(storage.get_session(session_id, self.db_path)["status"], "cancelled")
        self.assertEqual(storage.get_step(step_id, self.db_path)["status"], "cancelled")

    def test_planning_retry_does_not_execute_a_stale_clarification_plan(self) -> None:
        session_id = self._session()
        stale = _plan(profile_policy="use_existing").to_dict()
        stale["needs_clarification"] = True
        stale["clarification_questions"] = ["Which organization do you mean?"]
        stale["queries"] = []
        storage.update_session(session_id, latest_plan=stale, db_path=self.db_path)
        storage.transition_session(
            session_id,
            "needs_review",
            stage="planning",
            updates={"review_reason": "Planner temporarily unavailable."},
            db_path=self.db_path,
        )

        with self.assertRaisesRegex(ValueError, "requires clarification"):
            continue_last_plan(session_id, db_path=self.db_path)
        self.assertEqual(retry_ai_stage(session_id, db_path=self.db_path), "planning")
        self.assertEqual(storage.get_session(session_id, self.db_path)["status"], "planning")

    def test_restart_recovers_a_linked_child_without_creating_a_duplicate(self) -> None:
        session_id = self._session(status="queued")
        storage.freeze_cohort(session_id, self.district_ids[:1], db_path=self.db_path)
        step_id = storage.add_step(
            session_id,
            "search_round",
            input_data={"round": 1, "queries": ["community schools"]},
            db_path=self.db_path,
        )
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO search_runs (
                    query_text, states_json, agency_types_json, max_districts,
                    max_pages_per_district, search_method, search_provider,
                    cancel_requested, debug_logging, status, districts_matched,
                    districts_searched, districts_failed, started_at
                ) VALUES ('community schools', '[]', '[]', 1, 10, 'crawler',
                          'crawler', 0, 0, 'queued', 1, 0, 0, ?)
                """,
                (utc_now_iso(),),
            )
            run_id = int(cursor.lastrowid)
            conn.commit()
        storage.link_child_run(
            session_id,
            "search",
            run_id,
            step_id=step_id,
            round_number=1,
            query_text="community schools",
            db_path=self.db_path,
        )
        search_queue: list[int] = []
        orchestrator = GuidedSearchOrchestrator(
            db_path=self.db_path,
            enqueue_search=search_queue.append,
        )

        outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "searching")
        self.assertEqual(search_queue, [])
        self.assertEqual(
            len(storage.list_child_runs(session_id, child_type="search", db_path=self.db_path)),
            1,
        )
        session = storage.get_session(session_id, self.db_path)
        self.assertEqual(session["status"], "searching")
        self.assertEqual(session["round_number"], 1)


if __name__ == "__main__":
    unittest.main()
