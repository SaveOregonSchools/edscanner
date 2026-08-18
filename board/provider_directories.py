from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from common import normalize_state

from .adapters.base import assess_challenge, collapse_ws
from .http import BoardHTTPClient, BoardHTTPError


BOARD_PROVIDER_DIRECTORY_ENV = "EDSCANNER_BOARD_PROVIDER_DIRECTORY_ENABLED"
BOARD_PROVIDER_DIRECTORY_WARNING = (
    "Provider-directory lookup is disabled by default. Enable it only after the "
    "operator confirms that the provider permits automated directory access."
)
BOARD_BOOK_DIRECTORY_URL = "https://meetings.boardbook.org/Public"

_BOARD_BOOK_ORGANIZATION_PATH = re.compile(
    r"^/Public/Organization/(?P<external_id>[A-Za-z0-9][A-Za-z0-9_-]{0,127})/?$",
    re.IGNORECASE,
)
_BOARD_BOOK_LOCATION = re.compile(
    r"^\s*(?P<city>.+?),\s*(?P<state>[A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$"
)
_MISSING_VALUES = {"", "-", "--", "n/a", "na", "none", "null", "not applicable", "†"}
_GENERIC_NAME_TOKENS = {
    "administration",
    "administrative",
    "board",
    "education",
    "independent",
    "local",
    "public",
    "school",
    "schools",
    "district",
    "districts",
}
_NAME_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "adm": ("administrative",),
    "boe": ("board", "education"),
    "csd": ("community", "school", "district"),
    "cusd": ("community", "unit", "school", "district"),
    "isd": ("independent", "school", "district"),
    "sd": ("school", "district"),
    "usd": ("unified", "school", "district"),
}


