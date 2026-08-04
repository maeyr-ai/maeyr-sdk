"""Typed usage/metric contracts with bounded, instance-first recording."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from viksa_platform._buffered import BufferedDispatcher
from viksa_platform.lifecycle import BoundedAsyncLifecycle, BufferConfig, RecorderStats
from viksa_platform.security.internal import TenantContext

MetricValue = int | float
MetricAttributeValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class UsageContext:
    """Tenant-qualified usage attribution supplied by the application."""

    tenant: TenantContext
    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("resource_type", self.resource_type),
            ("resource_id", self.resource_id),
        ):
            normalized = value.strip()
            if not normalized or len(normalized) > 256 or "\n" in normalized or "\r" in normalized:
                raise ValueError(f"{label} is invalid")
            object.__setattr__(self, label, normalized)


@dataclass(frozen=True, slots=True)
class MetricEvent:
    """Transport-neutral, immutable metric or usage event."""

    name: str
    value: MetricValue
    timestamp_unix_ns: int
    context: UsageContext
    attributes: Mapping[str, MetricAttributeValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 256 or "\n" in name or "\r" in name:
            raise ValueError("metric name is invalid")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        if self.timestamp_unix_ns < 0:
            raise ValueError("metric timestamp is invalid")
        normalized: dict[str, MetricAttributeValue] = {}
        for key, value in self.attributes.items():
            if not key or len(key) > 256 or "\n" in key or "\r" in key:
                raise ValueError("metric attribute key is invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("metric attribute values must be finite")
            normalized[key] = value
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "attributes", MappingProxyType(normalized))

    @classmethod
    def now(
        cls,
        *,
        name: str,
        value: MetricValue,
        context: UsageContext,
        attributes: Mapping[str, MetricAttributeValue] | None = None,
    ) -> MetricEvent:
        return cls(
            name=name,
            value=value,
            timestamp_unix_ns=time.time_ns(),
            context=context,
            attributes=attributes or {},
        )


class MetricsTransport(Protocol):
    """Injected acknowledgement boundary for a batch of usage events."""

    async def emit_batch(self, items: Sequence[MetricEvent]) -> None:
        """Deliver or durably reserve a batch, raising on failure."""


class MetricsRecorder(BoundedAsyncLifecycle, Protocol):
    """Application-facing usage recorder port."""

    def record(self, event: MetricEvent) -> bool:
        """Accept an event into the bounded queue, or return false."""

    def stats(self) -> RecorderStats:
        """Return a secret-free state snapshot."""


class BufferedMetricsRecorder:
    """Bounded single-worker implementation of the metrics recorder port."""

    def __init__(
        self,
        transport: MetricsTransport,
        config: BufferConfig | None = None,
    ) -> None:
        self._dispatcher = BufferedDispatcher(transport, config or BufferConfig())

    @property
    def running(self) -> bool:
        return self._dispatcher.running

    async def start(self) -> None:
        await self._dispatcher.start()

    def record(self, event: MetricEvent) -> bool:
        return self._dispatcher.submit(event)

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        return await self._dispatcher.drain(timeout_seconds)

    async def stop(self, timeout_seconds: float | None = None) -> bool:
        return await self._dispatcher.stop(timeout_seconds)

    def stats(self) -> RecorderStats:
        return self._dispatcher.stats()


_default_recorder: BufferedMetricsRecorder | None = None


def configure_recorder(
    transport: MetricsTransport,
    *,
    config: BufferConfig | None = None,
) -> BufferedMetricsRecorder:
    """Configure the migration-only process-global recorder facade."""

    global _default_recorder
    if _default_recorder is not None and _default_recorder.running:
        raise RuntimeError("cannot replace a running default metrics recorder")
    _default_recorder = BufferedMetricsRecorder(transport, config)
    return _default_recorder


def configure_transport(
    transport: MetricsTransport,
    *,
    config: BufferConfig | None = None,
) -> BufferedMetricsRecorder:
    """Compatibility alias for configure_recorder."""

    return configure_recorder(transport, config=config)


async def start_recorder() -> None:
    if _default_recorder is None:
        raise RuntimeError("default metrics recorder is not configured")
    await _default_recorder.start()


def record_usage(event: MetricEvent) -> bool:
    if _default_recorder is None:
        return False
    return _default_recorder.record(event)


async def drain_recorder(timeout_seconds: float | None = None) -> bool:
    if _default_recorder is None:
        return True
    return await _default_recorder.drain(timeout_seconds)


async def stop_recorder(timeout_seconds: float | None = None) -> bool:
    if _default_recorder is None:
        return True
    return await _default_recorder.stop(timeout_seconds)


def get_recorder_stats() -> RecorderStats:
    if _default_recorder is None:
        return RecorderStats(0, 0, 0, 0, 0, False)
    return _default_recorder.stats()


__all__ = [
    "BufferedMetricsRecorder",
    "MetricAttributeValue",
    "MetricEvent",
    "MetricValue",
    "MetricsRecorder",
    "MetricsTransport",
    "UsageContext",
    "configure_recorder",
    "configure_transport",
    "drain_recorder",
    "get_recorder_stats",
    "record_usage",
    "start_recorder",
    "stop_recorder",
]
