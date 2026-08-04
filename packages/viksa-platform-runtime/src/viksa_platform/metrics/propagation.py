"""HTTP propagation for cross-service usage correlation."""

from __future__ import annotations

from viksa_platform.metrics.constants import (
    HEADER_ACTIVITY_ID,
    HEADER_ENTITY_ID,
    HEADER_ENTITY_TYPE,
    HEADER_TRACE_ID,
)
from viksa_platform.metrics.context import get_usage_context


def usage_headers() -> dict[str, str]:
    """Build downstream usage-correlation headers."""
    context = get_usage_context()
    headers: dict[str, str] = {}
    if not context:
        return headers
    if context.activity_id:
        headers[HEADER_ACTIVITY_ID] = context.activity_id
    if context.entity_type:
        headers[HEADER_ENTITY_TYPE] = context.entity_type
    if context.entity_id or context.resource_id:
        headers[HEADER_ENTITY_ID] = context.entity_id or context.resource_id or ""
    if context.trace_id:
        headers[HEADER_TRACE_ID] = context.trace_id
    return headers


def extract_usage_from_headers(headers: dict[str, str]) -> dict[str, str | None]:
    """Extract usage-correlation fields from inbound headers."""
    lower = {key.lower(): value for key, value in headers.items()}

    def _get(name: str) -> str | None:
        return lower.get(name.lower())

    return {
        "activity_id": _get(HEADER_ACTIVITY_ID),
        "entity_type": _get(HEADER_ENTITY_TYPE),
        "entity_id": _get(HEADER_ENTITY_ID),
        "trace_id": _get(HEADER_TRACE_ID),
    }


def merge_headers(existing: dict[str, str] | None = None) -> dict[str, str]:
    """Merge usage propagation into an existing header mapping."""
    output = dict(existing or {})
    output.update(usage_headers())
    return output


__all__ = ["extract_usage_from_headers", "merge_headers", "usage_headers"]
