from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlparse

from common import clean_source_header, connect_db, init_db, normalize_website, utc_now_iso
from search_engine import (
    SearchSettings,
    create_search_run,
    execute_search_run,
    export_search_run_csv,
    run_search,
    search_district,
)
from site_search_discovery import discover_district_search_profile, get_best_search_profile


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


class CoreTests(unittest.TestCase):
    def test_header_and_website_normalization(self):
        self.assertEqual(
            clean_source_header("Total Students All Grades (Excludes AE) [District] 2024-25"),
            "Total Students All Grades (Excludes AE)",
        )
        self.assertEqual(normalize_website("example.k12.or.us"), ("https://example.k12.or.us", 1))
        self.assertEqual(normalize_website("†"), ("", 0))

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
