"""Typed tracing contracts with bounded, instance-first recording."""

from __future__ import annotations

import math
import re
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from viksa_platform._buffered import BufferedDispatcher
from viksa_platform.lifecycle import BoundedAsyncLifecycle, BufferConfig, RecorderStats

AttributeValue = str | int | float | bool | None

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_TRACEPARENT_PATTERN = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


def _valid_nonzero_hex(value: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.fullmatch(value)) and any(character != "0" for character in value)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """A validated W3C trace context independent of any tracing vendor."""

    trace_id: str
    span_id: str
    sampled: bool = True

    def __post_init__(self) -> None:
        if not _valid_nonzero_hex(self.trace_id, _TRACE_ID_PATTERN):
            raise ValueError("trace_id must be a non-zero lowercase 128-bit hex value")
        if not _valid_nonzero_hex(self.span_id, _SPAN_ID_PATTERN):
            raise ValueError("span_id must be a non-zero lowercase 64-bit hex value")

    @classmethod
    def new_root(cls, *, sampled: bool = True) -> TraceContext:
        return cls(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            sampled=sampled,
        )

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> TraceContext | None:
        value = next(
            (
                str(item).strip().lower()
                for name, item in headers.items()
                if name.lower() == "traceparent"
            ),
            "",
        )
        match = _TRACEPARENT_PATTERN.fullmatch(value)
        if match is None:
            return None
        flags = int(match.group("flags"), 16)
        try:
            return cls(
                trace_id=match.group("trace_id"),
                span_id=match.group("span_id"),
                sampled=bool(flags & 0x01),
            )
        except ValueError:
            return None

    def child(self) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            sampled=self.sampled,
        )

    def traceparent(self) -> str:
        flags = "01" if self.sampled else "00"
        return f"00-{self.trace_id}-{self.span_id}-{flags}"

    def inject_headers(self, headers: Mapping[str, str] | None = None) -> dict[str, str]:
        result = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() != "traceparent"
        }
        result["traceparent"] = self.traceparent()
        return result


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """Transport-neutral, immutable span data accepted by a trace recorder."""

    context: TraceContext
    name: str
    started_unix_ns: int
    ended_unix_ns: int
    parent_span_id: str | None = None
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 256 or "\n" in name or "\r" in name:
            raise ValueError("span name is invalid")
        if self.started_unix_ns < 0 or self.ended_unix_ns < self.started_unix_ns:
            raise ValueError("span timestamps are invalid")
        if self.parent_span_id is not None and not _valid_nonzero_hex(
            self.parent_span_id, _SPAN_ID_PATTERN
        ):
            raise ValueError("parent_span_id is invalid")
        normalized: dict[str, AttributeValue] = {}
        for key, value in self.attributes.items():
            if not key or len(key) > 256 or "\n" in key or "\r" in key:
                raise ValueError("span attribute key is invalid")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("span attribute values must be finite")
            normalized[key] = value
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "attributes", MappingProxyType(normalized))

    @classmethod
    def instant(
        cls,
        *,
        context: TraceContext,
        name: str,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> SpanRecord:
        timestamp = time.time_ns()
        return cls(
            context=context,
            name=name,
            started_unix_ns=timestamp,
            ended_unix_ns=timestamp,
            attributes=attributes or {},
        )


class TraceTransport(Protocol):
    """Injected acknowledgement boundary for a batch of spans."""

    async def emit_batch(self, items: Sequence[SpanRecord]) -> None:
        """Deliver or durably reserve a batch, raising on failure."""


class TraceRecorder(BoundedAsyncLifecycle, Protocol):
    """Application-facing trace recorder port."""

    def record(self, span: SpanRecord) -> bool:
        """Accept a span into the bounded queue, or return false."""

    def stats(self) -> RecorderStats:
        """Return a secret-free state snapshot."""


class BufferedTraceRecorder:
    """Bounded single-worker implementation of the trace recorder port."""

    def __init__(
        self,
        transport: TraceTransport,
        config: BufferConfig | None = None,
    ) -> None:
        self._dispatcher = BufferedDispatcher(transport, config or BufferConfig())

    @property
    def running(self) -> bool:
        return self._dispatcher.running

    async def start(self) -> None:
        await self._dispatcher.start()

    def record(self, span: SpanRecord) -> bool:
        return self._dispatcher.submit(span)

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        return await self._dispatcher.drain(timeout_seconds)

    async def stop(self, timeout_seconds: float | None = None) -> bool:
        return await self._dispatcher.stop(timeout_seconds)

    def stats(self) -> RecorderStats:
        return self._dispatcher.stats()


_trace_context: ContextVar[TraceContext | None] = ContextVar(
    "viksa_trace_context",
    default=None,
)
_default_recorder: BufferedTraceRecorder | None = None


def get_trace_context() -> TraceContext | None:
    return _trace_context.get()


def set_trace_context(context: TraceContext | None) -> Token[TraceContext | None]:
    return _trace_context.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    _trace_context.reset(token)


@contextmanager
def bind_trace_context(context: TraceContext) -> Iterator[TraceContext]:
    token = set_trace_context(context)
    try:
        yield context
    finally:
        reset_trace_context(token)


def configure_recorder(
    transport: TraceTransport,
    *,
    config: BufferConfig | None = None,
) -> BufferedTraceRecorder:
    """Configure the migration-only process-global recorder facade."""

    global _default_recorder
    if _default_recorder is not None and _default_recorder.running:
        raise RuntimeError("cannot replace a running default trace recorder")
    _default_recorder = BufferedTraceRecorder(transport, config)
    return _default_recorder


def configure_transport(
    transport: TraceTransport,
    *,
    config: BufferConfig | None = None,
) -> BufferedTraceRecorder:
    """Compatibility alias for configure_recorder."""

    return configure_recorder(transport, config=config)


async def start_recorder() -> None:
    if _default_recorder is None:
        raise RuntimeError("default trace recorder is not configured")
    await _default_recorder.start()


def record_span(span: SpanRecord) -> bool:
    if _default_recorder is None:
        return False
    return _default_recorder.record(span)


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
    "AttributeValue",
    "BufferedTraceRecorder",
    "SpanRecord",
    "TraceContext",
    "TraceRecorder",
    "TraceTransport",
    "bind_trace_context",
    "configure_recorder",
    "configure_transport",
    "drain_recorder",
    "get_recorder_stats",
    "get_trace_context",
    "record_span",
    "reset_trace_context",
    "set_trace_context",
    "start_recorder",
    "stop_recorder",
]