def provider_directory_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the explicitly opt-in provider-directory feature is enabled."""

    source = os.environ if environ is None else environ
    return str(source.get(BOARD_PROVIDER_DIRECTORY_ENV, "")).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ascii_words(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", errors="ignore").decode("ascii").casefold()
    text = text.replace("&", " and ")
    raw = re.findall(r"[a-z0-9]+", text)
    expanded: list[str] = []
    for token in raw:
        expanded.extend(_NAME_EXPANSIONS.get(token, (token,)))
    return expanded


def normalized_organization_name(value: Any) -> str:
    """Normalize organizational wording while retaining meaningful place tokens."""

    tokens = [token for token in _ascii_words(value) if token not in _GENERIC_NAME_TOKENS]
    return " ".join(tokens)


def _name_score(left: Any, right: Any) -> float:
    left_words = normalized_organization_name(left)
    right_words = normalized_organization_name(right)
    if not left_words or not right_words:
        return 0.0
    if left_words == right_words:
        return 1.0

    left_tokens = left_words.split()
    right_tokens = right_words.split()
    left_numbers = {token for token in left_tokens if token.isdigit()}
    right_numbers = {token for token in right_tokens if token.isdigit()}
    if left_numbers and right_numbers and left_numbers != right_numbers:
        number_penalty = 0.72
    else:
        number_penalty = 1.0

    left_compact = "".join(left_tokens)
    right_compact = "".join(right_tokens)
    compact_ratio = SequenceMatcher(None, left_compact, right_compact).ratio()
    phrase_ratio = SequenceMatcher(None, left_words, right_words).ratio()
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    token_overlap = len(left_set & right_set) / max(1, len(left_set | right_set))
    score = max(compact_ratio, phrase_ratio, token_overlap)
    return round(min(1.0, score * number_penalty), 6)


def _clean_location_value(value: Any) -> str:
    text = collapse_ws(value)
    return "" if text.casefold() in _MISSING_VALUES else text


def _raw_district_values(district: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = district.get("raw_json")
    if isinstance(raw, Mapping):
        value = raw
    else:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(value, Mapping):
        return {}
    # NCES imports preserve three header views under raw_json. Prefer the
    # human-readable cleaned headers while retaining compatibility with older
    # flat fixtures/imports and the snake-case view.
    flattened = dict(value)
    for group_name in ("original_headers", "snake_case", "cleaned_headers"):
        group = value.get(group_name)
        if isinstance(group, Mapping):
            flattened.update(group)
    return flattened


def district_location_values(district: Mapping[str, Any]) -> tuple[str, frozenset[str]]:
    """Return the NCES state plus any location/mailing cities retained in raw JSON."""

    expected_state = normalize_state(district.get("state"))
    cities: set[str] = set()
    for key, value in _raw_district_values(district).items():
        normalized_key = " ".join(_ascii_words(key))
        if normalized_key.startswith(("location city", "mailing city")):
            city = _clean_location_value(value)
            if city:
                cities.add(" ".join(_ascii_words(city)))
    return expected_state, frozenset(cities)


def _district_identity_signals(
    district: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str], str]:
    """Return place words, explicit district identifiers, and website host.

    Provider display names often omit administrative numbers. State-only
    corroboration is not enough in that case because multiple same-state
    districts can share a short place name such as ``Union``.
    """

    name = district.get("agency_name") or district.get("name")
    identity_words = normalized_organization_name(name).split()
    identifiers = frozenset(
        word for word in identity_words if any(char.isdigit() for char in word)
    )
    place_words = frozenset(word for word in identity_words if word not in identifiers)
    website = str(
        district.get("website_normalized") or district.get("website") or ""
    ).strip()
    website_host = (urlsplit(website).hostname or "").casefold().removeprefix("www.")
    return place_words, identifiers, website_host


def _homepage_matches(host: str, homepage_urls: Sequence[str]) -> bool:
    if not host:
        return False
    for url in homepage_urls:
        candidate = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
        if candidate and candidate == host:
            return True
    return False


@dataclass(frozen=True, slots=True)
class BoardBookDirectoryEntry:
    external_id: str
    organization_name: str
    public_url: str


@dataclass(frozen=True, slots=True)
class BoardBookDirectoryCandidate:
    entry: BoardBookDirectoryEntry
    name_score: float


@dataclass(frozen=True, slots=True)
class BoardBookOrganizationEvidence:
    entry: BoardBookDirectoryEntry
    organization_name: str
    states: frozenset[str]
    cities: frozenset[str]
    meeting_count: int
    homepage_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardBookDirectoryMatch:
    status: str
    reason: str
    candidates: tuple[BoardBookDirectoryCandidate, ...] = ()
    verified: BoardBookDirectoryCandidate | None = None
    evidence: tuple[BoardBookOrganizationEvidence, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def is_verified(self) -> bool:
        return self.status == "verified" and self.verified is not None


def parse_boardbook_directory(
    content: bytes | str,
    *,
    directory_url: str = BOARD_BOOK_DIRECTORY_URL,
) -> tuple[BoardBookDirectoryEntry, ...]:
    """Parse BoardBook's public, server-rendered organization directory."""

    soup = BeautifulSoup(content, "lxml")
    entries: list[BoardBookDirectoryEntry] = []
    seen_ids: set[str] = set()
    for anchor in soup.select(
        'main#MainPage ul.list-unstyled.list-striped li > a[href^="/Public/Organization/" i]'
    ):
        href = str(anchor.get("href") or "").strip()
        absolute = urljoin(directory_url, href)
        parsed = urlsplit(absolute)
        if (parsed.hostname or "").casefold() != "meetings.boardbook.org":
            continue
        match = _BOARD_BOOK_ORGANIZATION_PATH.fullmatch(parsed.path)
        if not match or parsed.query or parsed.fragment:
            continue
        external_id = match.group("external_id")
        identity = external_id.casefold()
        organization_name = collapse_ws(anchor.get_text(" ", strip=True))
        if not organization_name or identity in seen_ids:
            continue
        seen_ids.add(identity)
        entries.append(
            BoardBookDirectoryEntry(
                external_id=external_id,
                organization_name=organization_name,
                public_url=(
                    f"https://meetings.boardbook.org/Public/Organization/{external_id}"
                ),
            )
        )
    return tuple(entries)


