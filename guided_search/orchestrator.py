from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ai_matcher import get_ollama_model
from common import (
    BRAVE_SEARCH_API_KEY_ENV,
    GUIDED_SEARCH_RUN_LOGS_DIR,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    connect_db,
    get_local_setting,
    has_brave_search_api_key,
    utc_now_iso,
)
from profile_runs import create_profile_discovery_run, district_search_coverage
from search_engine import (
    SEARCH_METHODS,
    RunDebugLogger,
    SearchSettings,
    create_search_run,
    list_matching_districts,
    parse_search_query,
)

from . import storage
from .ai import (
    AIResult,
    GuidedAIError,
    StructuredOllamaClient,
    evaluate_search_results,
    plan_search,
    revise_search_plan,
    summarize_guided_search,
)
from .evidence import EvidenceBundle, build_evidence_bundle, canonical_evidence_url, effectively_same_query
from .example_content import ExampleContentFetcher
from .models import (
    GuidedSummary,
    PlanRevision,
    SearchEvaluation,
    SearchPlan,
    SearchQuery,
    mode_policy,
)
from .prompts import (
    EVALUATE_PROMPT_VERSION,
    PLAN_PROMPT_VERSION,
    REVISE_PROMPT_VERSION,
    SUMMARY_PROMPT_VERSION,
)


