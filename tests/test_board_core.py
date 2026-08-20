from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import patch

from board.adapters.boardbook import BoardBookAdapter
from board.discovery import DiscoveryOutcome
from board.documents import extract_document_text, store_board_document
from board.http import HTTPResult
from board.models import (
    AgendaItem,
    DocumentRef,
    DownloadedDocument,
    MeetingRef,
    NormalizedMeeting,
)
from board.runs import (
    _meeting_needs_refresh,
    create_board_discovery_run,
    create_board_sync_run,
    execute_board_discovery_run,
    execute_board_sync_run,
)
from board.search import search_board_content
from board.storage import (
    audit_legacy_working_board_sources,
    persist_meeting_bundle,
    upsert_board_source,
)
from common import connect_db, init_db, utc_now_iso


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class FixtureBoardBookSyncAdapter:
    """Exercise the production BoardBook parsers without making network calls."""

    def __init__(
        self,
        *,
        fail_external_source_ids: set[str] | None = None,
        cancel_db_path: Path | None = None,
        cancel_run_id: int | None = None,
    ) -> None:
        # Parsing methods do not need HTTP. An inert object makes accidental
        # network use fail immediately instead of making this fixture flaky.
        self.parser = BoardBookAdapter(client=object())
        self.fail_external_source_ids = fail_external_source_ids or set()
        self.cancel_db_path = cancel_db_path
        self.cancel_run_id = cancel_run_id

    def _check_source(self, source) -> None:
        if source.external_source_id in self.fail_external_source_ids:
            raise RuntimeError(f"fixture failure for source {source.external_source_id}")

    def list_meetings(self, source, since=None):
        self._check_source(source)
        return self.parser.parse_meeting_list(
            fixture_bytes("boardbook_organization.html"),
            "https://meetings.boardbook.org/Public/Organization/2221",
            source,
            since=since,
        )

    def fetch_meeting(self, source, meeting_ref):
        self._check_source(source)
        meeting = self.parser.parse_meeting_detail(
            fixture_bytes("boardbook_agenda.html"),
            meeting_ref.agenda_url or meeting_ref.url,
            source,
            meeting_ref,
        )
        if self.cancel_db_path is not None and self.cancel_run_id is not None:
            with connect_db(self.cancel_db_path) as conn:
                conn.execute(
                    "UPDATE board_sync_runs SET cancel_requested = 1 WHERE id = ?",
                    (self.cancel_run_id,),
                )
                conn.commit()
        return meeting

    def fetch_document(self, document_ref):
        # Keep the BoardBook identity/provenance while using easy-to-extract fixture text.
        text_ref = DocumentRef(
            title=document_ref.title,
            url=document_ref.url,
            external_document_id=document_ref.external_document_id,
            document_type=document_ref.document_type,
            agenda_item_external_id=document_ref.agenda_item_external_id,
            content_type="text/plain",
            file_name="boardbook-attachment.txt",
            metadata=dict(document_ref.metadata),
        )
        return DownloadedDocument(
            document_ref=text_ref,
            content=b"BoardBook fixture attachment about the adopted budget.",
            final_url=text_ref.url,
            status_code=200,
            content_type="text/plain",
        )


class BoardDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(handle.name)
        handle.close()
        self.storage_temp = TemporaryDirectory()
        self.storage_root = Path(self.storage_temp.name)
        init_db(self.db_path)
        self.district_id = self.add_district(
            "4100001",
            "Fixture School District",
            state="OR",
            website="https://district.example/",
        )

    def tearDown(self) -> None:
        self.storage_temp.cleanup()
        self.db_path.unlink(missing_ok=True)

    def add_district(
        self,
        nces_id: str,
        name: str,
        *,
        state: str = "OR",
        website: str = "https://district.example/",
        agency_type: str = "1-Regular local school district",
        enrollment: int = 1_500,
    ) -> int:
        now = utc_now_iso()
        with connect_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO districts (
                    source_file, source_row_number, agency_id_nces, agency_name,
                    state, agency_type, total_enrollment_excludes_ae, website,
                    website_normalized, has_searchable_website, raw_json,
                    created_at, updated_at
                ) VALUES ('fixture.csv', 1, ?, ?, ?, ?, ?, ?, ?, 1, '{}', ?, ?)
                """,
                (
                    nces_id,
                    name,
                    state,
                    agency_type,
                    enrollment,
                    website,
                    website,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def add_source(
        self,
        district_id: int | None = None,
        *,
        external_id: str = "2221",
        url: str = "https://meetings.boardbook.org/Public/Organization/2221",
        platform: str = "boardbook",
        status: str = "working",
    ) -> dict:
        return upsert_board_source(
            district_id or self.district_id,
            {
                "platform": platform,
                "source_url": url,
                "source_status": status,
                "organization_external_id": external_id,
                "confidence": 99,
            },
            db_path=self.db_path,
        )

    @staticmethod
    def normalized_meeting(*, revised: bool = False) -> NormalizedMeeting:
        child = AgendaItem(
            external_item_id="item-1a",
            parent_external_item_id="item-1",
            depth=1,
            order_index=2,
            item_number="1.A",
            title="Adopt the revised budget" if revised else "Adopt the budget",
            description="Public testimony and final board action.",
        )
        parent = AgendaItem(
            external_item_id="item-1",
            depth=0,
            order_index=1,
            item_number="1",
            title="Business services",
        )
        return NormalizedMeeting(
            external_meeting_id="meeting-2026-08-24",
            source_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=9001",
            title="Regular Board Meeting",
            platform="boardbook",
            meeting_date="2026-08-24",
            meeting_start_time="18:00:00",
            meeting_type="Regular",
            location="District Office Board Room",
            description="Budget adoption and student services.",
            agenda_url="https://meetings.boardbook.org/Public/Agenda/2221?meeting=9001",
            minutes_url=(
                "https://meetings.boardbook.org/Public/Minutes/2221?meeting=9001"
                if revised
                else None
            ),
            agenda_items=[parent, child],
        )


class BoardStorageAndSearchTests(BoardDatabaseTestCase):
    def test_startup_audit_repairs_ids_and_flags_legacy_false_working_sources(self):
        pendleton = self.add_district(
            "4100002", "Pendleton SD 16", website="https://pendleton.k12.or.us/"
        )
        canonical = self.add_district(
            "4100003", "Canonical BoardBook District", website="https://canonical.example/"
        )
        tillamook = self.add_district(
            "4100004", "Tillamook SD 9", website="https://www.tillamook.k12.or.us/"
        )
        willamina = self.add_district(
            "4100005", "Willamina SD 30J", website="https://www.willamina.k12.or.us/"
        )
        confirmed = self.add_district(
            "4100006", "Confirmed Manual District", website="https://confirmed.example/"
        )
        rows = {
            "pendleton": upsert_board_source(
                pendleton,
                {
                    "platform": "boardbook",
                    "source_status": "working",
                    "source_url": "https://pendleton.k12.or.us/board-meetings",
                },
                db_path=self.db_path,
            ),
            "canonical": upsert_board_source(
                canonical,
                {
                    "platform": "boardbook",
                    "source_status": "working",
                    "source_url": "https://meetings.boardbook.org/Public/Organization/1559",
                },
                db_path=self.db_path,
            ),
            "tillamook": upsert_board_source(
                tillamook,
                {
                    "platform": "diligent_community",
                    "source_status": "working",
                    "source_url": "https://www.tillamook.k12.or.us/Portal",
                    "organization_external_id": "www",
                    "platform_tenant": "www",
                },
                db_path=self.db_path,
            ),
            "willamina": upsert_board_source(
                willamina,
                {
                    "platform": "generic",
                    "source_status": "working",
                    "source_url": "https://www.willamina.k12.or.us/district/news/board-meeting",
                },
                db_path=self.db_path,
            ),
            "confirmed": upsert_board_source(
                confirmed,
                {
                    "platform": "generic",
                    "source_status": "working",
                    "source_url": "https://confirmed.example/news/board-meeting",
                    "raw": {
                        "discovery_method": "manual_entry",
                        "operator_confirmed_district_identity": True,
                        "verified": True,
                    },
                },
                db_path=self.db_path,
            ),
        }

        preview = audit_legacy_working_board_sources(
            db_path=self.db_path, apply_changes=False
        )
        self.assertEqual(len(preview["repaired_ids"]), 1)
        self.assertEqual(len(preview["review_required"]), 3)
        result = audit_legacy_working_board_sources(db_path=self.db_path)

        with connect_db(self.db_path) as conn:
            saved = {
                name: conn.execute(
                    "SELECT * FROM board_sources WHERE id = ?", (row["id"],)
                ).fetchone()
                for name, row in rows.items()
            }
        self.assertEqual(result["repaired_ids"], [rows["canonical"]["id"]])
        self.assertEqual(saved["canonical"]["source_status"], "working")
        self.assertEqual(saved["canonical"]["organization_external_id"], "1559")
        self.assertEqual(saved["pendleton"]["source_status"], "manual_review")
        self.assertEqual(saved["tillamook"]["source_status"], "manual_review")
        self.assertEqual(saved["willamina"]["source_status"], "manual_review")
        self.assertEqual(saved["confirmed"]["source_status"], "working")

    def test_startup_audit_normalizes_provider_urls_and_validates_all_tenants(self):
        def district(number: int, name: str) -> int:
            return self.add_district(f"4199{number:03d}", name)

        rows = {
            "boardbook": upsert_board_source(
                district(1, "HTTP BoardBook"),
                {
                    "platform": "boardbook",
                    "source_status": "working",
                    "source_url": (
                        "http://meetings.boardbook.org/Public/Organization/2413"
                    ),
                },
                db_path=self.db_path,
            ),
            "diligent": upsert_board_source(
                district(2, "HTTP Diligent"),
                {
                    "platform": "diligent_community",
                    "source_status": "working",
                    "source_url": "http://district.civicweb.net/meetings",
                },
                db_path=self.db_path,
            ),
            "simbli": upsert_board_source(
                district(3, "HTTP Simbli"),
                {
                    "platform": "simbli",
                    "source_status": "working",
                    "source_url": (
                        "http://simbli.eboardsolutions.com/SB_Meetings/"
                        "ViewMeeting.aspx?MID=1&S=SITE_9"
                    ),
                },
                db_path=self.db_path,
            ),
            "boarddocs": upsert_board_source(
                district(4, "HTTP BoardDocs"),
                {
                    "platform": "boarddocs",
                    "source_status": "working",
                    "source_url": (
                        "http://go.boarddocs.com/or/example/Board.nsf/goto?id=1"
                    ),
                },
                db_path=self.db_path,
            ),
            "civicclerk": upsert_board_source(
                district(5, "HTTP CivicClerk"),
                {
                    "platform": "civicclerk",
                    "source_status": "working",
                    "source_url": "http://usbe.api.civicclerk.com/v1/Events",
                },
                db_path=self.db_path,
            ),
            "boarddocs_wrapper": upsert_board_source(
                district(6, "BoardDocs Wrapper"),
                {
                    "platform": "boarddocs",
                    "source_status": "working",
                    "source_url": "https://wrapper.example/boarddocs",
                },
                db_path=self.db_path,
            ),
            "civicclerk_wrapper": upsert_board_source(
                district(7, "CivicClerk Wrapper"),
                {
                    "platform": "civicclerk",
                    "source_status": "working",
                    "source_url": "https://wrapper.example/civicclerk",
                },
                db_path=self.db_path,
            ),
            "unsafe_transport": upsert_board_source(
                district(8, "Unsafe Provider Transport"),
                {
                    "platform": "boardbook",
                    "source_status": "working",
                    "source_url": (
                        "ftp://meetings.boardbook.org/Public/Organization/9999"
                    ),
                },
                db_path=self.db_path,
            ),
            "confirmed_manual": upsert_board_source(
                district(9, "Confirmed Manual Provider"),
                {
                    "platform": "civicclerk",
                    "source_status": "working",
                    "source_url": "http://manual.portal.civicclerk.com/",
                    "raw": {
                        "discovery_method": "manual_entry",
                        "operator_confirmed_district_identity": True,
                        "verified": True,
                    },
                },
                db_path=self.db_path,
            ),
        }
        collision_district = district(10, "HTTPS History Collision")
        rows["collision_http"] = upsert_board_source(
            collision_district,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": (
                    "http://meetings.boardbook.org/Public/Organization/9876"
                ),
                "organization_external_id": "9876",
            },
            db_path=self.db_path,
        )
        rows["collision_https"] = upsert_board_source(
            collision_district,
            {
                "platform": "boardbook",
                "source_status": "working",
                "source_url": (
                    "https://meetings.boardbook.org/Public/Organization/9876"
                ),
                "organization_external_id": "9876",
            },
            db_path=self.db_path,
        )

        preview = audit_legacy_working_board_sources(
            db_path=self.db_path, apply_changes=False
        )
        repaired_ids = {
            rows[name]["id"]
            for name in ("boardbook", "diligent", "simbli", "boarddocs", "civicclerk")
        }
        review_ids = {
            rows[name]["id"]
            for name in (
                "boarddocs_wrapper",
                "civicclerk_wrapper",
                "unsafe_transport",
                "collision_http",
            )
        }
        self.assertEqual(set(preview["repaired_ids"]), repaired_ids)
        self.assertEqual(
            {item["id"] for item in preview["review_required"]}, review_ids
        )

        audit_legacy_working_board_sources(db_path=self.db_path)
        with connect_db(self.db_path) as conn:
            saved = {
                name: dict(
                    conn.execute(
                        "SELECT * FROM board_sources WHERE id = ?", (row["id"],)
                    ).fetchone()
                )
                for name, row in rows.items()
            }
            source_count = conn.execute(
                "SELECT COUNT(*) FROM board_sources WHERE district_id = ?",
                (collision_district,),
            ).fetchone()[0]

        expected_repairs = {
            "boardbook": (
                "https://meetings.boardbook.org/Public/Organization/2413",
                "2413",
                None,
            ),
            "diligent": (
                "https://district.civicweb.net/Portal",
                "district",
                "district",
            ),
            "simbli": (
                "https://simbli.eboardsolutions.com/"
                "SB_Meetings/SB_MeetingListing.aspx?S=SITE_9",
                "SITE_9",
                None,
            ),
            "boarddocs": (
                "https://go.boarddocs.com/or/example/Board.nsf/Public",
                "or/example",
                None,
            ),
            "civicclerk": (
                "https://usbe.portal.civicclerk.com/",
                "usbe",
                "usbe",
            ),
        }
        for name, (url, external_id, tenant) in expected_repairs.items():
            self.assertEqual(saved[name]["source_status"], "working")
            self.assertEqual(saved[name]["source_url"], url)
            self.assertEqual(saved[name]["organization_external_id"], external_id)
            self.assertEqual(saved[name]["platform_tenant"], tenant)
        for name in (
            "boarddocs_wrapper",
            "civicclerk_wrapper",
            "unsafe_transport",
            "collision_http",
        ):
            self.assertEqual(saved[name]["source_status"], "manual_review")
        self.assertEqual(saved["collision_https"]["source_status"], "working")
        self.assertEqual(saved["confirmed_manual"]["source_status"], "working")
        self.assertEqual(
            saved["confirmed_manual"]["source_url"],
            "http://manual.portal.civicclerk.com/",
        )
        self.assertEqual(source_count, 2)

    def test_init_db_migrates_existing_board_sources_to_explicit_current_history(self):
        handle = NamedTemporaryFile(suffix=".db", delete=False)
        legacy_db = Path(handle.name)
        handle.close()
        try:
            legacy_conn = sqlite3.connect(legacy_db)
            try:
                legacy_conn.executescript(
                    """
                    CREATE TABLE board_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        district_id INTEGER NOT NULL,
                        platform TEXT NOT NULL,
                        source_status TEXT NOT NULL,
                        source_url TEXT NOT NULL COLLATE NOCASE,
                        organization_external_id TEXT,
                        platform_tenant TEXT,
                        discovered_from_url TEXT,
                        confidence REAL NOT NULL DEFAULT 0,
                        requires_javascript INTEGER NOT NULL DEFAULT 0,
                        last_discovered_at TEXT,
                        last_successful_sync_at TEXT,
                        last_checked_at TEXT,
                        error_message TEXT,
                        raw_discovery_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(district_id, platform, source_url)
                    );
                    """
                )
                legacy_conn.commit()
            finally:
                legacy_conn.close()
            init_db(legacy_db)
            with connect_db(legacy_db) as conn:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(board_sources)")
                }
                indexes = {
                    row["name"] for row in conn.execute("PRAGMA index_list(board_sources)")
                }
            self.assertIn("is_active", columns)
            self.assertIn("superseded_at", columns)
            self.assertIn("idx_board_sources_district_active", indexes)
        finally:
            legacy_db.unlink(missing_ok=True)

    def test_source_changes_preserve_history_and_mark_one_current_source(self):
        original = self.add_source(
            url="https://meetings.boardbook.org/Public/Organization/1111",
            external_id="1111",
        )
        replacement = self.add_source(
            url="https://meetings.boardbook.org/Public/Organization/2222",
            external_id="2222",
        )
        with connect_db(self.db_path) as conn:
            after_replacement = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM board_sources ORDER BY id"
                )
            ]
        self.assertEqual(len(after_replacement), 2)
        self.assertEqual([row["is_active"] for row in after_replacement], [0, 1])
        self.assertIsNotNone(after_replacement[0]["superseded_at"])

        rediscovered = self.add_source(
            url=original["source_url"],
            external_id="1111",
        )
        with connect_db(self.db_path) as conn:
            final_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM board_sources ORDER BY id"
                )
            ]
        self.assertEqual(rediscovered["id"], original["id"])
        self.assertEqual(replacement["id"], final_rows[1]["id"])
        self.assertEqual([row["is_active"] for row in final_rows], [1, 0])
        self.assertEqual(len(final_rows), 2)

    def test_unverified_alternate_sources_remain_inactive_evidence(self):
        current = self.add_source(
            url="https://meetings.boardbook.org/Public/Organization/1111",
            external_id="1111",
        )
        candidates = (
            ("manual_review", "https://district.example/board", "generic"),
            (
                "requires_javascript",
                "https://go.boarddocs.com/or/example/Board.nsf/Public",
                "boarddocs",
            ),
            (
                "blocked_by_challenge",
                "https://simbli.eboardsolutions.com/Index.aspx?S=1234",
                "simbli",
            ),
            (
                "blocked_by_robots",
                "https://district.example/board/meetings",
                "generic",
            ),
        )
        candidate_rows = [
            self.add_source(url=url, platform=platform, status=status)
            for status, url, platform in candidates
        ]

        with connect_db(self.db_path) as conn:
            active = conn.execute(
                "SELECT * FROM board_sources WHERE district_id = ? AND is_active = 1",
                (self.district_id,),
            ).fetchall()
            all_rows = conn.execute(
                "SELECT * FROM board_sources WHERE district_id = ? ORDER BY id",
                (self.district_id,),
            ).fetchall()

        self.assertEqual([row["id"] for row in active], [current["id"]])
        self.assertEqual(active[0]["source_status"], "working")
        self.assertTrue(all(row["is_active"] == 0 for row in candidate_rows))
        self.assertEqual(len(all_rows), 1 + len(candidates))

    def test_exact_current_source_can_update_health_without_losing_current_status(self):
        current = self.add_source(
            url="https://meetings.boardbook.org/Public/Organization/1111",
            external_id="1111",
        )

        unhealthy = self.add_source(
            url=current["source_url"],
            external_id="1111",
            status="blocked_by_challenge",
        )

        self.assertEqual(unhealthy["id"], current["id"])
        self.assertEqual(unhealthy["source_status"], "blocked_by_challenge")
        self.assertEqual(unhealthy["is_active"], 1)
        self.assertIsNone(unhealthy["superseded_at"])

        replacement = self.add_source(
            url="https://meetings.boardbook.org/Public/Organization/2222",
            external_id="2222",
            status="working",
        )
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM board_sources WHERE district_id = ? ORDER BY id",
                (self.district_id,),
            ).fetchall()

        self.assertEqual([row["is_active"] for row in rows], [0, 1])
        self.assertIsNotNone(rows[0]["superseded_at"])
        self.assertEqual(replacement["is_active"], 1)

    def test_first_reviewable_source_is_current_until_a_working_source_is_found(self):
        candidate = self.add_source(
            url="https://district.example/board",
            platform="generic",
            status="manual_review",
        )

        self.assertEqual(candidate["source_status"], "manual_review")
        self.assertEqual(candidate["is_active"], 1)
        self.assertIsNone(candidate["superseded_at"])

    def test_source_meeting_agenda_and_document_dedup_create_only_real_versions(self):
        source = self.add_source()
        duplicate_source = self.add_source(url=f"{source['source_url']}/")
        self.assertEqual(source["id"], duplicate_source["id"])

        first = persist_meeting_bundle(
            self.district_id,
            source["id"],
            self.normalized_meeting(),
            db_path=self.db_path,
        )
        unchanged = persist_meeting_bundle(
            self.district_id,
            source["id"],
            self.normalized_meeting(),
            db_path=self.db_path,
        )
        revised = persist_meeting_bundle(
            self.district_id,
            source["id"],
            self.normalized_meeting(revised=True),
            db_path=self.db_path,
        )

        self.assertTrue(first["created"])
        self.assertFalse(unchanged["created"])
        self.assertFalse(unchanged["changed"])
        self.assertFalse(unchanged["version_created"])
        self.assertTrue(revised["changed"])
        self.assertEqual(revised["version_number"], 2)

        document = {
            "external_document_id": "packet-9001",
            "agenda_item_external_id": "item-1a",
            "document_type": "packet",
            "title": "Adopted Budget Packet",
            "source_url": "https://district.example/board/budget-packet.txt",
            "filename": "budget-packet.txt",
            "mime_type": "text/plain",
            "http_status": 200,
            "metadata": {
                "redirect_chain": ["https://cdn.example/budget-packet.txt"],
                "insecure_tls": False,
                "tls_mode": "verified",
            },
        }
        first_document = store_board_document(
            self.district_id,
            first["id"],
            document,
            b"Adopted budget allocation for student mental health services.",
            storage_root=self.storage_root,
            db_path=self.db_path,
        )
        same_document = store_board_document(
            self.district_id,
            first["id"],
            document,
            b"Adopted budget allocation for student mental health services.",
            storage_root=self.storage_root,
            db_path=self.db_path,
        )
        changed_document = store_board_document(
            self.district_id,
            first["id"],
            document,
            b"Revised adopted budget allocation for student counseling services.",
            storage_root=self.storage_root,
            db_path=self.db_path,
        )

        self.assertTrue(first_document["created"])
        self.assertTrue(first_document["version_created"])
        self.assertTrue(same_document["unchanged"])
        self.assertFalse(same_document["version_created"])
        self.assertTrue(changed_document["changed"])
        self.assertEqual(changed_document["version_number"], 2)
        self.assertTrue(Path(changed_document["local_path"]).is_file())

        with connect_db(self.db_path) as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
                for table in (
                    "board_sources",
                    "board_meetings",
                    "board_meeting_versions",
                    "board_agenda_items",
                    "board_documents",
                    "board_document_versions",
                )
            }
            child_row = conn.execute(
                "SELECT * FROM board_agenda_items WHERE external_item_id = 'item-1a'"
            ).fetchone()
            parent_row = conn.execute(
                "SELECT * FROM board_agenda_items WHERE external_item_id = 'item-1'"
            ).fetchone()
            meeting_row = conn.execute(
                "SELECT revision_detected, minutes_url FROM board_meetings WHERE id = ?",
                (first["id"],),
            ).fetchone()
            document_row = conn.execute(
                "SELECT retrieval_metadata_json FROM board_documents WHERE id = ?",
                (first_document["id"],),
            ).fetchone()
            document_version = conn.execute(
                """
                SELECT retrieval_metadata_json FROM board_document_versions
                WHERE board_document_id = ? ORDER BY version_number DESC LIMIT 1
                """,
                (first_document["id"],),
            ).fetchone()

        self.assertEqual(
            counts,
            {
                "board_sources": 1,
                "board_meetings": 1,
                "board_meeting_versions": 2,
                "board_agenda_items": 2,
                "board_documents": 1,
                "board_document_versions": 2,
            },
        )
        self.assertEqual(child_row["parent_item_id"], parent_row["id"])
        self.assertEqual(child_row["title"], "Adopt the revised budget")
        self.assertEqual(meeting_row["revision_detected"], 1)
        self.assertIn("/Minutes/", meeting_row["minutes_url"])
        self.assertEqual(json.loads(document_row["retrieval_metadata_json"])["tls_mode"], "verified")
        self.assertEqual(
            json.loads(document_version["retrieval_metadata_json"])["redirect_chain"],
            ["https://cdn.example/budget-packet.txt"],
        )

    def test_document_extraction_reports_supported_empty_unsupported_large_and_failed(self):
        self.assertEqual(
            extract_document_text(
                b"<html><body><p>Public budget hearing</p></body></html>",
                mime_type="text/html",
                filename="hearing.html",
            ).status,
            "extracted",
        )
        self.assertEqual(
            extract_document_text(b"   \n", mime_type="text/plain", filename="empty.txt").status,
            "no_text",
        )
        self.assertEqual(
            extract_document_text(b"\x00\x01\x02", filename="scan.bin").status,
            "unsupported",
        )
        self.assertEqual(
            extract_document_text(b"too long", filename="large.txt", max_bytes=3).status,
            "too_large",
        )
        self.assertEqual(
            extract_document_text(b"%PDF-not-valid", filename="broken.pdf").status,
            "failed",
        )

    def test_full_text_search_filters_and_returns_source_provenance(self):
        source = self.add_source()
        saved = persist_meeting_bundle(
            self.district_id,
            source["id"],
            self.normalized_meeting(revised=True),
            db_path=self.db_path,
        )
        document = store_board_document(
            self.district_id,
            saved["id"],
            {
                "external_document_id": "budget-doc",
                "document_type": "packet",
                "title": "Student Support Budget",
                "source_url": "https://district.example/board/student-support-budget.txt",
                "filename": "student-support-budget.txt",
                "mime_type": "text/plain",
            },
            b"The adopted budget expands counseling and mental health services.",
            storage_root=self.storage_root,
            db_path=self.db_path,
        )
        # A second byte revision marks the document projection as changed too.
        store_board_document(
            self.district_id,
            saved["id"],
            {
                "external_document_id": "budget-doc",
                "document_type": "packet",
                "title": "Student Support Budget",
                "source_url": "https://district.example/board/student-support-budget.txt",
                "filename": "student-support-budget.txt",
                "mime_type": "text/plain",
            },
            b"The revised adopted budget expands counseling and behavioral health services.",
            storage_root=self.storage_root,
            db_path=self.db_path,
        )

        matches = search_board_content(
            '"adopted budget"',
            states=["OR"],
            district_ids=[self.district_id],
            platforms=["boardbook"],
            document_types=["packet"],
            entity_types=["document"],
            date_from="2026-01-01",
            date_to="2026-12-31",
            changed_only=True,
            db_path=self.db_path,
        )
        self.assertEqual(len(matches), 1)
        result = matches[0]
        self.assertEqual(result["entity_type"], "document")
        self.assertEqual(result["document_id"], document["id"])
        self.assertEqual(result["district_name"], "Fixture School District")
        self.assertEqual(result["state"], "OR")
        self.assertEqual(result["platform"], "boardbook")
        self.assertEqual(result["document_type"], "packet")
        self.assertEqual(
            result["source_url"],
            "https://district.example/board/student-support-budget.txt",
        )
        self.assertIn("/Public/Agenda/", result["meeting_source_url"])
        self.assertTrue(result["document_sha256"])
        self.assertIn("budget", (result["excerpt"] or "").casefold())

        agenda_matches = search_board_content(
            "public testimony",
            state="OR",
            district="Fixture School",
            platform="boardbook",
            entity_types=["agenda_item"],
            db_path=self.db_path,
        )
        self.assertEqual(len(agenda_matches), 1)
        self.assertEqual(agenda_matches[0]["agenda_item_title"], "Adopt the revised budget")


class BoardRunTests(BoardDatabaseTestCase):
    def execute_with_adapter(self, run_id: int, db_path: Path, adapter) -> None:
        class NoNetworkRunClient:
            def __init__(self, settings) -> None:
                self.settings = settings

            def get(self, url, **_kwargs) -> HTTPResult:
                return HTTPResult(
                    requested_url=url,
                    final_url=url,
                    status_code=200,
                    headers={"Content-Type": "text/html"},
                    content=fixture_bytes("boardbook_agenda.html"),
                )

            def close(self) -> None:
                return None

        with (
            patch("board.adapters.get_adapter", return_value=adapter),
            patch("board.runs.BoardHTTPClient", NoNetworkRunClient),
        ):
            execute_board_sync_run(
                run_id,
                db_path=db_path,
                document_storage_root=self.storage_root / "documents",
                snapshot_storage_root=self.storage_root / "snapshots",
            )

    def test_new_minutes_url_forces_incremental_refresh(self):
        ref = MeetingRef(
            external_meeting_id="old-meeting",
            url="https://district.example/board/meeting/old-meeting",
            meeting_date="2020-01-10",
            agenda_url="https://district.example/board/meeting/old-meeting/agenda",
        )
        existing = {
            "meeting_date": "2020-01-10",
            "agenda_url": ref.agenda_url,
            "minutes_url": None,
            "packet_url": None,
            "video_url": None,
            "last_checked_at": utc_now_iso(),
        }

        self.assertFalse(
            _meeting_needs_refresh(ref, existing, sync_mode="monitor", force=False)
        )
        ref.minutes_url = "https://district.example/board/meeting/old-meeting/minutes"
        self.assertTrue(
            _meeting_needs_refresh(ref, existing, sync_mode="monitor", force=False)
        )

    def test_discovery_and_sync_runs_persist_an_exact_planned_item_ledger(self):
        unchecked_or = self.add_district(
            "4100002", "Unchecked Oregon District", website="https://unchecked.example/"
        )
        washington = self.add_district(
            "5300001", "Washington District", state="WA", website="https://wa.example/"
        )
        source = self.add_source()
        self.add_source(
            washington,
            external_id="5301",
            url="https://meetings.boardbook.org/Public/Organization/5301",
        )

        discovery_run = create_board_discovery_run(
            states=["OR"],
            force=False,
            max_districts=10,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        forced_discovery_run = create_board_discovery_run(
            states=["OR"],
            force=True,
            max_districts=10,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        sync_run = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            source_status="working",
            date_from="2026-01-01",
            date_to="2026-12-31",
            max_districts=10,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )

        with connect_db(self.db_path) as conn:
            discovery = conn.execute(
                "SELECT * FROM board_discovery_runs WHERE id = ?", (discovery_run,)
            ).fetchone()
            discovery_items = conn.execute(
                "SELECT district_id, status FROM board_discovery_run_items WHERE run_id = ?",
                (discovery_run,),
            ).fetchall()
            forced_items = conn.execute(
                "SELECT district_id FROM board_discovery_run_items WHERE run_id = ? ORDER BY district_id",
                (forced_discovery_run,),
            ).fetchall()
            sync = conn.execute(
                "SELECT * FROM board_sync_runs WHERE id = ?", (sync_run,)
            ).fetchone()
            sync_items = conn.execute(
                "SELECT district_id, board_source_id, status FROM board_sync_run_items WHERE run_id = ?",
                (sync_run,),
            ).fetchall()

        self.assertEqual(discovery["status"], "queued")
        self.assertEqual(discovery["network_ipv4_only"], 1)
        self.assertEqual(discovery["network_https_only"], 1)
        self.assertEqual(discovery["districts_matched"], 1)
        self.assertEqual(discovery["districts_planned"], 1)
        self.assertEqual(
            [(row["district_id"], row["status"]) for row in discovery_items],
            [(unchecked_or, "queued")],
        )
        self.assertEqual(
            [row["district_id"] for row in forced_items],
            sorted([self.district_id, unchecked_or]),
        )
        self.assertEqual(sync["status"], "queued")
        self.assertEqual(sync["districts_matched"], 1)
        self.assertEqual(sync["districts_planned"], 1)
        self.assertEqual(
            [(row["district_id"], row["board_source_id"], row["status"]) for row in sync_items],
            [(self.district_id, source["id"], "queued")],
        )

    def test_discovery_run_persists_provider_directory_opt_in(self):
        with patch("board.runs.provider_directory_enabled", return_value=True):
            run_id = create_board_discovery_run(
                states=["OR"],
                force=True,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )
        with connect_db(self.db_path) as conn:
            requested = conn.execute(
                "SELECT provider_directory_requested FROM board_discovery_runs WHERE id = ?",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(requested, 1)

    def test_discovery_run_persists_explicit_search_fallback_choice(self):
        with patch("board.runs.has_brave_search_api_key", return_value=True):
            enabled_run_id = create_board_discovery_run(
                states=["OR"],
                force=True,
                search_fallback=True,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )
            disabled_run_id = create_board_discovery_run(
                states=["OR"],
                force=True,
                search_fallback=False,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )
        with patch("board.runs.has_brave_search_api_key", return_value=False):
            unavailable_run_id = create_board_discovery_run(
                states=["OR"],
                force=True,
                search_fallback=True,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )

        with connect_db(self.db_path) as conn:
            choices = {
                row["id"]: row["search_fallback_requested"]
                for row in conn.execute(
                    """
                    SELECT id, search_fallback_requested
                    FROM board_discovery_runs WHERE id IN (?, ?, ?)
                    """,
                    (enabled_run_id, disabled_run_id, unavailable_run_id),
                )
            }

        self.assertEqual(choices[enabled_run_id], 1)
        self.assertEqual(choices[disabled_run_id], 0)
        self.assertEqual(choices[unavailable_run_id], 0)

    def test_legacy_discovery_run_does_not_implicitly_enable_search_fallback(self):
        class NoNetworkClient:
            def __init__(self, settings) -> None:
                self.settings = settings

            def close(self) -> None:
                return None

        outcome = DiscoveryOutcome(
            status="working",
            platform="boardbook",
            source_url="https://meetings.boardbook.org/Public/Organization/2221",
            organization_external_id="2221",
            confidence=99,
        )
        with (
            patch("board.runs.has_brave_search_api_key", return_value=True),
            patch("board.runs.provider_directory_enabled", return_value=False),
        ):
            run_id = create_board_discovery_run(
                states=["OR"],
                force=True,
                search_fallback=False,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )
            with connect_db(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE board_discovery_runs
                    SET search_fallback_requested = NULL
                    WHERE id = ?
                    """,
                    (run_id,),
                )
                conn.commit()
            with (
                patch("board.runs.BoardHTTPClient", NoNetworkClient),
                patch(
                    "board.runs.discover_board_source",
                    return_value=outcome,
                ) as discover,
            ):
                execute_board_discovery_run(run_id, db_path=self.db_path)

        self.assertFalse(discover.call_args.kwargs["search_fallback"])
        with connect_db(self.db_path) as conn:
            persisted = conn.execute(
                """
                SELECT search_fallback_requested
                FROM board_discovery_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(persisted, 0)

    def test_discovery_transport_error_counts_as_failure_not_not_found(self):
        run_id = create_board_discovery_run(
            states=["OR"],
            force=False,
            max_districts=10,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )

        class NoNetworkClient:
            def __init__(self, settings) -> None:
                self.settings = settings

            def close(self) -> None:
                return None

        outcome = DiscoveryOutcome(
            status="error",
            platform="unknown",
            source_url="https://district.example/",
            discovered_from_url="https://district.example/",
            error_message="No district page could be inspected because every transport attempt failed.",
            raw={"fetch_errors": [{"kind": "transport", "error": "fixture TLS failure"}]},
        )
        with (
            patch("board.runs.BoardHTTPClient", NoNetworkClient),
            patch("board.runs.discover_board_source", return_value=outcome),
        ):
            execute_board_discovery_run(run_id, db_path=self.db_path)

        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM board_discovery_runs WHERE id = ?", (run_id,)
            ).fetchone()
            item = conn.execute(
                "SELECT * FROM board_discovery_run_items WHERE run_id = ?", (run_id,)
            ).fetchone()
            source = conn.execute(
                "SELECT * FROM board_sources WHERE id = ?", (item["board_source_id"],)
            ).fetchone()

        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["districts_processed"], 1)
        self.assertEqual(run["sources_failed"], 1)
        self.assertEqual(run["sources_not_found"], 0)
        self.assertEqual(item["status"], "error")
        self.assertEqual(source["source_status"], "error")
        self.assertEqual(source["is_active"], 0)

    def test_mixed_discovery_run_reports_errors_and_logs_terminal_outcomes(self):
        self.add_district(
            "4100002",
            "Second Fixture District",
            website="https://second.example/",
        )

        class NoNetworkClient:
            def __init__(self, settings) -> None:
                self.settings = settings

            def close(self) -> None:
                return None

        outcomes = [
            DiscoveryOutcome(
                status="working",
                platform="boardbook",
                source_url="https://meetings.boardbook.org/Public/Organization/2221",
                organization_external_id="2221",
                confidence=99,
                raw={
                    "candidate_count": 2,
                    "visited_urls": ["https://district.example/"],
                    "successful_page_fetches": 1,
                    "website_migration": {
                        "original_url": "https://old-district.example/",
                        "final_url": "https://district.example/",
                    },
                },
            ),
            DiscoveryOutcome(
                status="error",
                platform="unknown",
                source_url="https://second.example/",
                error_message="fixture transport failure",
                raw={
                    "visited_urls": ["https://second.example/"],
                    "successful_page_fetches": 0,
                    "fetch_errors": [{"kind": "transport", "error": "TLS"}],
                },
            ),
        ]
        log_root = self.storage_root / "discovery-logs"
        with (
            patch("board.runs.BOARD_DISCOVERY_RUN_LOGS_DIR", log_root),
            patch("board.runs.BoardHTTPClient", NoNetworkClient),
            patch("board.runs.provider_directory_enabled", return_value=False),
            patch("board.runs.discover_board_source", side_effect=outcomes),
        ):
            run_id = create_board_discovery_run(
                states=["OR"],
                max_workers=1,
                debug_logging=True,
                db_path=self.db_path,
            )
            execute_board_discovery_run(run_id, db_path=self.db_path)

        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM board_discovery_runs WHERE id = ?", (run_id,)
            ).fetchone()
            moved_item = conn.execute(
                """
                SELECT website_original_url, website_final_url
                FROM board_discovery_run_items
                WHERE run_id = ? AND website_original_url IS NOT NULL
                """,
                (run_id,),
            ).fetchone()
        log_text = (log_root / f"run-{run_id}.log").read_text(encoding="utf-8")

        self.assertEqual(run["status"], "completed_with_errors")
        self.assertEqual(run["sources_working"], 1)
        self.assertEqual(run["sources_failed"], 1)
        self.assertEqual(run["website_moves_accepted"], 1)
        self.assertEqual(
            (moved_item["website_original_url"], moved_item["website_final_url"]),
            ("https://old-district.example/", "https://district.example/"),
        )
        self.assertEqual(log_text.count("board_discovery_outcome"), 2)
        self.assertIn('provider_directory_requested=false', log_text)
        self.assertIn('status="completed_with_errors"', log_text)
        self.assertIn('sources_failed=1', log_text)

    def test_discovery_setup_failure_terminalizes_every_planned_item(self):
        self.add_district(
            "4100002",
            "Second Fixture District",
            website="https://second.example/",
        )
        log_root = self.storage_root / "discovery-setup-failure-logs"
        with patch("board.runs.BOARD_DISCOVERY_RUN_LOGS_DIR", log_root):
            run_id = create_board_discovery_run(
                states=["OR"],
                max_workers=1,
                debug_logging=True,
                db_path=self.db_path,
            )
        with patch(
            "board.runs.BoardHTTPClient",
            side_effect=RuntimeError("fixture client setup failure"),
        ):
            execute_board_discovery_run(run_id, db_path=self.db_path)

        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM board_discovery_runs WHERE id = ?", (run_id,)
            ).fetchone()
            items = conn.execute(
                """
                SELECT status, error_message, finished_at
                FROM board_discovery_run_items WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        log_text = (log_root / f"run-{run_id}.log").read_text(encoding="utf-8")

        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["districts_processed"], 2)
        self.assertEqual(run["sources_failed"], 2)
        self.assertEqual([row["status"] for row in items], ["failed", "failed"])
        self.assertTrue(all(row["finished_at"] for row in items))
        self.assertTrue(
            all("client setup failure" in row["error_message"] for row in items)
        )
        self.assertIn("board_discovery_error", log_text)
        self.assertIn('status="failed"', log_text)
        self.assertIn("board_discovery_finished", log_text)

    def test_queued_sync_item_is_cancelled_if_its_source_was_superseded(self):
        original = self.add_source()
        run_id = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        replacement = self.add_source(
            external_id="replacement",
            url="https://meetings.boardbook.org/Public/Organization/replacement",
        )
        self.assertNotEqual(replacement["id"], original["id"])

        class NoNetworkClient:
            def __init__(self, settings) -> None:
                self.settings = settings

            def close(self) -> None:
                return None

        with (
            patch("board.runs.BoardHTTPClient", NoNetworkClient),
            patch("board.adapters.get_adapter") as get_adapter,
        ):
            execute_board_sync_run(
                run_id,
                db_path=self.db_path,
                document_storage_root=self.storage_root / "documents",
                snapshot_storage_root=self.storage_root / "snapshots",
            )

        get_adapter.assert_not_called()
        with connect_db(self.db_path) as conn:
            item = conn.execute(
                "SELECT * FROM board_sync_run_items WHERE run_id = ?", (run_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
        self.assertEqual(item["status"], "cancelled")
        self.assertIn("inactive", item["error_message"])
        self.assertEqual(run["districts_processed"], 1)
        self.assertEqual(run["status"], "completed")

    def test_boardbook_sync_is_idempotent_across_persistent_runs(self):
        self.add_source()
        adapter = FixtureBoardBookSyncAdapter()

        run_ids: list[int] = []
        for _ in range(2):
            run_id = create_board_sync_run(
                states=["OR"],
                platforms=["boardbook"],
                date_from="2026-01-01",
                date_to="2026-12-31",
                force=True,
                max_workers=1,
                debug_logging=False,
                db_path=self.db_path,
            )
            self.execute_with_adapter(run_id, self.db_path, adapter)
            run_ids.append(run_id)

        with connect_db(self.db_path) as conn:
            runs = [
                dict(conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone())
                for run_id in run_ids
            ]
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
                for table in (
                    "board_meetings",
                    "board_meeting_versions",
                    "board_agenda_items",
                    "board_documents",
                    "board_document_versions",
                )
            }
            local_paths = [
                row["local_path"]
                for row in conn.execute(
                    "SELECT local_path FROM board_documents WHERE local_path IS NOT NULL"
                )
            ]
            snapshot_paths = [
                row["raw_snapshot_path"]
                for row in conn.execute(
                    """
                    SELECT raw_snapshot_path FROM board_meeting_versions
                    WHERE raw_snapshot_path IS NOT NULL
                    """
                )
            ]

        self.assertEqual([run["status"] for run in runs], ["completed", "completed"])
        self.assertEqual(runs[0]["meetings_added"], 1)
        self.assertEqual(runs[0]["documents_added"], 2)
        self.assertEqual(runs[1]["meetings_discovered"], 1)
        self.assertEqual(runs[1]["meetings_added"], 0)
        self.assertEqual(runs[1]["meetings_updated"], 0)
        self.assertEqual(runs[1]["documents_added"], 0)
        self.assertEqual(runs[1]["documents_updated"], 0)
        self.assertEqual(
            counts,
            {
                "board_meetings": 1,
                "board_meeting_versions": 1,
                "board_agenda_items": 4,
                "board_documents": 2,
                "board_document_versions": 2,
            },
        )
        self.assertTrue(local_paths)
        self.assertTrue(snapshot_paths)
        for path in [*local_paths, *snapshot_paths]:
            self.assertTrue(Path(path).resolve().is_relative_to(self.storage_root.resolve()))

    def test_malformed_first_meeting_does_not_block_later_meetings(self):
        source_row = self.add_source()
        previous_success = "2025-02-03T04:05:06+00:00"
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE board_sources SET last_successful_sync_at = ? WHERE id = ?",
                (previous_success, source_row["id"]),
            )
            conn.commit()

        class PartiallyMalformedAdapter:
            def list_meetings(_self, source, since=None):
                base = source.public_url.rstrip("/")
                return [
                    MeetingRef(
                        external_meeting_id="malformed-first",
                        url=f"{base}/malformed-first",
                        meeting_date="2026-08-20",
                        agenda_url=f"{base}/malformed-first",
                    ),
                    MeetingRef(
                        external_meeting_id="valid-second",
                        url=f"{base}/valid-second",
                        meeting_date="2026-08-21",
                        agenda_url=f"{base}/valid-second",
                    ),
                ]

            def fetch_meeting(_self, source, meeting_ref):
                if meeting_ref.external_meeting_id == "malformed-first":
                    raise ValueError("malformed agenda payload")
                meeting = BoardDatabaseTestCase.normalized_meeting()
                meeting.external_meeting_id = meeting_ref.external_meeting_id
                meeting.source_url = meeting_ref.url
                meeting.agenda_url = meeting_ref.agenda_url
                return meeting

        run_id = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            date_from="2026-01-01",
            force=True,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        self.execute_with_adapter(run_id, self.db_path, PartiallyMalformedAdapter())

        with connect_db(self.db_path) as conn:
            run = conn.execute(
                "SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            item = conn.execute(
                "SELECT * FROM board_sync_run_items WHERE run_id = ?", (run_id,)
            ).fetchone()
            source = conn.execute("SELECT * FROM board_sources").fetchone()
            meetings = [
                row["external_meeting_id"]
                for row in conn.execute(
                    "SELECT external_meeting_id FROM board_meetings ORDER BY id"
                )
            ]

        self.assertEqual(meetings, ["valid-second"])
        self.assertEqual(item["status"], "completed_with_errors")
        self.assertEqual(item["meetings_discovered"], 2)
        self.assertEqual(item["meetings_added"], 1)
        self.assertIn("malformed-first", item["error_message"])
        self.assertEqual(source["source_status"], "platform_changed_or_parser_broken")
        self.assertEqual(source["last_successful_sync_at"], previous_success)
        self.assertEqual(run["failures"], 1)

    def test_cancellation_keeps_a_meeting_saved_before_the_signal(self):
        source = self.add_source()
        previous_success = "2025-01-02T03:04:05+00:00"
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE board_sources SET last_successful_sync_at = ? WHERE id = ?",
                (previous_success, source["id"]),
            )
            conn.commit()
        run_id = create_board_sync_run(
            states=["OR"],
            date_from="2026-01-01",
            force=True,
            max_workers=1,
            debug_logging=False,
            db_path=self.db_path,
        )
        adapter = FixtureBoardBookSyncAdapter(
            cancel_db_path=self.db_path,
            cancel_run_id=run_id,
        )
        self.execute_with_adapter(run_id, self.db_path, adapter)

        with connect_db(self.db_path) as conn:
            run = conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone()
            item = conn.execute(
                "SELECT * FROM board_sync_run_items WHERE run_id = ?", (run_id,)
            ).fetchone()
            meetings = conn.execute("SELECT COUNT(*) AS count FROM board_meetings").fetchone()["count"]
            documents = conn.execute("SELECT COUNT(*) AS count FROM board_documents").fetchone()["count"]
            source_after = conn.execute(
                "SELECT * FROM board_sources WHERE id = ?", (source["id"],)
            ).fetchone()

        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(meetings, 1)
        self.assertEqual(documents, 0)
        self.assertEqual(source_after["last_successful_sync_at"], previous_success)

    def test_one_source_failure_does_not_discard_another_sources_results(self):
        bad_district = self.add_district(
            "4100003", "Broken Source District", website="https://broken.example/"
        )
        self.add_source(external_id="good")
        self.add_source(
            bad_district,
            external_id="bad",
            url="https://meetings.boardbook.org/Public/Organization/9999",
        )
        run_id = create_board_sync_run(
            states=["OR"],
            platforms=["boardbook"],
            date_from="2026-01-01",
            force=True,
            max_workers=2,
            debug_logging=False,
            db_path=self.db_path,
        )
        adapter = FixtureBoardBookSyncAdapter(fail_external_source_ids={"bad"})
        self.execute_with_adapter(run_id, self.db_path, adapter)

        with connect_db(self.db_path) as conn:
            run = conn.execute("SELECT * FROM board_sync_runs WHERE id = ?", (run_id,)).fetchone()
            statuses = [
                row["status"]
                for row in conn.execute(
                    "SELECT status FROM board_sync_run_items WHERE run_id = ? ORDER BY district_id",
                    (run_id,),
                )
            ]
            saved_districts = [
                row["district_id"]
                for row in conn.execute("SELECT district_id FROM board_meetings ORDER BY district_id")
            ]

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["failures"], 1)
        self.assertCountEqual(statuses, ["completed", "failed"])
        self.assertEqual(saved_districts, [self.district_id])


if __name__ == "__main__":
    unittest.main()
