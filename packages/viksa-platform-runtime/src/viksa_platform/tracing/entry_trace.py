"""Canonical entry-trace helpers for HTTP and background boundaries."""

from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .constants import SPAN_MESSAGE_RECEIVE
from .context import TraceContext, clear_trace_context, get_trace_context, start_trace
from .ids import normalize_trace_id
from .inbound import bind_inbound_server_trace
from .propagation import extract_trace_from_headers
from .recorder import record_span


async def record_entry_span(
    ctx: TraceContext,
    *,
    span_name: str,
    account_id: str,
    org_id: str,
    project_id: str,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: str = "internal",
    entity_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    service: str = "unknown",
    span_kind: str = "server",
    operation: Optional[str] = None,
) -> None:
    started = datetime.now(timezone.utc)
    is_root = ctx.parent_span_id is None
    await record_span(
        span_id=ctx.span_id,
        trace_id=ctx.trace_id,
        span_name=span_name,
        operation=operation,
        span_kind=span_kind,
        status="ok",
        started_at=started,
        ended_at=started,
        duration_ms=0,
        is_root=is_root,
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        user_email=user_email,
        activity_id=activity_id,
        entity_type=entity_type,
        entity_id=entity_id,
        resource_refs=resource_refs,
        service=service,
    )


async def begin_entry_trace(
    *,
    account_id: str,
    org_id: str,
    project_id: str,
    span_name: str = SPAN_MESSAGE_RECEIVE,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: str = "internal",
    entity_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    service: str = "unknown",
    headers: Optional[Dict[str, str]] = None,
    operation: Optional[str] = None,
) -> Tuple[
    Optional[TraceContext],
    Optional[contextvars.Token[Optional[TraceContext]]],
]:
    """Start or continue trace at a service entry boundary. Returns (ctx, reset_token)."""
    extracted = extract_trace_from_headers(headers or {}) if headers else {}
    active = get_trace_context()
    fallback = normalize_trace_id(
        extracted.get("trace_id") or (active.trace_id if active else "") or ""
    )

    if extracted.get("trace_id"):
        ctx, tok = bind_inbound_server_trace(
            extracted,
            service=service,
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            user_email=user_email,
            entity_type=entity_type,
            entity_id=entity_id,
            activity_id=activity_id,
            resource_refs=resource_refs,
            fallback_trace_id=fallback or None,
        )
    else:
        ctx, tok = start_trace(
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
            trace_id=fallback or None,
            activity_id=activity_id,
            user_id=user_id,
            user_email=user_email,
            entity_type=entity_type,
            entity_id=entity_id,
            resource_refs=resource_refs,
            service=service,
        )

    if ctx:
        await record_entry_span(
            ctx,
            span_name=span_name,
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            user_email=user_email,
            entity_type=entity_type,
            entity_id=entity_id,
            activity_id=activity_id,
            resource_refs=resource_refs,
            service=service,
            operation=operation,
        )
    return ctx, tok


def end_entry_trace(
    token: Optional[contextvars.Token[Optional[TraceContext]]],
) -> None:
    if token is not None:
        try:
            clear_trace_context(token)
        except Exception:
            pass
