"""Canonical durable trace transport with HTTP fallback."""

import asyncio
import hashlib
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, cast

from .constants import REDIS_PROCESSING_QUEUE_KEY, REDIS_QUEUE_KEY

logger = logging.getLogger("platform_traces.transport")

_redis_client: Optional[Any] = None
_use_redis: bool = False
_http_fallback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, object]]] = None
_durable_fallback: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None
_redis_unavailable_since: Optional[datetime] = None
_dead_letter_memory: Deque[Dict[str, Any]] = deque(maxlen=5000)
_DEAD_LETTER_MAX: int = 5000
_dead_letter_overflow_drops: int = 0
_redis_queue_full_rejections: int = 0
_durable_fallback_accepts: int = 0
_durable_fallback_failures: int = 0
_recovery_required: bool = True


class _ReservedSpan(dict[str, Any]):
    """Span reserved in Redis until the canonical sink acknowledges it."""

    __slots__ = ("redis_payload",)

    def __init__(self, doc: Dict[str, Any], redis_payload: str | bytes) -> None:
        super().__init__(doc)
        self.redis_payload = redis_payload


def _configured_queue_max() -> int:
    try:
        configured = int(os.getenv("TRACE_REDIS_QUEUE_MAX", "100000"))
    except (TypeError, ValueError):
        configured = 100_000
    return min(max(configured, 1), 1_000_000)


_redis_queue_max: int = _configured_queue_max()

_BOUNDED_LPUSH_SCRIPT = """
local pending = redis.call('LLEN', KEYS[1])
local processing = redis.call('LLEN', KEYS[2])
if pending + processing >= tonumber(ARGV[2]) then
  return 0
end
redis.call('LPUSH', KEYS[1], ARGV[1])
return 1
"""


def configure_transport(
    redis_client: Optional[Any] = None,
    http_fallback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, object]]] = None,
    durable_fallback: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
    queue_max: int | None = None,
) -> None:
    global _redis_client, _use_redis, _http_fallback, _durable_fallback
    global _redis_unavailable_since
    global _redis_queue_max
    global _recovery_required
    _redis_client = redis_client
    _use_redis = redis_client is not None
    _http_fallback = http_fallback
    _durable_fallback = durable_fallback
    if queue_max is not None:
        _redis_queue_max = min(max(int(queue_max), 1), 1_000_000)
    if _use_redis:
        _redis_unavailable_since = None
        # A previous consumer may have died after reserving work but before
        # acknowledging Mongo. Recover those reservations on the first drain.
        _recovery_required = True
    if _durable_fallback is not None and _dead_letter_memory:
        _schedule_dead_letter_drain()


def _mark_redis_unavailable() -> None:
    global _redis_unavailable_since
    if _redis_unavailable_since is None:
        _redis_unavailable_since = datetime.now(timezone.utc)


def _mark_redis_available() -> None:
    global _redis_unavailable_since
    was_unavailable = _redis_unavailable_since is not None
    _redis_unavailable_since = None
    if was_unavailable and _dead_letter_memory:
        _schedule_dead_letter_drain()


def _schedule_dead_letter_drain() -> None:
    """Fire-and-forget drain when Redis recovers and dead-letter backlog exists."""
    if not _dead_letter_memory:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(drain_dead_letter_memory(), name="trace_dead_letter_drain")
    except RuntimeError:
        pass


