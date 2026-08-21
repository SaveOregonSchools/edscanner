from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from common import connect_db, init_db, utc_now_iso
from guided_search import storage
from guided_search.ai import AIResult, GuidedAIError, ModelCallMetadata
from guided_search.models import GuidedSummary, SearchEvaluation, SearchPlan
from guided_search.orchestrator import GuidedSearchOrchestrator, request_manual_query
from profile_runs import create_profile_discovery_run


def _call(prompt_version: str, *, success: bool = True) -> ModelCallMetadata:
    return ModelCallMetadata(
        endpoint="http://local-ollama:11434",
        model="edge-test-model",
        prompt_version=prompt_version,
        attempt=1,
        repair=False,
        success=success,
        validation_status="valid" if success else "transport_error",
        latency_seconds=0.01,
        error=None if success else "offline",
    )


def _plan(
    *,
    profile_policy: str = "use_existing",
    preferred_method: str = "crawler",
    max_rounds: int = 3,
    max_children: int = 3,
) -> SearchPlan:
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
            "preferred_search_method": preferred_method,
            "evaluation_criteria": {
                "positive_signals": ["named partner"],
                "negative_signals": ["generic policy mention"],
                "minimum_evidence": "A district page describing an active program.",
            },
            "stop_policy": {
                "max_rounds": max_rounds,
                "max_child_search_runs": max_children,
            },
            "resource_policy": {"initial_workers": 2, "max_workers": 3},
            "short_explanation": "Use direct district evidence and a bounded cohort.",
        },
        mode="balanced",
        brave_allowed=preferred_method in {"brave", "hybrid"},
    )


def _evaluation(
    action: str,
    *,
    proposed_query: str | None = None,
    reason: str = "The evidence needs another bounded step.",
) -> SearchEvaluation:
    proposed = (
        [
            {
                "query_text": proposed_query,
                "purpose": "Find a distinct implementation detail.",
                "priority": 1,
            }
        ]
        if proposed_query
        else []
    )
    return SearchEvaluation.from_dict(
        {
            "assessment": "poor" if action != "accept" else "strong",
            "confidence": 0.65,
            "appears_to_match_user_intent": action == "accept",
            "useful_result_ids": [],
            "likely_false_positive_ids": [],
            "positive_patterns": [],
            "false_positive_patterns": ["generic mention"],
            "missing_concepts": ["implementation detail"],
            "new_information_gain": 0.1,
            "recommended_action": action,
            "proposed_queries": proposed,
            "recommended_search_method": None,
            "short_reason": reason,
        },
        mode="balanced",
    )


def _summary() -> GuidedSummary:
    return GuidedSummary.from_dict(
        {
            "overview": "The bounded run completed without search evidence.",
            "what_was_searched": "The exact cohort was prepared and profiled.",
            "what_was_found": "No candidate web evidence was available.",
            "how_search_evolved": "The session stopped before a search round.",
            "limitations": ["Missing evidence does not prove absence."],
            "highest_confidence_result_ids": [],
            "uncertain_result_ids": [],
        },
        available_result_ids=[],
    )


