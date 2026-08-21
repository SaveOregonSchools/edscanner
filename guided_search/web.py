from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from ai_matcher import get_ollama_model, local_llm_is_configured
from common import (
    GUIDED_SEARCH_RUN_LOGS_DIR,
    MAX_TOTAL_DISTRICTS_PER_RUN,
    connect_db,
    has_brave_search_api_key,
    list_filter_options,
)
from search_engine import count_matching_districts, parse_optional_int

from . import storage
from .evidence import effectively_same_query
from .models import MODE_POLICIES, mode_policy
from .orchestrator import (
    continue_last_plan,
    request_manual_query,
    request_session_cancellation,
    retry_ai_stage,
    stop_and_summarize,
)
from .prompts import (
    EVALUATE_PROMPT_VERSION,
    PLAN_PROMPT_VERSION,
    REVISE_PROMPT_VERSION,
    SUMMARY_PROMPT_VERSION,
)
from .worker import enqueue_guided_search


bp = Blueprint("guided_search", __name__, url_prefix="/guided-search")
BRAVE_DISPLAY_COST_PER_CALL = 0.005


def _selected(name: str) -> list[str]:
    out: list[str] = []
    for value in request.values.getlist(name):
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = parse_optional_int(value)
    return max(minimum, min(maximum, parsed if parsed is not None else default))


def _scope_from_request(options: Mapping[str, list[str]]) -> dict[str, Any]:
    states = _selected("states")
    agency_types = _selected("agency_types")
    invalid_states = sorted(set(states) - set(options.get("states") or []))
    invalid_types = sorted(set(agency_types) - set(options.get("agency_types") or []))
    if invalid_states or invalid_types:
        raise ValueError("District scope contains an option that is not in the imported data.")
    raw_minimum = str(request.values.get("min_enrollment") or "").strip()
    raw_maximum = str(request.values.get("max_enrollment") or "").strip()
    raw_limit = str(request.values.get("max_districts") or "").strip()
    minimum = parse_optional_int(raw_minimum)
    maximum = parse_optional_int(raw_maximum)
    requested_limit = parse_optional_int(raw_limit)
    if raw_minimum and minimum is None:
        raise ValueError("Minimum enrollment must be a whole number.")
    if raw_maximum and maximum is None:
        raise ValueError("Maximum enrollment must be a whole number.")
    if raw_limit and requested_limit is None:
        raise ValueError("Maximum districts must be a whole number.")
    if minimum is not None and minimum < 0:
        raise ValueError("Minimum enrollment cannot be negative.")
    if maximum is not None and maximum < 0:
        raise ValueError("Maximum enrollment cannot be negative.")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Minimum enrollment cannot exceed maximum enrollment.")
    limit = requested_limit if requested_limit is not None else MAX_TOTAL_DISTRICTS_PER_RUN
    if limit < 1 or limit > MAX_TOTAL_DISTRICTS_PER_RUN:
        raise ValueError(
            f"Maximum districts must be between 1 and {MAX_TOTAL_DISTRICTS_PER_RUN:,}."
        )
    return {
        "states": states,
        "agency_types": agency_types,
        "min_enrollment": minimum,
        "max_enrollment": maximum,
        "max_districts": limit,
    }