def _doc_to_redis(doc: Dict[str, Any]) -> str:
    out = dict(doc)
    for key in ("started_at", "ended_at", "expires_at", "created_at", "failed_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
    return json.dumps(out, default=str)


def durable_outbox_event_id(doc: Dict[str, Any]) -> str:
    """Return a stable id for one immutable lifecycle event.

    A span id alone is insufficient because a running span can have many
    checkpoints before its terminal event. Hashing the normalized event makes
    producer outbox writes retry-safe without collapsing distinct revisions.
    """
    span_id = str(doc.get("span_id") or doc.get("_id") or "span")
    digest = hashlib.sha256(_doc_to_redis(doc).encode("utf-8")).hexdigest()
    return f"{span_id}:{digest}"


async def _persist_externally(doc: Dict[str, Any]) -> bool:
    global _durable_fallback_accepts, _durable_fallback_failures
    if _durable_fallback is None:
        return False
    try:
        accepted = bool(await _durable_fallback(doc))
    except Exception:
        accepted = False
        logger.exception("external durable trace fallback failed")
    if accepted:
        _durable_fallback_accepts += 1
        return True
    _durable_fallback_failures += 1
    return False


def _doc_from_redis(raw: str) -> Dict[str, Any]:
    loaded: object = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("queued trace document must be a JSON object")
    doc = cast(Dict[str, Any], loaded)
    for key in ("started_at", "ended_at", "expires_at", "failed_at"):
        if isinstance(doc.get(key), str):
            try:
                doc[key] = datetime.fromisoformat(doc[key].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
    return doc


async def _push_pending_if_capacity(redis: Any, payload: str) -> bool:
    """Atomically admit a span while pending plus processing is below its cap."""
    evaluate = getattr(redis, "eval", None)
    if callable(evaluate):
        result = await evaluate(
            _BOUNDED_LPUSH_SCRIPT,
            2,
            REDIS_QUEUE_KEY,
            REDIS_PROCESSING_QUEUE_KEY,
            payload,
            _redis_queue_max,
        )
        return bool(int(result or 0))

    # Compatibility for lightweight Redis adapters used by local deployments.
    # Production Redis clients use the atomic Lua path above.
    pending = int(await redis.llen(REDIS_QUEUE_KEY) or 0)
    processing = int(await redis.llen(REDIS_PROCESSING_QUEUE_KEY) or 0)
    if pending + processing >= _redis_queue_max:
        return False
    await redis.lpush(REDIS_QUEUE_KEY, payload)
    return True


async def enqueue_span(doc: Dict[str, Any]) -> bool:
    """Push a span event to the capped durable queue."""
    global _redis_queue_full_rejections
    if _use_redis and _redis_client:
        try:
            redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
            admitted = await _push_pending_if_capacity(redis, _doc_to_redis(doc))
            _mark_redis_available()
            if admitted:
                return True
            _redis_queue_full_rejections += 1
            if (
                _redis_queue_full_rejections == 1
                or (_redis_queue_full_rejections & (_redis_queue_full_rejections - 1)) == 0
            ):
                logger.error(
                    "trace Redis queue full rejected=%d max=%d",
                    _redis_queue_full_rejections,
                    _redis_queue_max,
                )
        except Exception:
            _mark_redis_unavailable()
    if await _persist_externally(doc):
        return True
    if _http_fallback:
        asyncio.create_task(_http_fallback(doc), name="trace_span_http_fallback")
    return False


async def re_enqueue_spans(docs: List[Dict[str, Any]]) -> int:
    """
    Push failed flush batches back to Redis for retry.
    Returns count successfully re-enqueued; remainder go to in-memory dead letter.
    """
    if not docs:
        return 0
    global _dead_letter_overflow_drops
    re_enqueued = 0
    if _use_redis and _redis_client:
        try:
            redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
            for doc in docs:
                admitted = await _push_pending_if_capacity(redis, _doc_to_redis(doc))
                if not admitted:
                    break
                re_enqueued += 1
            _mark_redis_available()
            if re_enqueued == len(docs):
                return re_enqueued
        except Exception:
            _mark_redis_unavailable()
            logger.exception(
                "re_enqueue_spans failed for %d docs; buffering to dead letter memory",
                len(docs),
            )

    remaining: List[Dict[str, Any]] = []
    externally_persisted = 0
    for doc in docs[re_enqueued:]:
        if await _persist_externally(doc):
            externally_persisted += 1
        else:
            remaining.append(doc)
    for doc in remaining:
        if len(_dead_letter_memory) >= _DEAD_LETTER_MAX:
            _dead_letter_memory.popleft()
            _dead_letter_overflow_drops += 1
        _dead_letter_memory.append(doc)
    if remaining:
        logger.critical(
            "trace spans dead-lettered in memory (%d docs, queue=%d)",
            len(remaining),
            len(_dead_letter_memory),
        )
    return re_enqueued + externally_persisted


async def drain_queue(batch_size: int) -> List[Dict[str, Any]]:
    """Reserve spans; delete them only after the sink commits."""
    batch: List[Dict[str, Any]] = []
    if not _use_redis or not _redis_client:
        return batch
    try:
        redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
        if _recovery_required:
            await recover_inflight_spans()
            if _recovery_required:
                return batch
        for _ in range(batch_size):
            raw = await redis.rpoplpush(REDIS_QUEUE_KEY, REDIS_PROCESSING_QUEUE_KEY)
            if raw is None:
                break
            payload = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            batch.append(_ReservedSpan(_doc_from_redis(payload), raw))
        if batch:
            _mark_redis_available()
    except Exception:
        _mark_redis_unavailable()
    return batch


async def recover_inflight_spans(limit: int | None = None) -> int:
    """Return crash-orphaned reservations to the pending queue."""
    global _recovery_required
    if not _use_redis or not _redis_client:
        return 0
    redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
    recovered = 0
    maximum = _redis_queue_max if limit is None else max(0, int(limit))
    try:
        for _ in range(maximum):
            raw = await redis.rpoplpush(REDIS_PROCESSING_QUEUE_KEY, REDIS_QUEUE_KEY)
            if raw is None:
                break
            recovered += 1
        _recovery_required = False
        if recovered:
            _mark_redis_available()
            logger.warning("Recovered %d in-flight trace span(s)", recovered)
    except Exception:
        _recovery_required = True
        _mark_redis_unavailable()
        logger.exception("trace reservation recovery failed after %d span(s)", recovered)
    return recovered


async def acknowledge_spans(docs: List[Dict[str, Any]]) -> int:
    """Delete Redis reservations only after canonical storage commits."""
    if not docs or not _use_redis or not _redis_client:
        return 0
    redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
    acknowledged = 0
    try:
        for doc in docs:
            raw = getattr(doc, "redis_payload", None)
            if raw is not None:
                acknowledged += int(await redis.lrem(REDIS_PROCESSING_QUEUE_KEY, 1, raw) or 0)
        _mark_redis_available()
    except Exception:
        _mark_redis_unavailable()
        logger.exception(
            "trace reservation acknowledgement failed after %d span(s)",
            acknowledged,
        )
    return acknowledged


async def release_spans(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Move failed reservations back to pending without a loss window.

    Returned documents were not transferred (including process-memory spans)
    and still require the external/dead-letter fallback.
    """
    if not docs:
        return []
    if not _use_redis or not _redis_client:
        return list(docs)
    redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
    unresolved: List[Dict[str, Any]] = []
    for doc in docs:
        raw = getattr(doc, "redis_payload", None)
        if raw is None:
            unresolved.append(doc)
            continue
        try:
            # Enqueue before deleting the reservation. A crash between these
            # operations may duplicate one span, which canonical upserts handle;
            # the reverse order could lose it permanently.
            await redis.lpush(REDIS_QUEUE_KEY, raw)
            removed = int(await redis.lrem(REDIS_PROCESSING_QUEUE_KEY, 1, raw) or 0)
            if removed < 1:
                # Another recovery worker may already have transferred it. It
                # remains durable in Redis, so no local fallback is required.
                logger.debug("trace reservation was already released")
            _mark_redis_available()
        except Exception:
            _mark_redis_unavailable()
            unresolved.append(doc)
    return unresolved


async def drain_dead_letter_memory(batch_size: int = 500) -> int:
    """
    Move in-memory dead-letter spans back to Redis when it recovers.
    Returns count successfully re-enqueued.
    """
    global _redis_unavailable_since
    if not _dead_letter_memory:
        return 0

    redis = None
    if _use_redis and _redis_client:
        redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
    if redis is None and _durable_fallback is None:
        return 0

    total_drained = 0

    while _dead_letter_memory:
        batch: List[Dict[str, Any]] = []
        while _dead_letter_memory and len(batch) < batch_size:
            batch.append(_dead_letter_memory.popleft())
        if not batch:
            break

        drained = 0
        try:
            for doc in batch:
                admitted = False
                if redis is not None:
                    try:
                        admitted = await _push_pending_if_capacity(
                            redis,
                            _doc_to_redis(doc),
                        )
                    except Exception:
                        _mark_redis_unavailable()
                if not admitted:
                    admitted = await _persist_externally(doc)
                if not admitted:
                    break
                drained += 1
            total_drained += drained
            if redis is not None and drained:
                _redis_unavailable_since = None
            for doc in reversed(batch[drained:]):
                _dead_letter_memory.appendleft(doc)
            if drained != len(batch):
                break
        except Exception:
            _mark_redis_unavailable()
            for doc in reversed(batch[drained:]):
                _dead_letter_memory.appendleft(doc)
            logger.exception(
                "drain_dead_letter_memory failed after %d/%d docs (total_drained=%d)",
                drained,
                len(batch),
                total_drained,
            )
            break

    if total_drained:
        logger.info("Drained %d dead-letter span(s) back to Redis", total_drained)
    return total_drained


async def queue_length() -> int:
    if not _use_redis or not _redis_client:
        return 0
    try:
        redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
        pending = int(await redis.llen(REDIS_QUEUE_KEY) or 0)
        processing = int(await redis.llen(REDIS_PROCESSING_QUEUE_KEY) or 0)
        return pending + processing
    except Exception:
        _mark_redis_unavailable()
        return 0


def get_transport_stats() -> Dict[str, Any]:
    return {
        "redis_unavailable_since": (
            _redis_unavailable_since.isoformat() if _redis_unavailable_since else None
        ),
        "dead_letter_memory_size": len(_dead_letter_memory),
        "dead_letter_memory_max": _DEAD_LETTER_MAX,
        "dead_letter_overflow_drops": _dead_letter_overflow_drops,
        "redis_queue_max": _redis_queue_max,
        "redis_queue_full_rejections": _redis_queue_full_rejections,
        "durable_fallback_configured": _durable_fallback is not None,
        "durable_fallback_accepts": _durable_fallback_accepts,
        "durable_fallback_failures": _durable_fallback_failures,
        "recovery_required": _recovery_required,
        "use_redis": _use_redis,
    }
