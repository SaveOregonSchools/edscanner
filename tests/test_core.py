from __future__ import annotations

import json
import io
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from pypdf import PdfWriter

from common import (
    clean_source_header,
    connect_db,
    init_db,
    normalize_website,
    parse_env_line,
    prefer_https_url,
    quote_env_value,
    utc_now_iso,
)
from search_engine import (
    SearchSettings,
    create_search_run,
    execute_search_run,
    export_search_run_csv,
    run_search,
    search_district,
)
from site_search_discovery import (
    _extract_edlio_config,
    discover_district_search_profile,
    get_best_search_profile,
    parse_edlio_search_results,
    parse_finalsite_algolia_search_results,
    parse_search_results_page,
    select_best_profile_test_result,
)
from contract_discovery import (
    ContractDiscoverySettings,
    archive_document_files,
    classify_bargaining_unit,
    create_contract_discovery_run,
    district_archive_directory,
    execute_contract_discovery_run,
    export_contract_discovery_csv,
    extract_agreement_dates,
    select_contract_districts,
)
from ai_matcher import analyze_contract_candidate, parse_ollama_endpoints


class LocalSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/search"):
            body = json.dumps(
                {
                    "web": {
                        "results": [
                            {
                                "title": "Community Schools Policy",
                                "url": f"http://{self.headers['Host']}/policy.html",
                                "description": "The district supports community schools partnerships.",
                            }
                        ]
                    }
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/sitemap.xml":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/":
            body = b"""
            <html>
              <head><title>District Home</title></head>
              <body>
                <form action="/search" method="get">
                  <input type="search" name="q" placeholder="Search">
                  <button>Search</button>
                </form>
                <a href="/policy.html">Policy</a>
              </body>
            </html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/search"):
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0].casefold()
            if "calendar" in query:
                link = "/calendar.html"
                title = "District Calendar"
                snippet = "The district calendar includes school board meetings."
            else:
                link = "/policy.html"
                title = "Community Schools Policy"
                snippet = "The district supports community schools partnerships."
            body = f"""
            <html>
              <head><title>Search results</title></head>
              <body>
                <main id="search-results">
                  <article class="search-result">
                    <a href="{link}">{title}</a>
                    <p>{snippet}</p>
                  </article>
                </main>
              </body>
            </html>
            """.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/policy.html":
            body = b"""
            <html>
              <head><title>Community Schools Policy</title></head>
              <body>
                <h1>Community Schools</h1>
                <p>The district supports community schools partnerships.</p>
              </body>
            </html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/calendar.html":
            body = b"""
            <html>
              <head><title>District Calendar</title></head>
              <body>
                <h1>Calendar</h1>
                <p>The district calendar includes school board meetings and family events.</p>
              </body>
            </html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


class ContractSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b"""
            <html><head><title>Example District</title></head><body>
              <a href="/human-resources">Human Resources</a>
            </body></html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/human-resources":
            body = b"""
            <html><head><title>Contracts and Salary Schedules</title></head><body>
              <h1>Collective Bargaining Agreements</h1>
              <a href="/licensed-2024-2027.pdf">Licensed Collective Bargaining Agreement 2024-2027</a>
              <a href="/osea-classified-2025-2028.pdf">OSEA Classified Collective Bargaining Agreement 2025-2028</a>
            </body></html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith(".pdf"):
            stream = io.BytesIO()
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.write(stream)
            body = stream.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.payload)


class CoreTests(unittest.TestCase):
    def test_contract_archive_paths_are_bounded_unique_and_store_text(self):
        first = {"id": 101, "agency_id_nces": "4100010", "agency_name": "A" * 120, "state": "OR"}
        second = {"id": 102, "agency_id_nces": "4100020", "agency_name": "A" * 120, "state": "OR"}
        with TemporaryDirectory() as archive_temp:
            archive_root = Path(archive_temp)
            first_dir = district_archive_directory(first, archive_root, 40)
            second_dir = district_archive_directory(second, archive_root, 40)
            settings = ContractDiscoverySettings(
                archive_root=archive_root,
                district_dir_name_max=40,
            )
            archived = archive_document_files(
                first,
                {
                    "bargaining_unit_type": "classified",
                    "document_type": "base_agreement",
                    "title": "Classified Staff Agreement 2025-2028",
                    "url": "https://example.test/contract.pdf",
                    "content_type": "application/pdf",
                },
                b"%PDF archived contract test",
                "Extracted agreement text",
                settings,
            )

            self.assertNotEqual(first_dir, second_dir)
            self.assertEqual(first_dir.parent.name, "OR")
            self.assertLessEqual(len(first_dir.name.split("--", 1)[0]), 40)
            self.assertTrue(Path(archived["local_file_path"]).is_file())
            self.assertTrue(Path(archived["extracted_text_path"]).is_file())
            self.assertEqual(
                Path(archived["extracted_text_path"]).read_text(encoding="utf-8"),
                "Extracted agreement text",
            )

    def test_recent_contract_scan_is_skipped_unless_expired_or_forced(self):
        temp_db = NamedTemporaryFile(suffix=".db", delete=False)
        temp_db_path = Path(temp_db.name)
        temp_db.close()
        try:
            init_db(temp_db_path)
            now = utc_now_iso()
            with connect_db(temp_db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('test.csv', 1, '4109999', 'Rescan District', 'OR',
                              '1-Regular local school district', 5000,
                              'https://example.test', 'https://example.test', 1, '{}', ?, ?)
                    """,
                    (now, now),
                )
                district_id = int(cursor.lastrowid)
                conn.commit()
            run_id = create_contract_discovery_run(
                states=["OR"],
                max_districts=1,
                force_rescan=True,
                db_path=temp_db_path,
            )
            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO district_contract_scan_status (
                        district_id, last_run_id, last_attempted_at,
                        last_successful_scan_at, last_status, updated_at
                    ) VALUES (?, ?, ?, ?, 'success', ?)
                    """,
                    (district_id, run_id, now, now, now),
                )
                conn.commit()

            selected, matched, skipped = select_contract_districts(
                ["OR"], [], None, None, 10, temp_db_path,
                rescan_after_days=180,
                recheck_expired=False,
            )
            self.assertEqual((len(selected), matched, skipped), (0, 1, 1))

            forced, _, _ = select_contract_districts(
                ["OR"], [], None, None, 10, temp_db_path,
                rescan_after_days=180,
                recheck_expired=False,
                force_rescan=True,
            )
            self.assertEqual(len(forced), 1)

            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO district_contract_packages (
                        discovery_run_id, district_id, district_name, state,
                        bargaining_unit_type, expiration_date, agreement_status,
                        current_as_of, created_at, updated_at
                    ) VALUES (?, ?, 'Rescan District', 'OR', 'classified',
                              '2025-06-30', 'expired', '2026-08-08', ?, ?)
                    """,
                    (run_id, district_id, now, now),
                )
                conn.commit()
            expired_due, _, expired_skipped = select_contract_districts(
                ["OR"], [], None, None, 10, temp_db_path,
                rescan_after_days=180,
                recheck_expired=True,
            )
            self.assertEqual(len(expired_due), 1)
            self.assertEqual(expired_skipped, 0)

            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO district_contract_packages (
                        discovery_run_id, district_id, district_name, state,
                        bargaining_unit_type, expiration_date, agreement_status,
                        current_as_of, created_at, updated_at
                    ) VALUES (?, ?, 'Rescan District', 'OR', 'classified',
                              '2999-06-30', 'current', '2026-08-08', ?, ?)
                    """,
                    (run_id, district_id, now, now),
                )
                conn.commit()
            successor_found, _, successor_skipped = select_contract_districts(
                ["OR"], [], None, None, 10, temp_db_path,
                rescan_after_days=180,
                recheck_expired=True,
            )
            self.assertEqual(len(successor_found), 0)
            self.assertEqual(successor_skipped, 1)
        finally:
            temp_db_path.unlink(missing_ok=True)

    def test_parse_ollama_endpoints_preserves_priority_and_normalizes_api_paths(self):
        endpoints = parse_ollama_endpoints(
            '["http://primary.test:11434/api/chat","http://fallback.test:11434/","http://primary.test:11434"]'
        )

        self.assertEqual(
            endpoints,
            ["http://primary.test:11434", "http://fallback.test:11434"],
        )
        encoded = quote_env_value(json.dumps(endpoints, separators=(",", ":")))
        self.assertEqual(
            parse_env_line(f"ENDPOINTS={encoded}")[1],
            json.dumps(endpoints, separators=(",", ":")),
        )

    def test_ollama_contract_classification_fails_over_to_second_server(self):
        successful_response = Mock()
        successful_response.raise_for_status.return_value = None
        successful_response.json.return_value = {
            "message": {
                "content": json.dumps(
                    {
                        "is_labor_agreement_document": True,
                        "bargaining_unit_type": "classified",
                        "union_name": "Example Classified Association",
                        "confidence": 0.94,
                    }
                )
            }
        }

        with (
            patch(
                "ai_matcher.get_ollama_endpoints",
                return_value=["http://primary.test:11434", "http://fallback.test:11434"],
            ),
            patch("ai_matcher.get_ollama_model", return_value="gemma4:12b"),
            patch(
                "ai_matcher.requests.post",
                side_effect=[OSError("primary offline"), successful_response],
            ) as post,
        ):
            result = analyze_contract_candidate(
                district_name="Example District",
                title="Classified Agreement 2025-2028",
                url="https://example.test/classified.pdf",
                parent_context="Collective bargaining agreements",
                text_excerpt="Agreement between the district and classified association.",
            )

        self.assertEqual(result["bargaining_unit_type"], "classified")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], "http://primary.test:11434/api/chat")
        self.assertEqual(post.call_args_list[1].args[0], "http://fallback.test:11434/api/chat")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "gemma4:12b")

    def test_contract_classification_keeps_units_separate(self):
        self.assertEqual(classify_bargaining_unit("Licensed Collective Bargaining Agreement")[0], "licensed")
        self.assertEqual(classify_bargaining_unit("OSEA Classified Collective Bargaining Agreement")[0], "classified")
        self.assertEqual(classify_bargaining_unit("Substitute Teacher Agreement")[0], "substitute")
        self.assertEqual(
            extract_agreement_dates("Collective Bargaining Agreement 2024-2027")[:2],
            ("2024-07-01", "2027-06-30"),
        )

    def test_contract_discovery_stores_distinct_bargaining_unit_packages(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ContractSiteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temp_db = NamedTemporaryFile(suffix=".db", delete=False)
        temp_db_path = Path(temp_db.name)
        temp_db.close()
        archive_temp = TemporaryDirectory()
        try:
            init_db(temp_db_path)
            now = utc_now_iso()
            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    ) VALUES ('test.csv', 1, 'contract-test', 'Example District',
                              'OR', '1-Regular local school district', 1000, ?, ?, 1, '{}', ?, ?)
                    """,
                    (f"http://127.0.0.1:{server.server_port}", f"http://127.0.0.1:{server.server_port}", now, now),
                )
                conn.commit()
            run_id = create_contract_discovery_run(
                states=["OR"],
                max_districts=1,
                max_pages_per_district=5,
                max_workers=1,
                db_path=temp_db_path,
            )
            execute_contract_discovery_run(
                run_id,
                db_path=temp_db_path,
                settings=ContractDiscoverySettings(
                    max_pages_per_district=5,
                    max_candidates_per_district=10,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                    archive_root=Path(archive_temp.name),
                ),
            )
            with connect_db(temp_db_path) as conn:
                run = conn.execute("SELECT * FROM contract_discovery_runs WHERE id = ?", (run_id,)).fetchone()
                packages = conn.execute(
                    "SELECT bargaining_unit_type, union_name FROM district_contract_packages WHERE discovery_run_id = ? ORDER BY bargaining_unit_type",
                    (run_id,),
                ).fetchall()
                document_rows = conn.execute(
                    "SELECT local_file_path, extracted_text_path FROM district_contract_documents WHERE discovery_run_id = ?",
                    (run_id,),
                ).fetchall()
                scan_status = conn.execute(
                    "SELECT * FROM district_contract_scan_status WHERE district_id = (SELECT id FROM districts LIMIT 1)"
                ).fetchone()
            self.assertEqual(run["status"], "completed")
            self.assertEqual([row["bargaining_unit_type"] for row in packages], ["classified", "licensed"])
            self.assertEqual(len(document_rows), 2)
            self.assertTrue(all(Path(row["local_file_path"]).is_file() for row in document_rows))
            self.assertEqual(scan_status["last_status"], "success")
            self.assertIsNotNone(scan_status["last_successful_scan_at"])
            csv_text = export_contract_discovery_csv(run_id, temp_db_path)
            self.assertIn("classified", csv_text)
            self.assertIn("licensed", csv_text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            temp_db_path.unlink(missing_ok=True)
            archive_temp.cleanup()

    def test_header_and_website_normalization(self):
        self.assertEqual(
            clean_source_header("Total Students All Grades (Excludes AE) [District] 2024-25"),
            "Total Students All Grades (Excludes AE)",
        )
        self.assertEqual(normalize_website("example.k12.or.us"), ("https://example.k12.or.us", 1))
        self.assertEqual(prefer_https_url("http://example.k12.or.us/path?q=1"), "https://example.k12.or.us/path?q=1")
        self.assertEqual(prefer_https_url("http://127.0.0.1:5000/path"), "http://127.0.0.1:5000/path")
        self.assertEqual(prefer_https_url("https://example.k12.or.us/path"), "https://example.k12.or.us/path")
        self.assertEqual(normalize_website("†"), ("", 0))

    def test_parse_edlio_search_results_keeps_same_domain_results(self):
        payload = {
            "items": [
                {
                    "_source": {
                        "Url": "https://www.fgsdk12.org/apps/pages/calendar",
                        "Title": "District Calendar",
                        "PreviewText": "<strong>Calendar</strong> dates and family events",
                    }
                },
                {
                    "_source": {
                        "Url": "https://example.com/offsite",
                        "Title": "External",
                        "PreviewText": "Should not be trusted as a district result",
                    }
                },
            ]
        }
        results = parse_edlio_search_results(
            payload,
            "https://www.fgsdk12.org/",
            "https://search.edlio.com/FORGS/search?q=calendar&offset=0&boostWebsiteId=FORGS",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "edlio_search_api")
        self.assertEqual(results[0]["url"], "https://www.fgsdk12.org/apps/pages/calendar")
        self.assertEqual(results[0]["snippet"], "Calendar dates and family events")

    def test_parse_search_results_page_falls_back_to_generic_rendered_links(self):
        html = b"""
        <html>
          <body>
            <nav>
              <a href="/">Home</a>
              <a href="/search-results?q=restorative">Search</a>
            </nav>
            <main>
              <div class="fsElement fsContent">
                <h2><a href="/departments/student-services/restorative-practices">Restorative Practices</a></h2>
                <p>Resources about restorative practices and student support.</p>
              </div>
              <div class="card">
                <a href="https://example.com/offsite">External restorative result</a>
              </div>
            </main>
          </body>
        </html>
        """
        results = parse_search_results_page(
            html,
            "https://district.example/search-results?q=restorative",
            "https://district.example/",
            "restorative",
            {"search_url_template": "https://district.example/search-results?q={query}"},
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["url"],
            "https://district.example/departments/student-services/restorative-practices",
        )
        self.assertEqual(results[0]["title"], "Restorative Practices")

    def test_parse_finalsite_algolia_search_results_uses_page_config(self):
        html = b"""
        <section class="fsElement fsSearchElement"
                 data-app-id="APPID"
                 data-index-prefix="district_"
                 data-api-key="public-key"
                 data-domains='{"4094":"district.example"}'
                 data-search-term="restorative">
        </section>
        """
        session = FakeSession(
            {
                "hits": [
                    {
                        "domain_id": 4094,
                        "page_name": "Belonging",
                        "page_path": "/departments/belonging",
                        "content": "Honor culture, identity and restorative healing.",
                        "_snippetResult": {
                            "content": {
                                "value": "identity and <em>restorative</em> healing"
                            }
                        },
                    }
                ]
            }
        )
        results = parse_finalsite_algolia_search_results(
            html,
            "https://district.example/",
            "restorative",
            session,
            SearchSettings(request_timeout_seconds=2, delay_seconds=0),
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "finalsite_algolia")
        self.assertEqual(results[0]["url"], "https://district.example/departments/belonging")
        self.assertEqual(results[0]["snippet"], "identity and restorative healing")
        self.assertIn("/district_pages/query", session.posts[0][0])

    def test_extract_edlio_config_from_corp_data_layer(self):
        html = b"""
        <script>
        edlioCorpDataLayer = [{
          "WebsiteName": "Newberg-Dundee Public Schools",
          "WebsiteId": "NEWSDJ",
          "DistrictWebsiteId": ""
        }];
        </script>
        """
        config = _extract_edlio_config(html)
        self.assertEqual(config["website_id"], "NEWSDJ")
        self.assertEqual(config["identifier"], "NEWSDJ")
        self.assertEqual(config["search_domain"], "https://search.edlio.com/")

    def test_select_best_profile_prefers_javascript_over_generic_failure(self):
        best = select_best_profile_test_result(
            [
                {"profile_status": "search_found_but_failed", "confidence": 0, "test_success": 0},
                {"profile_status": "requires_javascript", "confidence": 0, "test_success": 0},
            ]
        )
        self.assertEqual(best["profile_status"], "requires_javascript")

    def test_search_district_finds_local_html_match(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalSiteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/"
            results = search_district(
                {"agency_name": "Local District", "website_normalized": base_url},
                "community schools",
                SearchSettings(
                    max_pages_per_district=5,
                    max_results_per_district=5,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                ),
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(results)
        self.assertEqual(results[0]["content_type"], "text/html")
        self.assertIn("/policy.html", results[0]["url"])
        self.assertGreater(results[0]["score"], 0)
        self.assertIn("community schools", results[0]["snippet"].casefold())

    def test_brave_search_uses_api_results_and_fetches_page(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalSiteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/"
            results = search_district(
                {"agency_name": "Local District", "website_normalized": base_url},
                "community schools",
                SearchSettings(
                    search_method="brave",
                    brave_api_key="test-key",
                    brave_endpoint=f"{base_url}api/search",
                    api_results_per_district=3,
                    follow_depth=0,
                    max_pages_per_district=5,
                    max_results_per_district=5,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                ),
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(results)
        self.assertIn("/policy.html", results[0]["url"])
        self.assertEqual(results[0]["search_source"], "brave+fetch")
        self.assertIn("community schools", results[0]["snippet"].casefold())

    def test_district_search_profile_discovery_and_run_storage(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalSiteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temp_db = NamedTemporaryFile(suffix=".db", delete=False)
        temp_db_path = Path(temp_db.name)
        temp_db.close()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/"
            init_db(temp_db_path)
            now = utc_now_iso()
            with connect_db(temp_db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        "test.csv",
                        1,
                        "0000003",
                        "Search Profile District",
                        "OR",
                        "1-Regular local school district",
                        100,
                        base_url,
                        base_url,
                        "{}",
                        now,
                        now,
                    ),
                )
                district_id = int(cursor.lastrowid)
                conn.commit()
            district = {
                "id": district_id,
                "agency_name": "Search Profile District",
                "state": "OR",
                "agency_type": "1-Regular local school district",
                "total_enrollment_excludes_ae": 100,
                "website": base_url,
                "website_normalized": base_url,
            }
            profile = discover_district_search_profile(
                district,
                test_query="calendar",
                settings=SearchSettings(request_timeout_seconds=2, delay_seconds=0),
                force=True,
                db_path=temp_db_path,
            )
            direct_results = search_district(
                district,
                "community schools",
                SearchSettings(
                    search_method="district_search",
                    max_results_per_district=5,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                ),
                db_path=temp_db_path,
            )
            run_id = run_search(
                "community schools",
                states=["OR"],
                max_districts=1,
                db_path=temp_db_path,
                settings=SearchSettings(
                    search_method="district_search",
                    max_pages_per_district=5,
                    max_results_per_district=5,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                ),
            )
            csv_text = export_search_run_csv(run_id, temp_db_path)
            best_profile = get_best_search_profile(district_id, temp_db_path)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            temp_db_path.unlink(missing_ok=True)

        self.assertEqual(profile["profile_status"], "working")
        self.assertIn("/search", profile["search_url_template"])
        self.assertIsNotNone(best_profile)
        self.assertTrue(direct_results)
        self.assertEqual(direct_results[0]["search_source"], "district_search+fetch")
        self.assertIn("/policy.html", direct_results[0]["url"])
        self.assertIn("Search Profile District", csv_text)
        self.assertIn("district_search+fetch", csv_text)

    def test_run_search_stores_results_and_exports_csv(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalSiteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temp_db = NamedTemporaryFile(suffix=".db", delete=False)
        temp_db_path = Path(temp_db.name)
        temp_db.close()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/"
            init_db(temp_db_path)
            now = utc_now_iso()
            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        "test.csv",
                        1,
                        "0000001",
                        "Local District",
                        "OR",
                        "1-Regular local school district",
                        100,
                        base_url,
                        base_url,
                        "{}",
                        now,
                        now,
                    ),
                )
                conn.commit()
            run_id = run_search(
                "community schools",
                states=["OR"],
                max_districts=1,
                debug_logging=True,
                db_path=temp_db_path,
                settings=SearchSettings(
                    max_pages_per_district=5,
                    max_results_per_district=5,
                    request_timeout_seconds=2,
                    delay_seconds=0,
                ),
            )
            csv_text = export_search_run_csv(run_id, temp_db_path)
            with connect_db(temp_db_path) as conn:
                run = conn.execute("SELECT debug_logging, debug_log_path FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            temp_db_path.unlink(missing_ok=True)

        self.assertIn("Local District", csv_text)
        self.assertIn("Community Schools Policy", csv_text)
        self.assertIn("/policy.html", csv_text)
        self.assertEqual(run["debug_logging"], 1)
        debug_log_path = Path(run["debug_log_path"])
        self.assertTrue(debug_log_path.exists())
        debug_text = debug_log_path.read_text(encoding="utf-8")
        self.assertIn("run_start", debug_text)
        self.assertIn("page_result", debug_text)
        self.assertIn("/policy.html", debug_text)

    def test_cancelled_run_does_not_search_when_cancel_requested_before_start(self):
        temp_db = NamedTemporaryFile(suffix=".db", delete=False)
        temp_db_path = Path(temp_db.name)
        temp_db.close()
        try:
            init_db(temp_db_path)
            now = utc_now_iso()
            with connect_db(temp_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO districts (
                        source_file, source_row_number, agency_id_nces, agency_name,
                        state, agency_type, total_enrollment_excludes_ae, website,
                        website_normalized, has_searchable_website, raw_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        "test.csv",
                        1,
                        "0000002",
                        "Cancelled District",
                        "OR",
                        "1-Regular local school district",
                        100,
                        "https://example.test",
                        "https://example.test",
                        "{}",
                        now,
                        now,
                    ),
                )
                conn.commit()
            run_id = create_search_run(
                "community schools",
                states=["OR"],
                max_districts=1,
                db_path=temp_db_path,
                settings=SearchSettings(delay_seconds=0),
            )
            with connect_db(temp_db_path) as conn:
                conn.execute("UPDATE search_runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
                conn.commit()
            execute_search_run(run_id, db_path=temp_db_path, settings=SearchSettings(delay_seconds=0))
            with connect_db(temp_db_path) as conn:
                run = conn.execute("SELECT status, districts_searched FROM search_runs WHERE id = ?", (run_id,)).fetchone()
                result_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM search_results WHERE search_run_id = ?",
                    (run_id,),
                ).fetchone()["count"]
        finally:
            temp_db_path.unlink(missing_ok=True)

        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["districts_searched"], 0)
        self.assertEqual(result_count, 0)


if __name__ == "__main__":
    unittest.main()
    district_archive_directory,
