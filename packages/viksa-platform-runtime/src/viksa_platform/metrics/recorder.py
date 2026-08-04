"""Durable, bounded compatibility recorder for token usage."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from viksa_platform.metrics.constants import PREFIX_TOKEN_USAGE, entity_type_from_resource_type
from viksa_platform.metrics.context import _call_counter, get_usage_context
from viksa_platform.metrics.resource_refs import build_resource_refs, merge_resource_refs
from viksa_platform.metrics.transport import enqueue_event

logger = logging.getLogger("platform_metrics.recorder")

Batch = list[dict[str, Any]]
FlushHandler = Callable[[Batch], Awaitable[Any]]

_BATCH_SIZE = 20
_FLUSH_INTERVAL_SECONDS = 5.0
_MAX_QUEUE_SIZE = 10_000
_STOP_DRAIN_PASSES = 50
_FLUSH_TIMEOUT_SECONDS = 10.0
_SHUTDOWN_TIMEOUT_SECONDS = 15.0

_memory_queue: deque[dict[str, Any]] = deque(maxlen=_MAX_QUEUE_SIZE)
_flush_task: asyncio.Task[None] | None = None
_flush_wakeup: asyncio.Event | None = None
_running = False
_automatically_started = False
_flush_handler: FlushHandler | None = None
_memory_dropped = 0
_flush_failures = 0


def _append_memory(doc: dict[str, Any]) -> bool:
    global _memory_dropped
    if len(_memory_queue) >= _MAX_QUEUE_SIZE:
        _memory_dropped += 1
        logger.error(
            "token usage memory queue full dropped_total=%d capacity=%d",
            _memory_dropped,
            _MAX_QUEUE_SIZE,
        )
        return False
    _memory_queue.append(doc)
    return True


def _signal_flush() -> None:
    if _flush_wakeup is not None:
        _flush_wakeup.set()


def configure_recorder(flush_handler: FlushHandler | None) -> None:
    """Set the committed batch sink during service startup."""
    global _flush_handler
    _flush_handler = flush_handler


async def record_usage(
    tokens_used: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated: bool = False,
    model: str = "azure-openai",
    operation: str | None = None,
    sub_resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    **override_kwargs: Any,
) -> None:
    """Record one token-usage event without creating unsupervised tasks."""
    if tokens_used <= 0 and not (prompt_tokens or completion_tokens):
        return

    context = get_usage_context()
    now = datetime.now(timezone.utc)
    base = context.to_record_kwargs() if context else {}
    base.update(override_kwargs)
    if operation:
        base["operation"] = operation
    if sub_resource_id:
        base["sub_resource_id"] = sub_resource_id

    resolved_metadata = dict(base.get("metadata") or {})
    if metadata:
        resolved_metadata.update(metadata)
    if context and context.service:
        resolved_metadata.setdefault("service", context.service)

    entity_type = base.get("entity_type") or entity_type_from_resource_type(
        base.get("resource_type", "chat")
    )
    resource_type = base.get("resource_type") or entity_type
    sequence = base.get("call_sequence")
    if context and context.activity_id and sequence is None:
        sequence = _call_counter.get(0) + 1
        _call_counter.set(sequence)
        base["call_sequence"] = sequence

    total = tokens_used or ((prompt_tokens or 0) + (completion_tokens or 0))
    doc: dict[str, Any] = {
        "_id": override_kwargs.get("_id") or f"{PREFIX_TOKEN_USAGE}-{secrets.token_hex(16)}",
        "account_id": base.get("account_id", "unknown"),
        "org_id": base.get("org_id", ""),
        "project_id": base.get("project_id", ""),
        "user_id": base.get("user_id"),
        "user_email": base.get("user_email"),
        "activity_id": base.get("activity_id"),
        "trace_id": base.get("trace_id"),
        "span_id": base.get("span_id") or override_kwargs.get("span_id"),
        "call_sequence": base.get("call_sequence"),
        "parent_call_id": base.get("parent_call_id"),
        "entity_type": entity_type,
        "entity_id": base.get("entity_id") or base.get("resource_id"),
        "operation": base.get("operation"),
        "resource_type": resource_type,
        "resource_id": base.get("resource_id"),
        "sub_resource_id": base.get("sub_resource_id"),
        "tokens_used": total,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated": estimated,
        "model": model,
        "metadata": resolved_metadata or None,
        "resource_refs": base.get("resource_refs") or None,
        "created_at": now,
        "date_bucket": now.strftime("%Y-%m-%d"),
    }
    if not await enqueue_event(doc):
        _append_memory(doc)
    if not _running and _flush_handler is not None:
        _ensure_flush_worker(wake=False, automatic=True)
    else:
        _signal_flush()
    logger.debug(
        "token_usage_recorded activity_id=%s entity_type=%s prompt=%s completion=%s model=%s",
        doc.get("activity_id"),
        entity_type,
        prompt_tokens,
        completion_tokens,
        model,
    )


async def record_from_context(
    tokens_used: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated: bool = False,
    model: str = "azure-openai",
) -> None:
    """Backward-compatible recorder callback."""
    await record_usage(
        tokens_used=tokens_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated=estimated,
        model=model,
    )


async def ingest_events(events: list[dict[str, Any]]) -> None:
    """Ingest prebuilt events into the durable queue."""
    for doc in events:
        if not doc.get("created_at"):
            now = datetime.now(timezone.utc)
            doc["created_at"] = now
            doc.setdefault("date_bucket", now.strftime("%Y-%m-%d"))
        if not doc.get("_id"):
            doc["_id"] = f"{PREFIX_TOKEN_USAGE}-{secrets.token_hex(16)}"
        if not doc.get("entity_type") and doc.get("resource_type"):
            doc["entity_type"] = entity_type_from_resource_type(doc["resource_type"])
        if not doc.get("resource_refs"):
            event_metadata = doc.get("metadata") or {}
            doc["resource_refs"] = (
                merge_resource_refs(
                    build_resource_refs(
                        user_id=doc.get("user_id"),
                        user_email=doc.get("user_email"),
                        conversation_id=doc.get("resource_id"),
                        agent_ids=event_metadata.get("agent_ids"),
                        execution_id=event_metadata.get("execution_id"),
                        trigger_id=event_metadata.get("trigger_id"),
                        schedule_id=event_metadata.get("schedule_id"),
                    )
                )
                or None
            )
        if not await enqueue_event(doc):
            _append_memory(doc)
    _signal_flush()


def _ensure_flush_worker(*, wake: bool, automatic: bool = False) -> None:
    """Create the single supervised worker without yielding to it."""
    global _automatically_started, _flush_task, _flush_wakeup, _running
    if _running:
        if wake:
            _automatically_started = False
            _signal_flush()
        return
    _running = True
    _automatically_started = automatic
    _flush_wakeup = asyncio.Event()
    _flush_task = asyncio.create_task(_flush_loop(), name="platform_metrics_flush")
    if wake:
        _signal_flush()


async def start_recorder() -> None:
    """Start the single supervised flush worker."""
    _ensure_flush_worker(wake=True)


async def stop_recorder() -> None:
    """Stop the worker and perform bounded final drain passes."""
    global _automatically_started, _flush_task, _flush_wakeup, _running
    _running = False
    _signal_flush()
    if _flush_task and not _flush_task.done():
        try:
            await asyncio.wait_for(_flush_task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _flush_task.cancel()
            try:
                await _flush_task
            except asyncio.CancelledError:
                pass
    _flush_task = None
    _flush_wakeup = None
    _automatically_started = False
    from viksa_platform.metrics.transport import queue_length

    for _ in range(_STOP_DRAIN_PASSES):
        flushed = await _flush()
        if await queue_length() <= 0 and (not _memory_queue or not flushed):
            break


async def _flush_loop() -> None:
    while _running:
        try:
            if _flush_wakeup is None:
                return
            try:
                await asyncio.wait_for(
                    _flush_wakeup.wait(),
                    timeout=_FLUSH_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
            _flush_wakeup.clear()
            await _flush()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("platform_metrics flush loop error_type=%s", type(exc).__name__)


async def _flush() -> bool:
    from viksa_platform.metrics.transport import (
        acknowledge_events,
        deliver_http_fallback,
        drain_queue,
        release_events,
    )

    try:
        sink = deliver_http_fallback if _flush_handler is None else _flush_handler
        batch = await drain_queue(_BATCH_SIZE)
        memory_docs: list[dict[str, Any]] = []
        while _memory_queue and len(batch) < _BATCH_SIZE * 2:
            doc = _memory_queue.popleft()
            memory_docs.append(doc)
            batch.append(doc)
        if not batch:
            return False
        try:
            delivered = await asyncio.wait_for(sink(batch), timeout=_FLUSH_TIMEOUT_SECONDS)
            if _flush_handler is None and not delivered:
                raise RuntimeError("token usage sink is not configured")
        except Exception as exc:  # noqa: BLE001
            global _flush_failures, _memory_dropped
            _flush_failures += 1
            await release_events(batch)
            for doc in reversed(memory_docs):
                if len(_memory_queue) >= _MAX_QUEUE_SIZE:
                    _memory_dropped += 1
                else:
                    _memory_queue.appendleft(doc)
            logger.error(
                "platform_metrics flush failed docs=%d error_type=%s failures=%d",
                len(batch),
                type(exc).__name__,
                _flush_failures,
            )
            return False
        await acknowledge_events(batch)
        return True
    finally:
        await _stop_automatic_worker_after_manual_flush()


async def _stop_automatic_worker_after_manual_flush() -> None:
    """Let legacy manual flush callers take ownership without orphaning a task."""
    global _automatically_started, _flush_task, _flush_wakeup, _running
    task = _flush_task
    if not _automatically_started or task is None or asyncio.current_task() is task:
        return
    _running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _flush_task = None
    _flush_wakeup = None
    _automatically_started = False


def get_recorder_stats() -> dict[str, Any]:
    from viksa_platform.metrics.transport import get_transport_stats

    return {
        "memory_queue_size": len(_memory_queue),
        "max_queue_size": _MAX_QUEUE_SIZE,
        "batch_size": _BATCH_SIZE,
        "running": _running,
        "worker_active": _flush_task is not None and not _flush_task.done(),
        "memory_dropped": _memory_dropped,
        "flush_failures": _flush_failures,
        **get_transport_stats(),
    }


__all__ = [
    "configure_recorder",
    "get_recorder_stats",
    "ingest_events",
    "record_from_context",
    "record_usage",
    "start_recorder",
    "stop_recorder",
]
