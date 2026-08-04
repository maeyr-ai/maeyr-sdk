from __future__ import annotations

from collections.abc import Sequence

import pytest
from viksa_platform.lifecycle import BufferConfig
from viksa_platform.metrics import (
    BufferedMetricsRecorder,
    MetricEvent,
    UsageContext,
)
from viksa_platform.security.internal import TenantContext


class CapturingMetricsTransport:
    def __init__(self) -> None:
        self.items: list[MetricEvent] = []

    async def emit_batch(self, items: Sequence[MetricEvent]) -> None:
        self.items.extend(items)


@pytest.mark.asyncio
async def test_metrics_recorder_uses_typed_tenant_context_and_bounded_delivery() -> None:
    transport = CapturingMetricsTransport()
    recorder = BufferedMetricsRecorder(
        transport,
        BufferConfig(max_queue_size=1, max_batch_size=1, flush_interval_seconds=0.01),
    )
    event = MetricEvent(
        name="token.usage",
        value=12,
        timestamp_unix_ns=100,
        context=UsageContext(
            tenant=TenantContext("AC-1", "OR-1", "PR-1"),
            resource_type="agent",
            resource_id="AI-1",
        ),
        attributes={"model": "test"},
    )

    await recorder.start()
    assert recorder.record(event) is True
    assert recorder.record(event) is False
    assert await recorder.stop(timeout_seconds=1.0) is True

    assert transport.items == [event]
    assert recorder.stats().delivered == 1
    assert recorder.stats().dropped == 1


def test_metric_event_rejects_non_finite_values() -> None:
    context = UsageContext(
        tenant=TenantContext(account_id="AC-1"),
        resource_type="agent",
        resource_id="AI-1",
    )
    with pytest.raises(ValueError):
        MetricEvent.now(name="token.usage", value=float("nan"), context=context)
