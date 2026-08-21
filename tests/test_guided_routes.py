from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup

import common
from common import connect_db, init_db, utc_now_iso
from guided_search import storage
from guided_search.models import SearchPlan


os.environ["EDSCANNER_DISABLE_WORKER"] = "1"


def _valid_plan() -> SearchPlan:
    return SearchPlan.from_dict(
        {
            "objective": "Find districts with direct restorative-justice program evidence.",
            "needs_clarification": False,
            "clarification_questions": [],
            "search_concepts": [
                {
                    "name": "restorative justice program",
                    "required": True,
                    "synonyms": ["restorative practices"],
                    "exclude_terms": ["generic discipline policy"],
                }
            ],
            "queries": [
                {
                    "query_text": '"restorative justice" AND program',
                    "purpose": "Find direct program evidence.",
                    "priority": 1,
                }
            ],
            "profile_policy": "discover_missing",
            "preferred_search_method": "crawler",
            "evaluation_criteria": {
                "positive_signals": ["program implementation"],
                "negative_signals": ["generic discipline policy"],
                "minimum_evidence": "A district-owned page describing implementation.",
            },
            "stop_policy": {"max_rounds": 3, "max_child_search_runs": 6},
            "resource_policy": {"initial_workers": 3, "max_workers": 6},
            "short_explanation": "Start narrowly and evaluate direct district evidence.",
        },
        mode="balanced",
    )


class GuidedSearchRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.import_temp = TemporaryDirectory()
        self.imports_dir = Path(self.import_temp.name)
        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.imports_patch = patch.object(common, "IMPORTS_DIR", self.imports_dir)
        self.db_patch.start()
        self.imports_patch.start()
        init_db(self.db_path)
        self._insert_districts()

        if "app" not in sys.modules:
            self.app_module = importlib.import_module("app")
        else:
            self.app_module = sys.modules["app"]
        self.app_module.app.config.update(TESTING=True, SECRET_KEY="guided-route-tests")
        self.client = self.app_module.app.test_client()

    def tearDown(self) -> None:
        self.imports_patch.stop()
        self.db_patch.stop()
        self.import_temp.cleanup()
        self.db_path.unlink(missing_ok=True)

    def _insert_districts(self) -> None:
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            for ordinal, (name, state, agency_type, enrollment) in enumerate(
                (
                    ("Alpha", "OR", "Regular", 500),
                    ("Bravo", "OR", "Charter", 250),
                    ("Charlie", "WA", "Regular", 900),
                ),
                start=1,
            ):
                conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('routes.csv', ?, ?, ?, ?, ?, ?, ?, ?, 1, '{}', ?, ?)
                    """,
                    (
                        ordinal,
                        f"routes-{ordinal}",
                        name,
                        state,
                        agency_type,
                        enrollment,
                        f"https://{name.casefold()}.example",
                        f"https://{name.casefold()}.example",
                        now,
                        now,
                    ),
                )
            conn.commit()

    def test_wizard_uses_database_options_and_scope_preview(self) -> None:
        with patch("guided_search.web.local_llm_is_configured", return_value=True):
            response = self.client.get("/guided-search/")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        self.assertEqual(
            {option.get("value") for option in soup.select("#guided_states option")},
            {"OR", "WA"},
        )
        self.assertEqual(
            {option.get("value") for option in soup.select("#guided_agency_types option")},
            {"Regular", "Charter"},
        )
        preview = self.client.get(
            "/guided-search/scope-count?states=OR&agency_types=Regular&min_enrollment=400&max_districts=10"
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json(), {"matching_count": 1, "planned_count": 1})

    def test_intake_persists_verbatim_goal_and_enqueues_only_background_planning(self) -> None:
        queued: list[int] = []
        objective = "  Find restorative justice programs, not generic discipline policies.\n"
        with (
            patch("guided_search.web.local_llm_is_configured", return_value=True),
            patch("guided_search.web.get_ollama_model", return_value="qwen3.5:9b"),
            patch("guided_search.web.enqueue_guided_search", side_effect=queued.append),
            patch(
                "guided_search.orchestrator.plan_search",
                side_effect=AssertionError("AI must not run in the request thread"),
            ),
        ):
            response = self.client.post(
                "/guided-search/",
                data={
                    "objective": objective,
                    "states": ["OR"],
                    "agency_types": ["Regular", "Charter"],
                    "min_enrollment": "100",
                    "max_enrollment": "700",
                    "max_districts": "2",
                    "example_text": "A positive example mentions implementation teams.",
                    "example_url": "https://public.example/article",
                    "strategy_mode": "balanced",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertEqual(len(queued), 1)
        session = storage.get_session(queued[0], self.db_path)
        self.assertEqual(session["original_objective"], objective)
        self.assertEqual(session["status"], "planning")
        self.assertFalse(session["brave_allowed"])
        self.assertEqual(session["scope"]["states"], ["OR"])
        self.assertEqual(session["scope"]["max_districts"], 2)
        self.assertEqual(session["ai_model"], "qwen3.5:9b")

    def test_plan_review_start_and_manual_search_remain_separate(self) -> None:
        session_id = storage.create_session(
            "Find restorative justice program implementation.",
            scope={
                "states": ["OR"],
                "agency_types": ["Regular"],
                "min_enrollment": None,
                "max_enrollment": None,
                "max_districts": 1,
            },
            strategy_mode="balanced",
            status="ready",
            stage="plan_review",
            ai_model="test-model",
            max_rounds=3,
            max_child_search_runs=6,
            db_path=self.db_path,
        )
        storage.update_session(
            session_id,
            latest_plan=_valid_plan().to_dict(),
            profile_coverage={"working": 0, "requires_javascript": 0, "missing": 1, "review_or_error": 0},
            district_count=1,
            db_path=self.db_path,
        )
        detail = self.client.get(f"/guided-search/{session_id}")
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        body = detail.get_data(as_text=True)
        self.assertIn("Validated search plan", body)
        self.assertIn("Start Guided Search", body)
        detail_soup = BeautifulSoup(body, "html.parser")
        self.assertEqual(
            detail_soup.select_one(".query-plan-list code").get_text(strip=True),
            '"restorative justice" AND program',
        )

        queued: list[int] = []
        with patch("guided_search.web.enqueue_guided_search", side_effect=queued.append):
            started = self.client.post(f"/guided-search/{session_id}/start", follow_redirects=False)
        self.assertEqual(started.status_code, 302)
        self.assertEqual(queued, [session_id])
        self.assertEqual(storage.get_session(session_id, self.db_path)["status"], "queued")

        manual = self.client.get("/search")
        self.assertEqual(manual.status_code, 200)
        manual_soup = BeautifulSoup(manual.get_data(as_text=True), "html.parser")
        for field_name in ("query_text", "search_method", "max_districts", "max_workers"):
            self.assertIsNotNone(manual_soup.select_one(f'[name="{field_name}"]'))

    def test_wizard_rejects_unknown_scope_values_without_creating_session(self) -> None:
        with patch("guided_search.web.local_llm_is_configured", return_value=True):
            response = self.client.post(
                "/guided-search/",
                data={
                    "objective": "Find community school programs.",
                    "states": ["NOT-A-STATE"],
                    "max_districts": "10",
                    "strategy_mode": "balanced",
                },
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("not in the imported data", response.get_data(as_text=True))
        self.assertEqual(storage.list_sessions(db_path=self.db_path), [])


if __name__ == "__main__":
    unittest.main()