def _validate_example_url_shape(value: str) -> None:
    if not value:
        return
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("Example URL is invalid.") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise ValueError("Example URL must be a public HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("Credential-bearing example URLs are not allowed.")


def _default_form_values() -> dict[str, Any]:
    return {
        "objective": "",
        "states": [],
        "agency_types": [],
        "min_enrollment": None,
        "max_enrollment": None,
        "max_districts": MAX_TOTAL_DISTRICTS_PER_RUN,
        "example_text": "",
        "example_url": "",
        "strategy_mode": "balanced",
        "brave_allowed": False,
    }


@bp.route("/", methods=["GET", "POST"])
def index():
    options = list_filter_options()
    values = _default_form_values()
    if request.method == "POST":
        raw_objective = request.form.get("objective", "")
        values.update(
            {
                "objective": raw_objective,
                "states": _selected("states"),
                "agency_types": _selected("agency_types"),
                "min_enrollment": parse_optional_int(request.form.get("min_enrollment")),
                "max_enrollment": parse_optional_int(request.form.get("max_enrollment")),
                "max_districts": _bounded_int(request.form.get("max_districts"), MAX_TOTAL_DISTRICTS_PER_RUN, 1, MAX_TOTAL_DISTRICTS_PER_RUN),
                "example_text": request.form.get("example_text", ""),
                "example_url": request.form.get("example_url", "").strip(),
                "strategy_mode": request.form.get("strategy_mode", "balanced").strip().casefold(),
                "brave_allowed": request.form.get("brave_allowed", "").casefold() in {"1", "true", "yes", "on"},
            }
        )
        try:
            if not raw_objective.strip():
                raise ValueError("Describe what you are trying to find.")
            if len(raw_objective) > 8_000:
                raise ValueError("The research goal must be 8,000 characters or fewer.")
            if not local_llm_is_configured():
                raise ValueError("Configure a local Ollama server and model in Settings before creating a Guided Search plan.")
            scope = _scope_from_request(options)
            strategy_mode = str(values["strategy_mode"])
            if strategy_mode not in MODE_POLICIES:
                raise ValueError("Choose Fast, Balanced, or Thorough.")
            if len(str(values["example_text"])) > 16_000:
                raise ValueError("Example text must be 16,000 characters or fewer.")
            _validate_example_url_shape(str(values["example_url"]))
            match_count = count_matching_districts(
                scope["states"],
                scope["agency_types"],
                scope["min_enrollment"],
                scope["max_enrollment"],
            )
            if match_count <= 0:
                raise ValueError("No searchable districts match this scope.")
            brave_allowed = bool(values["brave_allowed"] and has_brave_search_api_key())
            policy = mode_policy(strategy_mode)
            session_id = storage.create_session(
                raw_objective,
                example_text=str(values["example_text"]),
                example_url=str(values["example_url"]),
                examples=[
                    *([{"kind": "text", "characters": len(str(values["example_text"]))}] if values["example_text"] else []),
                    *([{"kind": "url", "url": str(values["example_url"])}] if values["example_url"] else []),
                ],
                scope=scope,
                strategy_mode=strategy_mode,
                brave_allowed=brave_allowed,
                status="planning",
                stage="planning",
                ai_model=get_ollama_model(),
                prompt_versions={
                    "plan": PLAN_PROMPT_VERSION,
                    "evaluate": EVALUATE_PROMPT_VERSION,
                    "revise": REVISE_PROMPT_VERSION,
                    "summary": SUMMARY_PROMPT_VERSION,
                },
                resource_policy={
                    "initial_workers": policy.initial_workers,
                    "max_workers": policy.max_workers,
                },
                max_rounds=policy.max_rounds,
                max_child_search_runs=policy.max_child_search_runs,
            )
            storage.update_session(
                session_id,
                debug_log_path=str(GUIDED_SEARCH_RUN_LOGS_DIR / f"guided-search-{session_id}.log"),
            )
            storage.add_step(
                session_id,
                "session_created",
                status="completed",
                short_description="Guided Search intake saved; AI planning queued",
                input_data={"scope": scope, "strategy_mode": strategy_mode, "brave_allowed": brave_allowed},
            )
            enqueue_guided_search(session_id)
            return redirect(url_for("guided_search.detail", session_id=session_id))
        except Exception as exc:
            flash(str(exc), "error")

    initial_matching_count = count_matching_districts(
        values["states"],
        values["agency_types"],
        values["min_enrollment"],
        values["max_enrollment"],
    )
    recent_sessions = storage.list_sessions(limit=10)
    return render_template(
        "guided_search.html",
        options=options,
        form_values=values,
        initial_matching_count=initial_matching_count,
        initial_planned_count=min(
            initial_matching_count,
            int(values["max_districts"]),
        ),
        brave_key_present=has_brave_search_api_key(),
        llm_configured=local_llm_is_configured(),
        recent_sessions=recent_sessions,
    )


@bp.get("/scope-count")
def scope_count():
    try:
        scope = _scope_from_request(list_filter_options())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    match_count = count_matching_districts(
        scope["states"],
        scope["agency_types"],
        scope["min_enrollment"],
        scope["max_enrollment"],
    )
    return jsonify(
        {
            "matching_count": match_count,
            "planned_count": min(match_count, scope["max_districts"]),
        }
    )


def _elapsed(started_at: str | None, finished_at: str | None) -> str:
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at) if finished_at else datetime.now(start.tzinfo)
    except ValueError:
        return ""
    seconds = max(0, int((finish - start).total_seconds()))
    minutes, remaining = divmod(seconds, 60)
    return f"{minutes}m {remaining}s" if minutes else f"{remaining}s"


