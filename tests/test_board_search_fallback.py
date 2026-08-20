from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from board.adapters import build_adapters
from board.discovery import discover_board_source
from board.http import BoardHTTPSettings, HTTPResult
from board.provider_directories import BoardBookDirectoryCatalog
from board.search_fallback import search_known_board_sources


BOARD_BOOK_BEND = b"""
<!doctype html>
<html><body>
  <div id="DisplayHeader"><h1>Bend-La Pine Schools Public View</h1></div>
  <table id="PublicMeetingsTable">
    <tr class="row-for-board">
      <td>August 18, 2026 - Regular School Board Meeting</td>
      <td><span id="location-1-csz">Bend, OR 97703</span></td>
    </tr>
  </table>
</body></html>
"""


class SearchClient:
    def __init__(self, *, state_html: bytes = BOARD_BOOK_BEND) -> None:
        self.settings = BoardHTTPSettings(
            delay_seconds=0,
            respect_robots=False,
        )
        self.state_html = state_html
        self.search_calls = 0
        self.last_search_url = ""

    def validate_target_url(self, url: str) -> str:
        if not str(url).startswith("https://"):
            raise ValueError("HTTPS required")
        return str(url)

    def can_fetch(self, _url: str) -> bool:
        return True

    @contextmanager
    def host_slot(self, _url: str):
        yield

    def get_json(self, _url: str, **_kwargs):
        self.search_calls += 1
        self.last_search_url = _url
        payload = {
            "web": {
                "results": [
                    {
                        "title": "Bend-La Pine Schools Public View - BoardBook Premier",
                        "url": "https://meetings.boardbook.org/public/Organization/2413",
                        "description": "School board agendas and minutes",
                    },
                    {
                        "title": "Salem-Keizer Public Schools BoardBook",
                        "url": "https://meetings.boardbook.org/Public/Organization/9999",
                        "description": "Board meetings",
                    },
                    {
                        "title": "Bend-La Pine school board news",
                        "url": "https://example.org/article",
                        "description": "One news item",
                    },
                ]
            }
        }
        result = HTTPResult(
            requested_url="https://api.search.brave.com/res/v1/web/search",
            final_url="https://api.search.brave.com/res/v1/web/search",
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        return result, payload

    def get(self, url: str, **_kwargs) -> HTTPResult:
        return HTTPResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers={"Content-Type": "text/html"},
            content=self.state_html,
        )

    def close(self) -> None:
        return None


class BoardSearchFallbackTests(unittest.TestCase):
    def district(self) -> dict[str, object]:
        return {
            "id": 1,
            "agency_name": "BEND-LA PINE ADMINISTRATIVE SD 1",
            "state": "OR",
            "website": "",
            "website_normalized": "",
            "raw_json": {
                "cleaned_headers": {"Location City": "Bend"}
            },
        }

    def identity_catalog(self, *districts):
        catalog = BoardBookDirectoryCatalog(entries=())
        catalog.configure_district_universe(list(districts))
        return catalog

    def test_search_returns_only_name_matched_known_provider_urls(self):
        client = SearchClient()
        adapters = build_adapters(client, allow_browser_fallback=False)
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            results, diagnostics = search_known_board_sources(
                self.district(),
                client,
                adapters,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].platform, "boardbook")
        self.assertEqual(
            results[0].canonical_source_url,
            "https://meetings.boardbook.org/Public/Organization/2413",
        )
        self.assertEqual(diagnostics["known_provider_candidates"], 1)
        query = parse_qs(urlsplit(client.last_search_url).query)["q"][0]
        self.assertEqual(
            query,
            (
                "bend la pine Oregon "
                '("board meetings" OR BoardBook OR BoardDocs OR Diligent '
                "OR Simbli OR CivicClerk)"
            ),
        )
        self.assertNotIn('"bend la pine"', query.casefold())
        self.assertNotIn("administrative", query.casefold())
        self.assertNotIn('"Oregon"', query)
        for provider in (
            "BoardBook",
            "BoardDocs",
            "Diligent",
            "Simbli",
            "CivicClerk",
        ):
            self.assertIn(provider, query)
        self.assertEqual(diagnostics["query"], query)

    def test_search_without_a_configured_key_makes_no_external_request(self):
        client = SearchClient()
        adapters = build_adapters(client, allow_browser_fallback=False)
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="",
        ):
            results, diagnostics = search_known_board_sources(
                self.district(),
                client,
                adapters,
            )

        self.assertEqual(results, [])
        self.assertEqual(client.search_calls, 0)
        self.assertFalse(diagnostics["available"])
        self.assertEqual(diagnostics["status"], "unavailable")

    def test_malformed_search_results_fail_closed_without_breaking_discovery(self):
        client = SearchClient()

        def malformed_response(_url: str, **_kwargs):
            response = HTTPResult(
                requested_url="https://api.search.brave.com/res/v1/web/search",
                final_url="https://api.search.brave.com/res/v1/web/search",
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"{}",
            )
            return response, {"web": {"results": {"unexpected": "object"}}}

        client.get_json = malformed_response
        adapters = build_adapters(client, allow_browser_fallback=False)
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            results, diagnostics = search_known_board_sources(
                self.district(),
                client,
                adapters,
            )

        self.assertEqual(results, [])
        self.assertEqual(diagnostics["status"], "invalid_response")

    def test_discovery_activates_search_result_only_with_name_and_state_evidence(self):
        client = SearchClient()
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            outcome = discover_board_source(
                self.district(),
                client=client,
                search_fallback=True,
                identity_catalog=self.identity_catalog(self.district()),
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(outcome.platform, "boardbook")
        self.assertEqual(outcome.organization_external_id, "2413")
        self.assertEqual(
            outcome.source_url,
            "https://meetings.boardbook.org/Public/Organization/2413",
        )
        self.assertTrue(
            outcome.raw["search_identity_verification"]["verified"]
        )
        self.assertEqual(outcome.raw["search_fallback"]["status"], "completed")

    def test_mismatched_provider_state_remains_manual_review(self):
        client = SearchClient(
            state_html=BOARD_BOOK_BEND.replace(b"Bend, OR", b"Bend, WA")
        )
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            outcome = discover_board_source(
                self.district(),
                client=client,
                search_fallback=True,
                identity_catalog=self.identity_catalog(self.district()),
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "manual_review")
        self.assertFalse(
            outcome.raw["search_identity_verification"]["verified"]
        )
        self.assertIn("state evidence", outcome.error_message)

    def test_search_title_cannot_override_mismatched_provider_organization(self):
        client = SearchClient(
            state_html=BOARD_BOOK_BEND.replace(
                b"Bend-La Pine Schools Public View",
                b"Salem-Keizer Public Schools Public View",
            )
        )
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            outcome = discover_board_source(
                self.district(),
                client=client,
                search_fallback=True,
                identity_catalog=self.identity_catalog(self.district()),
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "manual_review")
        self.assertFalse(
            outcome.raw["search_identity_verification"]["verified"]
        )
        self.assertIn("organization name", outcome.error_message)

    def test_same_city_name_collision_requires_manual_confirmation(self):
        client = SearchClient()
        other = {
            **self.district(),
            "id": 2,
            "agency_name": "BEND-LA PINE ADMINISTRATIVE SD 2",
        }
        with patch(
            "board.search_fallback.get_local_setting",
            return_value="fixture-key",
        ):
            outcome = discover_board_source(
                self.district(),
                client=client,
                search_fallback=True,
                identity_catalog=self.identity_catalog(self.district(), other),
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "manual_review")
        self.assertIn("Manual confirmation", outcome.error_message)


if __name__ == "__main__":
    unittest.main()