class GuidedOrchestratorEdgeTests(unittest.TestCase):
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
            for ordinal, name in enumerate(("Alpha", "Bravo"), start=1):
                cursor = conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('guided-edge.csv', ?, ?, ?, 'OR', 'Regular', ?, ?, ?, 1,
                              '{}', ?, ?)
                    """,
                    (
                        ordinal,
                        f"edge-{ordinal}",
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

    def _session(
        self,
        *,
        status: str,
        plan: SearchPlan | None = None,
        round_number: int = 0,
        clarification_round: int = 0,
        brave_allowed: bool = False,
        freeze: bool = True,
    ) -> int:
        plan = plan or _plan()
        session_id = storage.create_session(
            "Find direct evidence of community-school partnerships; exclude job postings.",
            scope={
                "states": ["OR"],
                "agency_types": ["Regular"],
                "min_enrollment": None,
                "max_enrollment": None,
                "max_districts": 2,
            },
            strategy_mode="balanced",
            brave_allowed=brave_allowed,
            status=status,
            stage=status,
            max_rounds=plan.stop_policy.max_rounds,
            max_child_search_runs=plan.stop_policy.max_child_search_runs,
            db_path=self.db_path,
        )
        storage.update_session(
            session_id,
            latest_plan=plan.to_dict(),
            round_number=round_number,
            clarification_round=clarification_round,
            db_path=self.db_path,
        )
        if freeze:
            storage.freeze_cohort(session_id, self.district_ids, db_path=self.db_path)
        return session_id

    def _search_child(
        self,
        session_id: int,
        *,
        query: str = '"community school" AND partnership',
        method: str = "crawler",
        round_number: int = 1,
        status: str = "completed",
        result_url: str | None = None,
        step_id: int | None = None,
    ) -> tuple[int, int | None]:
        now = utc_now_iso()
        finished_at = now if status in {"completed", "failed", "cancelled"} else None
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO search_runs (
                    query_text, states_json, agency_types_json, max_districts,
                    max_pages_per_district, search_method, search_provider,
                    cancel_requested, debug_logging, status, districts_matched,
                    districts_searched, districts_failed, started_at, finished_at
                ) VALUES (?, '[]', '[]', 2, 10, ?, ?, 0, 0, ?, 2, ?, 0, ?, ?)
                """,
                (
                    query,
                    method,
                    method,
                    status,
                    2 if status == "completed" else 0,
                    now,
                    finished_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            result_id: int | None = None
            if result_url:
                result_id = int(
                    conn.execute(
                        """
                        INSERT INTO search_results (
                            search_run_id, district_id, district_name, state, agency_type,
                            result_rank, url, title, content_type, status_code,
                            search_source, score, snippet, matched_terms_json, created_at
                        ) VALUES (?, ?, 'Alpha', 'OR', 'Regular', 1, ?,
                                  'Community Schools', 'text/html', 200, 'test', 55,
                                  'Alpha names a partner for its active program.',
                                  '["community school", "partner"]', ?)
                        """,
                        (run_id, self.district_ids[0], result_url, now),
                    ).lastrowid
                )
            conn.commit()
        storage.link_child_run(
            session_id,
            "search",
            run_id,
            step_id=step_id,
            round_number=round_number,
            query_text=query,
            purpose="Find district-owned program evidence.",
            db_path=self.db_path,
        )
        return run_id, result_id

    def _profile_child(
        self,
        session_id: int,
        *,
        status: str,
        step_id: int | None = None,
    ) -> int:
        run_id = create_profile_discovery_run(
            [],
            [],
            None,
            None,
            [],
            "",
            len(self.district_ids),
            1,
            "community schools",
            False,
            district_ids=self.district_ids,
            status="queued",
            db_path=self.db_path,
        )
        finished_at = utc_now_iso() if status in {"completed", "failed", "cancelled"} else None
        with connect_db(self.db_path) as conn:
            conn.execute(
                """
                UPDATE profile_discovery_runs
                SET status = ?, districts_processed = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    len(self.district_ids) if status == "completed" else 0,
                    finished_at,
                    run_id,
                ),
            )
            conn.commit()
        storage.link_child_run(
            session_id,
            "profile_discovery",
            run_id,
            step_id=step_id,
            purpose="Improve exact-cohort profile coverage",
            db_path=self.db_path,
        )
        return run_id

    def _insert_working_profiles(self) -> None:
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            for district_id in self.district_ids:
                conn.execute(
                    """
                    INSERT INTO district_search_profiles (
                        district_id, website_normalized, profile_status,
                        search_url_template, search_method, last_discovered_at,
                        created_at, updated_at
                    ) VALUES (?, ?, 'working', ?, 'GET', ?, ?, ?)
                    """,
                    (
                        district_id,
                        f"https://district-{district_id}.example",
                        f"https://district-{district_id}.example/search?q={{query}}",
                        now,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def test_paused_cancellation_finalizes_and_active_children_receive_flags(self) -> None:
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)
        for status in ("ready", "needs_review"):
            with self.subTest(status=status):
                session_id = self._session(status=status)
                storage.request_cancellation(session_id, db_path=self.db_path)
                self.assertEqual(orchestrator.advance(session_id).status, "cancelled")
                self.assertEqual(
                    storage.get_session(session_id, self.db_path)["status"],
                    "cancelled",
                )

        session_id = self._session(status="needs_review")
        search_run_id, _ = self._search_child(session_id, status="running")
        profile_run_id = self._profile_child(session_id, status="running")
        storage.request_cancellation(session_id, db_path=self.db_path)

        self.assertEqual(orchestrator.advance(session_id).status, "needs_review")
        with connect_db(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT cancel_requested FROM search_runs WHERE id = ?",
                    (search_run_id,),
                ).fetchone()["cancel_requested"],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT cancel_requested FROM profile_discovery_runs WHERE id = ?",
                    (profile_run_id,),
                ).fetchone()["cancel_requested"],
                1,
            )
            conn.execute(
                "UPDATE search_runs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (utc_now_iso(), search_run_id),
            )
            conn.execute(
                "UPDATE profile_discovery_runs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (utc_now_iso(), profile_run_id),
            )
            conn.commit()
        self.assertEqual(orchestrator.advance(session_id).status, "cancelled")

    def test_evaluator_user_input_transition_honors_two_round_limit(self) -> None:
        evaluation = _evaluation(
            "needs_user_input",
            reason="Which partnership program should be treated as authoritative?",
        )
        for clarification_round, expected in ((0, "needs_clarification"), (2, "needs_review")):
            with self.subTest(clarification_round=clarification_round):
                session_id = self._session(
                    status="evaluating",
                    plan=_plan(max_rounds=3),
                    round_number=1,
                    clarification_round=clarification_round,
                )
                self._search_child(
                    session_id,
                    result_url=f"https://alpha.example/evidence-{clarification_round}",
                )
                with patch(
                    "guided_search.orchestrator.evaluate_search_results",
                    return_value=AIResult(evaluation, (_call("guided-evaluate-v1"),)),
                ):
                    outcome = GuidedSearchOrchestrator(db_path=self.db_path).advance(
                        session_id
                    )

                self.assertEqual(outcome.status, expected)
                session = storage.get_session(session_id, self.db_path)
                self.assertEqual(session["status"], expected)
                if expected == "needs_clarification":
                    self.assertEqual(
                        session["clarification_questions"],
                        [evaluation.short_reason],
                    )
                else:
                    self.assertIn("clarification-round limit", session["review_reason"])

    def test_planner_clarification_transition_honors_two_round_limit(self) -> None:
        raw = _plan().to_dict()
        raw.update(
            {
                "needs_clarification": True,
                "clarification_questions": [
                    "Should historical partnership pages count?"
                ],
                "queries": [],
            }
        )
        clarification_plan = SearchPlan.from_dict(raw, mode="balanced")
        for clarification_round, expected in ((0, "needs_clarification"), (2, "needs_review")):
            with self.subTest(clarification_round=clarification_round):
                session_id = storage.create_session(
                    "Find direct evidence of community-school partnerships.",
                    scope={"states": ["OR"], "max_districts": 2},
                    strategy_mode="balanced",
                    status="planning",
                    stage="planning",
                    db_path=self.db_path,
                )
                storage.update_session(
                    session_id,
                    clarification_round=clarification_round,
                    db_path=self.db_path,
                )
                with patch(
                    "guided_search.orchestrator.plan_search",
                    return_value=AIResult(
                        clarification_plan,
                        (_call("guided-plan-v1"),),
                    ),
                ):
                    outcome = GuidedSearchOrchestrator(
                        db_path=self.db_path
                    ).advance(session_id)

                self.assertEqual(outcome.status, expected)
                session = storage.get_session(session_id, self.db_path)
                self.assertEqual(session["status"], expected)
                if expected == "needs_clarification":
                    self.assertEqual(
                        session["clarification_questions"],
                        list(clarification_plan.clarification_questions),
                    )
                else:
                    self.assertIn("clarification", session["review_reason"].casefold())

    def test_persisted_brave_plan_loads_and_falls_back_when_key_disappears(self) -> None:
        plan = _plan(preferred_method="brave", max_rounds=2)
        session_id = self._session(
            status="queued",
            plan=plan,
            brave_allowed=True,
        )
        queued: list[int] = []
        orchestrator = GuidedSearchOrchestrator(
            db_path=self.db_path,
            enqueue_search=queued.append,
        )

        with patch(
            "guided_search.orchestrator.has_brave_search_api_key",
            return_value=False,
        ):
            self.assertEqual(orchestrator._plan_model(storage.get_session(session_id, self.db_path)).preferred_search_method, "brave")
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "searching")
        self.assertEqual(len(queued), 1)
        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT search_method, search_provider FROM search_runs WHERE id = ?",
                (queued[0],),
            ).fetchone()
        self.assertEqual(run["search_method"], "crawler")
        self.assertEqual(run["search_provider"], "crawler")

    def test_duplicate_manual_query_is_skipped_when_no_distinct_query_remains(self) -> None:
        plan = _plan(max_rounds=3, max_children=3)
        session_id = self._session(status="ready", plan=plan, round_number=1)
        self._search_child(session_id, query=plan.queries[0].query_text)
        request_manual_query(
            session_id,
            plan.queries[0].query_text,
            db_path=self.db_path,
        )

        outcome = GuidedSearchOrchestrator(db_path=self.db_path).advance(session_id)

        self.assertEqual(outcome.status, "summarizing")
        manual_steps = [
            step
            for step in storage.list_steps(session_id, db_path=self.db_path)
            if step["step_type"] == "manual_query"
        ]
        self.assertEqual(len(manual_steps), 1)
        self.assertEqual(manual_steps[0]["status"], "skipped")
        self.assertIn("No distinct validated queries", manual_steps[0]["error_message"])

    def test_profile_only_summary_context_reports_profile_and_zero_search_rounds(self) -> None:
        session_id = self._session(status="summarizing", plan=_plan())
        profile_run_id = self._profile_child(session_id, status="completed")
        captured: dict[str, object] = {}

        def summarize(context: dict[str, object], **_: object) -> AIResult[GuidedSummary]:
            captured.update(context)
            return AIResult(_summary(), (_call("guided-summary-v1"),))

        with patch(
            "guided_search.orchestrator.summarize_guided_search",
            side_effect=summarize,
        ):
            outcome = GuidedSearchOrchestrator(db_path=self.db_path).advance(session_id)

        self.assertEqual(outcome.status, "completed")
        metrics = captured["deterministic_aggregate_metrics"]
        self.assertEqual(metrics["rounds_completed"], 0)
        self.assertEqual(metrics["search_child_runs"], 0)
        self.assertEqual(metrics["profile_discovery_runs"], 1)
        self.assertTrue(metrics["profile_discovery_performed"])
        self.assertEqual(
            captured["deterministic_latest_round_metrics"]["round_number"],
            0,
        )
        self.assertEqual(captured["deterministic_search_history_metrics_and_decisions"], [])
        self.assertEqual(captured["child_runs"][0]["child_run_id"], profile_run_id)
        self.assertEqual(captured["child_runs"][0]["child_type"], "profile_discovery")

    def test_completed_search_then_evaluator_failure_preserves_evidence(self) -> None:
        session_id = self._session(
            status="searching",
            plan=_plan(max_rounds=3),
            round_number=1,
        )
        self._search_child(
            session_id,
            result_url="https://alpha.example/community-schools?utm_source=edge",
        )
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)
        self.assertEqual(orchestrator.advance(session_id).status, "evaluating")
        self.assertEqual(
            len(storage.list_evidence(session_id, db_path=self.db_path)),
            1,
        )
        failure = GuidedAIError(
            "Evaluator is unavailable.",
            calls=(_call("guided-evaluate-v1", success=False),),
        )

        with patch(
            "guided_search.orchestrator.evaluate_search_results",
            side_effect=failure,
        ):
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "needs_review")
        self.assertEqual(
            len(storage.list_evidence(session_id, db_path=self.db_path)),
            1,
        )
        self.assertEqual(storage.get_session(session_id, self.db_path)["results_found"], 1)
        calls = storage.list_model_calls(session_id, db_path=self.db_path)
        self.assertEqual(len(calls), 1)
        self.assertFalse(bool(calls[0]["success"]))

    def test_poor_evaluation_launches_a_distinct_second_query_round(self) -> None:
        plan = _plan(max_rounds=2, max_children=3)
        session_id = self._session(
            status="searching",
            plan=plan,
            round_number=1,
        )
        self._search_child(
            session_id,
            result_url="https://alpha.example/first-round",
        )
        queued: list[int] = []
        orchestrator = GuidedSearchOrchestrator(
            db_path=self.db_path,
            enqueue_search=queued.append,
        )
        self.assertEqual(orchestrator.advance(session_id).status, "evaluating")
        evaluation = _evaluation(
            "run_additional_query",
            proposed_query='"community school" AND implementation',
        )

        with patch(
            "guided_search.orchestrator.evaluate_search_results",
            return_value=AIResult(evaluation, (_call("guided-evaluate-v1"),)),
        ):
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "searching")
        self.assertEqual(storage.get_session(session_id, self.db_path)["round_number"], 2)
        children = storage.list_child_runs(
            session_id,
            child_type="search",
            db_path=self.db_path,
        )
        self.assertEqual(len(children), 2)
        self.assertEqual(children[-1]["round_number"], 2)
        self.assertEqual(children[-1]["query_text"], evaluation.proposed_queries[0].query_text)
        self.assertEqual(queued, [children[-1]["child_run_id"]])

    def test_max_child_budget_stops_before_launching_proposed_query(self) -> None:
        plan = _plan(max_rounds=3, max_children=1)
        session_id = self._session(
            status="searching",
            plan=plan,
            round_number=1,
        )
        self._search_child(
            session_id,
            result_url="https://alpha.example/max-child",
        )
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)
        self.assertEqual(orchestrator.advance(session_id).status, "evaluating")
        evaluation = _evaluation(
            "run_additional_query",
            proposed_query='"community school" AND services',
        )

        with patch(
            "guided_search.orchestrator.evaluate_search_results",
            return_value=AIResult(evaluation, (_call("guided-evaluate-v1"),)),
        ):
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "summarizing")
        self.assertEqual(
            len(storage.list_child_runs(session_id, child_type="search", db_path=self.db_path)),
            1,
        )
        stops = [
            step
            for step in storage.list_steps(session_id, db_path=self.db_path)
            if step["step_type"] == "stop_condition"
        ]
        self.assertIn("Maximum child search runs", stops[-1]["short_description"])

    def test_second_round_with_no_new_url_stops_deterministically(self) -> None:
        plan = _plan(max_rounds=3, max_children=3)
        session_id = self._session(
            status="searching",
            plan=plan,
            round_number=2,
        )
        repeated_url = "https://alpha.example/repeated-evidence"
        self._search_child(
            session_id,
            round_number=1,
            result_url=repeated_url,
        )
        self._search_child(
            session_id,
            query='"community school" AND implementation',
            round_number=2,
            result_url=repeated_url,
        )
        orchestrator = GuidedSearchOrchestrator(db_path=self.db_path)
        self.assertEqual(orchestrator.advance(session_id).status, "evaluating")
        evaluation = _evaluation(
            "run_additional_query",
            proposed_query='"community school" AND outcomes',
        )

        with patch(
            "guided_search.orchestrator.evaluate_search_results",
            return_value=AIResult(evaluation, (_call("guided-evaluate-v1"),)),
        ):
            outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "summarizing")
        stops = [
            step
            for step in storage.list_steps(session_id, db_path=self.db_path)
            if step["step_type"] == "stop_condition"
        ]
        self.assertIn("no meaningful new URLs", stops[-1]["short_description"])

    def test_post_profile_coverage_reselects_method_and_working_profiles_are_skipped(self) -> None:
        plan = _plan(profile_policy="discover_missing", max_rounds=2)
        session_id = self._session(status="profiling", plan=plan)
        step_id = storage.add_step(
            session_id,
            "profile_discovery",
            input_data={"district_ids": self.district_ids, "followup_queries": []},
            db_path=self.db_path,
        )
        self._profile_child(session_id, status="completed", step_id=step_id)
        self._insert_working_profiles()
        search_queue: list[int] = []
        profile_queue: list[int] = []
        orchestrator = GuidedSearchOrchestrator(
            db_path=self.db_path,
            enqueue_search=search_queue.append,
            enqueue_profile=profile_queue.append,
        )

        self.assertEqual(
            orchestrator._profile_candidate_ids(self.district_ids, "discover_missing"),
            [],
        )
        outcome = orchestrator.advance(session_id)

        self.assertEqual(outcome.status, "searching")
        self.assertEqual(profile_queue, [])
        self.assertEqual(len(search_queue), 1)
        with connect_db(self.db_path) as conn:
            method = conn.execute(
                "SELECT search_method FROM search_runs WHERE id = ?",
                (search_queue[0],),
            ).fetchone()["search_method"]
        self.assertEqual(method, "district_search_hybrid")

    def test_planner_context_contains_pasted_example_text(self) -> None:
        example_text = "Use this pasted example: district names an active nonprofit partner."
        session_id = storage.create_session(
            "Find direct evidence of community-school partnerships.",
            example_text=example_text,
            scope={
                "states": ["OR"],
                "agency_types": ["Regular"],
                "max_districts": 2,
            },
            strategy_mode="balanced",
            status="planning",
            stage="planning",
            db_path=self.db_path,
        )
        captured: dict[str, object] = {}

        def plan_search(context: dict[str, object], **_: object) -> AIResult[SearchPlan]:
            captured.update(context)
            return AIResult(_plan(), (_call("guided-plan-v1"),))

        with patch(
            "guided_search.orchestrator.plan_search",
            side_effect=plan_search,
        ):
            outcome = GuidedSearchOrchestrator(db_path=self.db_path).advance(session_id)

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(captured["example_text"], example_text)
        self.assertEqual(captured["original_user_objective_verbatim"], "Find direct evidence of community-school partnerships.")


if __name__ == "__main__":
    unittest.main()
