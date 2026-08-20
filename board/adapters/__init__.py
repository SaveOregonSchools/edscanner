from __future__ import annotations

from collections.abc import Iterable

from board.http import BoardHTTPClient
from board.models import DetectionResult

from .base import BoardPlatformAdapter, Content
from .boardbook import BoardBookAdapter, resolve_boardbook_document_url
from .boarddocs import BoardDocsAdapter
from .civicclerk import CivicClerkAdapter
from .diligent_community import DiligentCommunityAdapter
from .generic import GenericBoardAdapter
from .simbli import SimbliAdapter


ADAPTER_CLASSES: tuple[type[BoardPlatformAdapter], ...] = (
    BoardBookAdapter,
    DiligentCommunityAdapter,
    BoardDocsAdapter,
    SimbliAdapter,
    CivicClerkAdapter,
    GenericBoardAdapter,
)

_PLATFORM_ALIASES = {
    "boardbook": "boardbook",
    "boardbook_premier": "boardbook",
    "diligent": "diligent_community",
    "diligent_community": "diligent_community",
    "community": "diligent_community",
    "boarddocs": "boarddocs",
    "legacy_boarddocs": "boarddocs",
    "simbli": "simbli",
    "eboardsolutions": "simbli",
    "gamut": "simbli",
    "civicclerk": "civicclerk",
    "civic_clerk": "civicclerk",
    "generic": "generic",
}


def build_adapters(
    client: BoardHTTPClient | None = None,
    *,
    allow_browser_fallback: bool = False,
) -> list[BoardPlatformAdapter]:
    shared_client = client or BoardHTTPClient()
    return [
        adapter_class(shared_client, allow_browser_fallback=allow_browser_fallback)
        for adapter_class in ADAPTER_CLASSES
    ]


def detect_platform(
    url: str,
    html: Content | None = None,
    adapters: Iterable[BoardPlatformAdapter] | None = None,
) -> DetectionResult:
    candidates = list(adapters) if adapters is not None else build_adapters()
    results: list[DetectionResult] = []
    for adapter in candidates:
        try:
            result = adapter.detect(url, html)
        except Exception:
            continue
        if result.matched:
            results.append(result)
    if not results:
        return DetectionResult(
            matched=False,
            platform="unknown",
            confidence=0.0,
            reason="No board platform adapter matched the URL or public markup.",
        )
    return max(results, key=lambda item: item.confidence)


def get_adapter(
    platform: str,
    client: BoardHTTPClient | None = None,
    *,
    allow_browser_fallback: bool = False,
) -> BoardPlatformAdapter:
    key = str(platform or "").strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = _PLATFORM_ALIASES.get(key)
    if normalized is None:
        raise KeyError(f"Unsupported board platform: {platform}")
    for adapter in build_adapters(client, allow_browser_fallback=allow_browser_fallback):
        if adapter.platform_name == normalized:
            return adapter
    raise KeyError(f"Unsupported board platform: {platform}")


adapter_for_platform = get_adapter


__all__ = [
    "ADAPTER_CLASSES",
    "BoardBookAdapter",
    "BoardDocsAdapter",
    "BoardPlatformAdapter",
    "CivicClerkAdapter",
    "DiligentCommunityAdapter",
    "GenericBoardAdapter",
    "SimbliAdapter",
    "adapter_for_platform",
    "build_adapters",
    "detect_platform",
    "get_adapter",
    "resolve_boardbook_document_url",
]
