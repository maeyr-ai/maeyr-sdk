"""Internal single-worker bounded dispatcher shared by public recorders."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Generic, Protocol, TypeVar

from viksa_platform.lifecycle import BufferConfig, RecorderStats

T = TypeVar("T")
T_contra = TypeVar("T_contra", contravariant=True)


class BatchTransport(Protocol[T_contra]):
    """Acknowledging transport used by the bounded dispatcher."""

    async def emit_batch(self, items: Sequence[T_contra]) -> None:
        """Return only after the batch has reached the transport boundary."""


class BufferedDispatcher(Generic[T]):
    """A bounded queue supervised by exactly one asynchronous worker."""

    def __init__(self, transport: BatchTransport[T], config: BufferConfig) -> None:
        self._transport = transport
        self._config = config
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=config.max_queue_size)
        self._state_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False
        self._stop_requested = False
        self._accepted = 0
        self._dropped = 0
        self._delivered = 0
        self._failed = 0

    @property
    def running(self) -> bool:
        worker = self._worker
        return worker is not None and not worker.done()

    async def start(self) -> None:
        async with self._state_lock:
            if self.running:
                return
            if not self._queue.empty():
                raise RuntimeError("cannot restart a dispatcher with undrained work")
            self._stop_requested = False
            self._accepting = True
            self._worker = asyncio.create_task(
                self._run(),
                name=f"{type(self).__name__}-worker",
            )

    def submit(self, item: T) -> bool:
        if not self._accepting or not self.running:
            self._dropped += 1
            return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        self._accepted += 1
        return True

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        self._validate_timeout(timeout_seconds)
        try:
            if timeout_seconds is None:
                await self._queue.join()
            else:
                await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def stop(self, timeout_seconds: float | None = None) -> bool:
        self._validate_timeout(timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        async with self._state_lock:
            self._accepting = False
            self._stop_requested = True
            worker = self._worker

        drained = await self.drain(self._remaining(deadline, loop.time()))
        if worker is None:
            return drained

        try:
            remaining = self._remaining(deadline, loop.time())
            if remaining is None:
                await worker
            else:
                await asyncio.wait_for(asyncio.shield(worker), timeout=remaining)
        except asyncio.TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            async with self._state_lock:
                if self._worker is worker:
                    self._worker = None
            return False

        async with self._state_lock:
            if self._worker is worker:
                self._worker = None
        return drained

    def stats(self) -> RecorderStats:
        return RecorderStats(
            accepted=self._accepted,
            dropped=self._dropped,
            delivered=self._delivered,
            failed=self._failed,
            queued=self._queue.qsize(),
            running=self.running,
        )

    async def _run(self) -> None:
        while not self._stop_requested or not self._queue.empty():
            try:
                first = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=self._config.flush_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

            batch = [first]
            while len(batch) < self._config.max_batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                await self._transport.emit_batch(tuple(batch))
            except asyncio.CancelledError:
                self._failed += len(batch)
                raise
            except Exception:
                self._failed += len(batch)
            else:
                self._delivered += len(batch)
            finally:
                for _item in batch:
                    self._queue.task_done()

    @staticmethod
    def _validate_timeout(timeout_seconds: float | None) -> None:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")

    @staticmethod
    def _remaining(deadline: float | None, now: float) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - now)