def parse_boardbook_organization_evidence(
    content: bytes | str,
    entry: BoardBookDirectoryEntry,
) -> BoardBookOrganizationEvidence:
    soup = BeautifulSoup(content, "lxml")
    heading = soup.select_one("#DisplayHeader h1")
    organization_name = collapse_ws(heading.get_text(" ", strip=True) if heading else "")
    organization_name = re.sub(
        r"\s+Public View\s*$",
        "",
        organization_name,
        flags=re.IGNORECASE,
    ) or entry.organization_name

    states: set[str] = set()
    cities: set[str] = set()
    for node in soup.select("#PublicMeetingsTable span[id]"):
        if not str(node.get("id") or "").casefold().endswith("-csz"):
            continue
        location = _clean_location_value(node.get_text(" ", strip=True))
        match = _BOARD_BOOK_LOCATION.fullmatch(location)
        if not match:
            continue
        state = normalize_state(match.group("state"))
        city = " ".join(_ascii_words(match.group("city")))
        if state:
            states.add(state)
        if city:
            cities.add(city)

    homepage_urls: list[str] = []
    for anchor in soup.select("a.mainLogoLink[href]"):
        url = urljoin(entry.public_url, str(anchor.get("href") or "").strip())
        host = (urlsplit(url).hostname or "").casefold()
        if host and not host.endswith("boardbook.org") and url not in homepage_urls:
            homepage_urls.append(url)

    return BoardBookOrganizationEvidence(
        entry=entry,
        organization_name=organization_name,
        states=frozenset(states),
        cities=frozenset(cities),
        meeting_count=len(soup.select("#PublicMeetingsTable tr.row-for-board")),
        homepage_urls=tuple(homepage_urls),
    )