LOGGER = logging.getLogger(__name__)
WAIT_FOR_CHILD_SECONDS = 3.0
MAX_CLARIFICATION_ROUNDS = 2
TERMINAL_CHILD_STATUSES = frozenset({"completed", "completed_with_errors", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class AdvanceOutcome:
    status: str
    wake_after_seconds: float | None = None
    description: str = ""


def _as_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _iso_wake_after(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=max(0.0, seconds))
    ).isoformat()


class GuidedSearchOrchestrator:
    """Advance one persisted Guided Search state-machine stage at a time."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        ai_client: StructuredOllamaClient | None = None,
        example_fetcher: ExampleContentFetcher | None = None,
        enqueue_search: Callable[[int], None] | None = None,
        enqueue_profile: Callable[[int], None] | None = None,
    ) -> None:
        self.db_path = db_path
        self.ai_client = ai_client
        self.example_fetcher = example_fetcher or ExampleContentFetcher()
        self._enqueue_search = enqueue_search
        self._enqueue_profile = enqueue_profile

    def _search_enqueuer(self) -> Callable[[int], None]:
        if self._enqueue_search is not None:
            return self._enqueue_search
        from run_workers import enqueue_search_run

        return enqueue_search_run

    def _profile_enqueuer(self) -> Callable[[int], None]:
        if self._enqueue_profile is not None:
            return self._enqueue_profile
        from run_workers import enqueue_profile_discovery_run

        return enqueue_profile_discovery_run

    def _debug_logger(self, session: Mapping[str, Any]) -> RunDebugLogger:
        configured = str(session.get("debug_log_path") or "").strip()
        path = Path(configured) if configured else GUIDED_SEARCH_RUN_LOGS_DIR / f"guided-search-{int(session['id'])}.log"
        if not configured:
            storage.update_session(int(session["id"]), debug_log_path=str(path), db_path=self.db_path)
        return RunDebugLogger(path)

    def _log(self, session: Mapping[str, Any], event: str, **fields: Any) -> None:
        try:
            self._debug_logger(session).log(event, session_id=int(session["id"]), **fields)
        except Exception:
            LOGGER.exception("Could not write Guided Search debug event %s", event)

    def _audit_calls(
        self,
        session_id: int,
        step_id: int,
        task_type: str,
        calls: Iterable[Any],
    ) -> None:
        session = storage.get_session(session_id, self.db_path)
        for call in calls:
            storage.record_model_call(
                session_id,
                task_type=task_type,
                prompt_version=str(call.prompt_version),
                step_id=step_id,
                model=str(call.model or ""),
                endpoint_identity=str(call.endpoint or ""),
                attempt=int(call.attempt),
                success=bool(call.success),
                validation_status=str(call.validation_status or ""),
                latency_ms=round(float(call.latency_seconds or 0) * 1000),
                prompt_tokens=call.prompt_eval_count,
                completion_tokens=call.eval_count,
                total_duration_ms=(round(int(call.total_duration_ns) / 1_000_000) if call.total_duration_ns is not None else None),
                metadata={"repair": bool(call.repair)},
                error_message=call.error,
                db_path=self.db_path,
            )
            if session is not None:
                self._log(
                    session,
                    "ai_call_attempt",
                    task_type=task_type,
                    step_id=step_id,
                    endpoint=str(call.endpoint or ""),
                    model=str(call.model or ""),
                    prompt_version=str(call.prompt_version),
                    attempt=int(call.attempt),
                    repair=bool(call.repair),
                    success=bool(call.success),
                    validation_status=str(call.validation_status or ""),
                    latency_seconds=float(call.latency_seconds or 0),
                    error=call.error,
                )

    def _ai_failure(
        self,
        session: Mapping[str, Any],
        step_id: int,
        task_type: str,
        exc: GuidedAIError,
    ) -> AdvanceOutcome:
        self._audit_calls(int(session["id"]), step_id, task_type, exc.calls)
        storage.finish_step(
            step_id,
            status="needs_review",
            error_message=str(exc),
            short_description=f"Local AI {task_type} needs review",
            db_path=self.db_path,
        )
        storage.transition_session(
            int(session["id"]),
            "needs_review",
            stage=task_type,
            updates={
                "review_reason": str(exc),
                "error_message": str(exc),
                "next_wake_at": None,
            },
            db_path=self.db_path,
        )
        self._log(session, "ai_call_failed", task_type=task_type, error=str(exc))
        return AdvanceOutcome("needs_review", description=str(exc))

    def _cancel_if_requested(self, session_id: int) -> AdvanceOutcome | None:
        current = storage.get_session(session_id, self.db_path)
        if (
            current
            and bool(current.get("cancel_requested"))
            and current["status"] not in storage.GUIDED_SEARCH_TERMINAL_STATUSES
        ):
            return self._cancel(current)
        return None

    def _coverage(self, cohort_ids: Sequence[int]) -> dict[str, Any]:
        raw = district_search_coverage(district_ids=cohort_ids, db_path=self.db_path)
        coverage = dict(raw)
        coverage.setdefault("matching", coverage.get("matching_count", len(cohort_ids)))
        coverage.setdefault("working", coverage.get("working_count", 0))
        coverage.setdefault("requires_javascript", coverage.get("javascript_count", 0))
        coverage.setdefault("missing", coverage.get("missing_count", 0))
        coverage.setdefault("stale", coverage.get("stale_count", 0))
        review_keys = (
            "manual_review",
            "error",
            "search_found_but_failed",
            "blocked_by_challenge",
            "blocked_by_robots",
            "no_search_found",
            "external_search_only",
            "javascript_unusable",
        )
        coverage.setdefault(
            "review_or_error",
            sum(int(coverage.get(key, coverage.get(f"{key}_count", 0)) or 0) for key in review_keys),
        )
        return coverage

    def _available_methods(self, session: Mapping[str, Any]) -> tuple[str, ...]:
        methods = set(SEARCH_METHODS)
        if not bool(session.get("brave_allowed")) or not has_brave_search_api_key():
            methods -= {"brave", "hybrid"}
        if not mode_policy(str(session.get("strategy_mode") or "balanced")).allow_browser_profiles:
            methods -= {"district_search_browser", "district_search_browser_hybrid"}
        return tuple(sorted(methods))

    def _freeze_planning_cohort(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        if bool(session.get("cohort_frozen")):
            return storage.get_cohort(int(session["id"]), self.db_path)
        scope = _as_dict(session.get("scope"))
        limit = max(1, min(int(scope.get("max_districts") or MAX_TOTAL_DISTRICTS_PER_RUN), MAX_TOTAL_DISTRICTS_PER_RUN))
        districts = list_matching_districts(
            list(scope.get("states") or []),
            list(scope.get("agency_types") or []),
            scope.get("min_enrollment"),
            scope.get("max_enrollment"),
            limit=limit,
            db_path=self.db_path,
        )
        storage.freeze_cohort(int(session["id"]), [int(row["id"]) for row in districts], db_path=self.db_path)
        return districts

    def _example_context(self, session: Mapping[str, Any], step_id: int) -> str:
        existing = str(session.get("example_context") or "").strip()
        if existing or not str(session.get("example_url") or "").strip():
            return existing
        fetched = self.example_fetcher.fetch(str(session["example_url"]))
        text = str(getattr(fetched, "text", getattr(fetched, "content", "")) or "")
        examples = [
            dict(item)
            for item in (session.get("examples") or [])
            if isinstance(item, Mapping) and str(item.get("kind") or "") != "url"
        ]
        examples.append(
            {
                "kind": "url",
                "url": str(getattr(fetched, "final_url", session["example_url"])),
                "content_type": str(getattr(fetched, "content_type", "")),
                "characters": len(text),
            }
        )
        storage.update_session(
            int(session["id"]),
            example_context=text,
            examples=examples,
            db_path=self.db_path,
        )
        self._log(session, "example_url_retrieved", step_id=step_id, characters=len(text))
        return text

    def _plan(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        self._log(
            session,
            "guided_search_started",
            objective_characters=len(str(session.get("original_objective") or "")),
            strategy_mode=session.get("strategy_mode"),
        )
        step_id = storage.add_step(
            session_id,
            "ai_plan",
            short_description="Creating a validated search plan",
            input_data={"objective": session["original_objective"], "scope": session.get("scope")},
            attempt=int(session.get("clarification_round") or 0) + 1,
            db_path=self.db_path,
        )
        try:
            districts = self._freeze_planning_cohort(session)
            if not districts:
                raise ValueError("No searchable districts match the selected scope.")
            cohort_ids = [int(row["id"]) for row in districts]
            coverage = self._coverage(cohort_ids)
            example_context = self._example_context(session, step_id)
            context = {
                "original_user_objective_verbatim": str(session["original_objective"]),
                "scope": session.get("scope") or {},
                "district_count": len(cohort_ids),
                "profile_coverage": coverage,
                "example_text": str(session.get("example_text") or "")[:16_000],
                "example_url_content_untrusted_evidence": example_context[:24_000],
                "clarification_answers": session.get("clarification_answers") or [],
                "brave_configured": has_brave_search_api_key(),
            }
            result = plan_search(
                context,
                client=self.ai_client,
                mode=str(session.get("strategy_mode") or "balanced"),
                available_methods=self._available_methods(session),
                brave_allowed=bool(session.get("brave_allowed")),
            )
        except GuidedAIError as exc:
            return self._ai_failure(session, step_id, "planning", exc)
        except Exception as exc:
            storage.finish_step(step_id, status="needs_review", error_message=str(exc), db_path=self.db_path)
            storage.transition_session(
                session_id,
                "needs_review",
                stage="planning",
                updates={"review_reason": str(exc), "error_message": str(exc)},
                db_path=self.db_path,
            )
            self._log(session, "planning_failed", error=str(exc))
            return AdvanceOutcome("needs_review", description=str(exc))

        self._audit_calls(session_id, step_id, "planning", result.calls)
        self._log(
            session,
            "ai_call_succeeded",
            task_type="planning",
            model=result.successful_call.model,
            endpoint=result.successful_call.endpoint,
            prompt_version=result.successful_call.prompt_version,
            latency_seconds=result.successful_call.latency_seconds,
        )
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled
        plan = result.value
        plan_data = plan.to_dict()
        storage.finish_step(
            step_id,
            output_data=plan_data,
            short_description=("Planner requested clarification" if plan.needs_clarification else "Search plan created"),
            db_path=self.db_path,
        )
        common_updates = {
            "latest_plan": plan_data,
            "ai_model": result.successful_call.model,
            "prompt_versions": {
                **_as_dict(session.get("prompt_versions")),
                "plan": PLAN_PROMPT_VERSION,
            },
            "resource_policy": plan.resource_policy.to_dict(),
            "profile_coverage": coverage,
            "max_rounds": plan.stop_policy.max_rounds,
            "max_child_search_runs": plan.stop_policy.max_child_search_runs,
            "review_reason": None,
            "error_message": None,
        }
        if plan.needs_clarification:
            clarification_round = int(session.get("clarification_round") or 0)
            if clarification_round >= MAX_CLARIFICATION_ROUNDS:
                reason = "The planner still requires clarification after the two-round limit."
                storage.transition_session(
                    session_id,
                    "needs_review",
                    stage="clarification_limit",
                    updates={**common_updates, "review_reason": reason},
                    db_path=self.db_path,
                )
                return AdvanceOutcome("needs_review", description=reason)
            storage.transition_session(
                session_id,
                "needs_clarification",
                stage="clarification",
                updates={**common_updates, "clarification_questions": list(plan.clarification_questions)},
                db_path=self.db_path,
            )
            self._log(session, "clarification_requested", questions=list(plan.clarification_questions))
            return AdvanceOutcome("needs_clarification")

        storage.transition_session(
            session_id,
            "ready",
            stage="plan_review",
            updates={**common_updates, "clarification_questions": []},
            db_path=self.db_path,
        )
        self._log(session, "plan_validated", queries=[query.query_text for query in plan.queries])
        return AdvanceOutcome("ready")

    def _profile_candidate_ids(self, cohort_ids: Sequence[int], policy: str) -> list[int]:
        if policy in {"use_existing", "skip_profiles"} or not cohort_ids:
            return []
        placeholders = ",".join("?" for _ in cohort_ids)
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT d.id, p.profile_status, p.search_method,
                       p.search_url_template, p.last_discovered_at
                FROM districts d
                LEFT JOIN (
                    SELECT p1.* FROM district_search_profiles p1
                    JOIN (SELECT district_id, MAX(id) AS id FROM district_search_profiles GROUP BY district_id) latest
                      ON latest.id = p1.id
                ) p ON p.district_id = d.id
                WHERE d.id IN ({placeholders})
                """,
                list(cohort_ids),
            ).fetchall()
        discovery_statuses = {
            None,
            "error",
            "manual_review",
            "search_found_but_failed",
            "blocked_by_challenge",
            "blocked_by_robots",
            "no_search_found",
            "external_search_only",
        }
        if policy == "discover_missing":
            return [
                int(row["id"])
                for row in rows
                if row["profile_status"] in discovery_statuses
                or (
                    row["profile_status"] == "requires_javascript"
                    and (
                        row["search_method"] != "GET"
                        or not str(row["search_url_template"] or "").strip()
                    )
                )
            ]
        # Stale rediscovery is intentionally deterministic and conservative.
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=180)
        selected: list[int] = []
        for row in rows:
            if row["profile_status"] in discovery_statuses:
                selected.append(int(row["id"]))
                continue
            if row["profile_status"] == "requires_javascript" and (
                row["search_method"] != "GET"
                or not str(row["search_url_template"] or "").strip()
            ):
                selected.append(int(row["id"]))
                continue
            try:
                tested = datetime.fromisoformat(str(row["last_discovered_at"] or ""))
                if tested.tzinfo is None:
                    tested = tested.replace(tzinfo=timezone.utc)
            except ValueError:
                selected.append(int(row["id"]))
            else:
                if tested < cutoff:
                    selected.append(int(row["id"]))
        return selected

    def _plan_model(self, session: Mapping[str, Any]) -> SearchPlan:
        # A persisted plan remains structurally valid if a runtime capability
        # (notably the Brave key) disappears. Execution chooses a safe currently
        # available method; session-level paid-search consent is still enforced.
        return SearchPlan.from_dict(
            _as_dict(session.get("latest_plan")),
            mode=str(session.get("strategy_mode") or "balanced"),
            available_methods=SEARCH_METHODS,
            brave_allowed=bool(session.get("brave_allowed")),
        )

    def _update_validated_plan(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        *,
        queries: Sequence[SearchQuery] | None = None,
        preferred_search_method: str | None = None,
        profile_policy: str | None = None,
        short_explanation: str | None = None,
    ) -> SearchPlan:
        data = plan.to_dict()
        if queries is not None:
            data["queries"] = [query.to_dict() for query in queries]
        if preferred_search_method is not None:
            data["preferred_search_method"] = preferred_search_method
        if profile_policy is not None:
            data["profile_policy"] = profile_policy
        if short_explanation:
            data["short_explanation"] = short_explanation[:1_500]
        updated = SearchPlan.from_dict(
            data,
            mode=str(session.get("strategy_mode") or "balanced"),
            available_methods=SEARCH_METHODS,
            brave_allowed=bool(session.get("brave_allowed")),
        )
        storage.update_session(
            int(session["id"]),
            latest_plan=updated.to_dict(),
            db_path=self.db_path,
        )
        self._log(
            session,
            "query_plan_revised",
            queries=[query.query_text for query in updated.queries],
            preferred_search_method=updated.preferred_search_method,
            profile_policy=updated.profile_policy,
            short_explanation=updated.short_explanation,
        )
        return updated

    def _guided_resource_policy(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
    ) -> dict[str, Any]:
        return {
            **plan.resource_policy.to_dict(),
            "min_workers": 1,
            "initial_delay_seconds": (
                0.5 if session["strategy_mode"] == "fast" else 0.75
            ),
        }

    def _fail_staged_child(
        self,
        child_type: str,
        run_id: int,
        error: BaseException,
    ) -> None:
        table = (
            "search_runs"
            if child_type == "search"
            else "profile_discovery_runs"
        )
        try:
            with connect_db(self.db_path) as conn:
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET status = 'failed', finished_at = ?, error_message = ?
                    WHERE id = ? AND status = 'staging'
                    """,
                    (
                        utc_now_iso(),
                        f"Guided Search could not link this staged child: {error}",
                        int(run_id),
                    ),
                )
                conn.commit()
        except Exception:
            LOGGER.exception("Could not terminalize staged %s run %s", child_type, run_id)

    def _create_profile_child(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        *,
        step_id: int,
        district_ids: Sequence[int],
    ) -> int:
        run_id = create_profile_discovery_run(
            [],
            [],
            None,
            None,
            [],
            "",
            max(1, len(district_ids)),
            plan.resource_policy.initial_workers,
            "calendar",
            plan.profile_policy == "rediscover_stale",
            district_ids=list(district_ids),
            status="staging",
            db_path=self.db_path,
        )
        try:
            storage.link_child_run(
                int(session["id"]),
                "profile_discovery",
                run_id,
                step_id=step_id,
                round_number=int(session.get("round_number") or 0),
                purpose="Improve exact-cohort district search profile coverage",
                activate_from_staging=True,
                db_path=self.db_path,
            )
        except Exception as exc:
            self._fail_staged_child("profile_discovery", run_id, exc)
            raise
        self._log(
            session,
            "child_run_created",
            child_type="profile_discovery",
            child_run_id=run_id,
            step_id=step_id,
            district_count=len(district_ids),
        )
        self._profile_enqueuer()(run_id)
        return run_id

    def _create_search_child(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        *,
        step_id: int,
        round_number: int,
        query: SearchQuery,
        method: str,
        cohort_ids: Sequence[int],
        resource_policy: Mapping[str, Any],
    ) -> int:
        policy = mode_policy(str(session["strategy_mode"]))
        settings = SearchSettings(
            max_pages_per_district=policy.max_pages_per_district,
            max_total_districts_per_run=max(1, len(cohort_ids)),
            search_method=method,
            api_results_per_district=10,
            follow_depth=1 if session["strategy_mode"] == "thorough" else 0,
            browser_for_javascript=method
            in {"district_search_browser", "district_search_browser_hybrid"},
            brave_api_key=(
                get_local_setting(BRAVE_SEARCH_API_KEY_ENV)
                if session.get("brave_allowed")
                else ""
            ),
        )
        run_id = create_search_run(
            query.query_text,
            max_districts=len(cohort_ids),
            max_workers=plan.resource_policy.max_workers,
            debug_logging=True,
            db_path=self.db_path,
            settings=settings,
            status="staging",
            district_ids=list(cohort_ids),
            adaptive_enabled=True,
            resource_policy=dict(resource_policy),
        )
        try:
            storage.link_child_run(
                int(session["id"]),
                "search",
                run_id,
                step_id=step_id,
                round_number=round_number,
                query_text=query.query_text,
                purpose=query.purpose,
                activate_from_staging=True,
                db_path=self.db_path,
            )
        except Exception as exc:
            self._fail_staged_child("search", run_id, exc)
            raise
        self._log(
            session,
            "child_run_created",
            child_type="search",
            child_run_id=run_id,
            step_id=step_id,
            round_number=round_number,
            query_text=query.query_text,
            purpose=query.purpose,
            search_method=method,
        )
        self._search_enqueuer()(run_id)
        return run_id

    def _start_profile_stage(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        district_ids: Sequence[int],
        *,
        followup_queries: Sequence[SearchQuery] | None = None,
    ) -> AdvanceOutcome:
        session_id = int(session["id"])
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled
        step_id = storage.add_step(
            session_id,
            "profile_discovery",
            short_description=f"Discovering search profiles for {len(district_ids)} districts",
            input_data={
                "district_ids": list(district_ids),
                "followup_queries": [query.to_dict() for query in followup_queries or ()],
                "profile_policy": plan.profile_policy,
            },
            db_path=self.db_path,
        )
        resource = plan.resource_policy
        run_id = self._create_profile_child(
            session,
            plan,
            step_id=step_id,
            district_ids=district_ids,
        )
        storage.transition_session(
            session_id,
            "profiling",
            stage="profile_discovery",
            updates={"current_workers": resource.initial_workers, "next_wake_at": _iso_wake_after(WAIT_FOR_CHILD_SECONDS)},
            db_path=self.db_path,
        )
        if followup_queries:
            self._complete_embedded_manual_steps(
                session_id,
                followup_queries,
                profile_first=True,
            )
        self._log(session, "profile_discovery_started", run_id=run_id, districts=len(district_ids))
        return AdvanceOutcome("profiling", WAIT_FOR_CHILD_SECONDS)

    def _choose_search_method(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        coverage: Mapping[str, Any],
        preferred_method: str | None = None,
    ) -> str:
        method = preferred_method or plan.preferred_search_method
        available = set(self._available_methods(session))
        if method not in available:
            method = (
                "crawler"
                if method in {"brave", "hybrid"}
                else (
                    "district_search_hybrid"
                    if "district_search_hybrid" in available
                    else "crawler"
                )
            )
        working = int(coverage.get("working", coverage.get("working_count", 0)) or 0)
        javascript = int(coverage.get("requires_javascript", coverage.get("javascript_count", 0)) or 0)
        matching = int(coverage.get("matching", coverage.get("matching_count", 0)) or 0)
        usable = working + javascript
        if method == "crawler" and matching > 0 and usable / matching >= 0.5:
            if javascript > 0 and "district_search_browser_hybrid" in available:
                method = "district_search_browser_hybrid"
            elif "district_search_hybrid" in available:
                method = "district_search_hybrid"
        if method == "district_search" and working <= 0:
            method = (
                "district_search_browser_hybrid"
                if javascript > 0 and "district_search_browser_hybrid" in available
                else "crawler"
            )
        if method == "district_search_browser" and working + javascript <= 0:
            method = "crawler"
        if (
            method == "district_search_hybrid"
            and working <= 0
            and javascript > 0
            and "district_search_browser_hybrid" in available
        ):
            method = "district_search_browser_hybrid"
        if method == "district_search_hybrid" and working + javascript <= 0:
            method = "crawler"
        if method == "district_search_browser" and javascript <= 0:
            method = "district_search"
        if method == "district_search_browser_hybrid" and javascript <= 0:
            method = "district_search_hybrid"
        return method

    def _used_queries(self, session_id: int) -> list[str]:
        return [
            str(child.get("query_text") or "")
            for child in storage.list_child_runs(session_id, child_type="search", db_path=self.db_path)
            if str(child.get("query_text") or "").strip()
        ]

    def _complete_embedded_manual_steps(
        self,
        session_id: int,
        queries: Sequence[SearchQuery],
        *,
        profile_first: bool = False,
    ) -> None:
        for step in storage.list_steps(session_id, db_path=self.db_path):
            if step.get("step_type") != "manual_query" or step.get("status") != "queued":
                continue
            query_text = str(_as_dict(step.get("input")).get("query_text") or "")
            if not any(
                effectively_same_query(query_text, query.query_text)
                for query in queries
            ):
                continue
            storage.finish_step(
                int(step["id"]),
                output_data={"accepted": True, "profile_first": profile_first},
                db_path=self.db_path,
            )

    def _reject_embedded_manual_steps(
        self,
        session_id: int,
        queries: Sequence[SearchQuery],
        reason: str,
    ) -> None:
        for step in storage.list_steps(session_id, db_path=self.db_path):
            if step.get("step_type") != "manual_query" or step.get("status") != "queued":
                continue
            query_text = str(_as_dict(step.get("input")).get("query_text") or "")
            if not any(
                effectively_same_query(query_text, query.query_text)
                for query in queries
            ):
                continue
            storage.finish_step(
                int(step["id"]),
                status="skipped",
                output_data={"accepted": False, "reason": reason},
                error_message=reason,
                short_description=f"Manual query skipped: {reason}"[:500],
                db_path=self.db_path,
            )

    def _used_query_methods(self, session_id: int) -> list[tuple[str, str]]:
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT child.query_text, run.search_method
                FROM guided_search_child_runs child
                JOIN search_runs run ON run.id = child.child_run_id
                WHERE child.session_id = ? AND child.child_type = 'search'
                ORDER BY child.id
                """,
                (int(session_id),),
            ).fetchall()
        return [
            (str(row["query_text"] or ""), str(row["search_method"] or ""))
            for row in rows
            if str(row["query_text"] or "").strip()
        ]

    def _start_search_round(
        self,
        session: Mapping[str, Any],
        plan: SearchPlan,
        queries: Sequence[SearchQuery],
        *,
        method_override: str | None = None,
    ) -> AdvanceOutcome:
        session_id = int(session["id"])
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled
        cohort_ids = storage.get_cohort_ids(session_id, self.db_path)
        children = storage.list_child_runs(session_id, child_type="search", db_path=self.db_path)
        max_children = min(plan.stop_policy.max_child_search_runs, int(session.get("max_child_search_runs") or plan.stop_policy.max_child_search_runs))
        remaining = max(0, max_children - len(children))
        if remaining <= 0:
            reason = "Maximum child search-run budget reached."
            self._reject_embedded_manual_steps(session_id, queries, reason)
            return self._begin_summarizing(session, reason)
        coverage = self._coverage(cohort_ids)
        method = self._choose_search_method(
            session,
            plan,
            coverage,
            preferred_method=method_override,
        )
        self._log(
            session,
            "search_method_selected",
            requested_method=method_override or plan.preferred_search_method,
            selected_method=method,
            coverage=coverage,
        )
        used = self._used_query_methods(session_id)
        selected: list[SearchQuery] = []
        for query in queries:
            parse_search_query(query.query_text)
            if any(
                effectively_same_query(query.query_text, prior_query)
                and prior_method == method
                for prior_query, prior_method in used
            ):
                continue
            if any(effectively_same_query(query.query_text, prior.query_text) for prior in selected):
                continue
            selected.append(query)
            if len(selected) >= min(mode_policy(session["strategy_mode"]).max_queries_per_round, remaining):
                break
        if not selected:
            reason = "No distinct validated queries remain within the run budget."
            self._reject_embedded_manual_steps(session_id, queries, reason)
            return self._begin_summarizing(session, reason)

        next_round = max(1, int(session.get("round_number") or 0) + 1)
        if next_round > min(plan.stop_policy.max_rounds, int(session.get("max_rounds") or plan.stop_policy.max_rounds)):
            reason = "Maximum search-round budget reached."
            self._reject_embedded_manual_steps(session_id, queries, reason)
            return self._begin_summarizing(session, reason)
        resource_policy = self._guided_resource_policy(session, plan)
        step_id = storage.add_step(
            session_id,
            "search_round",
            short_description=f"Search round {next_round} started with {len(selected)} query{'ies' if len(selected) != 1 else ''}",
            input_data={
                "round_number": next_round,
                "queries": [query.to_dict() for query in selected],
                "search_method": method,
                "resource_policy": resource_policy,
            },
            db_path=self.db_path,
        )
        for query in selected:
            self._create_search_child(
                session,
                plan,
                step_id=step_id,
                round_number=next_round,
                query=query,
                method=method,
                cohort_ids=cohort_ids,
                resource_policy=resource_policy,
            )
        storage.transition_session(
            session_id,
            "searching",
            stage=f"search_round_{next_round}",
            updates={
                "round_number": next_round,
                "profile_coverage": coverage,
                "current_workers": plan.resource_policy.initial_workers,
                "current_delay_seconds": resource_policy["initial_delay_seconds"],
                "next_wake_at": _iso_wake_after(WAIT_FOR_CHILD_SECONDS),
            },
            db_path=self.db_path,
        )
        self._complete_embedded_manual_steps(session_id, selected)
        self._log(session, "search_round_started", round=next_round, method=method, queries=[query.query_text for query in selected])
        return AdvanceOutcome("searching", WAIT_FOR_CHILD_SECONDS)

    def _resume_queued(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        plan = self._plan_model(session)
        # A manual override is persisted as a queued step, never executed in the HTTP request.
        manual_steps = [step for step in storage.list_steps(int(session["id"]), db_path=self.db_path) if step["step_type"] == "manual_query" and step["status"] == "queued"]
        manual_step: Mapping[str, Any] | None = None
        manual_query: SearchQuery | None = None
        if manual_steps:
            manual_step = manual_steps[-1]
            data = _as_dict(manual_step.get("input"))
            manual_query = SearchQuery.from_dict({"query_text": data.get("query_text"), "purpose": data.get("purpose") or "User-requested follow-up", "priority": 1})

        cohort_ids = storage.get_cohort_ids(int(session["id"]), self.db_path)
        profile_ids = self._profile_candidate_ids(cohort_ids, plan.profile_policy)
        existing_profile_children = storage.list_child_runs(int(session["id"]), child_type="profile_discovery", db_path=self.db_path)
        self._log(
            session,
            "profile_coverage_decision",
            profile_policy=plan.profile_policy,
            coverage=self._coverage(cohort_ids),
            candidate_count=len(profile_ids),
            profile_child_already_exists=bool(existing_profile_children),
            action=(
                "discover_profiles"
                if profile_ids and not existing_profile_children
                else "continue_to_search"
            ),
        )
        if profile_ids and not existing_profile_children:
            return self._start_profile_stage(
                session,
                plan,
                profile_ids,
                followup_queries=([manual_query] if manual_query is not None else None),
            )
        if manual_step is not None and manual_query is not None:
            return self._start_search_round(session, plan, [manual_query])
        return self._start_search_round(session, plan, plan.queries)

    def _recover_linked_stage(
        self,
        session: Mapping[str, Any],
    ) -> AdvanceOutcome | None:
        """Recover a durable child-stage intent after a process interruption.

        Guided children are created in an inert ``staging`` state and become
        executable atomically with their relationship row. The running step is
        the durable fan-out intent: recovery recreates a child missing before
        linkage and completes a partially linked multi-query round without
        repeating children that are already attached.
        """

        session_id = int(session["id"])
        steps = [
            step
            for step in storage.list_steps(session_id, db_path=self.db_path)
            if step.get("status") == "running"
            and step.get("step_type") in {"profile_discovery", "search_round"}
        ]
        if not steps:
            return None
        step = max(
            steps,
            key=lambda item: (int(item.get("sequence") or 0), int(item["id"])),
        )
        target = (
            "profiling"
            if step["step_type"] == "profile_discovery"
            else "searching"
        )
        if session["status"] == target:
            return None
        if target not in storage.ALLOWED_STATUS_TRANSITIONS.get(str(session["status"]), frozenset()):
            return None
        step_input = _as_dict(step.get("input"))
        links = [
            child
            for child in storage.list_child_runs(session_id, db_path=self.db_path)
            if int(child.get("step_id") or 0) == int(step["id"])
        ]
        created_run_ids: list[int] = []
        embedded_queries: list[SearchQuery] = []
        if target == "profiling":
            for raw in step_input.get("followup_queries") or []:
                if isinstance(raw, Mapping):
                    embedded_queries.append(SearchQuery.from_dict(raw))
            if not any(child["child_type"] == "profile_discovery" for child in links):
                plan = self._plan_model(session)
                cohort = set(storage.get_cohort_ids(session_id, self.db_path))
                district_ids = [
                    int(value)
                    for value in step_input.get("district_ids") or []
                    if int(value) in cohort
                ]
                if not district_ids:
                    raise ValueError(
                        "Interrupted profile stage has no valid exact-cohort districts."
                    )
                requested_policy = str(
                    step_input.get("profile_policy") or plan.profile_policy
                )
                if requested_policy != plan.profile_policy:
                    plan = self._update_validated_plan(
                        session,
                        plan,
                        profile_policy=requested_policy,
                    )
                created_run_ids.append(
                    self._create_profile_child(
                        session,
                        plan,
                        step_id=int(step["id"]),
                        district_ids=district_ids,
                    )
                )
        else:
            round_number = max(1, int(step_input.get("round_number") or 1))
            intended_queries: list[SearchQuery] = []
            for raw in step_input.get("queries") or []:
                if isinstance(raw, Mapping):
                    intended_queries.append(SearchQuery.from_dict(raw))
                else:
                    intended_queries.append(
                        SearchQuery.from_dict(
                            {
                                "query_text": str(raw),
                                "purpose": "Recovered Guided Search query",
                                "priority": 1,
                            }
                        )
                    )
            if not intended_queries:
                raise ValueError("Interrupted search stage has no validated queries.")
            embedded_queries = intended_queries
            linked_query_text = {
                str(child.get("query_text") or "").strip().casefold()
                for child in links
                if child["child_type"] == "search"
            }
            missing_queries = [
                query
                for query in intended_queries
                if query.query_text.strip().casefold() not in linked_query_text
            ]
            if missing_queries:
                plan = self._plan_model(session)
                cohort_ids = storage.get_cohort_ids(session_id, self.db_path)
                coverage = self._coverage(cohort_ids)
                method = self._choose_search_method(
                    session,
                    plan,
                    coverage,
                    preferred_method=str(step_input.get("search_method") or "")
                    or None,
                )
                resource_policy = _as_dict(step_input.get("resource_policy"))
                if not resource_policy:
                    resource_policy = self._guided_resource_policy(session, plan)
            for query in missing_queries:
                created_run_ids.append(
                    self._create_search_child(
                        session,
                        plan,
                        step_id=int(step["id"]),
                        round_number=round_number,
                        query=query,
                        method=method,
                        cohort_ids=cohort_ids,
                        resource_policy=resource_policy,
                    )
                )
                linked_query_text.add(query.query_text.strip().casefold())
        updates: dict[str, Any] = {
            "next_wake_at": utc_now_iso(),
        }
        if target == "searching":
            updates["round_number"] = max(
                int(session.get("round_number") or 0),
                int(step_input.get("round_number") or 1),
            )
        storage.transition_session(
            session_id,
            target,
            stage=f"recovered_{step['step_type']}",
            updates=updates,
            db_path=self.db_path,
        )
        if embedded_queries:
            self._complete_embedded_manual_steps(
                session_id,
                embedded_queries,
                profile_first=target == "profiling",
            )
        self._log(
            session,
            "linked_child_stage_recovered",
            step_id=step["id"],
            recovered_status=target,
            existing_child_run_ids=[child["child_run_id"] for child in links],
            recreated_child_run_ids=created_run_ids,
        )
        return AdvanceOutcome(target, 0.0, "Recovered a previously linked child stage.")

    def _child_details(self, session_id: int) -> list[dict[str, Any]]:
        children = storage.list_child_runs(session_id, db_path=self.db_path)
        out: list[dict[str, Any]] = []
        with connect_db(self.db_path) as conn:
            for link in children:
                child = dict(link)
                if child["child_type"] == "search":
                    row = conn.execute(
                        "SELECT status, search_method, search_provider, districts_matched, max_districts, districts_searched, districts_failed, current_workers, current_delay_seconds, started_at, finished_at FROM search_runs WHERE id = ?",
                        (child["child_run_id"],),
                    ).fetchone()
                    if row:
                        child.update(dict(row))
                        child["run_status"] = row["status"]
                        child["planned"] = min(int(row["districts_matched"] or 0), int(row["max_districts"] or row["districts_matched"] or 0))
                        child["districts_planned"] = child["planned"]
                        child["processed"] = int(row["districts_searched"] or 0)
                        child["failures"] = int(row["districts_failed"] or 0)
                        child["result_count"] = int(conn.execute("SELECT COUNT(*) AS count FROM search_results WHERE search_run_id = ?", (child["child_run_id"],)).fetchone()["count"] or 0)
                else:
                    row = conn.execute(
                        "SELECT status, districts_planned, districts_processed, profiles_failed, started_at, finished_at FROM profile_discovery_runs WHERE id = ?",
                        (child["child_run_id"],),
                    ).fetchone()
                    if row:
                        child.update(dict(row))
                        child["run_status"] = row["status"]
                        child["planned"] = int(row["districts_planned"] or 0)
                        child["processed"] = int(row["districts_processed"] or 0)
                        child["failures"] = int(row["profiles_failed"] or 0)
                        child["result_count"] = 0
                status = str(child.get("run_status") or child.get("status") or "")
                storage.update_child_run(
                    int(child["id"]),
                    status=status,
                    started_at=child.get("started_at"),
                    finished_at=child.get("finished_at"),
                    db_path=self.db_path,
                )
                out.append(child)
        return out

    def _update_progress(self, session: Mapping[str, Any], children: Sequence[Mapping[str, Any]]) -> None:
        search_children = [child for child in children if child["child_type"] == "search"]
        profile_children = [
            child for child in children if child["child_type"] == "profile_discovery"
        ]
        current_round = int(session.get("round_number") or 0)
        current = [child for child in search_children if int(child.get("round_number") or 0) == current_round]
        if str(session.get("status") or "") == "profiling" and profile_children:
            completed = int(profile_children[-1].get("processed") or 0)
        elif current:
            completed = max(int(child.get("processed") or 0) for child in current)
        elif profile_children:
            completed = int(profile_children[-1].get("processed") or 0)
        else:
            completed = 0
        updates: dict[str, Any] = {
            "districts_completed": completed,
            "failures": sum(int(child.get("failures") or 0) for child in children),
            "results_found": len(storage.list_evidence(int(session["id"]), db_path=self.db_path)),
        }
        active = next((child for child in current if child.get("run_status") in {"queued", "running"}), None)
        if active:
            updates["current_workers"] = active.get("current_workers")
            updates["current_delay_seconds"] = active.get("current_delay_seconds")
        storage.update_session(int(session["id"]), db_path=self.db_path, **updates)

    def _poll_profile(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        children = self._child_details(int(session["id"]))
        profile_children = [child for child in children if child["child_type"] == "profile_discovery"]
        latest = profile_children[-1] if profile_children else None
        if latest is None:
            return self._review(session, "Profile stage has no linked profile-discovery run.")
        status = str(latest.get("run_status") or latest.get("status") or "")
        if status not in TERMINAL_CHILD_STATUSES:
            self._update_progress(session, children)
            return AdvanceOutcome("profiling", WAIT_FOR_CHILD_SECONDS)
        step = storage.get_step(int(latest["step_id"]), self.db_path) if latest.get("step_id") else None
        if step and step["status"] == "running":
            storage.finish_step(
                int(step["id"]),
                status="completed" if status != "failed" else "failed",
                output_data={"run_id": latest["child_run_id"], "status": status, "processed": latest.get("processed"), "failures": latest.get("failures")},
                error_message=("Profile discovery completed with failures." if status == "failed" else None),
                short_description=f"Profile discovery {status}",
                db_path=self.db_path,
            )
        cohort_ids = storage.get_cohort_ids(int(session["id"]), self.db_path)
        coverage = self._coverage(cohort_ids)
        storage.update_session(int(session["id"]), profile_coverage=coverage, db_path=self.db_path)
        plan = self._plan_model(session)
        followup: list[SearchQuery] = []
        if step:
            for raw in _as_dict(step.get("input")).get("followup_queries") or []:
                followup.append(SearchQuery.from_dict(raw))
        self._log(session, "profile_discovery_finished", run_id=latest["child_run_id"], status=status, coverage=coverage)
        return self._start_search_round(session, plan, followup or list(plan.queries))

    def _result_inputs(self, session_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        child_details = [child for child in self._child_details(session_id) if child["child_type"] == "search"]
        run_ids = [int(child["child_run_id"]) for child in child_details]
        if not run_ids:
            return child_details, []
        placeholders = ",".join("?" for _ in run_ids)
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM search_results WHERE search_run_id IN ({placeholders}) ORDER BY search_run_id, id",
                run_ids,
            ).fetchall()
        return child_details, [dict(row) for row in rows]

    def _persist_evidence(self, session_id: int, children: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
        child_by_run = {int(child["child_run_id"]): child for child in children}
        for row in rows:
            child = child_by_run.get(int(row["search_run_id"]))
            if child is None:
                continue
            canonical = canonical_evidence_url(row.get("url"))
            if not canonical:
                continue
            from .evidence import content_fingerprint

            evidence_id = storage.upsert_evidence(
                session_id,
                canonical,
                district_id=int(row["district_id"]),
                content_fingerprint=content_fingerprint(row.get("title"), row.get("snippet")),
                title=row.get("title"),
                snippet=row.get("snippet"),
                score=float(row.get("score") or 0),
                round_number=int(child.get("round_number") or 0),
                db_path=self.db_path,
            )
            storage.link_evidence_source(
                evidence_id,
                child_run_link_id=int(child["id"]),
                search_run_id=int(row["search_run_id"]),
                search_result_id=int(row["id"]),
                round_number=int(child.get("round_number") or 0),
                query_text=child.get("query_text"),
                purpose=child.get("purpose"),
                db_path=self.db_path,
            )

    def _poll_search(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        children = self._child_details(session_id)
        current_round = int(session.get("round_number") or 0)
        current = [child for child in children if child["child_type"] == "search" and int(child.get("round_number") or 0) == current_round]
        if not current:
            return self._review(session, "Search stage has no linked child search runs.")
        if any(str(child.get("run_status") or child.get("status")) not in TERMINAL_CHILD_STATUSES for child in current):
            self._update_progress(session, children)
            return AdvanceOutcome("searching", WAIT_FOR_CHILD_SECONDS)
        for child in current:
            if child.get("step_id"):
                step = storage.get_step(int(child["step_id"]), self.db_path)
                if step and step["status"] == "running":
                    storage.finish_step(
                        int(step["id"]),
                        output_data={"round_number": current_round, "child_run_ids": [item["child_run_id"] for item in current], "statuses": [item.get("run_status") for item in current]},
                        short_description=f"Search round {current_round} completed",
                        db_path=self.db_path,
                    )
                    break
        child_inputs, result_rows = self._result_inputs(session_id)
        self._persist_evidence(session_id, child_inputs, result_rows)
        self._update_progress(session, child_inputs)
        evidence_count = len(storage.list_evidence(session_id, db_path=self.db_path))
        storage.transition_session(
            session_id,
            "evaluating",
            stage=f"evaluate_round_{current_round}",
            updates={
                "results_found": evidence_count,
                "failures": sum(int(child.get("failures") or 0) for child in child_inputs),
                "next_wake_at": utc_now_iso(),
            },
            db_path=self.db_path,
        )
        self._log(session, "search_round_finished", round=current_round, evidence=evidence_count)
        return AdvanceOutcome("evaluating", 0.0)

    def _bundle(self, session: Mapping[str, Any]) -> EvidenceBundle:
        children, rows = self._result_inputs(int(session["id"]))
        return build_evidence_bundle(
            children,
            rows,
            round_number=max(1, int(session.get("round_number") or 1)),
            sample_limit=30,
        )

    def _classify_evidence(self, session_id: int, evaluation: SearchEvaluation, bundle: EvidenceBundle) -> None:
        # A later round may sample a repeated URL whose globally strongest row
        # came from an earlier round. Every evaluator-allowed result ID must be
        # classifiable, so index both the global representatives and the sample.
        by_result = {
            item.result_id: item
            for item in (*bundle.unique_items, *bundle.sample)
        }
        payload = evaluation.to_dict()
        for result_id in evaluation.useful_result_ids:
            item = by_result.get(result_id)
            if item:
                storage.upsert_evidence(session_id, item.canonical_url, classification="useful", confidence=evaluation.confidence, evaluation=payload, db_path=self.db_path)
        for result_id in evaluation.likely_false_positive_ids:
            item = by_result.get(result_id)
            if item:
                storage.upsert_evidence(session_id, item.canonical_url, classification="likely_false_positive", confidence=evaluation.confidence, evaluation=payload, db_path=self.db_path)

    def _evaluate(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        plan = self._plan_model(session)
        bundle = self._bundle(session)
        step_id = storage.add_step(
            session_id,
            "ai_evaluation",
            short_description=f"Evaluating search round {bundle.metrics.round_number}",
            input_data=bundle.to_dict(),
            db_path=self.db_path,
        )
        try:
            result = evaluate_search_results(
                {
                    "original_user_objective_verbatim": session["original_objective"],
                    "validated_search_plan": plan.to_dict(),
                    "deterministic_metrics": bundle.metrics.to_dict(),
                    "bounded_untrusted_web_evidence": [item.to_dict() for item in bundle.sample],
                    "queries_already_used": self._used_queries(session_id),
                    "executed_query_methods": [
                        {"query_text": query, "search_method": method}
                        for query, method in self._used_query_methods(session_id)
                    ],
                },
                client=self.ai_client,
                mode=str(session["strategy_mode"]),
                available_methods=self._available_methods(session),
                brave_allowed=bool(session.get("brave_allowed")),
                available_result_ids=[item.result_id for item in bundle.sample],
            )
        except GuidedAIError as exc:
            return self._ai_failure(session, step_id, "evaluation", exc)
        self._audit_calls(session_id, step_id, "evaluation", result.calls)
        self._log(
            session,
            "ai_call_succeeded",
            task_type="evaluation",
            model=result.successful_call.model,
            endpoint=result.successful_call.endpoint,
            prompt_version=result.successful_call.prompt_version,
            latency_seconds=result.successful_call.latency_seconds,
        )
        evaluation = result.value
        evaluation_data = evaluation.to_dict()
        storage.finish_step(
            step_id,
            output_data={"evaluation": evaluation_data, "metrics": bundle.metrics.to_dict()},
            short_description=f"AI evaluation: {evaluation.assessment} — {evaluation.short_reason}",
            db_path=self.db_path,
        )
        storage.update_session(
            session_id,
            latest_evaluation=evaluation_data,
            prompt_versions={**_as_dict(session.get("prompt_versions")), "evaluate": EVALUATE_PROMPT_VERSION},
            db_path=self.db_path,
        )
        self._classify_evidence(session_id, evaluation, bundle)
        self._log(session, "evaluation_completed", assessment=evaluation.assessment, action=evaluation.recommended_action, new_urls=bundle.metrics.new_unique_urls)
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled

        hard_rounds = min(plan.stop_policy.max_rounds, int(session.get("max_rounds") or plan.stop_policy.max_rounds))
        child_count = len(storage.list_child_runs(session_id, child_type="search", db_path=self.db_path))
        hard_children = min(plan.stop_policy.max_child_search_runs, int(session.get("max_child_search_runs") or plan.stop_policy.max_child_search_runs))
        if int(session.get("round_number") or 0) >= hard_rounds:
            return self._begin_summarizing(session, "Maximum search rounds reached.")
        if child_count >= hard_children:
            return self._begin_summarizing(session, "Maximum child search runs reached.")
        if int(session.get("round_number") or 0) > 1 and bundle.metrics.new_unique_urls == 0:
            return self._begin_summarizing(session, "The latest round produced no meaningful new URLs.")
        if evaluation.recommended_action in {"accept", "stop_no_evidence"}:
            return self._begin_summarizing(session, evaluation.short_reason)
        if evaluation.recommended_action == "needs_user_input":
            if int(session.get("clarification_round") or 0) >= MAX_CLARIFICATION_ROUNDS:
                return self._review(
                    session,
                    "The clarification-round limit was reached during evaluation.",
                )
            storage.transition_session(
                session_id,
                "needs_clarification",
                stage="clarification",
                updates={"clarification_questions": [evaluation.short_reason]},
                db_path=self.db_path,
            )
            self._log(
                session,
                "clarification_requested",
                source="evaluation",
                questions=[evaluation.short_reason],
            )
            return AdvanceOutcome("needs_clarification")
        if evaluation.recommended_action == "discover_profiles":
            cohort_ids = storage.get_cohort_ids(session_id, self.db_path)
            missing = self._profile_candidate_ids(cohort_ids, "discover_missing")
            if missing:
                followup = list(evaluation.proposed_queries) or list(plan.queries[:1])
                plan = self._update_validated_plan(
                    session,
                    plan,
                    queries=followup,
                    profile_policy="discover_missing",
                    short_explanation=evaluation.short_reason,
                )
                session = storage.transition_session(
                    session_id,
                    "replanning",
                    stage="profile_replan",
                    db_path=self.db_path,
                )
                return self._start_profile_stage(session, plan, missing, followup_queries=followup)
        if evaluation.recommended_action == "switch_method" and evaluation.recommended_search_method:
            queries = list(evaluation.proposed_queries) or list(plan.queries[:1])
            plan = self._update_validated_plan(
                session,
                plan,
                queries=queries,
                preferred_search_method=evaluation.recommended_search_method,
                short_explanation=evaluation.short_reason,
            )
            return self._start_search_round(
                session,
                plan,
                queries,
                method_override=evaluation.recommended_search_method,
            )
        if evaluation.proposed_queries:
            plan = self._update_validated_plan(
                session,
                plan,
                queries=list(evaluation.proposed_queries),
                short_explanation=evaluation.short_reason,
            )
            return self._start_search_round(
                session,
                plan,
                list(evaluation.proposed_queries),
            )
        storage.transition_session(session_id, "replanning", stage="revise_plan", updates={"next_wake_at": utc_now_iso()}, db_path=self.db_path)
        return AdvanceOutcome("replanning", 0.0)

    def _replan(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        plan = self._plan_model(session)
        bundle = self._bundle(session)
        step_id = storage.add_step(
            session_id,
            "ai_revision",
            short_description="Revising the bounded search plan",
            input_data={"plan": plan.to_dict(), "evaluation": session.get("latest_evaluation"), "metrics": bundle.metrics.to_dict()},
            db_path=self.db_path,
        )
        try:
            result = revise_search_plan(
                {
                    "original_user_objective_verbatim": session["original_objective"],
                    "validated_plan": plan.to_dict(),
                    "latest_evaluation": session.get("latest_evaluation") or {},
                    "deterministic_metrics": bundle.metrics.to_dict(),
                    "queries_already_used": self._used_queries(session_id),
                    "executed_query_methods": [
                        {"query_text": query, "search_method": method}
                        for query, method in self._used_query_methods(session_id)
                    ],
                },
                client=self.ai_client,
                mode=str(session["strategy_mode"]),
                available_methods=self._available_methods(session),
                brave_allowed=bool(session.get("brave_allowed")),
            )
        except GuidedAIError as exc:
            return self._ai_failure(session, step_id, "revision", exc)
        self._audit_calls(session_id, step_id, "revision", result.calls)
        self._log(
            session,
            "ai_call_succeeded",
            task_type="revision",
            model=result.successful_call.model,
            endpoint=result.successful_call.endpoint,
            prompt_version=result.successful_call.prompt_version,
            latency_seconds=result.successful_call.latency_seconds,
        )
        revision = result.value
        storage.finish_step(step_id, output_data=revision.to_dict(), short_description=revision.short_explanation, db_path=self.db_path)
        storage.update_session(session_id, prompt_versions={**_as_dict(session.get("prompt_versions")), "revise": REVISE_PROMPT_VERSION}, db_path=self.db_path)
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled
        revised_queries: list[SearchQuery] | None = (
            list(revision.queries) if revision.queries else None
        )
        if revision.action == "append_queries" and revision.queries:
            appended: list[SearchQuery] = []
            for query in revision.queries:
                if any(
                    effectively_same_query(query.query_text, prior.query_text)
                    for prior in appended
                ):
                    continue
                appended.append(query)
            limit = mode_policy(
                str(session["strategy_mode"])
            ).max_queries_per_round
            retained = [
                query
                for query in plan.queries
                if not any(
                    effectively_same_query(query.query_text, new_query.query_text)
                    for new_query in appended
                )
            ][: max(0, limit - len(appended))]
            revised_queries = [*retained, *appended][:limit]
        if (
            revised_queries is not None
            or revision.preferred_search_method is not None
            or revision.profile_policy is not None
        ):
            plan = self._update_validated_plan(
                session,
                plan,
                queries=revised_queries,
                preferred_search_method=revision.preferred_search_method,
                profile_policy=revision.profile_policy,
                short_explanation=revision.short_explanation,
            )
        if revision.action == "stop":
            return self._begin_summarizing(session, revision.short_explanation)
        if revision.action == "needs_user_input":
            if int(session.get("clarification_round") or 0) >= MAX_CLARIFICATION_ROUNDS:
                return self._review(session, "The clarification-round limit was reached during replanning.")
            storage.transition_session(
                session_id,
                "needs_clarification",
                stage="clarification",
                updates={"clarification_questions": list(revision.clarification_questions)},
                db_path=self.db_path,
            )
            return AdvanceOutcome("needs_clarification")
        if revision.action == "discover_profiles":
            ids = self._profile_candidate_ids(storage.get_cohort_ids(session_id, self.db_path), revision.profile_policy or "discover_missing")
            if ids:
                return self._start_profile_stage(session, plan, ids, followup_queries=list(revision.queries) or list(plan.queries[:1]))
        if revision.action == "switch_method" and revision.preferred_search_method:
            return self._start_search_round(
                session,
                plan,
                list(revision.queries) or list(plan.queries[:1]),
                method_override=revision.preferred_search_method,
            )
        if revision.queries:
            return self._start_search_round(session, plan, list(revision.queries))
        return self._begin_summarizing(session, revision.short_explanation)

    def _begin_summarizing(self, session: Mapping[str, Any], reason: str) -> AdvanceOutcome:
        session_id = int(session["id"])
        storage.add_step(
            session_id,
            "stop_condition",
            status="completed",
            short_description=reason[:500],
            input_data={"reason": reason},
            db_path=self.db_path,
        )
        storage.transition_session(
            session_id,
            "summarizing",
            stage="final_summary",
            updates={"next_wake_at": utc_now_iso()},
            db_path=self.db_path,
        )
        self._log(session, "stop_condition", reason=reason)
        return AdvanceOutcome("summarizing", 0.0, reason)

    def _summarize(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        plan = self._plan_model(session)
        all_children = self._child_details(session_id)
        search_children, rows = self._result_inputs(session_id)
        max_round = max(
            [int(child.get("round_number") or 0) for child in search_children]
            or [0]
        )
        # Evidence analytics use positive round numbers. A session stopped before
        # any search still reports zero completed rounds in the summary facts.
        bundle = build_evidence_bundle(
            search_children,
            rows,
            round_number=max(1, max_round),
            sample_limit=40,
        )
        latest_round_metrics = bundle.metrics.to_dict()
        if max_round == 0:
            latest_round_metrics["round_number"] = 0
        all_items = list(bundle.unique_items)[:40]
        all_unique_urls = {
            canonical_evidence_url(row.get("url"))
            for row in rows
            if canonical_evidence_url(row.get("url"))
        }
        evidence_district_ids = {
            int(row["district_id"])
            for row in rows
            if row.get("district_id") is not None
        }
        profile_children = [
            child
            for child in all_children
            if child["child_type"] == "profile_discovery"
        ]
        round_metrics = [
            build_evidence_bundle(
                search_children,
                rows,
                round_number=round_number,
                sample_limit=1,
            ).metrics.to_dict()
            for round_number in range(1, max_round + 1)
        ]
        steps = storage.list_steps(session_id, db_path=self.db_path)
        evaluations_by_round: dict[int, dict[str, Any]] = {}
        revisions_by_round: dict[int, list[dict[str, Any]]] = {}
        profile_stage_summaries: list[str] = []
        resource_adjustment_summaries: list[str] = []
        stop_reason = ""
        latest_round_seen = 0
        for step in steps:
            step_type = str(step.get("step_type") or "")
            step_input = _as_dict(step.get("input"))
            step_output = _as_dict(step.get("output"))
            if step_type == "search_round":
                latest_round_seen = max(
                    latest_round_seen,
                    int(step_input.get("round_number") or 0),
                )
            elif step_type == "ai_evaluation":
                metrics = _as_dict(step_output.get("metrics"))
                round_number = int(metrics.get("round_number") or latest_round_seen or 1)
                evaluations_by_round[round_number] = _as_dict(
                    step_output.get("evaluation")
                )
            elif step_type == "ai_revision":
                revisions_by_round.setdefault(max(1, latest_round_seen), []).append(
                    step_output
                )
            elif step_type == "stop_condition":
                stop_reason = str(
                    step_input.get("reason")
                    or step.get("short_description")
                    or stop_reason
                )
            elif step_type == "profile_discovery":
                profile_stage_summaries.append(
                    str(step.get("short_description") or "Profile discovery")
                )
            elif step_type == "resource_adjustment":
                resource_adjustment_summaries.append(
                    str(step.get("short_description") or "Resource adjustment")
                )
        decision_history = []
        for metrics in round_metrics:
            round_number = int(metrics.get("round_number") or 0)
            round_children = [
                child
                for child in search_children
                if int(child.get("round_number") or 0) == round_number
            ]
            decision_history.append(
                {
                    "round_number": round_number,
                    "queries": [
                        {
                            "query_text": child.get("query_text"),
                            "purpose": child.get("purpose"),
                            "search_method": child.get("search_method"),
                        }
                        for child in round_children
                    ],
                    "metrics": metrics,
                    "evaluation": evaluations_by_round.get(round_number) or {},
                    "revisions": revisions_by_round.get(round_number) or [],
                }
            )
        step_id = storage.add_step(
            session_id,
            "ai_summary",
            short_description="Creating the final evidence-grounded summary",
            input_data={"result_count": len(all_items), "rounds": max_round},
            db_path=self.db_path,
        )
        try:
            result = summarize_guided_search(
                {
                    "original_user_objective_verbatim": session["original_objective"],
                    "district_scope": session.get("scope") or {},
                    "district_count": session.get("district_count"),
                    "validated_plan": plan.to_dict(),
                    "profile_coverage": session.get("profile_coverage") or {},
                    "child_runs": [
                        {key: child.get(key) for key in ("child_type", "child_run_id", "round_number", "query_text", "purpose", "search_method", "run_status", "planned", "processed", "failures", "result_count")}
                        for child in all_children
                    ],
                    "deterministic_aggregate_metrics": {
                        "rounds_completed": max_round,
                        "search_child_runs": len(search_children),
                        "profile_discovery_runs": len(profile_children),
                        "profile_discovery_performed": bool(profile_children),
                        "total_result_rows": len(rows),
                        "total_unique_evidence_urls": len(all_unique_urls),
                        "districts_with_candidate_evidence": len(evidence_district_ids),
                        "total_search_failures": sum(
                            int(child.get("failures") or 0)
                            for child in search_children
                        ),
                        "total_profile_failures": sum(
                            int(child.get("failures") or 0)
                            for child in profile_children
                        ),
                        "total_child_failures": sum(
                            int(child.get("failures") or 0)
                            for child in all_children
                        ),
                        "search_methods_used": sorted(
                            {
                                str(child.get("search_method") or "")
                                for child in search_children
                                if str(child.get("search_method") or "")
                            }
                        ),
                        "stop_reason": stop_reason,
                        "profile_stage_summaries": profile_stage_summaries,
                        "resource_adjustment_count": len(
                            resource_adjustment_summaries
                        ),
                        "latest_resource_adjustments": resource_adjustment_summaries[-3:],
                    },
                    "deterministic_search_history_metrics_and_decisions": decision_history,
                    "bounded_untrusted_web_evidence": [item.to_dict() for item in all_items],
                    "deterministic_latest_round_metrics": latest_round_metrics,
                    "required_caveat": "A missing result never proves that a district lacks the material; relevance and completeness are heuristic.",
                },
                client=self.ai_client,
                available_result_ids=[item.result_id for item in all_items],
            )
        except GuidedAIError as exc:
            return self._ai_failure(session, step_id, "summary", exc)
        self._audit_calls(session_id, step_id, "summary", result.calls)
        self._log(
            session,
            "ai_call_succeeded",
            task_type="summary",
            model=result.successful_call.model,
            endpoint=result.successful_call.endpoint,
            prompt_version=result.successful_call.prompt_version,
            latency_seconds=result.successful_call.latency_seconds,
        )
        if cancelled := self._cancel_if_requested(session_id):
            return cancelled
        summary = result.value
        summary_data = summary.to_dict()
        storage.finish_step(step_id, output_data=summary_data, short_description="Guided Search completed", db_path=self.db_path)
        by_result = {item.result_id: item for item in all_items}
        for result_id in summary.highest_confidence_result_ids:
            item = by_result.get(result_id)
            if item:
                storage.upsert_evidence(session_id, item.canonical_url, classification="highest_confidence", evaluation=summary_data, db_path=self.db_path)
        for result_id in summary.uncertain_result_ids:
            item = by_result.get(result_id)
            if item:
                storage.upsert_evidence(session_id, item.canonical_url, classification="uncertain", evaluation=summary_data, db_path=self.db_path)
        storage.transition_session(
            session_id,
            "completed",
            stage="completed",
            updates={
                "final_summary": json.dumps(summary_data, ensure_ascii=False),
                "prompt_versions": {**_as_dict(session.get("prompt_versions")), "summary": SUMMARY_PROMPT_VERSION},
                "results_found": len(storage.list_evidence(session_id, db_path=self.db_path)),
                "review_reason": None,
                "error_message": None,
            },
            db_path=self.db_path,
        )
        self._log(session, "guided_search_completed", rounds=max_round, evidence=len(all_items))
        return AdvanceOutcome("completed")

    def _review(self, session: Mapping[str, Any], reason: str) -> AdvanceOutcome:
        storage.transition_session(
            int(session["id"]),
            "needs_review",
            stage=str(session.get("stage") or session.get("status") or "review"),
            updates={"review_reason": reason, "error_message": reason, "next_wake_at": None},
            db_path=self.db_path,
        )
        self._log(session, "needs_review", reason=reason)
        return AdvanceOutcome("needs_review", description=reason)

    def _cancel(self, session: Mapping[str, Any]) -> AdvanceOutcome:
        session_id = int(session["id"])
        children = self._child_details(session_id)
        active = [child for child in children if str(child.get("run_status") or child.get("status")) not in TERMINAL_CHILD_STATUSES]
        with connect_db(self.db_path) as conn:
            for child in active:
                if child["child_type"] == "search":
                    conn.execute("UPDATE search_runs SET cancel_requested = 1 WHERE id = ?", (child["child_run_id"],))
                else:
                    conn.execute("UPDATE profile_discovery_runs SET cancel_requested = 1 WHERE id = ?", (child["child_run_id"],))
            conn.commit()
        if active:
            return AdvanceOutcome(str(session["status"]), WAIT_FOR_CHILD_SECONDS)
        search_children, result_rows = self._result_inputs(session_id)
        if search_children:
            self._persist_evidence(session_id, search_children, result_rows)
            self._update_progress(session, children)
        evidence_count = len(
            storage.list_evidence(session_id, db_path=self.db_path)
        )
        for step in storage.list_steps(session_id, db_path=self.db_path):
            if step["status"] in {"running", "queued"}:
                storage.finish_step(int(step["id"]), status="cancelled", error_message="Cancelled by user.", db_path=self.db_path)
        storage.transition_session(
            session_id,
            "cancelled",
            stage="cancelled",
            updates={
                "error_message": "Cancelled by user.",
                "results_found": evidence_count,
            },
            db_path=self.db_path,
        )
        self._log(session, "guided_search_cancelled")
        return AdvanceOutcome("cancelled")

    def advance(self, session_id: int) -> AdvanceOutcome:
        session = storage.get_session(session_id, self.db_path)
        if session is None:
            return AdvanceOutcome("missing")
        cancellation_pending = bool(session.get("cancel_requested")) and session[
            "status"
        ] not in storage.GUIDED_SEARCH_TERMINAL_STATUSES
        if (
            session["status"] not in storage.GUIDED_SEARCH_ACTIVE_STATUSES
            and not cancellation_pending
        ):
            return AdvanceOutcome(str(session["status"]))
        claim_statuses = (
            (*storage.GUIDED_SEARCH_ACTIVE_STATUSES, str(session["status"]))
            if cancellation_pending
            else None
        )
        token = storage.claim_session(
            session_id,
            statuses=claim_statuses,
            db_path=self.db_path,
        )
        if token is None:
            return AdvanceOutcome(str(session["status"]), WAIT_FOR_CHILD_SECONDS)
        outcome = AdvanceOutcome(str(session["status"]))
        try:
            session = storage.get_session(session_id, self.db_path) or session
            self._log(session, "state_advance", status=session["status"], stage=session.get("stage"))
            if bool(session.get("cancel_requested")):
                outcome = self._cancel(session)
            elif recovered := self._recover_linked_stage(session):
                outcome = recovered
            elif session["status"] == "planning":
                outcome = self._plan(session)
            elif session["status"] == "queued":
                outcome = self._resume_queued(session)
            elif session["status"] == "profiling":
                outcome = self._poll_profile(session)
            elif session["status"] == "searching":
                outcome = self._poll_search(session)
            elif session["status"] == "evaluating":
                outcome = self._evaluate(session)
            elif session["status"] == "replanning":
                outcome = self._replan(session)
            elif session["status"] == "summarizing":
                outcome = self._summarize(session)
            current = storage.get_session(session_id, self.db_path)
            if (
                current
                and bool(current.get("cancel_requested"))
                and current["status"] not in storage.GUIDED_SEARCH_TERMINAL_STATUSES
            ):
                outcome = self._cancel(current)
        except Exception as exc:
            LOGGER.exception("Guided Search session %s failed while advancing", session_id)
            current = storage.get_session(session_id, self.db_path)
            if current and current["status"] in storage.GUIDED_SEARCH_ACTIVE_STATUSES:
                try:
                    outcome = self._review(current, f"{type(exc).__name__}: {exc}")
                except Exception:
                    LOGGER.exception("Could not move Guided Search session %s to review", session_id)
                    outcome = AdvanceOutcome("failed", description=str(exc))
        finally:
            storage.release_session_claim(
                session_id,
                token,
                next_wake_at=(_iso_wake_after(outcome.wake_after_seconds) if outcome.wake_after_seconds is not None else None),
                db_path=self.db_path,
            )
        return outcome


def request_manual_query(
    session_id: int,
    query_text: str,
    purpose: str = "User-requested follow-up",
    *,
    db_path: Path | str | None = None,
) -> None:
    parse_search_query(query_text)
    session = storage.get_session(session_id, db_path)
    if session is None:
        raise storage.SessionNotFoundError(f"Guided Search session not found: {session_id}")
    if session["status"] not in {"ready", "needs_review"}:
        raise ValueError("A manual follow-up can be queued only when the session is ready or needs review.")
    storage.add_step(
        session_id,
        "manual_query",
        status="queued",
        short_description=f"User requested another query: {query_text}",
        input_data={"query_text": query_text, "purpose": purpose},
        db_path=db_path,
    )
    storage.transition_session(session_id, "queued", stage="manual_query", updates={"review_reason": None, "error_message": None}, db_path=db_path)


def retry_ai_stage(session_id: int, *, db_path: Path | str | None = None) -> str:
    session = storage.get_session(session_id, db_path)
    if session is None:
        raise storage.SessionNotFoundError(f"Guided Search session not found: {session_id}")
    if session["status"] != "needs_review":
        raise ValueError("Retry AI is available only for a session that needs review.")
    failed_stage = str(session.get("stage") or "")
    if (
        not session.get("latest_plan")
        or "planning" in failed_stage
        or "clarification" in failed_stage
    ):
        target = "planning"
    elif "summary" in failed_stage:
        target = "summarizing"
    elif "evaluat" in failed_stage or "revision" in failed_stage:
        target = "evaluating" if "evaluat" in failed_stage else "replanning"
    else:
        children = storage.list_child_runs(session_id, child_type="search", db_path=db_path)
        if children:
            target = "evaluating" if not session.get("final_summary") else "summarizing"
        else:
            target = "queued"
    storage.transition_session(session_id, target, stage=f"retry_{target}", updates={"review_reason": None, "error_message": None}, db_path=db_path)
    return target


def continue_last_plan(session_id: int, *, db_path: Path | str | None = None) -> None:
    session = storage.get_session(session_id, db_path)
    if session is None or session["status"] != "needs_review" or not session.get("latest_plan"):
        raise ValueError("No safe validated plan is available to continue.")
    try:
        plan = GuidedSearchOrchestrator(db_path=db_path)._plan_model(session)
    except Exception as exc:
        raise ValueError("The last plan is no longer valid under current settings.") from exc
    if plan.needs_clarification or not plan.queries:
        raise ValueError("The last plan still requires clarification and cannot be executed safely.")
    has_search_children = bool(
        storage.list_child_runs(session_id, child_type="search", db_path=db_path)
    )
    if has_search_children:
        used = [
            str(child.get("query_text") or "")
            for child in storage.list_child_runs(
                session_id,
                child_type="search",
                db_path=db_path,
            )
        ]
        if not any(
            not any(effectively_same_query(query.query_text, prior) for prior in used)
            for query in plan.queries
        ):
            raise ValueError(
                "The last validated plan has no unused query to continue safely. "
                "Retry AI, add a manual query, or stop and summarize."
            )
    target = "queued"
    storage.transition_session(
        session_id,
        target,
        stage="continue_plan",
        updates={"review_reason": None, "error_message": None},
        db_path=db_path,
    )


def stop_and_summarize(session_id: int, *, db_path: Path | str | None = None) -> None:
    session = storage.get_session(session_id, db_path)
    if session is None or session["status"] != "needs_review":
        raise ValueError("Stop and summarize is available when the session needs review.")
    children = storage.list_child_runs(
        session_id,
        child_type="search",
        db_path=db_path,
    )
    if not children:
        raise ValueError("No completed search evidence is available to summarize.")
    run_ids = [int(child["child_run_id"]) for child in children]
    placeholders = ",".join("?" for _ in run_ids)
    with connect_db(db_path) as conn:
        statuses = {
            str(row["status"])
            for row in conn.execute(
                f"SELECT status FROM search_runs WHERE id IN ({placeholders})",
                run_ids,
            ).fetchall()
        }
    if not statuses or any(status not in TERMINAL_CHILD_STATUSES for status in statuses):
        raise ValueError(
            "Wait for all linked search runs to finish or cancel them before summarizing."
        )
    storage.transition_session(session_id, "summarizing", stage="manual_summary", updates={"review_reason": None, "error_message": None}, db_path=db_path)


def request_session_cancellation(session_id: int, *, db_path: Path | str | None = None) -> None:
    storage.request_cancellation(session_id, reason="Cancellation requested by user.", db_path=db_path)


__all__ = [
    "AdvanceOutcome",
    "GuidedSearchOrchestrator",
    "continue_last_plan",
    "request_manual_query",
    "request_session_cancellation",
    "retry_ai_stage",
    "stop_and_summarize",
]
