"""Canonical W3C and platform trace header propagation."""

import re
from typing import Dict, Optional

from .constants import (
    HEADER_ACTIVITY_ID,
    HEADER_ENTITY_ID,
    HEADER_ENTITY_TYPE,
    HEADER_PARENT_SPAN_ID,
    HEADER_SPAN_ID,
    HEADER_TENANT_ORG_ID,
    HEADER_TENANT_PROJECT_ID,
    HEADER_TRACE_ID,
    HEADER_TRACEPARENT,
    HEADER_TRACESTATE,
)
from .context import get_trace_context
from .ids import normalize_span_id, normalize_trace_id
from .sampling import traceparent_sampled
from .tracestate import build_tracestate, parse_tracestate, tenant_from_tracestate

_TRACEPARENT_RE = re.compile(
    r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-([\da-f]{2})$",
    re.IGNORECASE,
)


def build_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Build W3C traceparent header value."""
    tid = normalize_trace_id(trace_id)[-32:].zfill(32)
    sid = normalize_span_id(span_id)[-16:].zfill(16)
    flags = traceparent_sampled(sampled)
    return f"00-{tid}-{sid}-{flags}"


def parse_traceparent(value: str) -> Optional[Dict[str, str]]:
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip())
    if not m:
        return None
    return {
        "trace_id": m.group(1),
        "parent_span_id": m.group(2),
    }


def trace_headers() -> Dict[str, str]:
    """Build headers to propagate trace context to downstream services."""
    ctx = get_trace_context()
    if not ctx:
        return {}
    return ctx.to_headers()


def extract_trace_from_headers(headers: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Extract trace correlation fields from incoming request headers."""
    lower = {k.lower(): v for k, v in headers.items()}

    def _get(*names: str) -> Optional[str]:
        for n in names:
            v = lower.get(n.lower())
            if v:
                return v
        return None

    trace_id = _get(HEADER_TRACE_ID, "x-trace-id")
    span_id = _get(HEADER_SPAN_ID, "x-span-id")
    parent_span_id = _get(HEADER_PARENT_SPAN_ID, "x-parent-span-id")

    tp = _get(HEADER_TRACEPARENT, "traceparent")
    ts = parse_tracestate(_get(HEADER_TRACESTATE, "tracestate"))
    ts_org, ts_project = tenant_from_tracestate(ts)
    if tp:
        parsed = parse_traceparent(tp)
        if parsed:
            if not trace_id:
                trace_id = normalize_trace_id(parsed["trace_id"])
            if not parent_span_id:
                parent_span_id = parsed["parent_span_id"]
    elif trace_id:
        trace_id = normalize_trace_id(trace_id)
    if span_id:
        span_id = normalize_span_id(span_id)

    org_id = _get("X-Internal-Org-Id", HEADER_TENANT_ORG_ID) or ts_org
    project_id = _get("X-Internal-Project-Id", HEADER_TENANT_PROJECT_ID) or ts_project
    account_id = _get("X-Internal-Account-Id", "X-Caller-Account-Id", "X-Tenant-Account-Id")

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "activity_id": _get(HEADER_ACTIVITY_ID, "x-usage-activity-id"),
        "entity_type": _get(HEADER_ENTITY_TYPE, "x-usage-entity-type"),
        "entity_id": _get(HEADER_ENTITY_ID, "x-usage-entity-id"),
        "account_id": account_id,
        "org_id": org_id,
        "project_id": project_id,
    }


def merge_trace_headers(existing: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Merge trace headers without overriding authoritative caller headers."""
    out = trace_headers()
    explicit = dict(existing or {})
    out.update(explicit)

    # Signed internal calls carry authoritative tenant IDs separately from the
    # ambient trace context. Canonicalize every alias and tracestate field as a
    # unit so a trace that began in tenant A cannot relabel a request for B.
    lower_explicit = {key.lower(): value for key, value in explicit.items()}
    internal_names = (
        "x-internal-account-id",
        "x-internal-org-id",
        "x-internal-project-id",
    )
    if any(name in lower_explicit for name in internal_names):
        account_id = lower_explicit.get("x-internal-account-id")
        org_id = lower_explicit.get("x-internal-org-id")
        project_id = lower_explicit.get("x-internal-project-id")
        aliases = {
            "x-internal-account-id",
            "x-caller-account-id",
            "x-tenant-account-id",
            "x-internal-org-id",
            "x-tenant-org-id",
            "x-internal-project-id",
            "x-tenant-project-id",
            "tracestate",
        }
        prior_tracestate = lower_explicit.get("tracestate")
        if prior_tracestate is None:
            prior_tracestate = next(
                (value for key, value in out.items() if key.lower() == "tracestate"),
                None,
            )
        out = {key: value for key, value in out.items() if key.lower() not in aliases}
        if account_id:
            out["X-Internal-Account-Id"] = account_id
            out["X-Tenant-Account-Id"] = account_id
        if org_id:
            out["X-Internal-Org-Id"] = org_id
            out[HEADER_TENANT_ORG_ID] = org_id
        if project_id:
            out["X-Internal-Project-Id"] = project_id
            out[HEADER_TENANT_PROJECT_ID] = project_id

        state = parse_tracestate(prior_tracestate)
        state.pop("org_id", None)
        state.pop("project_id", None)
        tracestate = build_tracestate(
            org_id=org_id,
            project_id=project_id,
            extra=state,
        )
        if tracestate:
            out[HEADER_TRACESTATE] = tracestate
    return out