@dataclass(slots=True)
class BoardBookDirectoryCatalog:
    entries: tuple[BoardBookDirectoryEntry, ...]
    source_url: str = BOARD_BOOK_DIRECTORY_URL
    _evidence_cache: dict[str, BoardBookOrganizationEvidence] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _cache_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _district_universe: tuple[Mapping[str, Any], ...] = field(
        default=(),
        init=False,
        repr=False,
    )
    _district_by_state_city: dict[
        tuple[str, str], tuple[Mapping[str, Any], ...]
    ] = field(default_factory=dict, init=False, repr=False)

    def configure_district_universe(
        self,
        districts: Sequence[Mapping[str, Any]],
    ) -> None:
        """Set the NCES comparison universe used for reciprocal matching.

        Provider names frequently omit district numbers. A city match is only
        safe when this organization name resolves back to exactly one district
        in the full local NCES dataset, not merely one district in the run.
        """

        self._district_universe = tuple(dict(district) for district in districts)
        by_state_city: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for district in self._district_universe:
            state, cities = district_location_values(district)
            for city in cities:
                by_state_city.setdefault((state, city), []).append(district)
        self._district_by_state_city = {
            key: tuple(rows) for key, rows in by_state_city.items()
        }

    @staticmethod
    def _district_key(district: Mapping[str, Any]) -> tuple[Any, ...]:
        district_id = district.get("id")
        if district_id not in (None, ""):
            return ("id", str(district_id))
        state, cities = district_location_values(district)
        return (
            "identity",
            state,
            normalized_organization_name(district.get("agency_name") or district.get("name")),
            tuple(sorted(cities)),
        )

    def _reciprocal_district_match(
        self,
        district: Mapping[str, Any],
        evidence: BoardBookOrganizationEvidence,
        *,
        minimum_score: float,
    ) -> bool:
        if not self._district_by_state_city:
            return False
        current_key = self._district_key(district)
        plausible: set[tuple[Any, ...]] = set()
        expected_state, _expected_cities = district_location_values(district)
        comparison_rows: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for city in evidence.cities:
            for other in self._district_by_state_city.get((expected_state, city), ()):
                comparison_rows[self._district_key(other)] = other
        for other in comparison_rows.values():
            score = _name_score(
                other.get("agency_name") or other.get("name"),
                evidence.organization_name,
            )
            if score >= minimum_score:
                plausible.add(self._district_key(other))
        return plausible == {current_key}

    @classmethod
    def fetch(cls, client: BoardHTTPClient) -> "BoardBookDirectoryCatalog":
        """Fetch exactly the linked directory document through normal HTTP safeguards."""

        response = client.get(
            BOARD_BOOK_DIRECTORY_URL,
            check_robots=True,
            raise_for_status=False,
        )
        challenge = assess_challenge(
            response.status_code,
            response.content,
            response.final_url,
        )
        if challenge.is_challenge:
            raise BoardHTTPError(
                "BoardBook directory returned an access challenge "
                f"({challenge.marker or challenge.category})."
            )
        response.raise_for_status()
        entries = parse_boardbook_directory(response.content, directory_url=response.final_url)
        if not entries:
            raise BoardHTTPError("BoardBook public directory did not contain organization links.")
        return cls(entries=entries, source_url=response.final_url)

    def candidates_for(
        self,
        district: Mapping[str, Any],
        *,
        limit: int = 5,
        minimum_score: float = 0.78,
    ) -> tuple[BoardBookDirectoryCandidate, ...]:
        name = district.get("agency_name") or district.get("name")
        candidates = [
            BoardBookDirectoryCandidate(entry=entry, name_score=_name_score(name, entry.organization_name))
            for entry in self.entries
        ]
        eligible = [item for item in candidates if item.name_score >= minimum_score]
        eligible.sort(
            key=lambda item: (
                -item.name_score,
                item.entry.organization_name.casefold(),
                item.entry.external_id.casefold(),
            )
        )
        return tuple(eligible[: max(1, int(limit))])

    def organization_evidence(
        self,
        entry: BoardBookDirectoryEntry,
        client: BoardHTTPClient,
    ) -> BoardBookOrganizationEvidence:
        key = entry.external_id.casefold()
        with self._cache_lock:
            cached = self._evidence_cache.get(key)
        if cached is not None:
            return cached

        response = client.get(entry.public_url, check_robots=True, raise_for_status=False)
        challenge = assess_challenge(
            response.status_code,
            response.content,
            response.final_url,
        )
        if challenge.is_challenge:
            raise BoardHTTPError(
                f"BoardBook organization {entry.external_id} returned an access challenge "
                f"({challenge.marker or challenge.category})."
            )
        response.raise_for_status()
        evidence = parse_boardbook_organization_evidence(response.content, entry)
        with self._cache_lock:
            self._evidence_cache.setdefault(key, evidence)
            return self._evidence_cache[key]

    def match_and_verify(
        self,
        district: Mapping[str, Any],
        client: BoardHTTPClient,
        *,
        minimum_score: float = 0.84,
        ambiguity_delta: float = 0.04,
        max_candidates: int = 5,
    ) -> BoardBookDirectoryMatch:
        """Resolve a district only when one competitive name match confirms its state."""

        expected_state, expected_cities = district_location_values(district)
        _place_words, district_identifiers, district_website_host = _district_identity_signals(
            district
        )
        if not expected_state:
            return BoardBookDirectoryMatch(
                status="unconfirmed",
                reason="The district has no two-letter state value for provider corroboration.",
            )

        ranked_candidates = self.candidates_for(
            district,
            limit=max(1, len(self.entries)),
            minimum_score=minimum_score,
        )
        if not ranked_candidates:
            return BoardBookDirectoryMatch(
                status="no_candidate",
                reason="No BoardBook directory name was similar enough to the district.",
            )

        top_score = ranked_candidates[0].name_score
        all_competitive = tuple(
            candidate
            for candidate in ranked_candidates
            if candidate.name_score >= top_score - max(0.0, float(ambiguity_delta))
        )
        if len(all_competitive) > max(1, int(max_candidates)):
            return BoardBookDirectoryMatch(
                status="ambiguous",
                reason="Too many equally competitive BoardBook organization names require manual review.",
                candidates=all_competitive,
            )
        competitive = all_competitive
        evidence_rows: list[BoardBookOrganizationEvidence] = []
        verified: list[BoardBookDirectoryCandidate] = []
        unresolved: list[BoardBookDirectoryCandidate] = []
        errors: list[str] = []

        for candidate in competitive:
            try:
                evidence = self.organization_evidence(candidate.entry, client)
            except Exception as exc:
                unresolved.append(candidate)
                errors.append(f"{candidate.entry.external_id}: {exc}")
                continue
            evidence_rows.append(evidence)
            page_name_score = _name_score(
                district.get("agency_name") or district.get("name"),
                evidence.organization_name,
            )
            evidence_words = set(
                normalized_organization_name(evidence.organization_name).split()
            )
            missing_identifiers = district_identifiers - evidence_words
            city_matches = bool(expected_cities and evidence.cities & expected_cities)
            homepage_matches = _homepage_matches(
                district_website_host,
                evidence.homepage_urls,
            )
            reciprocal_match = self._reciprocal_district_match(
                district,
                evidence,
                minimum_score=minimum_score,
            )
            identity_confirmed = bool(
                homepage_matches
                or (
                    page_name_score == 1.0
                    and not missing_identifiers
                    and bool(district_identifiers)
                )
                or (city_matches and reciprocal_match)
            )
            if not evidence.states or page_name_score < minimum_score:
                unresolved.append(candidate)
                continue
            if evidence.states == {expected_state} and identity_confirmed:
                verified.append(candidate)
            elif expected_state in evidence.states:
                unresolved.append(candidate)

        if len(verified) == 1 and not unresolved:
            selected = verified[0]
            selected_evidence = next(
                row for row in evidence_rows if row.entry.external_id == selected.entry.external_id
            )
            city_note = ""
            if expected_cities and selected_evidence.cities & expected_cities:
                city_note = " District city also matched."
            return BoardBookDirectoryMatch(
                status="verified",
                reason=(
                    f"BoardBook organization state matched {expected_state}." + city_note
                ),
                candidates=competitive,
                verified=selected,
                evidence=tuple(evidence_rows),
                errors=tuple(errors),
            )

        if len(verified) > 1 or unresolved:
            reason = (
                "Multiple competitive BoardBook organizations matched the district and state."
                if len(verified) > 1
                else "A competitive BoardBook organization could not be state-confirmed."
            )
            return BoardBookDirectoryMatch(
                status="ambiguous",
                reason=reason,
                candidates=competitive,
                evidence=tuple(evidence_rows),
                errors=tuple(errors),
            )

        return BoardBookDirectoryMatch(
            status="unconfirmed",
            reason=f"Competitive BoardBook organizations did not corroborate state {expected_state}.",
            candidates=competitive,
            evidence=tuple(evidence_rows),
            errors=tuple(errors),
        )


def load_enabled_boardbook_directory(
    client: BoardHTTPClient,
    *,
    environ: Mapping[str, str] | None = None,
) -> BoardBookDirectoryCatalog | None:
    """Load the per-run catalog only after an explicit operator opt-in."""

    if not provider_directory_enabled(environ):
        return None
    return BoardBookDirectoryCatalog.fetch(client)


__all__ = [
    "BOARD_BOOK_DIRECTORY_URL",
    "BOARD_PROVIDER_DIRECTORY_ENV",
    "BOARD_PROVIDER_DIRECTORY_WARNING",
    "BoardBookDirectoryCandidate",
    "BoardBookDirectoryCatalog",
    "BoardBookDirectoryEntry",
    "BoardBookDirectoryMatch",
    "BoardBookOrganizationEvidence",
    "district_location_values",
    "load_enabled_boardbook_directory",
    "normalized_organization_name",
    "parse_boardbook_directory",
    "parse_boardbook_organization_evidence",
    "provider_directory_enabled",
]
