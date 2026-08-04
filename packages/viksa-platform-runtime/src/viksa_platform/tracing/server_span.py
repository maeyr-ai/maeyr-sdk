"""Canonical non-blocking HTTP server span recording."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .constants import SPAN_HTTP_SERVER, SpanKind, SpanOperation
from .context import TraceContext
from .ids import generate_span_id
from .semconv import ATTR_HTTP_METHOD, ATTR_HTTP_ROUTE, ATTR_HTTP_STATUS


def _new_span_id() -> str:
    return generate_span_id()


def schedule_http_server_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    service: str,
    method: str,
    route: str,
    status_code: int,
    duration_ms: int,
    started_at: datetime,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    activity_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire-and-forget SERVER span for an HTTP request (safe after context clear)."""
    from .recorder import record_span

    ended_at = datetime.now(timezone.utc)
    status = "ok" if status_code < 400 else "error"
    attrs = {
        ATTR_HTTP_METHOD: method.upper(),
        ATTR_HTTP_ROUTE: route,
        ATTR_HTTP_STATUS: status_code,
    }

    async def _emit() -> None:
        await record_span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            span_name=SPAN_HTTP_SERVER,
            operation=SpanOperation.HTTP_SERVER.value,
            span_kind=SpanKind.SERVER.value,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=max(duration_ms, 0),
            is_root=parent_span_id is None,
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            user_email=user_email,
            activity_id=activity_id,
            entity_type=entity_type,
            entity_id=entity_id,
            resource_refs=resource_refs,
            attributes=attrs,
            service=service,
        )

    asyncio.create_task(_emit(), name="http_server_span")


def schedule_http_server_span_from_context(
    ctx: TraceContext,
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: int,
    started_at: datetime,
) -> None:
    """Record SERVER span using a snapshot of the active trace context."""
    schedule_http_server_span(
        trace_id=ctx.trace_id,
        span_id=ctx.span_id or _new_span_id(),
        parent_span_id=ctx.parent_span_id,
        service=ctx.service or "unknown",
        method=method,
        route=route,
        status_code=status_code,
        duration_ms=duration_ms,
        started_at=started_at,
        account_id=ctx.account_id,
        org_id=ctx.org_id,
        project_id=ctx.project_id,
        user_id=ctx.user_id,
        user_email=ctx.user_email,
        activity_id=ctx.activity_id,
        entity_type=ctx.entity_type,
        entity_id=ctx.entity_id,
        resource_refs=dict(ctx.resource_refs) if ctx.resource_refs else None,
    )
