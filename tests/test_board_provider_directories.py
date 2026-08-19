from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from board.adapters.boardbook import BoardBookAdapter, resolve_boardbook_document_url
from board.discovery import discover_board_source
from board.http import HTTPResult
from board.provider_directories import (
    BOARD_BOOK_DIRECTORY_URL,
    BoardBookDirectoryEntry,
    BoardBookDirectoryCatalog,
    district_location_values,
    load_enabled_boardbook_directory,
    normalized_organization_name,
    parse_boardbook_directory,
    provider_directory_enabled,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class FakeClient:
    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.calls: list[str] = []
        self.closed = False

    def get(self, url: str, **_kwargs) -> HTTPResult:
        self.calls.append(url)
        if url not in self.responses:
            return HTTPResult(url, url, 404, {"Content-Type": "text/html"}, b"not found")
        return HTTPResult(
            url,
            url,
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            self.responses[url],
        )

    def close(self) -> None:
        self.closed = True


class BoardBookProviderDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = fixture_bytes("boardbook_public_directory.html")
        self.entries = parse_boardbook_directory(self.directory)
        self.catalog = BoardBookDirectoryCatalog(self.entries)

    def test_feature_is_disabled_by_default_and_requires_explicit_truthy_value(self):
        self.assertFalse(provider_directory_enabled({}))
        self.assertFalse(provider_directory_enabled({"EDSCANNER_BOARD_PROVIDER_DIRECTORY_ENABLED": "false"}))
        self.assertTrue(provider_directory_enabled({"EDSCANNER_BOARD_PROVIDER_DIRECTORY_ENABLED": "true"}))

        disabled_client = FakeClient({BOARD_BOOK_DIRECTORY_URL: self.directory})
        self.assertIsNone(load_enabled_boardbook_directory(disabled_client, environ={}))
        self.assertEqual(disabled_client.calls, [])

    def test_parser_accepts_numeric_and_safe_alias_ids_only(self):
        self.assertEqual(len(self.entries), 4)
        self.assertEqual(
            [entry.external_id for entry in self.entries],
            ["2413", "915", "portland", "Elgin"],
        )
        self.assertEqual(
            self.entries[0].public_url,
            "https://meetings.boardbook.org/Public/Organization/2413",
        )

    def test_enabled_catalog_fetches_the_full_directory_once(self):
        client = FakeClient({BOARD_BOOK_DIRECTORY_URL: self.directory})
        catalog = load_enabled_boardbook_directory(
            client,
            environ={"EDSCANNER_BOARD_PROVIDER_DIRECTORY_ENABLED": "1"},
        )

        self.assertIsNotNone(catalog)
        self.assertEqual(len(catalog.entries), 4)
        self.assertEqual(client.calls, [BOARD_BOOK_DIRECTORY_URL])

    def test_bend_nces_name_is_state_confirmed_by_organization_page(self):
        bend_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeClient(
            {bend_url: fixture_bytes("boardbook_bend_directory_match.html")}
        )
        district = {
            "id": 1,
            "agency_name": "Bend-Lapine Administrative SD 1",
            "state": "OR",
            "raw_json": json.dumps(
                {
                    "original_headers": {},
                    "cleaned_headers": {
                        "Location City [District] 2024-25": "Bend",
                        "Mailing City [District] 2024-25": "Bend",
                    },
                    "snake_case": {"location_city_district_2024_25": "Bend"},
                }
            ),
        }
        self.assertEqual(
            district_location_values(district),
            ("OR", frozenset({"bend"})),
        )
        self.assertEqual(
            district_location_values(
                {
                    "state": "OR",
                    "raw_json": {
                        "cleaned_headers": {
                            "Location City [District] 2024-25": "Bend"
                        }
                    },
                }
            ),
            ("OR", frozenset({"bend"})),
        )
        self.catalog.configure_district_universe([district])

        result = self.catalog.match_and_verify(district, client)

        self.assertTrue(result.is_verified)
        self.assertEqual(result.verified.entry.external_id, "2413")
        self.assertGreaterEqual(result.verified.name_score, 0.94)
        self.assertIn("District city also matched", result.reason)
        self.assertEqual(client.calls, [bend_url])

        cached = self.catalog.match_and_verify(district, client)
        self.assertTrue(cached.is_verified)
        self.assertEqual(client.calls, [bend_url])

    def test_name_match_with_wrong_state_is_never_verified(self):
        bend_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeClient(
            {bend_url: fixture_bytes("boardbook_bend_directory_match.html")}
        )

        result = self.catalog.match_and_verify(
            {"agency_name": "Bend-La Pine Schools", "state": "WA"},
            client,
        )

        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, "unconfirmed")

    def test_state_and_one_word_name_cannot_hide_a_missing_district_number(self):
        union_url = "https://meetings.boardbook.org/Public/Organization/union"
        catalog = BoardBookDirectoryCatalog(
            (
                BoardBookDirectoryEntry(
                    external_id="union",
                    organization_name="Union Public Schools",
                    public_url=union_url,
                ),
            )
        )
        client = FakeClient(
            {
                union_url: (
                    b'<main id="MainPage"><div id="DisplayHeader">'
                    b'<h1>Union Public Schools Public View</h1></div>'
                    b'<table id="PublicMeetingsTable"><tr class="row-for-board">'
                    b'<td>Meeting</td><td><span id="location-1-csz">Union, OR 97883</span>'
                    b'</td></tr></table></main>'
                )
            }
        )

        for district_number in ("1", "9"):
            with self.subTest(district_number=district_number):
                result = catalog.match_and_verify(
                    {
                        "agency_name": f"Union SD {district_number}",
                        "state": "OR",
                        "raw_json": json.dumps(
                            {"Location City [District] 2024-25": "Union"}
                        ),
                    },
                    client,
                )
                self.assertFalse(result.is_verified)
                self.assertEqual(result.status, "ambiguous")

    def test_city_match_is_not_enough_when_two_nces_districts_are_plausible(self):
        source_url = "https://meetings.boardbook.org/Public/Organization/south-holland"
        catalog = BoardBookDirectoryCatalog(
            (
                BoardBookDirectoryEntry(
                    external_id="south-holland",
                    organization_name="South Holland Public Schools",
                    public_url=source_url,
                ),
            )
        )
        districts = [
            {
                "id": district_id,
                "agency_name": f"South Holland SD {number}",
                "state": "IL",
                "raw_json": json.dumps(
                    {
                        "cleaned_headers": {
                            "Location City [District] 2024-25": "South Holland"
                        }
                    }
                ),
            }
            for district_id, number in ((150, "150"), (151, "151"))
        ]
        catalog.configure_district_universe(districts)
        client = FakeClient(
            {
                source_url: (
                    b'<div id="DisplayHeader"><h1>South Holland Public Schools Public View</h1></div>'
                    b'<table id="PublicMeetingsTable"><tr class="row-for-board"><td></td><td>'
                    b'<span id="location-csz">South Holland, IL 60473</span>'
                    b'</td></tr></table>'
                )
            }
        )

        for district in districts:
            with self.subTest(district=district["agency_name"]):
                result = catalog.match_and_verify(district, client)
                self.assertFalse(result.is_verified)
                self.assertEqual(result.status, "ambiguous")

    def test_exact_duplicate_with_one_unconfirmed_page_remains_ambiguous(self):
        numeric_url = "https://meetings.boardbook.org/Public/Organization/915"
        alias_url = "https://meetings.boardbook.org/Public/Organization/portland"
        client = FakeClient(
            {
                numeric_url: fixture_bytes("boardbook_portland_or_directory_match.html"),
                alias_url: fixture_bytes("boardbook_empty_portland_directory_match.html"),
            }
        )

        result = self.catalog.match_and_verify(
            {"agency_name": "Portland Public Schools", "state": "OR"},
            client,
        )

        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({item.entry.external_id for item in result.candidates}, {"915", "portland"})
        self.assertEqual(set(client.calls), {numeric_url, alias_url})

    def test_normalizer_handles_district_abbreviations_and_compound_spacing(self):
        self.assertEqual(
            normalized_organization_name("Bend-Lapine Administrative SD 1"),
            "bend lapine 1",
        )
        self.assertEqual(normalized_organization_name("Bend-La Pine Schools"), "bend la pine")

    def test_verified_directory_match_uses_normal_adapter_before_district_crawl(self):
        bend_url = "https://meetings.boardbook.org/Public/Organization/2413"
        district_url = "https://district.example/"
        client = FakeClient(
            {bend_url: fixture_bytes("boardbook_bend_directory_match.html")}
        )

        district = {
            "id": 1,
            "agency_name": "Bend-Lapine Administrative SD 1",
            "state": "OR",
            "website": district_url,
            "website_normalized": district_url,
            "raw_json": json.dumps(
                {"cleaned_headers": {"Location City [District] 2024-25": "Bend"}}
            ),
        }
        self.catalog.configure_district_universe([district])
        outcome = discover_board_source(
            district,
            client=client,
            provider_directory=self.catalog,
            allow_browser_fallback=False,
        )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(outcome.platform, "boardbook")
        self.assertEqual(outcome.source_url, bend_url)
        self.assertNotIn(district_url, client.calls)
        provider = outcome.raw["provider_directory"]
        self.assertEqual(provider["catalog_url"], BOARD_BOOK_DIRECTORY_URL)
        self.assertEqual(provider["verified_external_id"], "2413")
        self.assertEqual(provider["state_evidence"][0]["states"], ["OR"])

    def test_verified_directory_can_discover_district_without_a_website(self):
        bend_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeClient(
            {bend_url: fixture_bytes("boardbook_bend_directory_match.html")}
        )

        district = {
            "id": 3,
            "agency_name": "Bend-Lapine Administrative SD 1",
            "state": "OR",
            "website": "",
            "website_normalized": "",
            "raw_json": json.dumps(
                {"cleaned_headers": {"Location City [District] 2024-25": "Bend"}}
            ),
        }
        self.catalog.configure_district_universe([district])
        outcome = discover_board_source(
            district,
            client=client,
            provider_directory=self.catalog,
            allow_browser_fallback=False,
        )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(outcome.source_url, bend_url)

    def test_owned_client_is_closed_after_early_verified_directory_return(self):
        bend_url = "https://meetings.boardbook.org/Public/Organization/2413"
        client = FakeClient(
            {bend_url: fixture_bytes("boardbook_bend_directory_match.html")}
        )
        district = {
            "id": 4,
            "agency_name": "Bend-Lapine Administrative SD 1",
            "state": "OR",
            "website": "https://district.example/",
            "website_normalized": "https://district.example/",
            "raw_json": json.dumps(
                {"cleaned_headers": {"Location City [District] 2024-25": "Bend"}}
            ),
        }
        self.catalog.configure_district_universe([district])
        with patch("board.discovery.BoardHTTPClient", return_value=client):
            outcome = discover_board_source(
                district,
                provider_directory=self.catalog,
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "working")
        self.assertTrue(client.closed)

    def test_provider_match_error_is_diagnostic_and_site_crawl_continues(self):
        district_url = "https://district.example"
        client = FakeClient(
            {district_url: b"<html><title>District home</title><body>No board link</body></html>"}
        )
        failing_catalog = Mock(source_url=BOARD_BOOK_DIRECTORY_URL)
        failing_catalog.match_and_verify.side_effect = RuntimeError("fixture catalog error")

        outcome = discover_board_source(
            {
                "id": 5,
                "agency_name": "Example School District",
                "state": "OR",
                "website": district_url,
                "website_normalized": district_url,
            },
            client=client,
            provider_directory=failing_catalog,
            allow_browser_fallback=False,
            max_pages=1,
        )

        self.assertEqual(outcome.status, "not_found")
        self.assertEqual(outcome.raw["provider_directory"]["status"], "error")
        self.assertIn("fixture catalog error", outcome.raw["provider_directory"]["errors"][0])

    def test_cancellation_is_checked_before_provider_matching(self):
        client = FakeClient({})
        catalog = Mock(source_url=BOARD_BOOK_DIRECTORY_URL)

        outcome = discover_board_source(
            {
                "id": 6,
                "agency_name": "Example School District",
                "state": "OR",
                "website": "https://district.example/",
                "website_normalized": "https://district.example/",
            },
            client=client,
            provider_directory=catalog,
            cancel_requested=lambda: True,
            allow_browser_fallback=False,
        )

        self.assertEqual(outcome.status, "cancelled")
        catalog.match_and_verify.assert_not_called()

    def test_ambiguous_directory_result_does_not_seed_source_and_uses_site_fallback(self):
        numeric_url = "https://meetings.boardbook.org/Public/Organization/915"
        alias_url = "https://meetings.boardbook.org/Public/Organization/portland"
        district_url = "https://portland.example"
        client = FakeClient(
            {
                numeric_url: fixture_bytes("boardbook_portland_or_directory_match.html"),
                alias_url: fixture_bytes("boardbook_empty_portland_directory_match.html"),
                district_url: b"<html><title>District home</title><body>No board link</body></html>",
            }
        )

        outcome = discover_board_source(
            {
                "id": 2,
                "agency_name": "Portland Public Schools",
                "state": "OR",
                "website": district_url,
                "website_normalized": district_url,
            },
            client=client,
            provider_directory=self.catalog,
            allow_browser_fallback=False,
            max_pages=1,
        )

        self.assertEqual(outcome.status, "not_found")
        self.assertTrue(any(url.rstrip("/") == district_url.rstrip("/") for url in client.calls))
        self.assertEqual(outcome.raw["provider_directory"]["status"], "ambiguous")
        self.assertIsNone(outcome.raw["provider_directory"]["verified_external_id"])


class BoardBookAliasIdentifierTests(unittest.TestCase):
    def test_adapter_detects_and_preserves_alias_organization_id(self):
        url = "https://meetings.boardbook.org/Public/Organization/Elgin"
        content = fixture_bytes("boardbook_elgin_alias.html")
        detection = BoardBookAdapter().detect(url, content)

        self.assertTrue(detection.matched)
        self.assertEqual(detection.metadata["external_source_id"], "Elgin")
        self.assertEqual(detection.canonical_url, url)

        meetings = BoardBookAdapter().parse_meeting_list(content, url)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].external_meeting_id, "2")
        self.assertEqual(
            meetings[0].agenda_url,
            "https://meetings.boardbook.org/Public/Agenda/Elgin?meeting=2",
        )

    def test_alias_id_resolves_direct_document_url(self):
        self.assertEqual(
            resolve_boardbook_document_url(
                "https://meetings.boardbook.org/Documents/FileViewerOrPublic/Elgin?file=2381887"
            ),
            "https://meetings.boardbook.org/Documents/DownloadPDF/2381887?org=Elgin",
        )


if __name__ == "__main__":
    unittest.main()