def _child_views(session_id: int) -> list[dict[str, Any]]:
    children = storage.list_child_runs(session_id)
    with connect_db() as conn:
        for child in children:
            if child["child_type"] == "search":
                run = conn.execute(
                    "SELECT status, search_method, districts_matched, max_districts, districts_searched, districts_failed FROM search_runs WHERE id = ?",
                    (child["child_run_id"],),
                ).fetchone()
                if run:
                    child["run_status"] = run["status"]
                    child["search_method"] = run["search_method"]
                    child["planned"] = min(int(run["districts_matched"] or 0), int(run["max_districts"] or run["districts_matched"] or 0))
                    child["processed"] = int(run["districts_searched"] or 0)
                    child["failures"] = int(run["districts_failed"] or 0)
                    child["result_count"] = int(conn.execute("SELECT COUNT(*) AS count FROM search_results WHERE search_run_id = ?", (child["child_run_id"],)).fetchone()["count"] or 0)
            else:
                run = conn.execute(
                    "SELECT status, districts_planned, districts_processed, profiles_failed FROM profile_discovery_runs WHERE id = ?",
                    (child["child_run_id"],),
                ).fetchone()
                if run:
                    child["run_status"] = run["status"]
                    child["planned"] = int(run["districts_planned"] or 0)
                    child["processed"] = int(run["districts_processed"] or 0)
                    child["failures"] = int(run["profiles_failed"] or 0)
                    child["result_count"] = 0
    return children


