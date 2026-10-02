"""Bounded admission for legacy fire-and-forget trace I/O."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

logger = logging.getLogger("platform_traces.tasks")


def _configured_limit() -> int:
    try:
        requested = int(os.getenv("TRACE_BACKGROUND_MAX_TASKS", "512"))
    except (TypeError, ValueError):
        requested = 512
    return max(1, min(requested, 4096))


_MAX_INFLIGHT_TASKS = _configured_limit()
_inflight: set[asyncio.Task[None]] = set()
_dropped = 0


def schedule_trace_task(
    factory: Callable[[], Awaitable[object]], *, name: str
) -> asyncio.Task[None] | None:
    """Schedule telemetry only while the process has available I/O capacity."""
    global _dropped
    if len(_inflight) >= _MAX_INFLIGHT_TASKS:
        _dropped += 1
        if _dropped == 1 or _dropped & (_dropped - 1) == 0:
            logger.warning(
                "trace background task capacity exhausted dropped=%d max=%d",
                _dropped,
                _MAX_INFLIGHT_TASKS,
            )
        return None

    async def _run() -> None:
        try:
            await factory()
        except Exception:
            logger.exception("trace background task failed name=%s", name)

    task = asyncio.create_task(_run(), name=name)
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


async def drain_trace_tasks(timeout_seconds: float = 5.0) -> bool:
    """Best-effort shutdown drain with a fixed deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while _inflight:
        remaining = deadline - loop.time()
        if remaining <= 0:
            for task in tuple(_inflight):
                task.cancel()
            return False
        await asyncio.wait(tuple(_inflight), timeout=remaining)
    return True


def trace_task_stats() -> dict[str, int]:
    return {
        "background_tasks_inflight": len(_inflight),
        "background_tasks_max": _MAX_INFLIGHT_TASKS,
        "background_tasks_dropped": _dropped,
    }
