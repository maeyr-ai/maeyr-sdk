import asyncio
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from maeyr_platform.tracing import transport
from maeyr_platform.tracing.constants import REDIS_PROCESSING_QUEUE_KEY
from maeyr_platform.tracing.remote_recorder import RemoteTraceRecorder


def _span() -> dict[str, object]:
    return {
        "span_id": "0123456789abcdef",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "account_id": "account-1",
        "org_id": "org-1",
        "project_id": "project-1",
        "span_name": "telegram.receive",
        "status": "running",
    }


@pytest.mark.asyncio
async def test_acknowledged_push_returns_after_remote_acceptance() -> None:
    recorder = RemoteTraceRecorder("volt-engine-service", internal_key="k" * 32)

    with (
        patch.object(
            RemoteTraceRecorder,
            "_post_once",
            new=AsyncMock(return_value=(True, False)),
        ) as post,
        patch.object(RemoteTraceRecorder, "schedule_push") as queue,
        patch(
            "maeyr_platform.tracing.transport.enqueue_span",
            new=AsyncMock(return_value=False),
        ),
    ):
        accepted = await recorder.push_spans_acknowledged([_span()])

    assert accepted is True
    post.assert_awaited_once()
    queue.assert_not_called()
    assert recorder.sent_spans == 1


@pytest.mark.asyncio
async def test_failed_acknowledgement_requeues_same_span_id() -> None:
    recorder = RemoteTraceRecorder("chat-service", internal_key="k" * 32)

    with (
        patch.object(
            RemoteTraceRecorder,
            "_post_once",
            new=AsyncMock(return_value=(False, True)),
        ),
        patch.object(RemoteTraceRecorder, "schedule_push") as queue,
        patch(
            "maeyr_platform.tracing.transport.enqueue_span",
            new=AsyncMock(return_value=False),
        ),
    ):
        accepted = await recorder.push_spans_acknowledged([_span()])

    assert accepted is False
    queued = queue.call_args.args[0]
    assert queued[0]["span_id"] == "0123456789abcdef"
    assert recorder.delivery_stats["failed"] == 1


@pytest.mark.asyncio
async def test_failed_http_acknowledgement_uses_durable_outbox() -> None:
    recorder = RemoteTraceRecorder("chat-service", internal_key="k" * 32)

    with (
        patch.object(
            RemoteTraceRecorder,
            "_post_once",
            new=AsyncMock(return_value=(False, True)),
        ) as direct_http,
        patch.object(RemoteTraceRecorder, "schedule_push") as memory_queue,
        patch(
            "maeyr_platform.tracing.transport.enqueue_span",
            new=AsyncMock(return_value=True),
        ) as durable_queue,
    ):
        accepted = await recorder.push_spans_acknowledged([_span()])

    assert accepted is True
    durable_queue.assert_awaited_once()
    direct_http.assert_not_awaited()
    memory_queue.assert_not_called()
    assert recorder.durably_queued_spans == 1
    assert recorder.delivery_stats == {
        "queued": 0,
        "sent": 0,
        "failed": 0,
        "dropped": 0,
    }


@pytest.mark.asyncio
async def test_degraded_acknowledgement_has_a_short_circuit_broken_budget() -> None:
    recorder = RemoteTraceRecorder(
        "chat-service",
        internal_key="k" * 32,
        ack_http_timeout_seconds=0.01,
    )

    async def stalled_http(_spans: object) -> tuple[bool, bool]:
        await asyncio.sleep(1)
        return True, False

    with (
        patch(
            "maeyr_platform.tracing.transport.enqueue_span",
            new=AsyncMock(return_value=False),
        ),
        patch.object(RemoteTraceRecorder, "_post_once", side_effect=stalled_http) as post,
        patch.object(RemoteTraceRecorder, "schedule_push"),
    ):
        started = time.perf_counter()
        assert await recorder.push_spans_acknowledged([_span()]) is False
        first_elapsed = time.perf_counter() - started
        assert await recorder.push_spans_acknowledged([_span()]) is False

    assert first_elapsed < 0.1
    assert post.await_count == 1


