from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from common import (
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    OLLAMA_ENDPOINTS_ENV,
    OLLAMA_MODEL_ENV,
    get_local_setting,
)


LOGGER = logging.getLogger(__name__)


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


def parse_ollama_endpoints(value: str) -> list[str]:
    """Return unique Ollama server roots in configured priority order."""

    raw = str(value or "").strip()
    if not raw:
        return []
    values: list[Any]
    try:
        parsed = json.loads(raw)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        values = re.split(r"[\r\n,;]+", raw)

    endpoints: list[str] = []
    for item in values:
        endpoint = str(item or "").strip().rstrip("/")
        for suffix in ("/api/chat", "/api/generate", "/api/tags", "/v1"):
            if endpoint.casefold().endswith(suffix):
                endpoint = endpoint[: -len(suffix)].rstrip("/")
                break
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def get_ollama_endpoints() -> list[str]:
    configured = get_local_setting(OLLAMA_ENDPOINTS_ENV).strip()
    if configured:
        return parse_ollama_endpoints(configured)
    # Compatibility with the earlier single-endpoint setting.
    return parse_ollama_endpoints(get_local_setting(LLM_BASE_URL_ENV))


def get_ollama_model() -> str:
    return (
        get_local_setting(OLLAMA_MODEL_ENV).strip()
        or get_local_setting(LLM_MODEL_ENV).strip()
    )


def local_llm_is_configured() -> bool:
    return bool(get_ollama_endpoints() and get_ollama_model())


def test_ollama_endpoints(
    endpoints: list[str] | None = None,
    timeout_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    """Query each configured server without sending document content."""

    results: list[dict[str, Any]] = []
    for base_url in endpoints if endpoints is not None else get_ollama_endpoints():
        endpoint = f"{base_url.rstrip('/')}/api/tags"
        try:
            response = requests.get(endpoint, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()
            models = [
                str(item.get("name", "")).strip()
                for item in body.get("models", [])
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ]
            results.append({"endpoint": base_url, "ok": True, "models": models, "error": ""})
        except Exception as exc:
            results.append({"endpoint": base_url, "ok": False, "models": [], "error": str(exc)})
    return results


def _json_from_message(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.casefold().startswith("json"):
            value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def analyze_contract_candidate(
    *,
    district_name: str,
    title: str,
    url: str,
    parent_context: str,
    text_excerpt: str,
    timeout_seconds: float = 45.0,
) -> dict[str, Any] | None:
    """Classify one candidate with the configured native Ollama servers.

    The deterministic classifier remains authoritative when no server is configured
    or when every server fails. Servers are attempted in priority order. This
    function intentionally sends a bounded excerpt instead of a complete agreement.
    """

    base_urls = get_ollama_endpoints()
    model = get_ollama_model()
    if not base_urls or not model:
        return None
    api_key = get_local_setting(LLM_API_KEY_ENV).strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    system_prompt = (
        "You classify public-school labor agreement documents. Return only one JSON object. "
        "Never assume an expired agreement remains current. Use unknown when evidence is insufficient."
    )
    user_prompt = f"""District: {district_name}
Title: {title}
URL: {url}
Parent page/link context: {parent_context[:3000]}
Document excerpt: {text_excerpt[:12000]}

Return these keys:
- is_labor_agreement_document: boolean
- bargaining_unit_type: one of licensed, classified, substitute, administrators, transportation, service, other, unknown
- bargaining_unit_name: string or null
- union_name: string or null
- document_type: one of base_agreement, extension, mou, amendment, salary_schedule, tentative_agreement, landing_page, other
- effective_date: YYYY-MM-DD or null
- expiration_date: YYYY-MM-DD or null
- confidence: number from 0 to 1
- explanation: short string grounded in the supplied evidence
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 900},
    }
    errors: list[str] = []
    for base_url in base_urls:
        endpoint = f"{base_url.rstrip('/')}/api/chat"
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()
            message = body.get("message") if isinstance(body, dict) else None
            content = message.get("content", "") if isinstance(message, dict) else ""
            result = _json_from_message(content)
            if result is None:
                raise ValueError("Ollama returned no usable JSON object")
            return result
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
            LOGGER.warning("Ollama contract classification failed via %s for %s: %s", base_url, url, exc)
    LOGGER.warning("All configured Ollama servers failed for %s: %s", url, "; ".join(errors))
    return None
