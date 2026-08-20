from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup

import common
from common import init_db
from help_content import HELP_TOPICS


os.environ["EDSCANNER_DISABLE_WORKER"] = "1"


class HelpAndHomeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.import_temp = TemporaryDirectory()
        self.imports_dir = Path(self.import_temp.name)

        self.db_patch = patch.object(common, "DB_PATH", self.db_path)
        self.imports_patch = patch.object(common, "IMPORTS_DIR", self.imports_dir)
        self.db_patch.start()
        self.imports_patch.start()
        init_db(self.db_path)

        if "app" not in sys.modules:
            self.app_module = importlib.import_module("app")
        else:
            self.app_module = sys.modules["app"]
        self.app_imports_patch = patch.object(self.app_module, "IMPORTS_DIR", self.imports_dir)
        self.app_imports_patch.start()
        self.app_module.app.config.update(TESTING=True, SECRET_KEY="help-ui-tests")
        self.client = self.app_module.app.test_client()

    def tearDown(self) -> None:
        self.app_imports_patch.stop()
        self.imports_patch.stop()
        self.db_patch.stop()
        self.import_temp.cleanup()
        self.db_path.unlink(missing_ok=True)

    def test_home_uses_module_cards_and_keeps_header_navigation_small(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")

        columns = soup.select(".module-columns > .module-column")
        self.assertEqual(len(columns), 2)
        module_labels = {link.get_text(" ", strip=True) for link in soup.select(".module-button")}
        self.assertTrue(
            {
                "Search Profiles",
                "Search Websites",
                "Import Districts",
                "Discover Sources",
                "Sync Meetings",
                "Search Board Content",
                "Monitoring Schedules",
                "Discover Contracts",
            }.issubset(module_labels)
        )

        main_nav = soup.select_one('nav[aria-label="Main navigation"]')
        self.assertIsNotNone(main_nav)
        direct_links = [link.get_text(" ", strip=True) for link in main_nav.find_all("a", recursive=False)]
        self.assertEqual(direct_links, ["Home", "Settings"])
        self.assertEqual(main_nav.select_one("summary").get_text(" ", strip=True), "Help")

    def test_help_center_and_every_topic_render(self):
        index_response = self.client.get("/help")
        self.assertEqual(index_response.status_code, 200)
        self.assertIn("Help Center", index_response.get_data(as_text=True))

        for slug, topic in HELP_TOPICS.items():
            with self.subTest(slug=slug):
                response = self.client.get(f"/help/{slug}")
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertIn(str(topic["title"]), response.get_data(as_text=True))
        self.assertEqual(self.client.get("/help/not-a-topic").status_code, 404)

    def test_import_page_explains_nces_scan_and_zip_extraction(self):
        (self.imports_dir / "districts.csv").write_text("Agency Name,State\nExample,OR\n", encoding="utf-8")
        (self.imports_dir / "elsi_export.zip").write_bytes(b"not-a-real-zip")

        response = self.client.get("/import?scan=1")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_data(as_text=True)
        self.assertIn("658416", body)
        self.assertIn("Open NCES ELSI", body)
        self.assertIn(str(self.imports_dir), body)
        self.assertIn("Scan completed", body)
        self.assertIn("districts.csv", body)
        self.assertIn("Import this file", body)
        self.assertIn("elsi_export.zip", body)
        self.assertIn("still need extraction", body)

    def test_search_route_reports_advanced_query_syntax_errors(self):
        response = self.client.post(
            "/search/run",
            data={"query_text": "budget AND", "search_method": "crawler"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("Invalid search query", response.get_data(as_text=True))

    def test_advanced_result_highlighting_marks_positive_terms_and_escapes_html(self):
        rendered = str(
            self.app_module.highlight(
                "Counseling and budget <script>alert(1)</script>; not an old draft.",
                'counsel* AND "budget" NOT draft',
            )
        )

        self.assertIn("<mark>Counseling</mark>", rendered)
        self.assertIn("<mark>budget</mark>", rendered)
        self.assertNotIn("<mark>draft</mark>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)


if __name__ == "__main__":
    unittest.main()