class _CappedRedis:
    def __init__(self) -> None:
        self.pending = 0
        self.processing = 0

    async def eval(self, _script: str, _keys: int, *args: object) -> int:
        maximum = int(cast(int, args[-1]))
        if self.pending + self.processing >= maximum:
            return 0
        self.pending += 1
        return 1


class _ReliableRedis:
    def __init__(self) -> None:
        self.queues: dict[str, list[object]] = {}

    async def eval(self, _script: str, _keys: int, *args: object) -> int:
        pending_key = str(args[0])
        processing_key = str(args[1])
        payload = args[2]
        maximum = int(cast(int, args[3]))
        if (
            len(self.queues.get(pending_key, [])) + len(self.queues.get(processing_key, []))
            >= maximum
        ):
            return 0
        await self.lpush(pending_key, payload)
        return 1

    async def lpush(self, key: str, value: object) -> int:
        self.queues.setdefault(key, []).insert(0, value)
        return len(self.queues[key])

    async def rpoplpush(self, source: str, destination: str) -> object | None:
        source_queue = self.queues.setdefault(source, [])
        if not source_queue:
            return None
        value = source_queue.pop()
        await self.lpush(destination, value)
        return value

    async def lrem(self, key: str, count: int, value: object) -> int:
        queue = self.queues.setdefault(key, [])
        removed = 0
        kept: list[object] = []
        for item in queue:
            if item == value and removed < count:
                removed += 1
            else:
                kept.append(item)
        self.queues[key] = kept
        return removed

    async def llen(self, key: str) -> int:
        return len(self.queues.get(key, []))


class _RecoveryFailsOnceRedis(_ReliableRedis):
    def __init__(self) -> None:
        super().__init__()
        self.fail_recovery = True

    async def rpoplpush(self, source: str, destination: str) -> object | None:
        if source == REDIS_PROCESSING_QUEUE_KEY and self.fail_recovery:
            self.fail_recovery = False
            raise ConnectionError("temporary Redis failure")
        return await super().rpoplpush(source, destination)


@pytest.mark.asyncio
async def test_trace_outbox_rejects_work_at_pending_plus_processing_cap() -> None:
    fake = _CappedRedis()
    fake.processing = 1
    transport.configure_transport(SimpleNamespace(redis=fake), queue_max=2)
    try:
        assert await transport.enqueue_span(_span()) is True
        assert await transport.enqueue_span({**_span(), "span_id": "fedcba9876543210"}) is False
        assert transport.get_transport_stats()["redis_queue_full_rejections"] >= 1
    finally:
        transport.configure_transport(None, queue_max=100_000)


@pytest.mark.asyncio
async def test_reserved_span_is_acknowledged_only_after_sink_commit() -> None:
    fake = _ReliableRedis()
    transport.configure_transport(SimpleNamespace(redis=fake), queue_max=10)
    try:
        assert await transport.enqueue_span(_span()) is True
        batch = await transport.drain_queue(1)
        assert len(batch) == 1
        assert await transport.queue_length() == 1

        assert await transport.acknowledge_spans(batch) == 1
        assert await transport.queue_length() == 0
    finally:
        transport.configure_transport(None, queue_max=100_000)


@pytest.mark.asyncio
async def test_crash_orphaned_reservation_is_recovered_on_restart() -> None:
    fake = _ReliableRedis()
    transport.configure_transport(SimpleNamespace(redis=fake), queue_max=10)
    try:
        assert await transport.enqueue_span(_span()) is True
        first_delivery = await transport.drain_queue(1)
        assert len(first_delivery) == 1

        # Reconfiguration models a new consumer process. Its first drain moves
        # the unacknowledged reservation back to pending before reserving it.
        transport.configure_transport(SimpleNamespace(redis=fake), queue_max=10)
        recovered = await transport.drain_queue(1)
        assert len(recovered) == 1
        assert recovered[0]["span_id"] == _span()["span_id"]
        assert await transport.acknowledge_spans(recovered) == 1
        assert await transport.queue_length() == 0
    finally:
        transport.configure_transport(None, queue_max=100_000)


