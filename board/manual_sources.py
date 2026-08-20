from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from board.adapters import build_adapters, detect_platform, get_adapter
from board.adapters.base import assess_challenge, content_text, html_soup
from board.http import BoardHTTPClient


AUTO_PLATFORM = "auto"


class ManualSourceValidationError(ValueError):
    """Raised when a submitted source cannot be safely fetched for validation."""


@dataclass(frozen=True, slots=True)
class ManualSourceValidation:
    platform: str
    source_url: str
    source_status: str
    confidence: float
    requires_javascript: bool
    organization_external_id: str | None
    platform_tenant: str | None
    error_message: str | None
    raw_discovery_json: dict[str, Any]
    verified: bool

    def as_storage_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "source_url": self.source_url,
            "source_status": self.source_status,
            "confidence": self.confidence,
            "requires_javascript": self.requires_javascript,
            "organization_external_id": self.organization_external_id,
            "platform_tenant": self.platform_tenant,
            "error_message": self.error_message,
            "raw_discovery_json": self.raw_discovery_json,
        }


def _confidence_percent(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence <= 1.0:
        confidence *= 100.0
    return max(0.0, min(100.0, confidence))


def _endpoint_has_platform_evidence(
    platform: str,
    content: bytes | str,
    parsed_source: Any,
) -> bool:
    raw_text = content_text(content)
    visible_text = html_soup(raw_text).get_text(" ", strip=True)
    if len(visible_text.strip()) < 20:
        return False
    text = raw_text.casefold()
    if platform == "boardbook":
        external_id = getattr(parsed_source, "external_source_id", None)
        return bool(
            external_id
            and (
                "boardbook premier" in text
                or "publicmeetingstable" in text
                or "displayheader" in text
            )
        )
    return True


def _validate_manual_board_source_with_client(
    district: Mapping[str, Any],
    source_url: str,
    platform: str = AUTO_PLATFORM,
    *,
    operator_confirmed: bool = False,
    client: BoardHTTPClient,
) -> ManualSourceValidation:
    requested_url = str(source_url or "").strip()
    if not requested_url:
        raise ManualSourceValidationError("Enter the public board source URL.")
    if len(requested_url) > 2_048:
        raise ManualSourceValidationError("The board source URL is too long.")

    requested_platform = str(platform or AUTO_PLATFORM).strip().casefold()
    http_client = client
    adapters = build_adapters(http_client)
    supported = {adapter.platform_name for adapter in adapters}
    if requested_platform != AUTO_PLATFORM and requested_platform not in supported:
        raise ManualSourceValidationError(
            "Select a supported board platform or Auto-detect."
        )
    try:
        response = http_client.get(
            requested_url,
            force=True,
            check_robots=True,
            raise_for_status=False,
        )
    except Exception as exc:
        raise ManualSourceValidationError(
            f"The public URL could not be safely retrieved: {exc}"
        ) from exc

    final_url = response.final_url
    content = response.content
    if requested_platform == AUTO_PLATFORM:
        detection = detect_platform(final_url, content, adapters=adapters)
        selected_platform = detection.platform if detection.matched else "generic"
        adapter = next(
            (candidate for candidate in adapters if candidate.platform_name == selected_platform),
            None,
        )
    else:
        selected_platform = requested_platform
        adapter = get_adapter(requested_platform, http_client)
        detection = adapter.detect(final_url, content)

    challenge = assess_challenge(response.status_code, content, final_url)
    challenge_page = challenge.is_challenge
    response_ok = 200 <= int(response.status_code) < 400
    verified = bool(response_ok and detection.matched and not challenge_page and adapter is not None)

    parsed_source = None
    parse_error: str | None = None
    if verified and adapter is not None:
        try:
            parsed_source = adapter.parse_source(content, final_url, district)
            if str(getattr(parsed_source, "status", "") or "").casefold() != "working":
                verified = False
                parse_error = (
                    f"The {selected_platform} adapter found only a tentative match; "
                    "the source still requires manual review."
                )
        except Exception as exc:
            verified = False
            parse_error = f"The {selected_platform} adapter could not parse this source: {exc}"
    if verified and not _endpoint_has_platform_evidence(
        selected_platform,
        content,
        parsed_source,
    ):
        verified = False
        parse_error = (
            f"The {selected_platform} page did not contain enough public source evidence "
            "to activate it."
        )
    if verified and not operator_confirmed:
        verified = False
        parse_error = (
            "Confirm that this public source belongs to the selected district before "
            "activating it."
        )

    if challenge_page:
        challenge_detail = challenge.marker or challenge.category
        validation_message = "The public page returned a challenge or access-denied response"
        if challenge_detail:
            validation_message += f" ({challenge_detail})"
        validation_message += "."
    elif not response_ok:
        validation_message = f"The public page returned HTTP {response.status_code}."
    elif not detection.matched:
        if requested_platform == AUTO_PLATFORM:
            validation_message = "No supported board platform adapter matched this page."
        else:
            validation_message = f"The page did not match the selected {requested_platform} adapter."
    else:
        validation_message = parse_error

    metadata = dict(getattr(parsed_source, "metadata", {}) or detection.metadata or {})
    external_source_id = getattr(parsed_source, "external_source_id", None)
    if external_source_id in (None, ""):
        external_source_id = detection.metadata.get("external_source_id")
    platform_tenant = metadata.get("platform_tenant") or metadata.get("tenant")
    canonical_url = (
        getattr(parsed_source, "public_url", None)
        or detection.canonical_url
        or final_url
    )
    raw_discovery = {
        **metadata,
        "discovery_method": "manual_entry",
        "manual_platform_selection": requested_platform,
        "requested_url": requested_url,
        "fetched_url": final_url,
        "http_status": int(response.status_code),
        "challenge_category": challenge.category or None,
        "challenge_marker": challenge.marker or None,
        "browser_retry_allowed": challenge.browser_retry_allowed,
        "adapter_matched": bool(detection.matched),
        "adapter_reason": detection.reason,
        "verified": verified,
        "operator_confirmed_district_identity": bool(operator_confirmed),
    }

    return ManualSourceValidation(
        platform=selected_platform,
        source_url=str(canonical_url),
        source_status="working" if verified else "manual_review",
        confidence=_confidence_percent(detection.confidence),
        requires_javascript=bool(
            detection.requires_javascript
            or getattr(parsed_source, "requires_javascript", False)
        ),
        organization_external_id=(
            str(external_source_id) if external_source_id not in (None, "") else None
        ),
        platform_tenant=(
            str(platform_tenant) if platform_tenant not in (None, "") else None
        ),
        error_message=None if verified else validation_message,
        raw_discovery_json=raw_discovery,
        verified=verified,
    )


def validate_manual_board_source(
    district: Mapping[str, Any],
    source_url: str,
    platform: str = AUTO_PLATFORM,
    *,
    operator_confirmed: bool = False,
    client: BoardHTTPClient | None = None,
) -> ManualSourceValidation:
    """Safely fetch and adapter-check a source entered by an operator.

    The HTTP client performs the same public-network, redirect, robots, size,
    and timeout checks used by automated board discovery. A link is marked
    ``working`` only when the response succeeds, is not a challenge page, and
    the selected adapter (or an automatically selected adapter) matches it.
    Safely fetched but unconfirmed links are retained as ``manual_review``
    evidence so an operator's work is not lost.
    """

    owns_client = client is None
    http_client = client or BoardHTTPClient()
    try:
        return _validate_manual_board_source_with_client(
            district,
            source_url,
            platform,
            operator_confirmed=operator_confirmed,
            client=http_client,
        )
    finally:
        if owns_client:
            http_client.close()
