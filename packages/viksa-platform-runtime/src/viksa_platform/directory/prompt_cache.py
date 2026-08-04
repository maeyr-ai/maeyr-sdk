"""Optional Redis L2 for per-project prompt bundles (shared across engine replicas)."""

from __future__ import annotations

import json
import os
from typing import Any

from viksa_platform.observability.logging import get_logger
from viksa_platform.redis.config import (
    create_redis_client as _create_redis_client,
)
from viksa_platform.redis.config import (
    redis_connection_kwargs,
    redis_connection_url,
)

logger = get_logger("[viksa_platform.directory.prompt_cache]")

_INVALIDATE_CHANNEL = "volt:prompts:invalidate"
_redis: Any = None
_redis_checked = False


def redis_enabled() -> bool:
    flag = (os.environ.get("VOLT_PROJECT_PROMPTS_REDIS") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool((os.environ.get("REDIS_URL") or os.environ.get("REDIS_HOST") or "").strip())


def connection_params() -> dict[str, Any] | None:
    url = redis_connection_url()
    if url is None:
        return None
    return {"url": url, **redis_connection_kwargs()}


def create_client(redis_module: Any) -> Any:
    """Build a Redis client through the single validated connection path."""
    return _create_redis_client(redis_module)


def _key(scope_key: str) -> str:
    return f"volt:prompts:{scope_key}"


async def _client() -> Any | None:
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    if not redis_enabled():
        return None
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning("redis package not installed — project prompt Redis cache disabled")
        return None
    client = None
    try:
        import asyncio

        client = create_client(aioredis)
        if client is None:
            return None
        await asyncio.wait_for(client.ping(), timeout=1.0)
        _redis = client
        logger.info("project prompt Redis L2 connected")
    except Exception as exc:  # noqa: BLE001
        if client is not None:
            try:
                close_client = getattr(client, "aclose", None) or client.close
                await close_client()
            except Exception:  # noqa: BLE001, S110
                pass
        logger.warning("project prompt Redis unavailable error_type=%s", type(exc).__name__)
        _redis = None
    return _redis


async def get_bundle(scope_key: str) -> dict[str, Any] | None:
    r = await _client()
    if not r:
        return None
    try:
        raw = await r.get(_key(scope_key))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("prompt cache redis get failed: %s", exc)
        return None


async def set_bundle(scope_key: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(_key(scope_key), max(1, ttl_seconds), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.debug("prompt cache redis set failed: %s", exc)


async def delete_bundle(scope_key: str) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.delete(_key(scope_key))
    except Exception as exc:  # noqa: BLE001
        logger.debug("prompt cache redis delete failed: %s", exc)


async def publish_invalidate(scope_key: str) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.publish(_INVALIDATE_CHANNEL, scope_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("prompt cache redis publish failed: %s", exc)


async def get_user_cache(
    account_id: str, org_id: str, project_id: str, email: str
) -> dict[str, Any] | None:
    r = await _client()
    if not r:
        return None
    try:
        raw = await r.get(f"volt:user_cache:{account_id}:{org_id}:{project_id}:{email}")
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("user cache redis get failed: %s", exc)
        return None


async def set_user_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    email: str,
    payload: dict[str, Any],
    ttl_seconds: int = 300,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(
            f"volt:user_cache:{account_id}:{org_id}:{project_id}:{email}",
            max(1, ttl_seconds),
            json.dumps(payload),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("user cache redis set failed: %s", exc)


async def get_project_agents_cache(
    account_id: str, org_id: str, project_id: str, key_suffix: str
) -> dict[str, Any] | None:
    r = await _client()
    if not r:
        return None
    try:
        raw = await r.get(
            f"volt:project_cache:{account_id}:{org_id}:{project_id}:agents:{key_suffix}"
        )
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("project agents cache redis get failed: %s", exc)
        return None


async def set_project_agents_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    key_suffix: str,
    payload: dict[str, Any],
    ttl_seconds: int = 86400,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(
            f"volt:project_cache:{account_id}:{org_id}:{project_id}:agents:{key_suffix}",
            max(1, ttl_seconds),
            json.dumps(payload),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("project agents cache redis set failed: %s", exc)


async def get_project_mappings_cache(
    account_id: str, org_id: str, project_id: str, mapping_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Retrieve mapping docs from Redis L2 cache. Returns (hits, misses)."""
    r = await _client()
    if not r:
        return [], mapping_ids
    hits = []
    misses = []
    try:
        keys = [f"volt:mapping:{account_id}:{org_id}:{project_id}:{mid}" for mid in mapping_ids]
        raw_vals = await r.mget(keys)
        for mid, val in zip(mapping_ids, raw_vals):
            if val:
                try:
                    hits.append(json.loads(val))
                except Exception:  # noqa: BLE001
                    misses.append(mid)
            else:
                misses.append(mid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("project mappings cache redis get failed: %s", exc)
        return [], mapping_ids
    return hits, misses


async def set_project_mappings_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    mappings: list[dict[str, Any]],
    ttl_seconds: int = 86400,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        pipe = r.pipeline()
        for m in mappings:
            mid = m.get("mapping_id") or m.get("_id")
            if not mid:
                continue
            key = f"volt:mapping:{account_id}:{org_id}:{project_id}:{mid}"
            pipe.setex(key, max(1, ttl_seconds), json.dumps(m))
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("project mappings cache redis set failed: %s", exc)


async def close() -> None:
    global _redis, _redis_checked
    if _redis is not None:
        try:
            close_client = getattr(_redis, "aclose", None) or _redis.close
            await close_client()
        except Exception:  # noqa: BLE001, S110
            pass
    _redis = None
    _redis_checked = False


INVALIDATE_CHANNEL = _INVALIDATE_CHANNEL
