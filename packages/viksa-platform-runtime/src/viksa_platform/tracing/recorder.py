"""Canonical fire-and-forget span recorder."""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .constants import DEFAULT_RETENTION_DAYS
from .context import get_trace_context
from .errors import attach_error_to_span_kwargs
from .ids import generate_span_id, generate_trace_id, normalize_span_id, normalize_trace_id
from .labels import derive_labels
from .sampling import should_sample
from .semconv import enrich_span_attributes, operation_for_span_name
from .tenant import span_ref, valid_tenant_id
from .transport import enqueue_span, get_transport_stats, re_enqueue_spans

logger = logging.getLogger("platform_traces.recorder")

_BATCH_SIZE = 30
_FLUSH_INTERVAL_SECONDS = 0.5
_MAX_QUEUE_SIZE = 20000
_STOP_DRAIN_PASSES = 50

_memory_queue: deque[Dict[str, Any]] = deque(maxlen=_MAX_QUEUE_SIZE)
_flush_task: Optional[asyncio.Task[None]] = None
_running = False
_flush_handler: Optional[Callable[[List[Dict[str, Any]]], Awaitable[object]]] = None
_remote_recorder: Optional[Any] = None
_retention_days: int = DEFAULT_RETENTION_DAYS
_redis_enqueue_failures: int = 0
_spans_dropped_invalid_tenant: int = 0
_spans_dropped_queue_overflow: int = 0


