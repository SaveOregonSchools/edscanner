from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from search_engine import SEARCH_METHODS, parse_search_query


MAX_CLARIFICATION_QUESTIONS = 3
MAX_SEARCH_CONCEPTS = 8
MAX_SYNONYMS_PER_CONCEPT = 12
MAX_EXCLUDE_TERMS_PER_CONCEPT = 12
MAX_QUERY_PRIORITY = 10
MAX_RESULT_IDS = 100
HARD_MAX_ROUNDS = 5
HARD_MAX_WORKERS = 8
HARD_MAX_CHILD_SEARCH_RUNS = 15

PROFILE_POLICIES = frozenset(
    {"use_existing", "discover_missing", "rediscover_stale", "skip_profiles"}
)
EVALUATION_ASSESSMENTS = frozenset({"good", "mixed", "poor", "no_evidence"})
EVALUATION_ACTIONS = frozenset(
    {
        "accept",
        "run_additional_query",
        "broaden_query",
        "narrow_query",
        "switch_method",
        "discover_profiles",
        "stop_no_evidence",
        "needs_user_input",
    }
)
REVISION_ACTIONS = frozenset(
    {
        "keep_plan",
        "replace_queries",
        "append_queries",
        "switch_method",
        "discover_profiles",
        "needs_user_input",
        "stop",
    }
)
BROWSER_SEARCH_METHODS = frozenset(
    {"district_search_browser", "district_search_browser_hybrid"}
)


class SchemaValidationError(ValueError):
    """Raised when an AI response does not exactly match its controlled schema."""


@dataclass(frozen=True, slots=True)
class ModePolicy:
    name: str
    max_pages_per_district: int
    initial_workers: int
    max_workers: int
    max_rounds: int
    max_queries_per_round: int
    max_child_search_runs: int
    allow_browser_profiles: bool


MODE_POLICIES: Mapping[str, ModePolicy] = MappingProxyType({
    "fast": ModePolicy(
        name="fast",
        max_pages_per_district=10,
        initial_workers=2,
        max_workers=4,
        max_rounds=2,
        max_queries_per_round=2,
        max_child_search_runs=3,
        allow_browser_profiles=False,
    ),
    "balanced": ModePolicy(
        name="balanced",
        max_pages_per_district=25,
        initial_workers=3,
        max_workers=6,
        max_rounds=3,
        max_queries_per_round=3,
        max_child_search_runs=6,
        allow_browser_profiles=True,
    ),
    "thorough": ModePolicy(
        name="thorough",
        max_pages_per_district=50,
        initial_workers=3,
        max_workers=8,
        max_rounds=5,
        max_queries_per_round=3,
        max_child_search_runs=10,
        allow_browser_profiles=True,
    ),
})


def mode_policy(mode: str | ModePolicy) -> ModePolicy:
    if isinstance(mode, ModePolicy):
        canonical = MODE_POLICIES.get(mode.name)
        if canonical != mode:
            raise SchemaValidationError("strategy mode policies are controlled by EdScanner")
        return canonical
    key = str(mode or "").strip().casefold()
    try:
        return MODE_POLICIES[key]
    except KeyError as exc:
        raise SchemaValidationError(
            f"strategy mode must be one of: {', '.join(MODE_POLICIES)}"
        ) from exc


