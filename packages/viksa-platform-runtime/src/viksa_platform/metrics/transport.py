"""Durable, bounded transport for token-usage events."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, cast

from viksa_platform.metrics.constants import (
    REDIS_PROCESSING_QUEUE_KEY,
    REDIS_QUEUE_KEY,
    REDIS_QUEUE_KEY_LEGACY,
    REDIS_QUEUE_MAX_SIZE,
)

logger = logging.getLogger("platform_metrics.transport")

HttpFallback = Callable[[dict[str, Any]], Awaitable[Any]]
DurableFallback = Callable[[dict[str, Any]], Awaitable[bool]]

_redis_client: Any | None = None
_use_redis = False
_http_fallback: HttpFallback | None = None
_durable_fallback: DurableFallback | None = None
_recovery_required = False
_redis_failures = 0
_redis_capacity_rejections = 0
_durable_fallback_accepts = 0
_durable_fallback_failures = 0

_BOUNDED_LPUSH = """
local current = redis.call('LLEN', KEYS[1])
if current >= tonumber(ARGV[2]) then
  return 0
end
redis.call('LPUSH', KEYS[1], ARGV[1])
return 1
"""


class _ReservedEvent(dict[str, Any]):
    """Event mapping whose reservation metadata is hidden from the sink."""

    __slots__ = ("redis_payload",)

    def __init__(self, doc: dict[str, Any], redis_payload: Any) -> None:
        super().__init__(doc)
        self.redis_payload = redis_payload


def configure_transport(
    redis_client: Any | None = None,
    http_fallback: HttpFallback | None = None,
    durable_fallback: DurableFallback | None = None,
) -> None:
    """Configure transport backends during service startup."""
    global _redis_client, _use_redis, _http_fallback, _durable_fallback
    global _recovery_required
    _redis_client = redis_client
    _use_redis = redis_client is not None
    _http_fallback = http_fallback
    _durable_fallback = durable_fallback
    _recovery_required = _use_redis


def _redis() -> Any:
    client = _redis_client
    if client is None:
        raise RuntimeError("metrics Redis transport is not configured")
    return client.redis if hasattr(client, "redis") else client


def _doc_to_redis(doc: dict[str, Any]) -> str:
    output = dict(doc)
    for key in ("created_at", "started_at", "completed_at", "last_call_at"):
        if isinstance(output.get(key), datetime):
            output[key] = output[key].isoformat()
    return json.dumps(output, default=str, separators=(",", ":"))


def _doc_from_redis(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    doc = cast(dict[str, Any], json.loads(raw))
    if isinstance(doc.get("created_at"), str):
        try:
            doc["created_at"] = datetime.fromisoformat(doc["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            doc["created_at"] = datetime.now(timezone.utc)
    return doc


async def _bounded_enqueue(raw: str) -> bool:
    """Atomically reject work when the pending queue is full."""
    global _redis_capacity_rejections
    redis = _redis()
    if hasattr(redis, "eval"):
        accepted = await redis.eval(
            _BOUNDED_LPUSH,
            1,
            REDIS_QUEUE_KEY,
            raw,
            REDIS_QUEUE_MAX_SIZE,
        )
        if int(accepted or 0) == 1:
            return True
        _redis_capacity_rejections += 1
        return False

    # Compatibility for older redis adapters that expose transactional
    # pipelines but not EVAL. MULTI makes LPUSH+LTRIM atomic; newer adapters
    # use the rejection-based Lua path above so capacity is observable.
    configured = os.getenv("PLATFORM_METRICS_REDIS_QUEUE_MAX", "").strip()
    try:
        maximum = int(configured) if configured else REDIS_QUEUE_MAX_SIZE
    except ValueError:
        maximum = REDIS_QUEUE_MAX_SIZE
    maximum = max(1, min(REDIS_QUEUE_MAX_SIZE, maximum))
    pipeline = redis.pipeline(transaction=True)
    pipeline.lpush(REDIS_QUEUE_KEY, raw)
    pipeline.ltrim(REDIS_QUEUE_KEY, 0, maximum - 1)
    results = await pipeline.execute()
    return bool(results and int(results[0] or 0) > 0)


async def enqueue_event(doc: dict[str, Any]) -> bool:
    """Persist an event without spawning a background task."""
    global _durable_fallback_accepts, _durable_fallback_failures, _redis_failures
    if _use_redis and _redis_client:
        try:
            if await _bounded_enqueue(_doc_to_redis(doc)):
                return True
        except Exception as exc:  # noqa: BLE001
            _redis_failures += 1
            logger.warning(
                "token usage Redis enqueue failed error_type=%s failures=%d",
                type(exc).__name__,
                _redis_failures,
            )
    if _durable_fallback is not None:
        try:
            if await _durable_fallback(dict(doc)):
                _durable_fallback_accepts += 1
                return True
            _durable_fallback_failures += 1
            logger.error(
                "token usage durable fallback rejected event failures=%d",
                _durable_fallback_failures,
            )
        except Exception as exc:  # noqa: BLE001
            _durable_fallback_failures += 1
            logger.error(
                "token usage durable fallback failed error_type=%s failures=%d",
                type(exc).__name__,
                _durable_fallback_failures,
            )
    return False


async def deliver_http_fallback(docs: list[dict[str, Any]]) -> bool:
    """Deliver fallback work within the recorder's supervised worker."""
    if not _http_fallback:
        return False
    for doc in docs:
        await _http_fallback(dict(doc))
    return True


