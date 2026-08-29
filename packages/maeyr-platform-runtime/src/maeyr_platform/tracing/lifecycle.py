"""Shared running/completion span lifecycle for service-owned recorders."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Protocol, cast

from maeyr_platform.tracing.errors import attach_error_to_span_kwargs
from maeyr_platform.tracing.ids import generate_span_id


class _SpanRecorder(Protocol):
    """The legacy recorder surface required by lifecycle orchestration."""

    def schedule_span_start(self, **kwargs: Any) -> None: ...

    def schedule_span_end(self, **kwargs: Any) -> None: ...

    async def record_span_start(self, **kwargs: Any) -> Any: ...

    async def record_span_end(self, **kwargs: Any) -> Any: ...


def _service_recorder() -> _SpanRecorder:
    """Resolve the importing service's configured recorder compatibility boundary."""
    return cast(_SpanRecorder, import_module("common.platform_traces.recorder"))


@dataclass(frozen=True, slots=True)
class SpanHandle:
    """Handle for a long-running span started with emit/schedule lifecycle helpers."""

    span_id: str
    started_at: datetime
    perf_started: float | None = None


def make_span_handle(
    span_id: str,
    *,
    started_at: datetime | None = None,
    perf_started: float | None = None,
) -> SpanHandle:
    return SpanHandle(
        span_id=span_id,
        started_at=started_at or datetime.now(timezone.utc),
        perf_started=perf_started if perf_started is not None else time.perf_counter(),
    )


def resolve_duration_ms(
    handle: SpanHandle,
    *,
    ended_at: datetime | None = None,
    duration_ms: int | None = None,
) -> tuple[datetime, int]:
    """Return (ended_at, duration_ms) from explicit value, perf counter, or wall clock."""
    ended = ended_at or datetime.now(timezone.utc)
    if duration_ms is not None:
        return ended, max(0, int(duration_ms))
    if handle.perf_started is not None:
        return ended, max(0, round((time.perf_counter() - handle.perf_started) * 1000))
    delta = (ended - handle.started_at).total_seconds() * 1000
    return ended, max(0, round(delta))


def _start_kwargs(**kwargs: Any) -> tuple[dict[str, Any], SpanHandle]:
    span_id = kwargs.pop("span_id", None) or generate_span_id()
    started_at = kwargs.pop("started_at", None) or datetime.now(timezone.utc)
    perf_started = kwargs.pop("perf_started", None)
    if perf_started is None:
        perf_started = time.perf_counter()
    base: dict[str, Any] = {
        "span_id": span_id,
        "started_at": started_at,
        "status": "running",
        "duration_ms": 0,
        **kwargs,
    }
    return base, make_span_handle(
        span_id,
        started_at=started_at,
        perf_started=perf_started,
    )


def _end_kwargs(handle: SpanHandle, **kwargs: Any) -> dict[str, Any]:
    ended_at, duration_ms = resolve_duration_ms(
        handle,
        ended_at=kwargs.pop("ended_at", None),
        duration_ms=kwargs.pop("duration_ms", None),
    )
    attach_error_to_span_kwargs(kwargs)
    return {
        "span_id": handle.span_id,
        "started_at": handle.started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "_is_completion": True,
        **kwargs,
    }


def schedule_lifecycle_start(**kwargs: Any) -> SpanHandle:
    """Schedule a non-blocking span start through the service recorder."""
    start_kwargs, handle = _start_kwargs(**kwargs)
    _service_recorder().schedule_span_start(**start_kwargs)
    return handle


def schedule_lifecycle_end(handle: SpanHandle, **kwargs: Any) -> None:
    """Schedule a non-blocking span completion through the service recorder."""
    _service_recorder().schedule_span_end(**_end_kwargs(handle, **kwargs))


def schedule_root_start(**kwargs: Any) -> SpanHandle:
    root_kwargs = dict(kwargs)
    root_kwargs.pop("is_root", None)
    return schedule_lifecycle_start(is_root=True, **root_kwargs)


def schedule_root_end(handle: SpanHandle, **kwargs: Any) -> None:
    root_kwargs = dict(kwargs)
    root_kwargs.pop("is_root", None)
    schedule_lifecycle_end(handle, is_root=True, **root_kwargs)


async def emit_lifecycle_start(**kwargs: Any) -> SpanHandle:
    """Await a span start through the service recorder."""
    start_kwargs, handle = _start_kwargs(**kwargs)
    await _service_recorder().record_span_start(**start_kwargs)
    return handle


async def emit_lifecycle_end(handle: SpanHandle, **kwargs: Any) -> None:
    """Await a span completion through the service recorder."""
    await _service_recorder().record_span_end(**_end_kwargs(handle, **kwargs))


async def emit_root_start(**kwargs: Any) -> SpanHandle:
    root_kwargs = dict(kwargs)
    root_kwargs.pop("is_root", None)
    return await emit_lifecycle_start(is_root=True, **root_kwargs)


async def emit_root_end(handle: SpanHandle, **kwargs: Any) -> None:
    root_kwargs = dict(kwargs)
    root_kwargs.pop("is_root", None)
    await emit_lifecycle_end(handle, is_root=True, **root_kwargs)


__all__ = [
    "SpanHandle",
    "emit_lifecycle_end",
    "emit_lifecycle_start",
    "emit_root_end",
    "emit_root_start",
    "make_span_handle",
    "resolve_duration_ms",
    "schedule_lifecycle_end",
    "schedule_lifecycle_start",
    "schedule_root_end",
    "schedule_root_start",
]
