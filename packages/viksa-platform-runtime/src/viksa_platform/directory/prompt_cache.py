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
_VERSIONED_USER_CACHE_WRITE = """
local current = redis.call('GET', KEYS[1])
if current == false then
  current = '0'
end
if ARGV[1] ~= '' and tostring(current) ~= ARGV[1] then
  return 0
end
local envelope = cjson.encode({
  __viksa_cache_generation = tonumber(current),
  payload = cjson.decode(ARGV[3])
})
redis.call('SETEX', KEYS[2], tonumber(ARGV[2]), envelope)
return 1
"""
_VERSIONED_USER_CACHE_READ = """
local current = redis.call('GET', KEYS[1])
if current == false then
  current = '0'
end
local cached = redis.call('GET', KEYS[2])
return {current, cached}
"""
_VERSIONED_SHARED_CACHE_READ_MANY = """
local current = redis.call('GET', KEYS[1])
if current == false then
  current = '0'
end
local result = {current}
for index = 2, #KEYS do
  result[#result + 1] = redis.call('GET', KEYS[index])
end
return result
"""
_VERSIONED_SHARED_CACHE_WRITE_MANY = """
local current = redis.call('GET', KEYS[1])
if current == false then
  current = '0'
end
if tostring(current) ~= ARGV[1] then
  return 0
end
local ttl = tonumber(ARGV[2])
for index = 2, #KEYS do
  local envelope = cjson.encode({
    __viksa_cache_generation = tonumber(current),
    payload = cjson.decode(ARGV[index + 1])
  })
  redis.call('SETEX', KEYS[index], ttl, envelope)
end
return #KEYS - 1
"""


def _generation_key(account_id: str, org_id: str, project_id: str) -> str:
    return f"volt:cache_generation:{account_id}:{org_id}:{project_id}"


def _user_cache_key(
    account_id: str,
    org_id: str,
    project_id: str,
    email: str,
) -> str:
    normalized_email = (email or "").strip().lower()
    return f"volt:user_cache:{account_id}:{org_id}:{project_id}:{normalized_email}"


def _project_agents_cache_key(
    account_id: str,
    org_id: str,
    project_id: str,
    key_suffix: str,
) -> str:
    return f"volt:project_cache:{account_id}:{org_id}:{project_id}:agents:{key_suffix}"


def _project_mapping_cache_key(
    account_id: str,
    org_id: str,
    project_id: str,
    mapping_id: str,
) -> str:
    return f"volt:mapping:{account_id}:{org_id}:{project_id}:{mapping_id}"


def _decode_versioned_payload(raw: Any, generation: int) -> dict[str, Any] | None:
    if raw is None:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        return None
    stored_generation = data.get("__viksa_cache_generation")
    payload = data.get("payload")
    if (
        isinstance(stored_generation, bool)
        or not isinstance(stored_generation, int)
        or stored_generation != generation
        or not isinstance(payload, dict)
    ):
        return None
    return payload


def _valid_expected_generation(expected_generation: int | None) -> bool:
    return (
        isinstance(expected_generation, int)
        and not isinstance(expected_generation, bool)
        and expected_generation >= 0
    )


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
        pair = await r.eval(
            _VERSIONED_USER_CACHE_READ,
            2,
            _generation_key(account_id, org_id, project_id),
            _user_cache_key(account_id, org_id, project_id, email),
        )
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        generation_raw, cached_raw = pair
        if cached_raw is None:
            return None
        generation = int(generation_raw)
        data = json.loads(cached_raw)
        if not isinstance(data, dict):
            return None
        stored_generation = data.get("__viksa_cache_generation")
        payload = data.get("payload")
        if (
            isinstance(stored_generation, bool)
            or not isinstance(stored_generation, int)
            or stored_generation != generation
            or not isinstance(payload, dict)
        ):
            return None
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.debug("user cache redis get failed: %s", exc)
        return None


async def get_cache_generation(
    account_id: str,
    org_id: str,
    project_id: str,
) -> int | None:
    """Capture the tenant generation before an authorization cache fill."""
    r = await _client()
    if not r:
        return None
    try:
        raw = await r.get(_generation_key(account_id, org_id, project_id))
        if raw is None:
            return 0
        generation = int(raw)
        return generation if generation >= 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("user cache generation read failed: %s", exc)
        return None


async def set_user_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    email: str,
    payload: dict[str, Any],
    ttl_seconds: int = 300,
    *,
    expected_generation: int | None = None,
) -> None:
    r = await _client()
    if not r or not _valid_expected_generation(expected_generation):
        return
    try:
        await r.eval(
            _VERSIONED_USER_CACHE_WRITE,
            2,
            _generation_key(account_id, org_id, project_id),
            _user_cache_key(account_id, org_id, project_id, email),
            str(expected_generation),
            str(max(1, ttl_seconds)),
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
        pair = await r.eval(
            _VERSIONED_SHARED_CACHE_READ_MANY,
            2,
            _generation_key(account_id, org_id, project_id),
            _project_agents_cache_key(account_id, org_id, project_id, key_suffix),
        )
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        generation = int(pair[0])
        return _decode_versioned_payload(pair[1], generation)
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
    *,
    expected_generation: int | None = None,
) -> None:
    r = await _client()
    if not r or not _valid_expected_generation(expected_generation):
        return
    try:
        await r.eval(
            _VERSIONED_SHARED_CACHE_WRITE_MANY,
            2,
            _generation_key(account_id, org_id, project_id),
            _project_agents_cache_key(account_id, org_id, project_id, key_suffix),
            str(expected_generation),
            str(max(1, ttl_seconds)),
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
        keys = [
            _project_mapping_cache_key(account_id, org_id, project_id, mid) for mid in mapping_ids
        ]
        raw_result = await r.eval(
            _VERSIONED_SHARED_CACHE_READ_MANY,
            len(keys) + 1,
            _generation_key(account_id, org_id, project_id),
            *keys,
        )
        if not isinstance(raw_result, (list, tuple)) or len(raw_result) != len(keys) + 1:
            return [], mapping_ids
        generation = int(raw_result[0])
        raw_vals = raw_result[1:]
        for mid, val in zip(mapping_ids, raw_vals):
            if val:
                try:
                    decoded = _decode_versioned_payload(val, generation)
                    if decoded is None:
                        misses.append(mid)
                    else:
                        hits.append(decoded)
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
    *,
    expected_generation: int | None = None,
) -> None:
    r = await _client()
    if not r or not _valid_expected_generation(expected_generation):
        return
    try:
        keys: list[str] = []
        payloads: list[str] = []
        for m in mappings:
            mid = m.get("mapping_id") or m.get("_id")
            if not mid:
                continue
            keys.append(
                _project_mapping_cache_key(
                    account_id,
                    org_id,
                    project_id,
                    str(mid),
                )
            )
            payloads.append(json.dumps(m))
        if not keys:
            return
        await r.eval(
            _VERSIONED_SHARED_CACHE_WRITE_MANY,
            len(keys) + 1,
            _generation_key(account_id, org_id, project_id),
            *keys,
            str(expected_generation),
            str(max(1, ttl_seconds)),
            *payloads,
        )
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