def _require_object(
    value: Any,
    *,
    name: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{name} must be a JSON object")
    required_keys = frozenset(required)
    allowed_keys = required_keys | frozenset(optional)
    actual_keys = frozenset(str(key) for key in value.keys())
    unknown = sorted(actual_keys - allowed_keys)
    missing = sorted(required_keys - actual_keys)
    if unknown:
        raise SchemaValidationError(f"{name} contains unknown field(s): {', '.join(unknown)}")
    if missing:
        raise SchemaValidationError(f"{name} is missing required field(s): {', '.join(missing)}")
    return value


def _text(
    value: Any,
    *,
    name: str,
    minimum: int = 0,
    maximum: int = 2_000,
) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{name} must be a string")
    result = value.strip()
    if len(result) < minimum:
        raise SchemaValidationError(f"{name} must contain at least {minimum} character(s)")
    if len(result) > maximum:
        raise SchemaValidationError(f"{name} must contain at most {maximum} characters")
    return result


def _boolean(value: Any, *, name: str) -> bool:
    if type(value) is not bool:
        raise SchemaValidationError(f"{name} must be a boolean")
    return value


def _integer(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise SchemaValidationError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise SchemaValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise SchemaValidationError(f"{name} must be between {minimum} and {maximum}")
    return result


def _string_list(
    value: Any,
    *,
    name: str,
    maximum_items: int,
    item_maximum: int = 500,
    minimum_items: int = 0,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{name} must be an array")
    if len(value) < minimum_items or len(value) > maximum_items:
        raise SchemaValidationError(
            f"{name} must contain between {minimum_items} and {maximum_items} item(s)"
        )
    result = tuple(
        _text(item, name=f"{name}[{index}]", minimum=1, maximum=item_maximum)
        for index, item in enumerate(value)
    )
    if len(set(item.casefold() for item in result)) != len(result):
        raise SchemaValidationError(f"{name} must not contain duplicate values")
    return result


def _result_ids(
    value: Any,
    *,
    name: str,
    allowed_ids: set[int] | None,
) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > MAX_RESULT_IDS:
        raise SchemaValidationError(f"{name} must be an array of at most {MAX_RESULT_IDS} integers")
    result: list[int] = []
    for index, item in enumerate(value):
        if type(item) is not int or item < 1:
            raise SchemaValidationError(f"{name}[{index}] must be a positive integer")
        if item in result:
            raise SchemaValidationError(f"{name} must not contain duplicate IDs")
        if allowed_ids is not None and item not in allowed_ids:
            raise SchemaValidationError(f"{name}[{index}] refers to an unavailable result ID")
        result.append(item)
    return tuple(result)


def _controlled_choice(value: Any, *, name: str, choices: Iterable[str]) -> str:
    result = _text(value, name=name, minimum=1, maximum=100)
    allowed = frozenset(choices)
    if result not in allowed:
        raise SchemaValidationError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return result


def _search_method(
    value: Any,
    *,
    name: str,
    available_methods: Iterable[str] | None,
    brave_allowed: bool,
) -> str:
    allowed = frozenset(available_methods if available_methods is not None else SEARCH_METHODS)
    if not allowed.issubset(SEARCH_METHODS):
        unknown = sorted(allowed - SEARCH_METHODS)
        raise SchemaValidationError(f"application supplied unknown search method(s): {', '.join(unknown)}")
    result = _controlled_choice(value, name=name, choices=allowed)
    if not brave_allowed and result in {"brave", "hybrid"}:
        raise SchemaValidationError(f"{name} requires explicit Brave Search permission")
    return result


def _nullable_search_method(
    value: Any,
    *,
    name: str,
    available_methods: Iterable[str] | None,
    brave_allowed: bool,
) -> str | None:
    if value is None:
        return None
    return _search_method(
        value,
        name=name,
        available_methods=available_methods,
        brave_allowed=brave_allowed,
    )


def _permitted_search_methods(
    available_methods: Iterable[str] | None,
    *,
    policy: ModePolicy,
    brave_allowed: bool,
) -> tuple[str, ...]:
    methods = frozenset(available_methods if available_methods is not None else SEARCH_METHODS)
    unknown = methods - SEARCH_METHODS
    if unknown:
        raise SchemaValidationError(
            f"application supplied unknown search method(s): {', '.join(sorted(unknown))}"
        )
    if not brave_allowed:
        methods -= {"brave", "hybrid"}
    if not policy.allow_browser_profiles:
        methods -= BROWSER_SEARCH_METHODS
    return tuple(sorted(methods))


def _query_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["query_text", "purpose", "priority"],
        "properties": {
            "query_text": {"type": "string", "minLength": 1, "maxLength": 500},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
            "priority": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_PRIORITY},
        },
    }


@dataclass(frozen=True, slots=True)
class SearchConcept:
    name: str
    required: bool
    synonyms: tuple[str, ...]
    exclude_terms: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SearchConcept":
        data = _require_object(
            value,
            name="search_concept",
            required=("name", "required", "synonyms", "exclude_terms"),
        )
        return cls(
            name=_text(data["name"], name="search_concept.name", minimum=1, maximum=200),
            required=_boolean(data["required"], name="search_concept.required"),
            synonyms=_string_list(
                data["synonyms"],
                name="search_concept.synonyms",
                maximum_items=MAX_SYNONYMS_PER_CONCEPT,
                item_maximum=200,
            ),
            exclude_terms=_string_list(
                data["exclude_terms"],
                name="search_concept.exclude_terms",
                maximum_items=MAX_EXCLUDE_TERMS_PER_CONCEPT,
                item_maximum=200,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "synonyms": list(self.synonyms),
            "exclude_terms": list(self.exclude_terms),
        }

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "required", "synonyms", "exclude_terms"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "required": {"type": "boolean"},
                "synonyms": {
                    "type": "array",
                    "maxItems": MAX_SYNONYMS_PER_CONCEPT,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "exclude_terms": {
                    "type": "array",
                    "maxItems": MAX_EXCLUDE_TERMS_PER_CONCEPT,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                },
            },
        }


@dataclass(frozen=True, slots=True)
class SearchQuery:
    query_text: str
    purpose: str
    priority: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SearchQuery":
        data = _require_object(
            value,
            name="query",
            required=("query_text", "purpose", "priority"),
        )
        query_text = _text(data["query_text"], name="query.query_text", minimum=1, maximum=500)
        try:
            parse_search_query(query_text)
        except ValueError as exc:
            raise SchemaValidationError(f"query.query_text is not valid EdScanner syntax: {exc}") from exc
        return cls(
            query_text=query_text,
            purpose=_text(data["purpose"], name="query.purpose", minimum=1, maximum=500),
            priority=_integer(
                data["priority"],
                name="query.priority",
                minimum=1,
                maximum=MAX_QUERY_PRIORITY,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_text": self.query_text,
            "purpose": self.purpose,
            "priority": self.priority,
        }

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return _query_schema()


@dataclass(frozen=True, slots=True)
class EvaluationCriteria:
    positive_signals: tuple[str, ...]
    negative_signals: tuple[str, ...]
    minimum_evidence: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationCriteria":
        data = _require_object(
            value,
            name="evaluation_criteria",
            required=("positive_signals", "negative_signals", "minimum_evidence"),
        )
        return cls(
            positive_signals=_string_list(
                data["positive_signals"],
                name="evaluation_criteria.positive_signals",
                maximum_items=12,
            ),
            negative_signals=_string_list(
                data["negative_signals"],
                name="evaluation_criteria.negative_signals",
                maximum_items=12,
            ),
            minimum_evidence=_text(
                data["minimum_evidence"],
                name="evaluation_criteria.minimum_evidence",
                minimum=1,
                maximum=1_000,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "positive_signals": list(self.positive_signals),
            "negative_signals": list(self.negative_signals),
            "minimum_evidence": self.minimum_evidence,
        }

    @staticmethod
    def json_schema() -> dict[str, Any]:
        signal_schema = {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["positive_signals", "negative_signals", "minimum_evidence"],
            "properties": {
                "positive_signals": signal_schema,
                "negative_signals": signal_schema,
                "minimum_evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1_000,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class StopPolicy:
    max_rounds: int
    max_child_search_runs: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, policy: ModePolicy) -> "StopPolicy":
        data = _require_object(
            value,
            name="stop_policy",
            required=("max_rounds", "max_child_search_runs"),
        )
        return cls(
            max_rounds=_integer(
                data["max_rounds"],
                name="stop_policy.max_rounds",
                minimum=1,
                maximum=min(HARD_MAX_ROUNDS, policy.max_rounds),
            ),
            max_child_search_runs=_integer(
                data["max_child_search_runs"],
                name="stop_policy.max_child_search_runs",
                minimum=1,
                maximum=min(HARD_MAX_CHILD_SEARCH_RUNS, policy.max_child_search_runs),
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_rounds": self.max_rounds,
            "max_child_search_runs": self.max_child_search_runs,
        }

    @staticmethod
    def json_schema(policy: ModePolicy) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_rounds", "max_child_search_runs"],
            "properties": {
                "max_rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": min(HARD_MAX_ROUNDS, policy.max_rounds),
                },
                "max_child_search_runs": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": min(HARD_MAX_CHILD_SEARCH_RUNS, policy.max_child_search_runs),
                },
            },
        }


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    initial_workers: int
    max_workers: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, policy: ModePolicy) -> "ResourcePolicy":
        data = _require_object(
            value,
            name="resource_policy",
            required=("initial_workers", "max_workers"),
        )
        maximum = min(HARD_MAX_WORKERS, policy.max_workers)
        initial = _integer(
            data["initial_workers"],
            name="resource_policy.initial_workers",
            minimum=1,
            maximum=maximum,
        )
        max_workers = _integer(
            data["max_workers"],
            name="resource_policy.max_workers",
            minimum=1,
            maximum=maximum,
        )
        if initial > max_workers:
            raise SchemaValidationError("resource_policy.initial_workers cannot exceed max_workers")
        return cls(initial_workers=initial, max_workers=max_workers)

    def to_dict(self) -> dict[str, int]:
        return {"initial_workers": self.initial_workers, "max_workers": self.max_workers}

    @staticmethod
    def json_schema(policy: ModePolicy) -> dict[str, Any]:
        maximum = min(HARD_MAX_WORKERS, policy.max_workers)
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["initial_workers", "max_workers"],
            "properties": {
                "initial_workers": {"type": "integer", "minimum": 1, "maximum": maximum},
                "max_workers": {"type": "integer", "minimum": 1, "maximum": maximum},
            },
        }


