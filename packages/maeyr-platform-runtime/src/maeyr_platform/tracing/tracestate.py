"""W3C tracestate parse/build helpers for tenant and vendor propagation."""

from __future__ import annotations

from maeyr_platform.tracing.constants import (
    HEADER_TENANT_ORG_ID,
    HEADER_TENANT_PROJECT_ID,
)


def parse_tracestate(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    output: dict[str, str] = {}
    for part in value.split(","):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        key, item = piece.split("=", 1)
        output[key.strip()] = item.strip()
    return output


def build_tracestate(
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    parts = dict(extra or {})
    if org_id:
        parts["org_id"] = org_id
    if project_id:
        parts["project_id"] = project_id
    if not parts:
        return ""
    return ",".join(f"{key}={value}" for key, value in parts.items())


def tenant_from_tracestate(state: dict[str, str]) -> tuple[str | None, str | None]:
    return state.get("org_id"), state.get("project_id")


def merge_tracestate_into_headers(
    headers: dict[str, str],
    *,
    org_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    output = dict(headers)
    tracestate = build_tracestate(org_id=org_id, project_id=project_id)
    if tracestate:
        output["tracestate"] = tracestate
    if org_id:
        output[HEADER_TENANT_ORG_ID] = org_id
    if project_id:
        output[HEADER_TENANT_PROJECT_ID] = project_id
    return output


__all__ = [
    "build_tracestate",
    "merge_tracestate_into_headers",
    "parse_tracestate",
    "tenant_from_tracestate",
]
