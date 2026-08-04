from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from viksa_platform.lifecycle import BoundedAsyncLifecycle, BufferConfig
from viksa_platform.tracing import (
    BufferedTraceRecorder,
    SpanRecord,
    TraceContext,
    bind_trace_context,
    get_trace_context,
)


class CapturingTraceTransport:
    def __init__(self) -> None:
        self.items: list[SpanRecord] = []

    async def emit_batch(self, items: Sequence[SpanRecord]) -> None:
        self.items.extend(items)


class BlockingTraceTransport:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def emit_batch(self, items: Sequence[SpanRecord]) -> None:
        assert items
        self.entered.set()
        await asyncio.Event().wait()


def _span(name: str = "agent.execute") -> SpanRecord:
    return SpanRecord(
        context=TraceContext(
            trace_id="1" * 32,
            span_id="2" * 16,
        ),
        name=name,
        started_unix_ns=10,
        ended_unix_ns=20,
        attributes={"attempt": 1},
    )


def test_trace_context_round_trips_w3c_headers_and_task_binding() -> None:
    context = TraceContext(trace_id="a" * 32, span_id="b" * 16, sampled=True)
    headers = context.inject_headers({"X-Test": "yes", "TraceParent": "old"})

    assert headers == {"X-Test": "yes", "traceparent": context.traceparent()}
    assert TraceContext.from_headers(headers) == context
    assert get_trace_context() is None
    with bind_trace_context(context):
        assert get_trace_context() == context
    assert get_trace_context() is None


@pytest.mark.asyncio
async def test_trace_recorder_bounds_admission_and_drains_one_worker() -> None:
    transport = CapturingTraceTransport()
    recorder = BufferedTraceRecorder(
        transport,
        BufferConfig(max_queue_size=2, max_batch_size=2, flush_interval_seconds=0.01),
    )
    assert isinstance(recorder, BoundedAsyncLifecycle)

    await recorder.start()
    assert recorder.record(_span("first")) is True
    assert recorder.record(_span("second")) is True
    assert recorder.record(_span("overflow")) is False
    assert await recorder.stop(timeout_seconds=1.0) is True

    assert [item.name for item in transport.items] == ["first", "second"]
    stats = recorder.stats()
    assert stats.accepted == 2
    assert stats.dropped == 1
    assert stats.delivered == 2
    assert stats.failed == 0
    assert stats.queued == 0
    assert stats.running is False


@pytest.mark.asyncio
async def test_trace_recorder_shutdown_is_time_bounded() -> None:
    transport = BlockingTraceTransport()
    recorder = BufferedTraceRecorder(
        transport,
        BufferConfig(max_queue_size=1, max_batch_size=1, flush_interval_seconds=0.01),
    )
    await recorder.start()
    assert recorder.record(_span()) is True
    await asyncio.wait_for(transport.entered.wait(), timeout=1.0)

    assert await recorder.stop(timeout_seconds=0.01) is False
    stats = recorder.stats()
    assert stats.failed == 1
    assert stats.queued == 0
    assert stats.running is False
