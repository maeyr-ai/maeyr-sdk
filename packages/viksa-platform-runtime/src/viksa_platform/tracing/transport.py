"""Canonical durable trace transport with HTTP fallback."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, cast

from .constants import REDIS_QUEUE_KEY

logger = logging.getLogger("platform_traces.transport")

_redis_client: Optional[Any] = None
_use_redis: bool = False
_http_fallback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, object]]] = None
_redis_unavailable_since: Optional[datetime] = None
_dead_letter_memory: Deque[Dict[str, Any]] = deque(maxlen=5000)
_DEAD_LETTER_MAX: int = 5000


def configure_transport(
    redis_client: Optional[Any] = None,
    http_fallback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, object]]] = None,
) -> None:
    global _redis_client, _use_redis, _http_fallback, _redis_unavailable_since
    _redis_client = redis_client
    _use_redis = redis_client is not None
    _http_fallback = http_fallback
    if _use_redis:
        _redis_unavailable_since = None


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


async def enqueue_span(doc: Dict[str, Any]) -> bool:
    """Push a span event to the durable queue. Fire-and-forget."""
    if _use_redis and _redis_client:
        try:
            redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
            await redis.lpush(REDIS_QUEUE_KEY, _doc_to_redis(doc))
            _mark_redis_available()
            return True
        except Exception:
            _mark_redis_unavailable()
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
    re_enqueued = 0
    if _use_redis and _redis_client:
        try:
            redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
            for doc in docs:
                await redis.lpush(REDIS_QUEUE_KEY, _doc_to_redis(doc))
                re_enqueued += 1
            _mark_redis_available()
            return re_enqueued
        except Exception:
            _mark_redis_unavailable()
            logger.exception(
                "re_enqueue_spans failed for %d docs; buffering to dead letter memory",
                len(docs),
            )

    remaining = docs[re_enqueued:]
    for doc in remaining:
        if len(_dead_letter_memory) >= _DEAD_LETTER_MAX:
            _dead_letter_memory.popleft()
        _dead_letter_memory.append(doc)
    if remaining:
        logger.critical(
            "trace spans dead-lettered in memory (%d docs, queue=%d)",
            len(remaining),
            len(_dead_letter_memory),
        )
    return re_enqueued


async def drain_queue(batch_size: int) -> List[Dict[str, Any]]:
    batch: List[Dict[str, Any]] = []
    if not _use_redis or not _redis_client:
        return batch
    try:
        redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client
        for _ in range(batch_size):
            raw = await redis.rpop(REDIS_QUEUE_KEY)
            if raw is None:
                break
            batch.append(_doc_from_redis(raw))
        if batch:
            _mark_redis_available()
    except Exception:
        _mark_redis_unavailable()
    return batch


async def drain_dead_letter_memory(batch_size: int = 500) -> int:
    """
    Move in-memory dead-letter spans back to Redis when it recovers.
    Returns count successfully re-enqueued.
    """
    global _redis_unavailable_since
    if not _dead_letter_memory or not _use_redis or not _redis_client:
        return 0

    total_drained = 0
    redis = _redis_client.redis if hasattr(_redis_client, "redis") else _redis_client

    while _dead_letter_memory:
        batch: List[Dict[str, Any]] = []
        while _dead_letter_memory and len(batch) < batch_size:
            batch.append(_dead_letter_memory.popleft())
        if not batch:
            break

        drained = 0
        try:
            for doc in batch:
                await redis.lpush(REDIS_QUEUE_KEY, _doc_to_redis(doc))
                drained += 1
            total_drained += drained
            _redis_unavailable_since = None
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
        return int(await redis.llen(REDIS_QUEUE_KEY))
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
        "use_redis": _use_redis,
    }
