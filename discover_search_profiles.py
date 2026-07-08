from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import configure_logging, connect_db, init_db
from search_engine import SearchSettings, list_matching_districts
from site_search_discovery import discover_profiles_for_districts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover built-in district website search profiles.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path. Defaults to EdScanner's configured DB.")
    parser.add_argument("--state", action="append", dest="states", default=[], help="State abbreviation. Can be repeated.")
    parser.add_argument("--agency-type", action="append", dest="agency_types", default=[], help="Agency type. Can be repeated.")
    parser.add_argument("--min-enrollment", type=int, default=None)
    parser.add_argument("--max-enrollment", type=int, default=None)
    parser.add_argument("--limit", type=int, default=25, help="Maximum districts to test.")
    parser.add_argument("--district-id", type=int, default=None, help="Discover one district by internal database ID.")
    parser.add_argument("--test-query", default="calendar")
    parser.add_argument("--force", action="store_true", help="Rediscover districts that already have working profiles.")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(debug=args.debug)
    init_db(args.db)
    started = time.monotonic()

    if args.district_id is not None:
        with connect_db(args.db) as conn:
            row = conn.execute("SELECT * FROM districts WHERE id = ?", (args.district_id,)).fetchone()
        districts = [dict(row)] if row else []
    else:
        districts = list_matching_districts(
            args.states,
            args.agency_types,
            args.min_enrollment,
            args.max_enrollment,
            limit=max(1, args.limit),
            db_path=args.db,
        )

    if not districts:
        print("No matching districts found.")
        return 1

    summary = discover_profiles_for_districts(
        districts,
        test_query=args.test_query,
        settings=SearchSettings(max_pages_per_district=10),
        force=args.force,
        limit=max(1, args.limit),
        db_path=args.db,
    )
    elapsed = time.monotonic() - started
    print(f"Districts matched: {summary['districts_matched']}")
    print(f"Districts tested: {summary['districts_tested']}")
    print(f"Working profiles: {summary['working_profiles']}")
    print(f"No search found: {summary['no_search_found']}")
    print(f"Manual review: {summary['manual_review']}")
    print(f"Errors/failed: {summary['errors']}")
    if summary["statuses"]:
        print("Statuses:")
        for status, count in sorted(summary["statuses"].items()):
            print(f"  {status}: {count}")
    if summary["provider_guesses"]:
        print("Provider guesses:")
        for provider, count in sorted(summary["provider_guesses"].items()):
            print(f"  {provider}: {count}")
    print(f"Elapsed seconds: {elapsed:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
