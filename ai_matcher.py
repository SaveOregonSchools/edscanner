from __future__ import annotations


def analyze_match(
    query_text: str,
    page_title: str,
    url: str,
    snippet: str,
    full_text: str | None = None,
) -> dict[str, object]:
    return {
        "is_likely_match": None,
        "confidence": None,
        "explanation": None,
    }
