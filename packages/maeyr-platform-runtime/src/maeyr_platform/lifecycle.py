"""Shared bounded lifecycle contracts and immutable recorder configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class BufferConfig:
    """Bounds for a single-process, in-memory batch recorder."""

    max_queue_size: int = 1_024
    max_batch_size: int = 100
    flush_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        if not 1 <= self.max_batch_size <= self.max_queue_size:
            raise ValueError("max_batch_size must be between 1 and max_queue_size")
        if self.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RecorderStats:
    """A secret-free snapshot of bounded recorder state."""

    accepted: int
    dropped: int
    delivered: int
    failed: int
    queued: int
    running: bool


@runtime_checkable
class BoundedAsyncLifecycle(Protocol):
    """Lifecycle required of injected recorders with a bounded shutdown."""

    @property
    def running(self) -> bool:
        """Whether the lifecycle worker is active."""

    async def start(self) -> None:
        """Start the owned worker; repeated calls are idempotent."""

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        """Wait for accepted work to finish, returning false on timeout."""

    async def stop(self, timeout_seconds: float | None = None) -> bool:
        """Stop accepting work, drain, and stop the worker within the bound."""