def _snapshot_tenant_fields(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Capture tenant fields at schedule time (safe for asyncio.create_task)."""
    snap = dict(kwargs)
    ctx = get_trace_context()
    if not ctx:
        return snap
    snap.setdefault("account_id", snap.get("account_id") or ctx.account_id)
    snap.setdefault("org_id", snap.get("org_id") or ctx.org_id)
    snap.setdefault("project_id", snap.get("project_id") or ctx.project_id)
    snap.setdefault("user_id", snap.get("user_id") or ctx.user_id)
    snap.setdefault("user_email", snap.get("user_email") or ctx.user_email)
    snap.setdefault("activity_id", snap.get("activity_id") or ctx.activity_id)
    snap.setdefault("entity_type", snap.get("entity_type") or ctx.entity_type)
    snap.setdefault("entity_id", snap.get("entity_id") or ctx.entity_id)
    snap.setdefault("resource_refs", snap.get("resource_refs") or ctx.resource_refs)
    if snap.get("service") in (None, "", "unknown"):
        snap["service"] = ctx.service or snap.get("service")
    if not snap.get("trace_id") and ctx.trace_id:
        snap["trace_id"] = ctx.trace_id
    if snap.get("parent_span_id") is None and ctx.span_id:
        snap["parent_span_id"] = ctx.span_id
    return snap


def set_remote_recorder(recorder: Any) -> None:
    global _remote_recorder
    _remote_recorder = recorder


def schedule_span_start(**kwargs: Any) -> None:
    """Non-blocking span start; tenant fields snapshotted before task runs."""
    snap = _snapshot_tenant_fields(kwargs)
    if _remote_recorder is not None:
        _remote_recorder.schedule_record(**snap)
        return
    asyncio.create_task(
        record_span_start(**snap),
        name=f"record_span_start_{snap.get('span_name', 'internal')}",
    )


def schedule_span_end(**kwargs: Any) -> None:
    """Non-blocking span end; tenant fields snapshotted before task runs."""
    snap = _snapshot_tenant_fields(kwargs)
    if _remote_recorder is not None:
        _remote_recorder.schedule_record(**snap)
        return
    asyncio.create_task(
        record_span_end(**snap),
        name=f"record_span_end_{snap.get('span_name', 'internal')}",
    )


def configure_recorder(
    flush_handler: Callable[[List[Dict[str, Any]]], Awaitable[object]],
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    global _flush_handler, _retention_days
    _flush_handler = flush_handler
    _retention_days = retention_days


def _new_span_id() -> str:
    return generate_span_id()


def _new_trace_id() -> str:
    return generate_trace_id()


def _build_span_doc(
    *,
    span_id: str,
    trace_id: str,
    parent_span_id: Optional[str],
    span_name: str,
    operation: Optional[str],
    span_kind: str,
    status: str,
    started_at: datetime,
    ended_at: Optional[datetime],
    duration_ms: Optional[int],
    service: str,
    account_id: str,
    org_id: str,
    project_id: str,
    user_id: Optional[str],
    user_email: Optional[str],
    activity_id: Optional[str],
    entity_type: Optional[str],
    entity_id: Optional[str],
    resource_refs: Optional[Dict[str, Any]],
    attributes: Optional[Dict[str, Any]],
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    tokens_used: Optional[int],
    cost_usd: Optional[float],
    is_root: bool,
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    resolved_operation = operation_for_span_name(span_name, operation)
    attrs = enrich_span_attributes(
        attributes,
        service=service,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    labels = derive_labels(
        status=status,
        operation=resolved_operation,
        attributes=attrs,
        span_name=span_name,
    )
    expires = now + timedelta(days=_retention_days)
    doc = {
        "_id": span_id,
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "activity_id": activity_id,
        "account_id": account_id,
        "org_id": org_id,
        "project_id": project_id,
        "user_id": user_id,
        "user_email": user_email,
        "service": service,
        "span_kind": span_kind,
        "span_name": span_name,
        "operation": resolved_operation,
        "status": status,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "resource_refs": resource_refs,
        "attributes": attrs or None,
        "labels": labels or None,
        "is_root": is_root,
        "expires_at": expires,
        "date_bucket": started_at.strftime("%Y-%m-%d"),
    }
    if ended_at is not None:
        doc["ended_at"] = ended_at
    doc.update({k: v for k, v in kwargs.items() if k not in doc and v is not None})
    return doc


async def _enqueue_or_buffer(doc: Dict[str, Any]) -> None:
    global _redis_enqueue_failures, _spans_dropped_invalid_tenant, _spans_dropped_queue_overflow
    if not valid_tenant_id(doc.get("account_id")):
        _spans_dropped_invalid_tenant += 1
        logger.warning(
            "Skipping span enqueue with invalid account_id: %s (dropped=%d)",
            span_ref(doc),
            _spans_dropped_invalid_tenant,
        )
        return
    enqueued = await enqueue_span(doc)
    if enqueued:
        return
    _redis_enqueue_failures += 1
    if _flush_handler:
        if len(_memory_queue) >= _MAX_QUEUE_SIZE:
            _spans_dropped_queue_overflow += 1
        _memory_queue.append(doc)
        logger.error(
            "Redis enqueue failed; buffered span in memory "
            "(queue=%d, failures=%d, overflow_drops=%d)",
            len(_memory_queue),
            _redis_enqueue_failures,
            _spans_dropped_queue_overflow,
        )
        asyncio.create_task(_flush(), name="trace_flush_immediate")
    elif _remote_recorder is not None:
        _remote_recorder.schedule_push([doc])
    else:
        if len(_memory_queue) >= _MAX_QUEUE_SIZE:
            _spans_dropped_queue_overflow += 1
        _memory_queue.append(doc)
        logger.error(
            "Redis enqueue failed with no remote sink; buffered span (queue=%d, overflow_drops=%d)",
            len(_memory_queue),
            _spans_dropped_queue_overflow,
        )
        asyncio.create_task(_flush(), name="trace_flush_immediate")


async def record_span(
    *,
    span_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    span_name: str = "internal",
    operation: Optional[str] = None,
    span_kind: str = "internal",
    status: str = "ok",
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    service: Optional[str] = None,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    activity_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    tokens_used: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_root: bool = False,
    **kwargs: Any,
) -> None:
    """Record a completed span. Fire-and-forget — never blocks hot path."""
    ctx = get_trace_context()
    now = datetime.now(timezone.utc)
    started = started_at or now
    ended = ended_at or now
    if duration_ms is None:
        duration_ms = int((ended - started).total_seconds() * 1000)

    sid = normalize_span_id(span_id or (ctx.span_id if ctx else "") or _new_span_id())
    tid = normalize_trace_id(trace_id or (ctx.trace_id if ctx else "") or _new_trace_id())
    if not should_sample(tid):
        return
    pid = parent_span_id
    if pid is None and ctx and ctx.span_id != sid:
        pid = ctx.span_id

    resolved_service = service or (ctx.service if ctx else None) or "unknown"
    span_kwargs: Dict[str, Any] = {
        "span_id": sid,
        "trace_id": tid,
        "parent_span_id": pid,
        "span_name": span_name,
        "operation": operation,
        "span_kind": span_kind,
        "status": status,
        "started_at": started,
        "ended_at": ended,
        "duration_ms": duration_ms,
        "service": resolved_service,
        "account_id": account_id or (ctx.account_id if ctx else None) or "unknown",
        "org_id": org_id or (ctx.org_id if ctx else None) or "",
        "project_id": project_id or (ctx.project_id if ctx else None) or "",
        "user_id": user_id or (ctx.user_id if ctx else None),
        "user_email": user_email or (ctx.user_email if ctx else None),
        "activity_id": activity_id or (ctx.activity_id if ctx else None),
        "entity_type": entity_type or (ctx.entity_type if ctx else None),
        "entity_id": entity_id or (ctx.entity_id if ctx else None),
        "resource_refs": resource_refs or (ctx.resource_refs if ctx else None),
        "attributes": attributes,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "is_root": is_root,
        **kwargs,
    }
    attach_error_to_span_kwargs(span_kwargs)
    doc = _build_span_doc(
        span_id=span_kwargs["span_id"],
        trace_id=span_kwargs["trace_id"],
        parent_span_id=span_kwargs.get("parent_span_id"),
        span_name=span_kwargs["span_name"],
        operation=span_kwargs.get("operation"),
        span_kind=span_kwargs.get("span_kind", "internal"),
        status=span_kwargs.get("status", "ok"),
        started_at=span_kwargs["started_at"],
        ended_at=span_kwargs["ended_at"],
        duration_ms=span_kwargs.get("duration_ms"),
        service=span_kwargs["service"],
        account_id=span_kwargs["account_id"],
        org_id=span_kwargs.get("org_id", ""),
        project_id=span_kwargs.get("project_id", ""),
        user_id=span_kwargs.get("user_id"),
        user_email=span_kwargs.get("user_email"),
        activity_id=span_kwargs.get("activity_id"),
        entity_type=span_kwargs.get("entity_type"),
        entity_id=span_kwargs.get("entity_id"),
        resource_refs=span_kwargs.get("resource_refs"),
        attributes=span_kwargs.get("attributes"),
        model=span_kwargs.get("model"),
        prompt_tokens=span_kwargs.get("prompt_tokens"),
        completion_tokens=span_kwargs.get("completion_tokens"),
        tokens_used=span_kwargs.get("tokens_used"),
        cost_usd=span_kwargs.get("cost_usd"),
        is_root=bool(span_kwargs.get("is_root", False)),
        kwargs={k: v for k, v in {**kwargs, **span_kwargs}.items() if k.startswith("_")},
    )
    await _enqueue_or_buffer(doc)


async def record_span_start(
    *,
    span_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    span_name: str = "internal",
    operation: Optional[str] = None,
    span_kind: str = "internal",
    started_at: Optional[datetime] = None,
    service: Optional[str] = None,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    activity_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    is_root: bool = False,
    **kwargs: Any,
) -> str:
    """Record the start of a long-running span (status=running). Returns span_id."""
    ctx = get_trace_context()
    now = datetime.now(timezone.utc)
    started = started_at or now
    sid = normalize_span_id(span_id or _new_span_id())
    tid = normalize_trace_id(trace_id or (ctx.trace_id if ctx else "") or _new_trace_id())
    if not should_sample(tid):
        return sid
    pid = parent_span_id
    if pid is None and ctx:
        pid = ctx.span_id

    resolved_service = service or (ctx.service if ctx else None) or "unknown"
    doc = _build_span_doc(
        span_id=sid,
        trace_id=tid,
        parent_span_id=pid,
        span_name=span_name,
        operation=operation,
        span_kind=span_kind,
        status="running",
        started_at=started,
        ended_at=None,
        duration_ms=0,
        service=resolved_service,
        account_id=account_id or (ctx.account_id if ctx else None) or "unknown",
        org_id=org_id or (ctx.org_id if ctx else None) or "",
        project_id=project_id or (ctx.project_id if ctx else None) or "",
        user_id=user_id or (ctx.user_id if ctx else None),
        user_email=user_email or (ctx.user_email if ctx else None),
        activity_id=activity_id or (ctx.activity_id if ctx else None),
        entity_type=entity_type or (ctx.entity_type if ctx else None),
        entity_id=entity_id or (ctx.entity_id if ctx else None),
        resource_refs=resource_refs or (ctx.resource_refs if ctx else None),
        attributes=attributes,
        model=model,
        prompt_tokens=None,
        completion_tokens=None,
        tokens_used=None,
        cost_usd=None,
        is_root=is_root,
        kwargs={**kwargs, "_is_completion": False},
    )
    await _enqueue_or_buffer(doc)
    return sid


async def record_span_end(
    *,
    span_id: str,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    span_name: str = "internal",
    operation: Optional[str] = None,
    span_kind: str = "internal",
    status: str = "ok",
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    service: Optional[str] = None,
    account_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    activity_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    resource_refs: Optional[Dict[str, Any]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    tokens_used: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_root: bool = False,
    **kwargs: Any,
) -> None:
    """Complete a long-running span started with record_span_start."""
    ctx = get_trace_context()
    now = datetime.now(timezone.utc)
    ended = ended_at or now
    sid = normalize_span_id(span_id)
    tid = normalize_trace_id(trace_id or (ctx.trace_id if ctx else "") or _new_trace_id())
    if not should_sample(tid):
        return
    if duration_ms is None and started_at is not None:
        duration_ms = int((ended - started_at).total_seconds() * 1000)
    elif duration_ms is None:
        duration_ms = 0

    resolved_service = service or (ctx.service if ctx else None) or "unknown"
    span_kwargs: Dict[str, Any] = {
        "span_id": sid,
        "trace_id": tid,
        "parent_span_id": parent_span_id or (ctx.span_id if ctx else None),
        "span_name": span_name,
        "operation": operation,
        "span_kind": span_kind,
        "status": status,
        "started_at": started_at or ended,
        "ended_at": ended,
        "duration_ms": duration_ms,
        "service": resolved_service,
        "account_id": account_id or (ctx.account_id if ctx else None) or "unknown",
        "org_id": org_id or (ctx.org_id if ctx else None) or "",
        "project_id": project_id or (ctx.project_id if ctx else None) or "",
        "user_id": user_id or (ctx.user_id if ctx else None),
        "user_email": user_email or (ctx.user_email if ctx else None),
        "activity_id": activity_id or (ctx.activity_id if ctx else None),
        "entity_type": entity_type or (ctx.entity_type if ctx else None),
        "entity_id": entity_id or (ctx.entity_id if ctx else None),
        "resource_refs": resource_refs or (ctx.resource_refs if ctx else None),
        "attributes": attributes,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "is_root": is_root,
        **kwargs,
        "_is_completion": True,
    }
    attach_error_to_span_kwargs(span_kwargs)
    doc = _build_span_doc(
        span_id=span_kwargs["span_id"],
        trace_id=span_kwargs["trace_id"],
        parent_span_id=span_kwargs.get("parent_span_id"),
        span_name=span_kwargs["span_name"],
        operation=span_kwargs.get("operation"),
        span_kind=span_kwargs.get("span_kind", "internal"),
        status=span_kwargs.get("status", "ok"),
        started_at=span_kwargs["started_at"],
        ended_at=span_kwargs["ended_at"],
        duration_ms=span_kwargs.get("duration_ms"),
        service=span_kwargs["service"],
        account_id=span_kwargs["account_id"],
        org_id=span_kwargs.get("org_id", ""),
        project_id=span_kwargs.get("project_id", ""),
        user_id=span_kwargs.get("user_id"),
        user_email=span_kwargs.get("user_email"),
        activity_id=span_kwargs.get("activity_id"),
        entity_type=span_kwargs.get("entity_type"),
        entity_id=span_kwargs.get("entity_id"),
        resource_refs=span_kwargs.get("resource_refs"),
        attributes=span_kwargs.get("attributes"),
        model=span_kwargs.get("model"),
        prompt_tokens=span_kwargs.get("prompt_tokens"),
        completion_tokens=span_kwargs.get("completion_tokens"),
        tokens_used=span_kwargs.get("tokens_used"),
        cost_usd=span_kwargs.get("cost_usd"),
        is_root=bool(span_kwargs.get("is_root", False)),
        kwargs={k: v for k, v in {**kwargs, **span_kwargs}.items() if k.startswith("_")},
    )
    await _enqueue_or_buffer(doc)


def _normalize_span_datetimes(doc: Dict[str, Any]) -> None:
    for key in ("started_at", "ended_at", "expires_at", "failed_at", "dlq_at"):
        val = doc.get(key)
        if isinstance(val, str):
            try:
                doc[key] = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass


async def ingest_spans(spans: List[Dict[str, Any]]) -> None:
    """Ingest pre-built spans from internal API."""
    now = datetime.now(timezone.utc)
    for doc in spans:
        _normalize_span_datetimes(doc)
        if not doc.get("span_id") and not doc.get("_id"):
            doc["span_id"] = doc["_id"] = _new_span_id()
        elif not doc.get("span_id"):
            doc["span_id"] = doc["_id"]
        elif not doc.get("_id"):
            doc["_id"] = doc["span_id"]
        if not doc.get("trace_id"):
            doc["trace_id"] = _new_trace_id()
        if not doc.get("started_at"):
            doc["started_at"] = now
        if not doc.get("expires_at"):
            doc["expires_at"] = now + timedelta(days=_retention_days)
        doc.setdefault("date_bucket", now.strftime("%Y-%m-%d"))
        if not valid_tenant_id(doc.get("account_id")):
            logger.debug(
                "Skipping ingest span with no account_id: %s",
                span_ref(doc),
            )
            continue
        await _enqueue_or_buffer(doc)


async def start_recorder() -> None:
    global _flush_task, _running
    if _running:
        return
    _running = True
    _flush_task = asyncio.create_task(_flush_loop(), name="platform_traces_flush")


async def stop_recorder() -> None:
    global _flush_task, _running
    _running = False
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
    for _ in range(_STOP_DRAIN_PASSES):
        await _flush()
        if not _memory_queue:
            break


async def _flush_loop() -> None:
    while _running:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            await _flush()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("platform_traces flush loop error")


async def _flush() -> None:
    if not _flush_handler:
        return
    from .transport import drain_queue

    batch: List[Dict[str, Any]] = await drain_queue(_BATCH_SIZE)
    while _memory_queue and len(batch) < _BATCH_SIZE * 2:
        batch.append(_memory_queue.popleft())
    if not batch:
        return
    try:
        await _flush_handler(batch)
    except Exception:
        logger.exception("platform_traces flush failed (%d docs); re-enqueueing", len(batch))
        await re_enqueue_spans(batch)


def get_recorder_stats() -> Dict[str, Any]:
    return {
        "memory_queue_size": len(_memory_queue),
        "max_queue_size": _MAX_QUEUE_SIZE,
        "batch_size": _BATCH_SIZE,
        "running": _running,
        "retention_days": _retention_days,
        "redis_enqueue_failures": _redis_enqueue_failures,
        **get_transport_stats(),
    }
