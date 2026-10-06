"""Trace sink failures must not create unbounded background work."""

from __future__ import annotations

import asyncio

import pytest

from maeyr_platform.tracing import _tasks, recorder, transport


@pytest.mark.asyncio
async def test_trace_task_admission_has_a_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _tasks.drain_trace_tasks()
    monkeypatch.setattr(_tasks, "_MAX_INFLIGHT_TASKS", 2)
    monkeypatch.setattr(_tasks, "_dropped", 0)
    release = asyncio.Event()

    async def blocked() -> None:
        await release.wait()

    assert _tasks.schedule_trace_task(blocked, name="first") is not None
    assert _tasks.schedule_trace_task(blocked, name="second") is not None
    assert _tasks.schedule_trace_task(blocked, name="rejected") is None
    assert _tasks.trace_task_stats()["background_tasks_inflight"] == 2
    assert _tasks.trace_task_stats()["background_tasks_dropped"] == 1

    release.set()
    assert await _tasks.drain_trace_tasks()
    assert _tasks.trace_task_stats()["background_tasks_inflight"] == 0


@pytest.mark.asyncio
async def test_redis_outage_schedules_only_one_immediate_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _tasks.drain_trace_tasks()
    release = asyncio.Event()

    async def blocked_flush() -> None:
        await release.wait()

    monkeypatch.setattr(recorder, "_flush", blocked_flush)
    monkeypatch.setattr(recorder, "_immediate_flush_task", None)

    for _ in range(100):
        recorder._schedule_immediate_flush()
    assert _tasks.trace_task_stats()["background_tasks_inflight"] == 1

    release.set()
    assert await _tasks.drain_trace_tasks()


@pytest.mark.asyncio
async def test_http_fallback_tasks_share_background_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _tasks.drain_trace_tasks()
    monkeypatch.setattr(_tasks, "_MAX_INFLIGHT_TASKS", 2)
    monkeypatch.setattr(_tasks, "_dropped", 0)
    release = asyncio.Event()

    async def blocked_fallback(_doc: dict[str, object]) -> None:
        await release.wait()

    transport.configure_transport(None, http_fallback=blocked_fallback)
    try:
        for index in range(10):
            assert (
                await transport.enqueue_span({"span_id": str(index), "span_name": "worker.execute"})
                is False
            )
        assert _tasks.trace_task_stats()["background_tasks_inflight"] == 2
        assert _tasks.trace_task_stats()["background_tasks_dropped"] == 8
    finally:
        release.set()
        assert await _tasks.drain_trace_tasks()
        transport.configure_transport(None)


@pytest.mark.asyncio
async def test_dead_letter_recovery_has_one_background_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _tasks.drain_trace_tasks()
    release = asyncio.Event()
    drains = 0

    async def blocked_drain() -> int:
        nonlocal drains
        drains += 1
        await release.wait()
        return 0

    monkeypatch.setattr(transport, "drain_dead_letter_memory", blocked_drain)
    monkeypatch.setattr(transport, "_dead_letter_drain_task", None)
    transport._dead_letter_memory.append({"span_id": "pending"})
    try:
        for _ in range(100):
            transport._schedule_dead_letter_drain()
        assert _tasks.trace_task_stats()["background_tasks_inflight"] == 1
        await asyncio.sleep(0)
        assert drains == 1
    finally:
        release.set()
        assert await _tasks.drain_trace_tasks()
        transport._dead_letter_memory.clear()
