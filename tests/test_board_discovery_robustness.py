from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import requests

from board.adapters.base import RenderedPage, RenderedPageRejected
from board.adapters.generic import GenericBoardAdapter
from board.discovery import discover_board_source, extract_board_candidates
from board.http import HTTPResult, InvalidPublicURL, RedirectDenied
from board.models import BoardSource, BoardSourceResult, DetectionResult
from search_engine import canonical_url


BOARD_BOOK_URL = "https://meetings.boardbook.org/Public/Organization/1573"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board"


def html_result(requested_url: str, content: bytes, *, final_url: str | None = None,
                status_code: int = 200, redirect_chain: tuple[str, ...] = (),
                website_migration_accepted: bool = False) -> HTTPResult:
    return HTTPResult(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        status_code=status_code,
        headers={"Content-Type": "text/html"},
        content=content,
        redirect_chain=redirect_chain,
        website_migration_accepted=website_migration_accepted,
    )


class MappingClient:
    def __init__(
        self,
        responses: dict[str, HTTPResult | BaseException],
        *,
        allow_host_variants: bool = False,
    ):
        self.responses = {canonical_url(key): value for key, value in responses.items()}
        self.calls: list[str] = []
        self.allow_host_variants = allow_host_variants

    def validate_target_url(self, url: str) -> str:
        return canonical_url(url)

    def validated_connection_target(self, url: str) -> tuple[str, tuple[str, ...]]:
        if not self.allow_host_variants:
            raise ValueError("fixture host variant is unavailable")
        return canonical_url(url), ("203.0.113.10",)

    def get(self, url: str, **_kwargs) -> HTTPResult:
        key = canonical_url(url)
        self.calls.append(key)
        value = self.responses[key]
        if isinstance(value, BaseException):
            raise value
        return value


class WorkingBoardBookAdapter:
    platform_name = "boardbook"

    def __init__(self) -> None:
        self.discover_calls: list[str] = []

    def detect(self, url: str, _html=None) -> DetectionResult:
        matched = canonical_url(url).casefold() == BOARD_BOOK_URL.casefold()
        return DetectionResult(
            matched=matched,
            platform=self.platform_name,
            confidence=0.99 if matched else 0.0,
            canonical_url=BOARD_BOOK_URL if matched else None,
            metadata={"external_source_id": "1573"} if matched else {},
        )

    def discover_source(self, district, candidate_url: str, html=None) -> BoardSourceResult:
        del html
        self.discover_calls.append(canonical_url(candidate_url))
        source = BoardSource(
            platform="boardbook",
            public_url=BOARD_BOOK_URL,
            external_source_id="1573",
            district_id=int(district["id"]),
            organization_name=str(district["agency_name"]),
            status="working",
        )
        return BoardSourceResult(
            detection=self.detect(BOARD_BOOK_URL),
            source=source,
            status="working",
            candidate_url=candidate_url,
        )


class GenericFixtureAdapter:
    platform_name = "generic"

    def __init__(
        self,
        *,
        rendered_page: RenderedPage | None = None,
        render_error: BaseException | None = None,
    ) -> None:
        self.rendered_page = rendered_page
        self.render_error = render_error
        self.render_calls: list[str] = []
        self.discover_calls: list[str] = []

    def detect(self, url: str, _html=None) -> DetectionResult:
        return DetectionResult(False, "generic", 0.0, canonical_url=url)

    def render_page_with_metadata(self, url: str) -> RenderedPage:
        self.render_calls.append(canonical_url(url))
        if self.render_error is not None:
            raise self.render_error
        if self.rendered_page is None:
            raise AssertionError("Browser rendering was not expected")
        return self.rendered_page

    def discover_source(self, district, candidate_url: str, html=None) -> BoardSourceResult:
        del html
        candidate_url = canonical_url(candidate_url)
        self.discover_calls.append(candidate_url)
        working = candidate_url.endswith("meetings-live")
        source = (
            BoardSource(
                platform="generic",
                public_url=candidate_url,
                external_source_id="district.example",
                district_id=int(district["id"]),
                organization_name=str(district["agency_name"]),
                status="working",
            )
            if working
            else None
        )
        return BoardSourceResult(
            detection=DetectionResult(
                working,
                "generic",
                0.78 if working else 0.0,
                canonical_url=candidate_url if working else None,
            ),
            source=source,
            status="working" if working else "manual_review",
            candidate_url=candidate_url,
            error="Fixture candidate was not verified." if not working else "",
        )