@bp.get("/<int:session_id>")
def detail(session_id: int):
    session = storage.get_session(session_id)
    if session is None:
        abort(404)
    steps = storage.list_steps(session_id)
    for step in steps:
        step["description"] = step.get("short_description")
    children = _child_views(session_id)
    evidence = storage.list_evidence(session_id, limit=100)
    for item in evidence:
        item["url"] = item["canonical_url"]
        item["district_name"] = item.get("agency_name") or "Unknown district"
        item["useful"] = item.get("classification") in {"useful", "highest_confidence"}
        item["likely_false_positive"] = item.get("classification") == "likely_false_positive"
    summary = _json_object(session.get("final_summary"))
    plan = _as_mapping(session.get("latest_plan"))
    evaluation = _as_mapping(session.get("latest_evaluation"))
    coverage = _as_mapping(session.get("profile_coverage"))
    coverage.setdefault("working", coverage.get("working_count", 0))
    coverage.setdefault("requires_javascript", coverage.get("javascript_count", 0))
    coverage.setdefault("missing", coverage.get("missing_count", 0))
    coverage.setdefault("stale", coverage.get("stale_count", 0))
    coverage.setdefault("review_or_error", 0)
    active = session["status"] in storage.GUIDED_SEARCH_ACTIVE_STATUSES
    search_children = [
        child for child in children if child["child_type"] == "search"
    ]
    plan_queries = list(plan.get("queries") or [])
    used_queries = [
        str(child.get("query_text") or "")
        for child in search_children
        if str(child.get("query_text") or "").strip()
    ]
    can_continue_plan = bool(plan_queries) and not bool(
        plan.get("needs_clarification")
    ) and any(
        not any(
            effectively_same_query(str(query.get("query_text") or ""), used)
            for used in used_queries
        )
        for query in plan_queries
        if isinstance(query, Mapping)
        and str(query.get("query_text") or "").strip()
    )
    child_terminal_statuses = {
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
    }
    can_stop_and_summarize = bool(search_children) and all(
        str(child.get("run_status") or child.get("status") or "")
        in child_terminal_statuses
        for child in search_children
    )
    prefill_method = str(
        (search_children[-1].get("search_method") if search_children else None)
        or plan.get("preferred_search_method")
        or "crawler"
    )
    if not session.get("brave_allowed") and prefill_method in {"brave", "hybrid"}:
        prefill_method = "crawler"
    policy = mode_policy(str(session.get("strategy_mode") or "balanced"))
    plan_resource = _as_mapping(plan.get("resource_policy"))
    manual_prefill = {
        "query_text": str(
            (search_children[-1].get("query_text") if search_children else None)
            or ((plan.get("queries") or [{}])[0].get("query_text") if plan.get("queries") else "")
        ),
        "search_method": prefill_method,
        "max_pages_per_district": policy.max_pages_per_district,
        "max_workers": int(plan_resource.get("max_workers") or policy.max_workers),
        "api_results_per_district": 10,
        "follow_depth": 1 if session.get("strategy_mode") == "thorough" else 0,
    }
    planned_query_count = len(plan.get("queries") or [])
    brave_calls = int(session.get("district_count") or 0) * planned_query_count if session.get("brave_allowed") and plan.get("preferred_search_method") in {"brave", "hybrid"} else 0
    brave_session_max_calls = (
        int(session.get("district_count") or 0)
        * int(session.get("max_child_search_runs") or 0)
        if session.get("brave_allowed")
        else 0
    )
    return render_template(
        "guided_search_detail.html",
        session=session,
        steps=steps,
        children=children,
        evidence=evidence,
        summary=summary,
        plan=plan,
        evaluation=evaluation,
        coverage=coverage,
        manual_prefill=manual_prefill,
        can_continue_plan=can_continue_plan,
        can_stop_and_summarize=can_stop_and_summarize,
        active=active,
        elapsed=_elapsed(session.get("started_at") or session.get("created_at"), session.get("finished_at")),
        estimated_brave_calls=brave_calls,
        estimated_brave_cost=brave_calls * BRAVE_DISPLAY_COST_PER_CALL,
        estimated_brave_session_max_calls=brave_session_max_calls,
        estimated_brave_session_max_cost=(
            brave_session_max_calls * BRAVE_DISPLAY_COST_PER_CALL
        ),
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


@bp.post("/<int:session_id>/clarify")
def clarify(session_id: int):
    session = storage.get_session(session_id)
    if session is None:
        abort(404)
    try:
        if session["status"] != "needs_clarification":
            raise ValueError("This session is not waiting for clarification.")
        if int(session.get("clarification_round") or 0) >= 2:
            raise ValueError("The two-round clarification limit has been reached.")
        questions = list(session.get("clarification_questions") or [])[:3]
        answers = [str(value or "").strip() for value in request.form.getlist("answers")]
        if len(answers) != len(questions) or any(not answer for answer in answers):
            raise ValueError("Answer each clarification question.")
        history = list(session.get("clarification_answers") or [])
        history.append(
            {
                "round": int(session.get("clarification_round") or 0) + 1,
                "answers": [
                    {"question": question, "answer": answer}
                    for question, answer in zip(questions, answers)
                ],
            }
        )
        storage.transition_session(
            session_id,
            "planning",
            stage="clarified_planning",
            updates={
                "clarification_round": int(session.get("clarification_round") or 0) + 1,
                "clarification_answers": history,
                "clarification_questions": [],
                "error_message": None,
                "review_reason": None,
            },
        )
        storage.add_step(session_id, "clarification", status="completed", short_description="User answered planner clarification questions", input_data=history[-1])
        enqueue_guided_search(session_id)
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/start")
def start(session_id: int):
    session = storage.get_session(session_id)
    if session is None:
        abort(404)
    try:
        storage.transition_session(session_id, "queued", stage="queued", expected_status="ready")
        storage.add_step(session_id, "plan_approved", status="completed", short_description="User approved the validated search plan")
        enqueue_guided_search(session_id)
        flash("Guided Search started.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/cancel")
def cancel(session_id: int):
    session = storage.get_session(session_id)
    if session is None:
        abort(404)
    try:
        if session["status"] not in storage.GUIDED_SEARCH_TERMINAL_STATUSES:
            request_session_cancellation(session_id)
            enqueue_guided_search(session_id)
        else:
            raise ValueError(f"Session #{session_id} is already {session['status']}.")
        flash("Cancellation requested. Stored evidence will be preserved.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/retry-ai")
def retry_ai(session_id: int):
    try:
        retry_ai_stage(session_id)
        enqueue_guided_search(session_id)
        flash("Local AI retry queued.", "success")
    except storage.SessionNotFoundError:
        abort(404)
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/continue-plan")
def continue_plan(session_id: int):
    try:
        continue_last_plan(session_id)
        enqueue_guided_search(session_id)
        flash("Continuing from the last validated plan.", "success")
    except storage.SessionNotFoundError:
        abort(404)
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/summarize")
def summarize(session_id: int):
    try:
        stop_and_summarize(session_id)
        enqueue_guided_search(session_id)
        flash("Final summary queued from the evidence already stored.", "success")
    except storage.SessionNotFoundError:
        abort(404)
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.post("/<int:session_id>/query")
def add_query(session_id: int):
    try:
        request_manual_query(
            session_id,
            request.form.get("query_text", "").strip(),
            request.form.get("purpose", "").strip() or "User-requested follow-up",
        )
        enqueue_guided_search(session_id)
        flash("Validated follow-up query queued.", "success")
    except storage.SessionNotFoundError:
        abort(404)
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("guided_search.detail", session_id=session_id))


@bp.get("/<int:session_id>/debug-log")
def debug_log(session_id: int):
    session = storage.get_session(session_id)
    if session is None:
        abort(404)
    if not session.get("debug_log_path"):
        abort(404)
    path = Path(str(session["debug_log_path"])).resolve()
    root = GUIDED_SEARCH_RUN_LOGS_DIR.resolve()
    if path != root and root not in path.parents:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="text/plain; charset=utf-8", as_attachment=False, download_name=path.name)


__all__ = ["bp"]