@dataclass(frozen=True, slots=True)
class SearchPlan:
    objective: str
    needs_clarification: bool
    clarification_questions: tuple[str, ...]
    search_concepts: tuple[SearchConcept, ...]
    queries: tuple[SearchQuery, ...]
    profile_policy: str
    preferred_search_method: str
    evaluation_criteria: EvaluationCriteria
    stop_policy: StopPolicy
    resource_policy: ResourcePolicy
    short_explanation: str

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
    ) -> "SearchPlan":
        policy = mode_policy(mode)
        permitted_methods = _permitted_search_methods(
            available_methods,
            policy=policy,
            brave_allowed=brave_allowed,
        )
        fields = (
            "objective",
            "needs_clarification",
            "clarification_questions",
            "search_concepts",
            "queries",
            "profile_policy",
            "preferred_search_method",
            "evaluation_criteria",
            "stop_policy",
            "resource_policy",
            "short_explanation",
        )
        data = _require_object(value, name="search_plan", required=fields)
        needs_clarification = _boolean(
            data["needs_clarification"], name="search_plan.needs_clarification"
        )
        questions = _string_list(
            data["clarification_questions"],
            name="search_plan.clarification_questions",
            minimum_items=1 if needs_clarification else 0,
            maximum_items=MAX_CLARIFICATION_QUESTIONS if needs_clarification else 0,
            item_maximum=500,
        )
        raw_concepts = data["search_concepts"]
        if not isinstance(raw_concepts, list) or len(raw_concepts) > MAX_SEARCH_CONCEPTS:
            raise SchemaValidationError(
                f"search_plan.search_concepts must be an array of at most {MAX_SEARCH_CONCEPTS} items"
            )
        if not needs_clarification and not raw_concepts:
            raise SchemaValidationError("search_plan.search_concepts must not be empty for a ready plan")
        concepts = tuple(SearchConcept.from_dict(item) for item in raw_concepts)
        raw_queries = data["queries"]
        if not isinstance(raw_queries, list):
            raise SchemaValidationError("search_plan.queries must be an array")
        minimum_queries = 0 if needs_clarification else 1
        if len(raw_queries) < minimum_queries or len(raw_queries) > policy.max_queries_per_round:
            raise SchemaValidationError(
                "search_plan.queries must contain between "
                f"{minimum_queries} and {policy.max_queries_per_round} item(s)"
            )
        queries = tuple(SearchQuery.from_dict(item) for item in raw_queries)
        if len({query.query_text.casefold() for query in queries}) != len(queries):
            raise SchemaValidationError("search_plan.queries must not contain duplicate query text")
        return cls(
            objective=_text(data["objective"], name="search_plan.objective", minimum=1, maximum=2_000),
            needs_clarification=needs_clarification,
            clarification_questions=questions,
            search_concepts=concepts,
            queries=queries,
            profile_policy=_controlled_choice(
                data["profile_policy"],
                name="search_plan.profile_policy",
                choices=PROFILE_POLICIES,
            ),
            preferred_search_method=_search_method(
                data["preferred_search_method"],
                name="search_plan.preferred_search_method",
                available_methods=permitted_methods,
                brave_allowed=brave_allowed,
            ),
            evaluation_criteria=EvaluationCriteria.from_dict(data["evaluation_criteria"]),
            stop_policy=StopPolicy.from_dict(data["stop_policy"], policy=policy),
            resource_policy=ResourcePolicy.from_dict(data["resource_policy"], policy=policy),
            short_explanation=_text(
                data["short_explanation"],
                name="search_plan.short_explanation",
                minimum=1,
                maximum=1_500,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "needs_clarification": self.needs_clarification,
            "clarification_questions": list(self.clarification_questions),
            "search_concepts": [item.to_dict() for item in self.search_concepts],
            "queries": [item.to_dict() for item in self.queries],
            "profile_policy": self.profile_policy,
            "preferred_search_method": self.preferred_search_method,
            "evaluation_criteria": self.evaluation_criteria.to_dict(),
            "stop_policy": self.stop_policy.to_dict(),
            "resource_policy": self.resource_policy.to_dict(),
            "short_explanation": self.short_explanation,
        }

    @classmethod
    def json_schema(
        cls,
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
    ) -> dict[str, Any]:
        policy = mode_policy(mode)
        methods = list(
            _permitted_search_methods(
                available_methods,
                policy=policy,
                brave_allowed=brave_allowed,
            )
        )
        properties = {
            "objective": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "needs_clarification": {"type": "boolean"},
            "clarification_questions": {
                "type": "array",
                "maxItems": MAX_CLARIFICATION_QUESTIONS,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "search_concepts": {
                "type": "array",
                "maxItems": MAX_SEARCH_CONCEPTS,
                "items": SearchConcept.json_schema(),
            },
            "queries": {
                "type": "array",
                "maxItems": policy.max_queries_per_round,
                "items": _query_schema(),
            },
            "profile_policy": {"type": "string", "enum": sorted(PROFILE_POLICIES)},
            "preferred_search_method": {"type": "string", "enum": methods},
            "evaluation_criteria": EvaluationCriteria.json_schema(),
            "stop_policy": StopPolicy.json_schema(policy),
            "resource_policy": ResourcePolicy.json_schema(policy),
            "short_explanation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1_500,
            },
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }


@dataclass(frozen=True, slots=True)
class SearchEvaluation:
    assessment: str
    confidence: float
    appears_to_match_user_intent: bool
    useful_result_ids: tuple[int, ...]
    likely_false_positive_ids: tuple[int, ...]
    positive_patterns: tuple[str, ...]
    false_positive_patterns: tuple[str, ...]
    missing_concepts: tuple[str, ...]
    new_information_gain: float
    recommended_action: str
    proposed_queries: tuple[SearchQuery, ...]
    recommended_search_method: str | None
    short_reason: str

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
        available_result_ids: Iterable[int] | None = None,
    ) -> "SearchEvaluation":
        policy = mode_policy(mode)
        permitted_methods = _permitted_search_methods(
            available_methods,
            policy=policy,
            brave_allowed=brave_allowed,
        )
        fields = (
            "assessment",
            "confidence",
            "appears_to_match_user_intent",
            "useful_result_ids",
            "likely_false_positive_ids",
            "positive_patterns",
            "false_positive_patterns",
            "missing_concepts",
            "new_information_gain",
            "recommended_action",
            "proposed_queries",
            "recommended_search_method",
            "short_reason",
        )
        data = _require_object(value, name="search_evaluation", required=fields)
        allowed_ids = set(available_result_ids) if available_result_ids is not None else None
        useful = _result_ids(
            data["useful_result_ids"],
            name="search_evaluation.useful_result_ids",
            allowed_ids=allowed_ids,
        )
        false_positive = _result_ids(
            data["likely_false_positive_ids"],
            name="search_evaluation.likely_false_positive_ids",
            allowed_ids=allowed_ids,
        )
        if set(useful) & set(false_positive):
            raise SchemaValidationError("a result cannot be both useful and a likely false positive")
        action = _controlled_choice(
            data["recommended_action"],
            name="search_evaluation.recommended_action",
            choices=EVALUATION_ACTIONS,
        )
        raw_queries = data["proposed_queries"]
        if not isinstance(raw_queries, list) or len(raw_queries) > policy.max_queries_per_round:
            raise SchemaValidationError(
                "search_evaluation.proposed_queries must be an array of at most "
                f"{policy.max_queries_per_round} items"
            )
        proposed = tuple(SearchQuery.from_dict(item) for item in raw_queries)
        if len({query.query_text.casefold() for query in proposed}) != len(proposed):
            raise SchemaValidationError(
                "search_evaluation.proposed_queries must not contain duplicate query text"
            )
        if action in {"run_additional_query", "broaden_query", "narrow_query"} and not proposed:
            raise SchemaValidationError(f"{action} requires at least one proposed query")
        recommended_method = _nullable_search_method(
            data["recommended_search_method"],
            name="search_evaluation.recommended_search_method",
            available_methods=permitted_methods,
            brave_allowed=brave_allowed,
        )
        if action == "switch_method" and recommended_method is None:
            raise SchemaValidationError("switch_method requires recommended_search_method")
        return cls(
            assessment=_controlled_choice(
                data["assessment"],
                name="search_evaluation.assessment",
                choices=EVALUATION_ASSESSMENTS,
            ),
            confidence=_number(
                data["confidence"], name="search_evaluation.confidence", minimum=0, maximum=1
            ),
            appears_to_match_user_intent=_boolean(
                data["appears_to_match_user_intent"],
                name="search_evaluation.appears_to_match_user_intent",
            ),
            useful_result_ids=useful,
            likely_false_positive_ids=false_positive,
            positive_patterns=_string_list(
                data["positive_patterns"],
                name="search_evaluation.positive_patterns",
                maximum_items=12,
            ),
            false_positive_patterns=_string_list(
                data["false_positive_patterns"],
                name="search_evaluation.false_positive_patterns",
                maximum_items=12,
            ),
            missing_concepts=_string_list(
                data["missing_concepts"],
                name="search_evaluation.missing_concepts",
                maximum_items=12,
            ),
            new_information_gain=_number(
                data["new_information_gain"],
                name="search_evaluation.new_information_gain",
                minimum=0,
                maximum=1,
            ),
            recommended_action=action,
            proposed_queries=proposed,
            recommended_search_method=recommended_method,
            short_reason=_text(
                data["short_reason"],
                name="search_evaluation.short_reason",
                minimum=1,
                maximum=1_000,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment,
            "confidence": self.confidence,
            "appears_to_match_user_intent": self.appears_to_match_user_intent,
            "useful_result_ids": list(self.useful_result_ids),
            "likely_false_positive_ids": list(self.likely_false_positive_ids),
            "positive_patterns": list(self.positive_patterns),
            "false_positive_patterns": list(self.false_positive_patterns),
            "missing_concepts": list(self.missing_concepts),
            "new_information_gain": self.new_information_gain,
            "recommended_action": self.recommended_action,
            "proposed_queries": [item.to_dict() for item in self.proposed_queries],
            "recommended_search_method": self.recommended_search_method,
            "short_reason": self.short_reason,
        }

    @classmethod
    def json_schema(
        cls,
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
    ) -> dict[str, Any]:
        policy = mode_policy(mode)
        methods = list(
            _permitted_search_methods(
                available_methods,
                policy=policy,
                brave_allowed=brave_allowed,
            )
        )
        string_array = {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        }
        id_array = {
            "type": "array",
            "maxItems": MAX_RESULT_IDS,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        }
        properties = {
            "assessment": {"type": "string", "enum": sorted(EVALUATION_ASSESSMENTS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "appears_to_match_user_intent": {"type": "boolean"},
            "useful_result_ids": id_array,
            "likely_false_positive_ids": id_array,
            "positive_patterns": string_array,
            "false_positive_patterns": string_array,
            "missing_concepts": string_array,
            "new_information_gain": {"type": "number", "minimum": 0, "maximum": 1},
            "recommended_action": {"type": "string", "enum": sorted(EVALUATION_ACTIONS)},
            "proposed_queries": {
                "type": "array",
                "maxItems": policy.max_queries_per_round,
                "items": _query_schema(),
            },
            "recommended_search_method": {
                "anyOf": [{"type": "null"}, {"type": "string", "enum": methods}]
            },
            "short_reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }


@dataclass(frozen=True, slots=True)
class PlanRevision:
    action: str
    queries: tuple[SearchQuery, ...]
    profile_policy: str | None
    preferred_search_method: str | None
    clarification_questions: tuple[str, ...]
    short_explanation: str

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
    ) -> "PlanRevision":
        policy = mode_policy(mode)
        permitted_methods = _permitted_search_methods(
            available_methods,
            policy=policy,
            brave_allowed=brave_allowed,
        )
        fields = (
            "action",
            "queries",
            "profile_policy",
            "preferred_search_method",
            "clarification_questions",
            "short_explanation",
        )
        data = _require_object(value, name="plan_revision", required=fields)
        action = _controlled_choice(
            data["action"], name="plan_revision.action", choices=REVISION_ACTIONS
        )
        raw_queries = data["queries"]
        if not isinstance(raw_queries, list) or len(raw_queries) > policy.max_queries_per_round:
            raise SchemaValidationError(
                f"plan_revision.queries must be an array of at most {policy.max_queries_per_round} items"
            )
        queries = tuple(SearchQuery.from_dict(item) for item in raw_queries)
        if len({query.query_text.casefold() for query in queries}) != len(queries):
            raise SchemaValidationError(
                "plan_revision.queries must not contain duplicate query text"
            )
        if action in {"replace_queries", "append_queries"} and not queries:
            raise SchemaValidationError(f"{action} requires at least one query")
        profile_policy = (
            None
            if data["profile_policy"] is None
            else _controlled_choice(
                data["profile_policy"],
                name="plan_revision.profile_policy",
                choices=PROFILE_POLICIES,
            )
        )
        preferred_method = _nullable_search_method(
            data["preferred_search_method"],
            name="plan_revision.preferred_search_method",
            available_methods=permitted_methods,
            brave_allowed=brave_allowed,
        )
        questions = _string_list(
            data["clarification_questions"],
            name="plan_revision.clarification_questions",
            minimum_items=1 if action == "needs_user_input" else 0,
            maximum_items=MAX_CLARIFICATION_QUESTIONS if action == "needs_user_input" else 0,
        )
        if action == "switch_method" and preferred_method is None:
            raise SchemaValidationError("switch_method requires preferred_search_method")
        if action == "discover_profiles" and profile_policy not in {
            "discover_missing",
            "rediscover_stale",
        }:
            raise SchemaValidationError("discover_profiles requires a discovery profile_policy")
        return cls(
            action=action,
            queries=queries,
            profile_policy=profile_policy,
            preferred_search_method=preferred_method,
            clarification_questions=questions,
            short_explanation=_text(
                data["short_explanation"],
                name="plan_revision.short_explanation",
                minimum=1,
                maximum=1_000,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "queries": [item.to_dict() for item in self.queries],
            "profile_policy": self.profile_policy,
            "preferred_search_method": self.preferred_search_method,
            "clarification_questions": list(self.clarification_questions),
            "short_explanation": self.short_explanation,
        }

    @classmethod
    def json_schema(
        cls,
        *,
        mode: str | ModePolicy = "balanced",
        available_methods: Iterable[str] | None = None,
        brave_allowed: bool = False,
    ) -> dict[str, Any]:
        policy = mode_policy(mode)
        methods = list(
            _permitted_search_methods(
                available_methods,
                policy=policy,
                brave_allowed=brave_allowed,
            )
        )
        properties = {
            "action": {"type": "string", "enum": sorted(REVISION_ACTIONS)},
            "queries": {
                "type": "array",
                "maxItems": policy.max_queries_per_round,
                "items": _query_schema(),
            },
            "profile_policy": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "string", "enum": sorted(PROFILE_POLICIES)},
                ]
            },
            "preferred_search_method": {
                "anyOf": [{"type": "null"}, {"type": "string", "enum": methods}]
            },
            "clarification_questions": {
                "type": "array",
                "maxItems": MAX_CLARIFICATION_QUESTIONS,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "short_explanation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1_000,
            },
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }


@dataclass(frozen=True, slots=True)
class GuidedSummary:
    overview: str
    what_was_searched: str
    what_was_found: str
    how_search_evolved: str
    limitations: tuple[str, ...]
    highest_confidence_result_ids: tuple[int, ...]
    uncertain_result_ids: tuple[int, ...]

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        available_result_ids: Iterable[int] | None = None,
    ) -> "GuidedSummary":
        fields = (
            "overview",
            "what_was_searched",
            "what_was_found",
            "how_search_evolved",
            "limitations",
            "highest_confidence_result_ids",
            "uncertain_result_ids",
        )
        data = _require_object(value, name="guided_summary", required=fields)
        allowed_ids = set(available_result_ids) if available_result_ids is not None else None
        high = _result_ids(
            data["highest_confidence_result_ids"],
            name="guided_summary.highest_confidence_result_ids",
            allowed_ids=allowed_ids,
        )
        uncertain = _result_ids(
            data["uncertain_result_ids"],
            name="guided_summary.uncertain_result_ids",
            allowed_ids=allowed_ids,
        )
        if set(high) & set(uncertain):
            raise SchemaValidationError(
                "a result cannot be both highest-confidence and uncertain"
            )
        return cls(
            overview=_text(data["overview"], name="guided_summary.overview", minimum=1, maximum=2_000),
            what_was_searched=_text(
                data["what_was_searched"],
                name="guided_summary.what_was_searched",
                minimum=1,
                maximum=4_000,
            ),
            what_was_found=_text(
                data["what_was_found"],
                name="guided_summary.what_was_found",
                minimum=1,
                maximum=6_000,
            ),
            how_search_evolved=_text(
                data["how_search_evolved"],
                name="guided_summary.how_search_evolved",
                minimum=1,
                maximum=4_000,
            ),
            limitations=_string_list(
                data["limitations"],
                name="guided_summary.limitations",
                minimum_items=1,
                maximum_items=20,
                item_maximum=1_000,
            ),
            highest_confidence_result_ids=high,
            uncertain_result_ids=uncertain,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "overview": self.overview,
            "what_was_searched": self.what_was_searched,
            "what_was_found": self.what_was_found,
            "how_search_evolved": self.how_search_evolved,
            "limitations": list(self.limitations),
            "highest_confidence_result_ids": list(self.highest_confidence_result_ids),
            "uncertain_result_ids": list(self.uncertain_result_ids),
        }

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        id_array = {
            "type": "array",
            "maxItems": MAX_RESULT_IDS,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        }
        properties = {
            "overview": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "what_was_searched": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4_000,
            },
            "what_was_found": {"type": "string", "minLength": 1, "maxLength": 6_000},
            "how_search_evolved": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4_000,
            },
            "limitations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
            },
            "highest_confidence_result_ids": id_array,
            "uncertain_result_ids": id_array,
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }


__all__ = [
    "EVALUATION_ACTIONS",
    "EVALUATION_ASSESSMENTS",
    "EvaluationCriteria",
    "GuidedSummary",
    "HARD_MAX_ROUNDS",
    "HARD_MAX_WORKERS",
    "MAX_CLARIFICATION_QUESTIONS",
    "MODE_POLICIES",
    "ModePolicy",
    "PROFILE_POLICIES",
    "PlanRevision",
    "REVISION_ACTIONS",
    "ResourcePolicy",
    "SchemaValidationError",
    "SearchConcept",
    "SearchEvaluation",
    "SearchPlan",
    "SearchQuery",
    "StopPolicy",
    "mode_policy",
]