def district(url: str = "https://district.example") -> dict[str, object]:
    return {
        "id": 9,
        "agency_name": "Example School District",
        "website": url,
        "website_normalized": url,
    }


class BoardDiscoveryRobustnessTests(unittest.TestCase):
    def test_html_base_href_resolves_glide_navigation_without_nested_false_paths(self):
        page_url = (
            "https://www.glide.k12.or.us/Board/Agendas--Minutes/index.html"
        )
        content = (FIXTURE_DIR / "glide_base_href_agenda_hub.html").read_bytes()

        candidates = extract_board_candidates(
            content,
            page_url,
            "https://www.glide.k12.or.us",
            adapters=[],
        )
        urls = {candidate.url for candidate in candidates}

        self.assertIn(page_url, urls)
        self.assertNotIn(
            "https://www.glide.k12.or.us/Board/Agendas--Minutes/"
            "About-Us/Public-Records/index.html",
            urls,
        )
        self.assertFalse(
            any(
                "/Board/Agendas--Minutes/Board/" in candidate.url
                for candidate in candidates
            )
        )
        self.assertNotIn(
            "https://drive.google.com/open?id=public-folder",
            urls,
        )

        adapter = GenericBoardAdapter()
        self.assertEqual(adapter.parse_meeting_list(content, page_url), [])
        self.assertEqual(
            adapter.parse_source(content, page_url).status,
            "manual_review",
        )

    def test_html_base_href_cannot_leave_or_downgrade_district_page(self):
        page_url = "https://district.example/nested/board.html"
        for base_href in (
            "https://unrelated.example/",
            "http://district.example/",
            "https://user:secret@district.example/",
        ):
            with self.subTest(base_href=base_href):
                candidates = extract_board_candidates(
                    (
                        f'<base href="{base_href}">'
                        '<a href="Board/Agendas/index.html">Board Agendas</a>'
                    ).encode(),
                    page_url,
                    "https://district.example",
                    adapters=[],
                )

                self.assertEqual(
                    [candidate.url for candidate in candidates],
                    [
                        "https://district.example/nested/Board/Agendas/"
                        "index.html"
                    ],
                )

    def test_glide_agenda_path_outranks_admin_pages_under_board_prefix(self):
        base = "https://www.glide.k12.or.us"
        junk = "".join(
            f'<a href="/Board/About-Us/Handbooks/page-{index}.html">'
            f"Handbook page {index}</a>"
            for index in range(69)
        )
        content = (
            junk
            + '<a href="/Board/A-Z">A-Z Site Map</a>'
            + '<a href="/Board/About-Us/Public-Records/index.html">Public Records</a>'
            + '<a href="/Board/Agendas--Minutes/index.html">Agendas &amp; Minutes</a>'
        ).encode()

        candidates = extract_board_candidates(content, base, base, adapters=[])

        self.assertEqual(
            candidates[0].url,
            f"{base}/Board/Agendas--Minutes/index.html",
        )
        self.assertIn("board agendas", candidates[0].evidence)
        self.assertNotIn(f"{base}/Board/A-Z", [item.url for item in candidates])

    def test_known_404_candidate_is_not_refetched_during_validation(self):
        homepage = "https://district.example"
        dead = f"{homepage}/school-board/meetings-dead"
        live = f"{homepage}/school-board/meetings-live"
        client = MappingClient(
            {
                homepage: html_result(
                    homepage,
                    (
                        f'<a href="{dead}">School Board Meetings</a>'
                        f'<a href="{live}">School Board Meetings</a>'
                    ).encode(),
                ),
                dead: html_result(dead, b"missing", status_code=404),
                live: html_result(
                    live,
                    b"<h1>School Board Meetings</h1><p>September 8, 2026</p>",
                ),
            },
        )
        adapter = GenericFixtureAdapter()

        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=3,
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls.count(dead), 1)
        self.assertNotIn(dead, adapter.discover_calls)
        self.assertEqual(adapter.discover_calls, [live])
        self.assertEqual(outcome.raw["skipped_http_candidates"], [
            {"url": dead, "status_code": 404}
        ])

    def test_empty_static_homepage_gets_one_bounded_browser_render(self):
        homepage = "https://district.example"
        moved_homepage = "https://moved-district.example"
        client = MappingClient(
            {homepage: html_result(homepage, b'<html><div id="app"></div></html>')}
        )
        generic = GenericFixtureAdapter(
            rendered_page=RenderedPage(
                content=(
                    f'<a href="{BOARD_BOOK_URL}">BoardBook agendas</a>'
                ).encode(),
                final_url=moved_homepage,
                status_code=200,
                browser_rendered=True,
                redirect_chain=(moved_homepage,),
                website_migration_accepted=True,
                website_migration_evidence=(
                    "browser_observed_unattributed_initial_website_move"
                ),
            )
        )
        boardbook = WorkingBoardBookAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(district(homepage), client=client, max_pages=1)

        self.assertEqual(outcome.status, "working")
        self.assertEqual(generic.render_calls, [homepage])
        self.assertEqual(boardbook.discover_calls, [BOARD_BOOK_URL])
        self.assertEqual(
            outcome.raw["initial_render_recoveries"][0]["candidate_count"], 1
        )
        self.assertEqual(
            outcome.raw["website_migration"]["evidence"],
            "initial_configured_website_browser_empty_homepage_render",
        )
        self.assertEqual(
            outcome.raw["website_migration"]["browser_navigation_evidence"],
            "browser_observed_unattributed_initial_website_move",
        )

    def test_transport_browser_move_rejected_at_404_recovers_moved_origin_once(self):
        homepage = "https://district.example"
        stale_path = "https://moved.example/old-board-path"
        moved_origin = "https://moved.example"
        rejected_page = RenderedPage(
            content=b"<h1>Not Found</h1>",
            final_url=stale_path,
            status_code=404,
            browser_rendered=True,
            redirect_chain=(stale_path,),
            website_migration_accepted=True,
        )
        client = MappingClient(
            {
                homepage: requests.exceptions.ConnectionError(
                    "fixture transport failure"
                ),
                moved_origin: html_result(
                    moved_origin,
                    f'<a href="{BOARD_BOOK_URL}">Board meetings</a>'.encode(),
                ),
            }
        )
        generic = GenericFixtureAdapter(
            render_error=RenderedPageRejected(
                "Browser received HTTP 404 for moved path.",
                page=rejected_page,
            )
        )
        boardbook = WorkingBoardBookAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=2
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls, [homepage, moved_origin])
        self.assertEqual(
            outcome.raw["website_migration"]["evidence"],
            "initial_configured_website_browser_transport_recovery",
        )
        self.assertEqual(
            outcome.raw["website_migration"]["stale_path_url"], stale_path
        )
        queued = [
            item
            for item in outcome.raw["website_recovery_attempts"]
            if item.get("kind") == "moved_origin_after_404"
            and item.get("status") == "queued"
        ]
        self.assertEqual(len(queued), 1)

    def test_empty_homepage_browser_move_rejected_at_404_recovers_origin_once(self):
        homepage = "https://district.example"
        stale_path = "https://moved.example/retired-homepage"
        moved_origin = "https://moved.example"
        rejected_page = RenderedPage(
            content=b"<h1>Not Found</h1>",
            final_url=stale_path,
            status_code=404,
            browser_rendered=True,
            redirect_chain=(stale_path,),
            website_migration_accepted=True,
        )
        client = MappingClient(
            {
                homepage: html_result(
                    homepage, b'<html><div id="app"></div></html>'
                ),
                moved_origin: html_result(
                    moved_origin,
                    f'<a href="{BOARD_BOOK_URL}">Board meetings</a>'.encode(),
                ),
            }
        )
        generic = GenericFixtureAdapter(
            render_error=RenderedPageRejected(
                "Browser received HTTP 404 for moved homepage.",
                page=rejected_page,
            )
        )
        boardbook = WorkingBoardBookAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=2
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls, [homepage, moved_origin])
        self.assertEqual(
            outcome.raw["website_migration"]["evidence"],
            "initial_configured_website_browser_empty_homepage_render",
        )
        queued = [
            item
            for item in outcome.raw["website_recovery_attempts"]
            if item.get("kind") == "moved_origin_after_404"
            and item.get("status") == "queued"
        ]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["trigger"], "browser_empty_homepage")

    def test_challenge_browser_move_rejected_at_404_uses_challenge_evidence(self):
        homepage = "https://district.example"
        stale_path = "https://moved.example/stale"
        moved_origin = "https://moved.example"
        rejected_page = RenderedPage(
            content=b"<h1>Not Found</h1>",
            final_url=stale_path,
            status_code=404,
            browser_rendered=True,
            redirect_chain=(stale_path,),
            website_migration_accepted=True,
        )
        client = MappingClient(
            {
                homepage: html_result(homepage, b"Access denied", status_code=403),
                moved_origin: html_result(
                    moved_origin,
                    f'<a href="{BOARD_BOOK_URL}">Board meetings</a>'.encode(),
                ),
            }
        )
        generic = GenericFixtureAdapter(
            render_error=RenderedPageRejected(
                "Browser received HTTP 404 for moved path.",
                page=rejected_page,
            )
        )
        boardbook = WorkingBoardBookAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=2
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls, [homepage, moved_origin])
        self.assertEqual(
            outcome.raw["website_migration"]["evidence"],
            "initial_configured_website_browser_challenge_recovery",
        )

    def test_rejected_non_404_browser_move_is_not_recorded_or_retried(self):
        homepage = "https://district.example"
        rejected_page = RenderedPage(
            content=b"<h1>Server Error</h1>",
            final_url="https://moved.example/server-error",
            status_code=500,
            browser_rendered=True,
            redirect_chain=("https://moved.example/server-error",),
            website_migration_accepted=True,
        )
        client = MappingClient(
            {
                homepage: requests.exceptions.ConnectionError(
                    "fixture transport failure"
                )
            }
        )
        generic = GenericFixtureAdapter(
            render_error=RenderedPageRejected(
                "Browser received HTTP 500.",
                page=rejected_page,
            )
        )

        with patch("board.discovery.build_adapters", return_value=[generic]):
            outcome = discover_board_source(district(homepage), client=client)

        self.assertEqual(outcome.status, "error")
        self.assertEqual(client.calls, [homepage])
        self.assertNotIn("website_migration", outcome.raw)
        self.assertFalse(
            any(
                item.get("kind") == "moved_origin_after_404"
                for item in outcome.raw["website_recovery_attempts"]
            )
        )

    def test_moved_path_404_retries_only_the_moved_https_origin(self):
        old = "https://old.example/Prairie-City"
        moved = "https://new.example/Prairie-City"
        moved_origin = "https://new.example"
        client = MappingClient(
            {
                old: html_result(
                    old,
                    b"missing",
                    final_url=moved,
                    status_code=404,
                    redirect_chain=(moved,),
                    website_migration_accepted=True,
                ),
                moved_origin: html_result(
                    moved_origin,
                    f'<a href="{BOARD_BOOK_URL}">Board meetings</a>'.encode(),
                ),
            }
        )
        boardbook = WorkingBoardBookAdapter()
        generic = GenericFixtureAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(old), client=client, max_pages=2,
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls, [old, moved_origin])
        self.assertTrue(outcome.raw["website_migration"]["moved_origin_recovery"])
        self.assertEqual(
            outcome.raw["website_migration"]["stale_path_url"], moved
        )
        self.assertEqual(
            outcome.raw["website_migration"]["final_url"], moved_origin
        )
        self.assertEqual(
            outcome.raw["website_migration"]["canonical_website_url"],
            moved_origin,
        )

    def test_wrapper_redirect_to_canonical_provider_is_validated_once(self):
        homepage = "https://district.example"
        wrapper = f"{homepage}/board-portal"
        client = MappingClient(
            {
                homepage: html_result(
                    homepage,
                    f'<a href="{wrapper}">School Board Meetings</a>'.encode(),
                ),
                wrapper: RedirectDenied(
                    "Redirect left the allowed organization/vendor boundary: "
                    f"{wrapper} -> {BOARD_BOOK_URL}"
                ),
            }
        )
        boardbook = WorkingBoardBookAdapter()
        generic = GenericFixtureAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=2,
                allow_browser_fallback=False,
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(boardbook.discover_calls, [BOARD_BOOK_URL])
        self.assertEqual(
            outcome.raw["provider_wrapper_redirects"][0]["target_url"],
            BOARD_BOOK_URL,
        )

    def test_wrapper_redirect_to_arbitrary_external_host_stays_denied(self):
        homepage = "https://district.example"
        wrapper = f"{homepage}/board-portal"
        external = "https://unrelated.example/meetings"
        client = MappingClient(
            {
                homepage: html_result(
                    homepage,
                    f'<a href="{wrapper}">School Board Meetings</a>'.encode(),
                ),
                wrapper: RedirectDenied(
                    "Redirect left the allowed organization/vendor boundary: "
                    f"{wrapper} -> {external}"
                ),
            }
        )
        generic = GenericFixtureAdapter()

        with patch("board.discovery.build_adapters", return_value=[generic]):
            outcome = discover_board_source(
                district(homepage), client=client, max_pages=2,
                allow_browser_fallback=False,
            )

        self.assertNotEqual(outcome.status, "working")
        self.assertNotIn(external, client.calls)
        self.assertNotIn("provider_wrapper_redirects", outcome.raw)

    def test_configured_apex_transport_failure_recovers_through_www_once(self):
        apex = "https://district6.org"
        www = "https://www.district6.org"
        client = MappingClient(
            {
                apex: requests.exceptions.SSLError("fixture apex TLS EOF"),
                www: html_result(
                    www,
                    f'<a href="{BOARD_BOOK_URL}">Board meetings</a>'.encode(),
                ),
            },
            allow_host_variants=True,
        )
        boardbook = WorkingBoardBookAdapter()
        generic = GenericFixtureAdapter()

        with patch(
            "board.discovery.build_adapters", return_value=[boardbook, generic]
        ):
            outcome = discover_board_source(
                district(apex), client=client, max_pages=2,
                allow_browser_fallback=True,
            )

        self.assertEqual(outcome.status, "working")
        self.assertEqual(client.calls, [apex, www])
        self.assertEqual(generic.render_calls, [])
        self.assertEqual(
            outcome.raw["website_migration"]["evidence"],
            "initial_configured_website_host_variant",
        )
        self.assertEqual(
            outcome.raw["website_migration"]["canonical_website_url"], www
        )
        self.assertEqual(
            outcome.raw["website_migration"]["redirect_chain"], [www]
        )

    def test_http_website_move_cannot_bootstrap_a_second_browser_move(self):
        original = "https://old-district.example"
        moved = "https://new-district.example"
        unrelated = "https://third-district.example"
        client = MappingClient(
            {
                original: html_result(
                    original,
                    b"<html><title>Access denied</title></html>",
                    final_url=moved,
                    status_code=403,
                    redirect_chain=(moved,),
                    website_migration_accepted=True,
                )
            }
        )

        class BrowserMoveProbe(GenericFixtureAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.district_render_calls: list[str] = []

            def render_district_website_with_metadata(self, url: str) -> RenderedPage:
                self.district_render_calls.append(canonical_url(url))
                return RenderedPage(
                    content=b"<h1>Third-party page</h1>",
                    final_url=unrelated,
                    status_code=200,
                    browser_rendered=True,
                    redirect_chain=(unrelated,),
                    website_migration_accepted=True,
                )

            def render_page_with_metadata(self, url: str) -> RenderedPage:
                self.render_calls.append(canonical_url(url))
                raise InvalidPublicURL(
                    "ordinary browser policy rejected the second host move"
                )

        adapter = BrowserMoveProbe()
        with patch("board.discovery.build_adapters", return_value=[adapter]):
            outcome = discover_board_source(
                district(original),
                client=client,
                max_pages=1,
                allow_browser_fallback=True,
            )

        self.assertEqual(adapter.district_render_calls, [])
        self.assertEqual(adapter.render_calls, [moved])
        self.assertEqual(
            outcome.raw["website_migration"]["original_url"], original
        )
        self.assertEqual(outcome.raw["website_migration"]["final_url"], moved)
        self.assertNotEqual(
            outcome.raw["website_migration"]["final_url"], unrelated
        )


if __name__ == "__main__":
    unittest.main()
