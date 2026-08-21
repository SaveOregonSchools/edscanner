"""Manual, live-Ollama planning harness for comparing configured local models.

This module is deliberately excluded from the ordinary test suite. Run it only
when a live configured Ollama endpoint should receive the checked-in synthetic
planning cases.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from search_engine import SEARCH_METHODS

from .ai import GuidedAIError, StructuredOllamaClient, plan_search


@dataclass(frozen=True, slots=True)
class PlanningCase:
    name: str
    objective: str
    example_text: str = ""

    def context(self) -> dict[str, Any]:
        return {
            "original_user_objective_verbatim": self.objective,
            "scope": {
                "states": ["OR"],
                "agency_types": ["Regular local school district"],
                "min_enrollment": None,
                "max_enrollment": None,
                "max_districts": 25,
            },
            "district_count": 25,
            "profile_coverage": {
                "working": 15,
                "requires_javascript": 2,
                "missing": 6,
                "review_or_error": 2,
            },
            "example_text": self.example_text,
            "example_url_content_untrusted_evidence": "",
            "clarification_answers": [],
            "brave_configured": False,
        }


DEFAULT_CASES: tuple[PlanningCase, ...] = (
    PlanningCase(
        "community-schools",
        "Find district-owned evidence that a community schools model is actually being implemented, not merely mentioned.",
    ),
    PlanningCase(
        "restorative-justice-exclusions",
        "Find restorative justice programs, but exclude generic student discipline policies and job postings.",
        "A useful result names a program, implementation activity, coordinator, partner, or participating schools.",
    ),
    PlanningCase(
        "ambiguous-organization",
        "Find districts working with Sunrise, including contracts, programs, or announcements.",
        "Sunrise is an organization name that may be ambiguous; ask a concise clarification if identity changes the search.",
    ),
)


def run_planning_cases(
    cases: Iterable[PlanningCase],
    *,
    client: StructuredOllamaClient,
    mode: str = "balanced",
    brave_allowed: bool = False,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = plan_search(
                case.context(),
                client=client,
                mode=mode,
                available_methods=SEARCH_METHODS,
                brave_allowed=brave_allowed,
            )
        except GuidedAIError as exc:
            reports.append(
                {
                    "case": case.name,
                    "success": False,
                    "error": str(exc),
                    "calls": [call.to_dict() for call in exc.calls],
                }
            )
            continue
        plan = result.value
        successful = result.successful_call
        reports.append(
            {
                "case": case.name,
                "success": True,
                "model": successful.model,
                "endpoint": successful.endpoint,
                "schema_valid_on_first_response": successful.attempt == 1 and not successful.repair,
                "repair_calls": sum(1 for call in result.calls if call.repair),
                "query_count": len(plan.queries),
                "queries": [query.query_text for query in plan.queries],
                "needs_clarification": plan.needs_clarification,
                "clarification_question_count": len(plan.clarification_questions),
                "preferred_search_method": plan.preferred_search_method,
                "profile_policy": plan.profile_policy,
                "latency_seconds": round(sum(call.latency_seconds for call in result.calls), 6),
                "calls": [call.to_dict() for call in result.calls],
            }
        )
    return reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run fixed Guided Search planning cases against a live local Ollama model."
    )
    parser.add_argument("--endpoint", action="append", dest="endpoints", help="Ollama server root; repeat to set failover order.")
    parser.add_argument("--model", help="Ollama model override; defaults to EdScanner Settings.")
    parser.add_argument("--mode", choices=("fast", "balanced", "thorough"), default="balanced")
    parser.add_argument("--case", action="append", dest="case_names", help="Run only a named checked-in case; repeat as needed.")
    parser.add_argument("--allow-brave", action="store_true", help="Permit Brave methods in plan schemas for this manual evaluation only.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selected = list(DEFAULT_CASES)
    if args.case_names:
        requested = set(args.case_names)
        selected = [case for case in selected if case.name in requested]
        missing = sorted(requested - {case.name for case in selected})
        if missing:
            raise SystemExit(f"Unknown case(s): {', '.join(missing)}")
    client = StructuredOllamaClient(endpoints=args.endpoints, model=args.model)
    reports = run_planning_cases(
        selected,
        client=client,
        mode=args.mode,
        brave_allowed=bool(args.allow_brave),
    )
    output: Mapping[str, Any] = {
        "mode": args.mode,
        "case_count": len(selected),
        "success_count": sum(1 for report in reports if report.get("success")),
        "reports": reports,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if all(report.get("success") for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DEFAULT_CASES", "PlanningCase", "main", "run_planning_cases"]
