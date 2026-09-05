"""Canonical contextvars-based distributed trace context."""

import contextvars
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, Optional

from .constants import HEADER_TENANT_ORG_ID, HEADER_TENANT_PROJECT_ID, PREFIX_SPAN, PREFIX_TRACE
from .ids import generate_span_id, generate_trace_id


def _generate_id(prefix: str) -> str:
    if prefix == PREFIX_TRACE:
        return generate_trace_id()
    return generate_span_id()


@dataclass
class TraceContext:
    """Active trace/span context for the current asyncio task."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    activity_id: Optional[str] = None

    account_id: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    user_email: Optional[str] = None

    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    resource_refs: Optional[Dict[str, Any]] = None
    service: Optional[str] = None

    is_root: bool = False

    def child_span_id(self) -> str:
        return _generate_id(PREFIX_SPAN)

    def to_headers(self) -> Dict[str, str]:
        from .propagation import build_traceparent

        headers: Dict[str, str] = {
            "X-Trace-ID": self.trace_id,
            "X-Span-Id": self.span_id,
        }
        if self.parent_span_id:
            headers["X-Parent-Span-Id"] = self.parent_span_id
        if self.activity_id:
            headers["X-Usage-Activity-Id"] = self.activity_id
        if self.entity_type:
            headers["X-Usage-Entity-Type"] = self.entity_type
        if self.entity_id:
            headers["X-Usage-Entity-Id"] = self.entity_id
        if self.account_id:
            headers["X-Internal-Account-Id"] = self.account_id
        if self.org_id:
            headers[HEADER_TENANT_ORG_ID] = self.org_id
            headers["X-Internal-Org-Id"] = self.org_id
        if self.project_id:
            headers[HEADER_TENANT_PROJECT_ID] = self.project_id
            headers["X-Internal-Project-Id"] = self.project_id
        from .tracestate import build_tracestate

        ts = build_tracestate(org_id=self.org_id, project_id=self.project_id)
        if ts:
            headers["tracestate"] = ts
        headers["traceparent"] = build_traceparent(self.trace_id, self.span_id)
        return headers

    def with_span(self, span_id: str, *, parent_span_id: Optional[str] = None) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id or self.span_id,
            activity_id=self.activity_id,
            account_id=self.account_id,
            org_id=self.org_id,
            project_id=self.project_id,
            user_id=self.user_id,
            user_email=self.user_email,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            resource_refs=dict(self.resource_refs) if self.resource_refs else None,
            service=self.service,
            is_root=False,
        )


_trace_context: contextvars.ContextVar[Optional[TraceContext]] = contextvars.ContextVar(
    "_trace_context", default=None
)

# Active span stack for nested spans within same task
_span_stack: contextvars.ContextVar[list[str]] = contextvars.ContextVar("_span_stack", default=[])


def get_trace_context() -> Optional[TraceContext]:
    return _trace_context.get()


def set_trace_context(
    ctx: Optional[TraceContext],
) -> contextvars.Token[Optional[TraceContext]]:
    return _trace_context.set(ctx)


def clear_trace_context(token: contextvars.Token[Optional[TraceContext]]) -> None:
    _trace_context.reset(token)


def enrich_trace_tenant(
    *,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> None:
    """Fill missing tenant fields on the active trace context (e.g. after auth)."""
    ctx = get_trace_context()
    if not ctx:
        return
    updated = TraceContext(
        trace_id=ctx.trace_id,
        span_id=ctx.span_id,
        parent_span_id=ctx.parent_span_id,
        activity_id=ctx.activity_id,
        account_id=account_id or ctx.account_id,
        org_id=org_id or ctx.org_id,
        project_id=project_id or ctx.project_id,
        user_id=user_id or ctx.user_id,
        user_email=user_email or ctx.user_email,
        entity_type=entity_type or ctx.entity_type,
        entity_id=ctx.entity_id,
        resource_refs=ctx.resource_refs,
        service=ctx.service,
        is_root=ctx.is_root,
    )
    set_trace_context(updated)
    if updated.account_id:
        try:
            from maeyr_platform.observability.logging import set_account_id

            set_account_id(updated.account_id)
        except Exception:
            pass


def start_trace(
    *,
    account_id: str,
    org_id: str,
    project_id: str,
    trace_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    service: Optional[str] = None,
    span_name: str = "root",
) -> tuple[TraceContext, contextvars.Token[Optional[TraceContext]]]:
    """Start a new trace root. Returns (context, reset_token)."""
    tid = trace_id or _generate_id(PREFIX_TRACE)
    sid = _generate_id(PREFIX_SPAN)
    ctx = TraceContext(
        trace_id=tid,
        span_id=sid,
        parent_span_id=None,
        activity_id=activity_id,
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        user_email=user_email,
        entity_type=entity_type,
        entity_id=entity_id,
        resource_refs=resource_refs,
        service=service,
        is_root=True,
    )
    tok = set_trace_context(ctx)
    _span_stack.set([sid])
    return ctx, tok


def bind_trace_context(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: Optional[str] = None,
    activity_id: Optional[str] = None,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    service: Optional[str] = None,
) -> contextvars.Token[Optional[TraceContext]]:
    """Bind context extracted from incoming headers."""
    ctx = TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        activity_id=activity_id,
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        user_email=user_email,
        entity_type=entity_type,
        entity_id=entity_id,
        resource_refs=resource_refs,
        service=service,
        is_root=parent_span_id is None,
    )
    tok = set_trace_context(ctx)
    stack = list(_span_stack.get() or [])
    stack.append(span_id)
    _span_stack.set(stack)
    return tok


@contextmanager
def trace_context_scope(ctx: TraceContext) -> Iterator[TraceContext]:
    tok = set_trace_context(ctx)
    try:
        yield ctx
    finally:
        clear_trace_context(tok)


@asynccontextmanager
async def trace_span(
    span_name: str,
    *,
    operation: Optional[str] = None,
    span_kind: str = "internal",
    attributes: Optional[Dict[str, Any]] = None,
    service: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    Async context manager: creates child span, records start/end pair.
    Yields span_id.
    """
    from .recorder import schedule_span_end, schedule_span_start

    parent = get_trace_context()
    if not parent:
        yield ""
        return

    span_id = parent.child_span_id()
    child_ctx = parent.with_span(span_id)
    tok = set_trace_context(child_ctx)
    stack = list(_span_stack.get() or [])
    stack.append(span_id)
    _span_stack.set(stack)
    started = datetime.now(timezone.utc)
    status = "ok"
    err_attrs: Dict[str, Any] = {}
    schedule_span_start(
        span_id=span_id,
        trace_id=parent.trace_id,
        parent_span_id=parent.span_id,
        span_name=span_name,
        operation=operation,
        span_kind=span_kind,
        started_at=started,
        attributes=attributes,
        service=service or parent.service,
        account_id=parent.account_id,
        org_id=parent.org_id,
        project_id=parent.project_id,
        user_id=parent.user_id,
        user_email=parent.user_email,
        activity_id=parent.activity_id,
        entity_type=parent.entity_type,
        entity_id=parent.entity_id,
        resource_refs=parent.resource_refs,
    )
    try:
        yield span_id
    except Exception as exc:
        status = "error"
        from .errors import error_attributes_from_exception

        err_attrs = error_attributes_from_exception(exc)
        raise
    finally:
        ended = datetime.now(timezone.utc)
        duration_ms = int((ended - started).total_seconds() * 1000)
        merged_attrs = dict(attributes or {})
        merged_attrs.update(err_attrs)
        schedule_span_end(
            span_id=span_id,
            trace_id=parent.trace_id,
            parent_span_id=parent.span_id,
            span_name=span_name,
            operation=operation,
            span_kind=span_kind,
            status=status,
            started_at=started,
            ended_at=ended,
            duration_ms=duration_ms,
            attributes=merged_attrs or None,
            service=service or parent.service,
            account_id=parent.account_id,
            org_id=parent.org_id,
            project_id=parent.project_id,
            user_id=parent.user_id,
            user_email=parent.user_email,
            activity_id=parent.activity_id,
            entity_type=parent.entity_type,
            entity_id=parent.entity_id,
            resource_refs=parent.resource_refs,
        )
        clear_trace_context(tok)
        if stack:
            stack.pop()
        _span_stack.set(stack)
