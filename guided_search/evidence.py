from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref_src",
    }
)
_WS_RE = re.compile(r"\s+")


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def canonical_evidence_url(value: str | None) -> str:
    """Canonicalize a result URL without discarding meaningful search parameters."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if scheme not in {"http", "https"} or not hostname:
            return raw
        try:
            port = parsed.port
        except ValueError:
            return raw
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query_items = []
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
            folded = key.casefold()
            if folded.startswith("utm_") or folded in TRACKING_QUERY_KEYS:
                continue
            query_items.append((key, item_value))
        query_items.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
        return urlunsplit((scheme, host, path, urlencode(query_items, doseq=True), ""))
    except Exception:
        return raw


def content_fingerprint(title: str | None, snippet: str | None) -> str:
    normalized = _WS_RE.sub(" ", f"{title or ''}\n{snippet or ''}").strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def _bounded_text(value: Any, maximum: int) -> str:
    text = _WS_RE.sub(" ", str(value or "")).strip()
    return text[:maximum]


def _matched_terms(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        text = _bounded_text(item, 120)
        if text and text not in out:
            out.append(text)
    return out[:20]


@dataclass(frozen=True)
class EvidenceItem:
    result_id: int
    search_run_id: int
    district_id: int
    district_name: str
    state: str
    query_text: str
    query_purpose: str
    url: str
    canonical_url: str
    title: str
    snippet: str
    score: float
    search_source: str
    matched_terms: tuple[str, ...]
    round_number: int
    is_new: bool
    content_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["matched_terms"] = list(self.matched_terms)
        return payload


@dataclass(frozen=True)
class RoundMetrics:
    round_number: int
    child_search_runs: int
    district_count: int
    district_search_attempts: int
    districts_with_hits: int
    districts_without_hits: int
    failures: int
    result_rows: int
    unique_urls: int
    new_unique_urls: int
    duplicate_result_rows: int
    information_gain: float
    result_sources: dict[str, int]
    score_distribution: dict[str, float | int | None]
    repeated_domains: list[dict[str, Any]]
    repeated_path_patterns: list[dict[str, Any]]
    top_titles: list[str]
    matched_terms: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceBundle:
    metrics: RoundMetrics
    sample: tuple[EvidenceItem, ...]
    unique_items: tuple[EvidenceItem, ...]

    def to_dict(self, *, include_all_unique: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "metrics": self.metrics.to_dict(),
            "sample": [item.to_dict() for item in self.sample],
        }
        if include_all_unique:
            payload["unique_items"] = [item.to_dict() for item in self.unique_items]
        return payload


def _score_summary(scores: Sequence[float]) -> dict[str, float | int | None]:
    if not scores:
        return {"count": 0, "minimum": None, "median": None, "maximum": None, "mean": None}
    return {
        "count": len(scores),
        "minimum": round(min(scores), 3),
        "median": round(float(median(scores)), 3),
        "maximum": round(max(scores), 3),
        "mean": round(sum(scores) / len(scores), 3),
    }


def _path_pattern(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        first_segment = next((part.casefold() for part in parsed.path.split("/") if part), "")
        return host, f"/{first_segment}" if first_segment else "/"
    except Exception:
        return "", "/"


def _make_item(
    row: Mapping[str, Any],
    child: Mapping[str, Any],
    *,
    round_number: int,
    previous_urls: set[str],
) -> EvidenceItem:
    result = _row_dict(row)
    child_data = _row_dict(child)
    canonical = canonical_evidence_url(result.get("url"))
    score = float(result.get("score") or 0.0)
    title = _bounded_text(result.get("title"), 300)
    snippet = _bounded_text(result.get("snippet"), 1200)
    return EvidenceItem(
        result_id=int(result.get("id") or 0),
        search_run_id=int(result.get("search_run_id") or 0),
        district_id=int(result.get("district_id") or 0),
        district_name=_bounded_text(result.get("district_name"), 300),
        state=_bounded_text(result.get("state"), 12),
        query_text=_bounded_text(child_data.get("query_text") or child_data.get("query"), 500),
        query_purpose=_bounded_text(child_data.get("purpose"), 500),
        url=_bounded_text(result.get("url"), 4000),
        canonical_url=canonical,
        title=title,
        snippet=snippet,
        score=score,
        search_source=_bounded_text(result.get("search_source"), 120),
        matched_terms=tuple(_matched_terms(result.get("matched_terms_json"))),
        round_number=round_number,
        is_new=canonical not in previous_urls,
        content_fingerprint=content_fingerprint(title, snippet),
    )


def _diverse_sample(items: Sequence[EvidenceItem], limit: int) -> tuple[EvidenceItem, ...]:
    limit = max(1, min(int(limit), 100))
    if len(items) <= limit:
        return tuple(items)

    ordered = sorted(
        items,
        key=lambda item: (
            not item.is_new,
            -item.score,
            item.district_name.casefold(),
            item.canonical_url,
        ),
    )
    selected: list[EvidenceItem] = []
    selected_ids: set[int] = set()
    per_district: Counter[int] = Counter()
    per_run: Counter[int] = Counter()

    # First pass: reserve representation for every query/run before district
    # diversity can consume the sample budget. This keeps a high-volume query
    # from hiding the evidence returned by other queries in the same round.
    for item in ordered:
        if per_run[item.search_run_id] or len(selected) >= limit:
            continue
        selected.append(item)
        selected_ids.add(item.result_id)
        per_district[item.district_id] += 1
        per_run[item.search_run_id] += 1

    # Second pass: add one strong/new result per district where possible.
    for item in ordered:
        if len(selected) >= limit:
            break
        if item.result_id in selected_ids or per_district[item.district_id]:
            continue
        selected.append(item)
        selected_ids.add(item.result_id)
        per_district[item.district_id] += 1
        per_run[item.search_run_id] += 1

    # Reserve a small slice for borderline results around the median score.
    scores = [item.score for item in items]
    middle = float(median(scores)) if scores else 0.0
    borderline_slots = min(5, max(1, limit // 6))
    borderline = sorted(items, key=lambda item: (abs(item.score - middle), item.canonical_url))
    for item in borderline:
        if len(selected) >= limit or borderline_slots <= 0:
            break
        if item.result_id in selected_ids or per_district[item.district_id] >= 2:
            continue
        selected.append(item)
        selected_ids.add(item.result_id)
        per_district[item.district_id] += 1
        per_run[item.search_run_id] += 1
        borderline_slots -= 1

    # Fill remaining slots with the best evidence, capped per district.
    for item in ordered:
        if len(selected) >= limit:
            break
        if item.result_id in selected_ids or per_district[item.district_id] >= 2:
            continue
        selected.append(item)
        selected_ids.add(item.result_id)
        per_district[item.district_id] += 1
        per_run[item.search_run_id] += 1

    return tuple(selected[:limit])


def build_evidence_bundle(
    child_runs: Iterable[Mapping[str, Any]],
    result_rows: Iterable[Mapping[str, Any]],
    *,
    round_number: int,
    sample_limit: int = 30,
) -> EvidenceBundle:
    """Build deterministic metrics and a bounded, diverse AI evidence sample.

    ``child_runs`` should include ordinary search-run counters and Guided query
    provenance. ``result_rows`` may contain results from every child through the
    requested round; later rounds are ignored.
    """

    round_number = max(1, int(round_number))
    children: list[dict[str, Any]] = []
    by_run: dict[int, dict[str, Any]] = {}
    for raw_child in child_runs:
        child = _row_dict(raw_child)
        kind = str(child.get("child_type") or "search")
        if kind != "search":
            continue
        child_round = int(child.get("round_number") or child.get("round") or 0)
        if child_round <= 0 or child_round > round_number:
            continue
        run_id = int(child.get("child_run_id") or child.get("run_id") or child.get("search_run_id") or 0)
        if run_id <= 0:
            continue
        child["_round"] = child_round
        child["_run_id"] = run_id
        children.append(child)
        by_run[run_id] = child

    raw_results: list[dict[str, Any]] = []
    previous_urls: set[str] = set()
    for raw_row in result_rows:
        row = _row_dict(raw_row)
        run_id = int(row.get("search_run_id") or 0)
        child = by_run.get(run_id)
        if child is None:
            continue
        canonical = canonical_evidence_url(row.get("url"))
        if child["_round"] < round_number and canonical:
            previous_urls.add(canonical)
        raw_results.append(row)

    items: list[EvidenceItem] = []
    for row in raw_results:
        child = by_run[int(row.get("search_run_id") or 0)]
        items.append(
            _make_item(
                row,
                child,
                round_number=int(child["_round"]),
                previous_urls=previous_urls,
            )
        )

    # Keep the strongest representative for a URL while retaining every source
    # row in the ordinary child run tables.
    best_by_url: dict[str, EvidenceItem] = {}
    for item in items:
        key = item.canonical_url or f"result:{item.result_id}"
        current = best_by_url.get(key)
        if current is None or (item.score, -item.result_id) > (current.score, -current.result_id):
            best_by_url[key] = item
    unique_items = tuple(
        sorted(
            best_by_url.values(),
            key=lambda item: (-item.score, item.district_name.casefold(), item.canonical_url),
        )
    )

    current_raw_items = [item for item in items if item.round_number == round_number]
    best_current_by_url: dict[str, EvidenceItem] = {}
    for item in current_raw_items:
        key = item.canonical_url or f"result:{item.result_id}"
        current = best_current_by_url.get(key)
        if current is None or (item.score, -item.result_id) > (
            current.score,
            -current.result_id,
        ):
            best_current_by_url[key] = item
    current_items = sorted(
        best_current_by_url.values(),
        key=lambda item: (-item.score, item.district_name.casefold(), item.canonical_url),
    )
    current_urls = {
        item.canonical_url or f"result:{item.result_id}"
        for item in current_raw_items
    }
    new_urls = current_urls - previous_urls
    districts_with_hits = {item.district_id for item in current_raw_items}
    current_children = [child for child in children if child["_round"] == round_number]
    district_count = max(
        [int(child.get("districts_planned") or child.get("planned") or child.get("districts_matched") or 0) for child in current_children]
        or [0]
    )
    district_attempts = sum(int(child.get("districts_searched") or 0) for child in current_children)
    failures = sum(int(child.get("districts_failed") or 0) for child in current_children)
    source_counts = Counter(item.search_source or "unknown" for item in current_items)
    term_counts = Counter(term for item in current_items for term in item.matched_terms)
    domain_counts: Counter[str] = Counter()
    path_counts: Counter[tuple[str, str]] = Counter()
    for item in current_items:
        domain, path = _path_pattern(item.canonical_url)
        if domain:
            domain_counts[domain] += 1
            path_counts[(domain, path)] += 1

    repeated_domains = [
        {"domain": key, "count": count}
        for key, count in domain_counts.most_common(10)
        if count > 1
    ]
    repeated_paths = [
        {"domain": key[0], "path_prefix": key[1], "count": count}
        for key, count in path_counts.most_common(10)
        if count > 1
    ]
    top_titles: list[str] = []
    for item in sorted(current_items, key=lambda evidence: -evidence.score):
        if item.title and item.title not in top_titles:
            top_titles.append(item.title)
        if len(top_titles) >= 12:
            break

    metrics = RoundMetrics(
        round_number=round_number,
        child_search_runs=len(current_children),
        district_count=district_count,
        district_search_attempts=district_attempts,
        districts_with_hits=len(districts_with_hits),
        districts_without_hits=max(0, district_count - len(districts_with_hits)),
        failures=failures,
        result_rows=len(current_raw_items),
        unique_urls=len(current_urls),
        new_unique_urls=len(new_urls),
        duplicate_result_rows=max(0, len(current_raw_items) - len(current_urls)),
        information_gain=round(len(new_urls) / max(1, len(current_urls)), 4),
        result_sources=dict(sorted(source_counts.items())),
        score_distribution=_score_summary([item.score for item in current_items]),
        repeated_domains=repeated_domains,
        repeated_path_patterns=repeated_paths,
        top_titles=top_titles,
        matched_terms=dict(term_counts.most_common(20)),
    )
    return EvidenceBundle(
        metrics=metrics,
        sample=_diverse_sample(current_items, sample_limit) if current_items else (),
        unique_items=unique_items,
    )


def effectively_same_query(left: str, right: str) -> bool:
    """Conservative duplicate-query check used by deterministic stop rules."""

    def normalize(value: str) -> str:
        text = _WS_RE.sub(" ", str(value or "")).strip().casefold()
        return re.sub(r"[^\w*?]+", " ", text).strip()

    left_norm = normalize(left)
    right_norm = normalize(right)
    if not left_norm or not right_norm:
        return left_norm == right_norm
    if left_norm == right_norm:
        return True
    left_terms = set(left_norm.split())
    right_terms = set(right_norm.split())
    union = left_terms | right_terms
    if not union:
        return True
    similarity = len(left_terms & right_terms) / len(union)
    return math.isclose(similarity, 1.0) or similarity >= 0.92