@pytest.mark.asyncio
async def test_failed_reservation_recovery_is_retried_before_new_delivery() -> None:
    fake = _RecoveryFailsOnceRedis()
    transport.configure_transport(SimpleNamespace(redis=fake), queue_max=10)
    try:
        assert await transport.enqueue_span(_span()) is True
        assert await transport.drain_queue(1) == []
        assert transport.get_transport_stats()["recovery_required"] is True

        recovered = await transport.drain_queue(1)
        assert len(recovered) == 1
        assert transport.get_transport_stats()["recovery_required"] is False
        assert await transport.acknowledge_spans(recovered) == 1
    finally:
        transport.configure_transport(None, queue_max=100_000)


@pytest.mark.asyncio
async def test_external_durable_fallback_acknowledges_when_redis_is_unavailable() -> None:
    durable = AsyncMock(return_value=True)
    transport.configure_transport(None, durable_fallback=durable)
    try:
        span = _span()
        assert await transport.enqueue_span(span) is True
        durable.assert_awaited_once_with(span)
        assert transport.get_transport_stats()["durable_fallback_accepts"] >= 1
    finally:
        transport.configure_transport(None)


@pytest.mark.asyncio
async def test_reenqueue_uses_external_storage_before_process_memory() -> None:
    durable = AsyncMock(return_value=True)
    transport._dead_letter_memory.clear()
    transport.configure_transport(None, durable_fallback=durable)
    try:
        spans = [_span(), {**_span(), "span_id": "fedcba9876543210"}]
        assert await transport.re_enqueue_spans(spans) == 2
        assert len(transport._dead_letter_memory) == 0
        assert durable.await_count == 2
    finally:
        transport.configure_transport(None)


@pytest.mark.asyncio
async def test_existing_process_dead_letter_drains_to_external_storage() -> None:
    transport.configure_transport(None)
    transport._dead_letter_memory.clear()
    transport._dead_letter_memory.append(_span())
    durable = AsyncMock(return_value=True)
    transport.configure_transport(None, durable_fallback=durable)
    try:
        await asyncio.sleep(0)
        await transport.drain_dead_letter_memory()
        assert len(transport._dead_letter_memory) == 0
        durable.assert_awaited()
    finally:
        transport.configure_transport(None)


def test_durable_outbox_event_id_is_retry_stable_and_revision_specific() -> None:
    start = _span()
    assert transport.durable_outbox_event_id(start) == transport.durable_outbox_event_id(start)
    completed = {**start, "status": "ok", "_is_completion": True}
    assert transport.durable_outbox_event_id(start) != transport.durable_outbox_event_id(completed)


@pytest.mark.asyncio
async def test_background_http_failure_moves_unresolved_span_to_dead_letter() -> None:
    recorder = RemoteTraceRecorder("chat-service", internal_key="k" * 32)

    with (
        patch.object(
            RemoteTraceRecorder,
            "_post_with_retry",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            RemoteTraceRecorder,
            "_enqueue_durable_fallback",
            new=AsyncMock(side_effect=lambda spans: spans),
        ),
        patch(
            "maeyr_platform.tracing.transport.re_enqueue_spans",
            new=AsyncMock(return_value=0),
        ) as dead_letter,
    ):
        recorder.schedule_push([_span()])
        recorder._closing = True
        assert recorder._drain_task is not None
        await recorder._drain_task

    dead_letter.assert_awaited_once()
    assert dead_letter.await_args is not None
    assert dead_letter.await_args.args[0][0]["span_id"] == "0123456789abcdef"