async def recover_inflight_events(limit: int = REDIS_QUEUE_MAX_SIZE) -> int:
    """Return orphaned reservations to the pending queue after restart."""
    if not _use_redis or not _redis_client:
        return 0
    redis = _redis()
    recovered = 0
    try:
        for _ in range(max(0, limit)):
            if int(await redis.llen(REDIS_QUEUE_KEY) or 0) >= REDIS_QUEUE_MAX_SIZE:
                break
            raw = await redis.rpoplpush(REDIS_PROCESSING_QUEUE_KEY, REDIS_QUEUE_KEY)
            if raw is None:
                break
            recovered += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "token usage reservation recovery failed error_type=%s",
            type(exc).__name__,
        )
    return recovered


async def drain_queue(batch_size: int) -> list[dict[str, Any]]:
    """Reserve events; remove them only after sink acknowledgement."""
    global _recovery_required
    batch: list[dict[str, Any]] = []
    if not _use_redis or not _redis_client:
        return batch
    redis = _redis()
    try:
        if _recovery_required:
            await recover_inflight_events()
            _recovery_required = False
        for key in (REDIS_QUEUE_KEY, REDIS_QUEUE_KEY_LEGACY):
            while len(batch) < max(0, batch_size):
                raw = await redis.rpoplpush(key, REDIS_PROCESSING_QUEUE_KEY)
                if raw is None:
                    break
                batch.append(_ReservedEvent(_doc_from_redis(raw), raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "token usage reservation failed error_type=%s",
            type(exc).__name__,
        )
    return batch


async def acknowledge_events(docs: list[dict[str, Any]]) -> int:
    """Remove reservations only after the sink commits."""
    if not docs or not _use_redis or not _redis_client:
        return 0
    redis = _redis()
    acknowledged = 0
    try:
        for doc in docs:
            raw = getattr(doc, "redis_payload", None)
            if raw is not None:
                acknowledged += int(await redis.lrem(REDIS_PROCESSING_QUEUE_KEY, 1, raw) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "token usage acknowledgement failed error_type=%s",
            type(exc).__name__,
        )
    return acknowledged


async def release_events(docs: list[dict[str, Any]]) -> int:
    """Release failed reservations without a delete-before-requeue window."""
    if not docs or not _use_redis or not _redis_client:
        return 0
    redis = _redis()
    released = 0
    try:
        for doc in docs:
            raw = getattr(doc, "redis_payload", None)
            if raw is None:
                continue
            if not await _bounded_enqueue(_doc_to_redis(dict(doc))):
                break
            await redis.lrem(REDIS_PROCESSING_QUEUE_KEY, 1, raw)
            released += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "token usage reservation release failed error_type=%s released=%d",
            type(exc).__name__,
            released,
        )
    return released


async def queue_length() -> int:
    """Return total pending and reserved events."""
    if not _use_redis or not _redis_client:
        return 0
    try:
        redis = _redis()
        total = 0
        for key in (REDIS_QUEUE_KEY, REDIS_QUEUE_KEY_LEGACY, REDIS_PROCESSING_QUEUE_KEY):
            total += int(await redis.llen(key) or 0)
        return total
    except Exception:  # noqa: BLE001
        return 0


def get_transport_stats() -> dict[str, Any]:
    return {
        "use_redis": _use_redis,
        "recovery_required": _recovery_required,
        "redis_failures": _redis_failures,
        "redis_capacity_rejections": _redis_capacity_rejections,
        "redis_queue_max_size": REDIS_QUEUE_MAX_SIZE,
        "durable_fallback_configured": _durable_fallback is not None,
        "durable_fallback_accepts": _durable_fallback_accepts,
        "durable_fallback_failures": _durable_fallback_failures,
    }


__all__ = [
    "acknowledge_events",
    "configure_transport",
    "deliver_http_fallback",
    "drain_queue",
    "enqueue_event",
    "get_transport_stats",
    "queue_length",
    "recover_inflight_events",
    "release_events",
]
