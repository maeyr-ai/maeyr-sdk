"""Canonical inbound trace binding from W3C and platform headers."""

from __future__ import annotations

import contextvars
from typing import Any, Dict, Mapping, Optional, Tuple

from .context import TraceContext, bind_trace_context, get_trace_context
from .ids import generate_span_id, generate_trace_id, normalize_trace_id
from .propagation import extract_trace_from_headers


def headers_from_asgi_scope(scope: Mapping[str, Any]) -> Dict[str, str]:
    """Decode ASGI scope headers into a lowercase string dict."""
    raw = scope.get("headers") or []
    out: Dict[str, str] = {}
    for key, value in raw:
        k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        out[k.lower()] = v
    return out


def _new_span_id() -> str:
    return generate_span_id()


def _new_trace_id() -> str:
    return generate_trace_id()


def resolve_inbound_parent_span_id(extracted: Dict[str, Optional[str]]) -> Optional[str]:
    """
    Resolve parent span for a server-side span from inbound headers.

    W3C traceparent carries the caller span as parent. Platform X-Parent-Span-Id
    takes precedence when explicitly set.
    """
    parent = extracted.get("parent_span_id")
    if parent:
        return parent
    # When upstream sent X-Span-Id without parent, treat it as parent (client span).
    if extracted.get("span_id"):
        return extracted["span_id"]
    return None


def bind_inbound_trace(
    headers: Mapping[str, str],
    *,
    service: str,
    fallback_trace_id: Optional[str] = None,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[contextvars.Token[Optional[TraceContext]]]:
    """
    Bind trace context from incoming headers when traceparent or X-Trace-ID is present.
    Creates a new server span ID (OTel SERVER span pattern). Returns reset token or None.
    """
    normalized = {str(k).lower(): v for k, v in headers.items()}
    extracted = extract_trace_from_headers(normalized)
    trace_id = extracted.get("trace_id") or fallback_trace_id
    if not trace_id:
        return None
    trace_id = normalize_trace_id(trace_id)
    parent_span_id = resolve_inbound_parent_span_id(extracted)
    span_id = _new_span_id()
    return bind_trace_context(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        activity_id=extracted.get("activity_id"),
        account_id=account_id or extracted.get("account_id"),
        org_id=org_id or extracted.get("org_id"),
        project_id=project_id or extracted.get("project_id"),
        entity_type=extracted.get("entity_type"),
        entity_id=extracted.get("entity_id"),
        service=service,
    )


def bind_inbound_server_trace(
    extracted: Dict[str, Optional[str]],
    *,
    service: str,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    fallback_trace_id: Optional[str] = None,
) -> Tuple[
    Optional[TraceContext],
    Optional[contextvars.Token[Optional[TraceContext]]],
]:
    """
    Bind a server span from extracted inbound headers.

    Returns (context, reset_token). When no trace_id is present, returns (None, None).
    """
    trace_id = extracted.get("trace_id") or fallback_trace_id
    if not trace_id:
        return None, None
    trace_id = normalize_trace_id(trace_id)
    parent_span_id = resolve_inbound_parent_span_id(extracted)
    span_id = _new_span_id()
    tok = bind_trace_context(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        activity_id=activity_id or extracted.get("activity_id"),
        account_id=account_id or extracted.get("account_id"),
        org_id=org_id or extracted.get("org_id"),
        project_id=project_id or extracted.get("project_id"),
        user_id=user_id,
        user_email=user_email,
        entity_type=entity_type or extracted.get("entity_type"),
        entity_id=entity_id or extracted.get("entity_id"),
        resource_refs=resource_refs,
        service=service,
    )
    return get_trace_context(), tok
