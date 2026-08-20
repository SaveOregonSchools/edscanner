from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from typing import Any

from board.adapters import detect_platform, get_adapter
from board.adapters.base import looks_like_blocked_page
from board.discovery import discover_board_source
from board.http import BoardHTTPClient
from board.models import BoardSource
from common import connect_db, init_db


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _source_for_district(district_id: int, client: BoardHTTPClient, browser: bool) -> BoardSource:
    with connect_db() as conn:
        district = conn.execute("SELECT * FROM districts WHERE id = ?", (district_id,)).fetchone()
        if district is None:
            raise SystemExit(f"District not found: {district_id}")
        source = conn.execute(
            """
            SELECT * FROM board_sources WHERE district_id = ? AND source_status = 'working'
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (district_id,),
        ).fetchone()
    if source is not None:
        return BoardSource(
            platform=source["platform"],
            public_url=source["source_url"],
            external_source_id=source["organization_external_id"],
            district_id=district_id,
            organization_name=district["agency_name"],
            status=source["source_status"],
            requires_javascript=bool(source["requires_javascript"]),
            metadata={"platform_tenant": source["platform_tenant"]},
        )
    outcome = discover_board_source(
        dict(district),
        client=client,
        allow_browser_fallback=browser,
    )
    if outcome.source is None:
        raise SystemExit(
            f"No working source discovered ({outcome.status}): {outcome.error_message or outcome.source_url}"
        )
    return outcome.source


def _source_for_url(url: str, client: BoardHTTPClient, browser: bool) -> BoardSource:
    response = client.get(url, raise_for_status=False)
    detection = detect_platform(response.final_url, response.content)
    if not detection.matched:
        detection = detect_platform(url)
    if not detection.matched:
        raise SystemExit(f"No supported public board platform detected: {detection.reason}")
    adapter = get_adapter(
        detection.platform,
        client=client,
        allow_browser_fallback=browser,
    )
    content = response.content
    final_url = response.final_url
    if response.status_code >= 400 or looks_like_blocked_page(content):
        if browser and (adapter.requires_javascript or detection.requires_javascript):
            content = adapter.render_page(url)
            final_url = url
        else:
            response.raise_for_status()
    return adapter.parse_source(content, final_url)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe one public school-board source without writing meetings or documents."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="Public board organization/listing URL")
    target.add_argument("--district-id", type=int, help="Imported EdScanner district ID")
    parser.add_argument("--since", help="Only list meetings on or after YYYY-MM-DD")
    parser.add_argument("--list-limit", type=int, default=10, help="Meeting references to print (default: 10)")
    parser.add_argument("--no-fetch-meeting", action="store_true", help="Do not fetch and normalize the first listed meeting")
    parser.add_argument("--browser", action="store_true", help="Allow Playwright fallback for public JavaScript pages")
    args = parser.parse_args()

    init_db()
    with BoardHTTPClient() as client:
        source = (
            _source_for_url(args.url, client, args.browser)
            if args.url
            else _source_for_district(args.district_id, client, args.browser)
        )
        adapter = get_adapter(
            source.platform,
            client=client,
            allow_browser_fallback=args.browser,
        )
        meetings = adapter.list_meetings(source, since=args.since)
        payload: dict[str, Any] = {
            "source": _jsonable(source),
            "meeting_count": len(meetings),
            "meetings": _jsonable(meetings[: max(0, args.list_limit)]),
        }
        if meetings and not args.no_fetch_meeting:
            payload["sample_meeting"] = _jsonable(adapter.fetch_meeting(source, meetings[0]))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
