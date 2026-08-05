"""Redis L2 cache for invoker resolution and project user schema."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from viksa_platform.directory.prompt_cache import _client
from viksa_platform.observability.logging import get_logger

logger = get_logger("[viksa_platform.directory.invoker_cache]")

INVOKER_TTL_SECONDS = 300
SCHEMA_TTL_SECONDS = 3600
NEGATIVE_INVOKER_TTL_SECONDS = 60

_CACHE_GENERATION_UNSET = object()
_VERSIONED_CACHE_WRITE = """
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
local ttl = tonumber(ARGV[2])
if ttl ~= nil and ttl > 0 then
  redis.call('SETEX', KEYS[2], ttl, envelope)
else
  redis.call('SET', KEYS[2], envelope)
end
return 1
"""
_VERSIONED_CACHE_READ = """
local current = redis.call('GET', KEYS[1])
if current == false then
  current = '0'
end
local cached = redis.call('GET', KEYS[2])
return {current, cached}
"""


def _generation_key(account_id: str, org_id: str, project_id: str) -> str:
    return f"volt:cache_generation:{account_id}:{org_id}:{project_id}"


async def get_cache_generation(
    account_id: str,
    org_id: str,
    project_id: str,
) -> Optional[int]:
    """Capture the tenant generation before reading authoritative storage."""
    r = await _client()
    if not r:
        return None
    try:
        raw = await r.get(_generation_key(account_id, org_id, project_id))
        if raw is None:
            return 0
        generation = int(raw)
        return generation if generation >= 0 else None
    except Exception as exc:
        logger.debug("cache generation read failed: %s", exc)
        return None


async def advance_cache_generation(
    account_id: str,
    org_id: str,
    project_id: str,
) -> Optional[int]:
    """Fence cache fills that started before this invalidation."""
    r = await _client()
    if not r:
        return None
    try:
        return int(await r.incr(_generation_key(account_id, org_id, project_id)))
    except Exception as exc:
        logger.debug("cache generation advance failed: %s", exc)
        return None


async def _set_cache_value(
    redis_client: Any,
    *,
    generation_key: str,
    cache_key: str,
    value: str,
    ttl_seconds: Optional[int],
    expected_generation: int | None | object,
) -> None:
    expected = ""
    if expected_generation is not _CACHE_GENERATION_UNSET:
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            # A generation read failure must disable the cache fill, not turn it
            # into an unfenced write after Redis recovers.
            return
        expected = str(expected_generation)
    await redis_client.eval(
        _VERSIONED_CACHE_WRITE,
        2,
        generation_key,
        cache_key,
        expected,
        str(ttl_seconds or 0),
        value,
    )


async def _get_cache_value(
    redis_client: Any,
    *,
    generation_key: str,
    cache_key: str,
) -> Optional[Dict[str, Any]]:
    pair = await redis_client.eval(
        _VERSIONED_CACHE_READ,
        2,
        generation_key,
        cache_key,
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
        # Legacy/unversioned entries are misses. They cannot be proven current.
        return None
    return payload


def _invoker_key(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
) -> str:
    return f"volt:invoker:{account_id}:{org_id}:{project_id}:{channel}:{external_user_id}"


def _schema_key(account_id: str, org_id: str, project_id: str) -> str:
    return f"volt:project_user_schema:{account_id}:{org_id}:{project_id}"


def _identity_key(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
) -> str:
    return (
        f"volt:project_user_identity:{account_id}:{org_id}:{project_id}:"
        f"{channel}:{external_user_id}"
    )


async def get_invoker_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
) -> Optional[Dict[str, Any]]:
    r = await _client()
    if not r:
        return None
    try:
        return await _get_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_invoker_key(
                account_id,
                org_id,
                project_id,
                channel,
                external_user_id,
            ),
        )
    except Exception as exc:
        logger.debug("invoker cache get failed: %s", exc)
        return None


async def set_invoker_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: int = INVOKER_TTL_SECONDS,
    expected_generation: int | None | object = _CACHE_GENERATION_UNSET,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await _set_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_invoker_key(
                account_id,
                org_id,
                project_id,
                channel,
                external_user_id,
            ),
            value=json.dumps(payload),
            ttl_seconds=max(1, ttl_seconds),
            expected_generation=expected_generation,
        )
    except Exception as exc:
        logger.debug("invoker cache set failed: %s", exc)


async def invalidate_invoker_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    *,
    channel: Optional[str] = None,
    external_user_id: Optional[str] = None,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        if channel and external_user_id and external_user_id != "*":
            await r.delete(_invoker_key(account_id, org_id, project_id, channel, external_user_id))
            await r.delete(_identity_key(account_id, org_id, project_id, channel, external_user_id))
            return
        channel_suffix = f"{channel}:*" if channel else "*"
        pattern = f"volt:invoker:{account_id}:{org_id}:{project_id}:{channel_suffix}"
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
        pattern = (
            f"volt:project_user_identity:{account_id}:{org_id}:{project_id}:"
            f"{channel_suffix}"
        )
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        logger.debug("invoker cache invalidate failed: %s", exc)


async def get_schema_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> Optional[Dict[str, Any]]:
    r = await _client()
    if not r:
        return None
    try:
        return await _get_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_schema_key(account_id, org_id, project_id),
        )
    except Exception as exc:
        logger.debug("schema cache get failed: %s", exc)
        return None


async def set_schema_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: int = SCHEMA_TTL_SECONDS,
    expected_generation: int | None | object = _CACHE_GENERATION_UNSET,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await _set_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_schema_key(account_id, org_id, project_id),
            value=json.dumps(payload),
            ttl_seconds=max(1, ttl_seconds),
            expected_generation=expected_generation,
        )
    except Exception as exc:
        logger.debug("schema cache set failed: %s", exc)


async def invalidate_schema_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.delete(_schema_key(account_id, org_id, project_id))
    except Exception as exc:
        logger.debug("schema cache invalidate failed: %s", exc)


def _access_policy_key(account_id: str, org_id: str, project_id: str) -> str:
    return f"volt:access_policy:{account_id}:{org_id}:{project_id}"


async def get_access_policy_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> Optional[Dict[str, Any]]:
    r = await _client()
    if not r:
        return None
    try:
        return await _get_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_access_policy_key(account_id, org_id, project_id),
        )
    except Exception as exc:
        logger.debug("access policy cache get failed: %s", exc)
        return None


async def set_access_policy_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: Optional[int] = None,
    expected_generation: int | None | object = _CACHE_GENERATION_UNSET,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await _set_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_access_policy_key(account_id, org_id, project_id),
            value=json.dumps(payload),
            ttl_seconds=ttl_seconds,
            expected_generation=expected_generation,
        )
    except Exception as exc:
        logger.debug("access policy cache set failed: %s", exc)


async def invalidate_access_policy_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.delete(_access_policy_key(account_id, org_id, project_id))
    except Exception as exc:
        logger.debug("access policy cache invalidate failed: %s", exc)


async def get_project_user_identity_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
) -> Optional[Dict[str, Any]]:
    r = await _client()
    if not r:
        return None
    try:
        return await _get_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_identity_key(
                account_id,
                org_id,
                project_id,
                channel,
                external_user_id,
            ),
        )
    except Exception as exc:
        logger.debug("project user identity cache get failed: %s", exc)
        return None


async def set_project_user_identity_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    channel: str,
    external_user_id: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: int = INVOKER_TTL_SECONDS,
    expected_generation: int | None | object = _CACHE_GENERATION_UNSET,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await _set_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_identity_key(
                account_id,
                org_id,
                project_id,
                channel,
                external_user_id,
            ),
            value=json.dumps(payload),
            ttl_seconds=max(1, ttl_seconds),
            expected_generation=expected_generation,
        )
    except Exception as exc:
        logger.debug("project user identity cache set failed: %s", exc)


def _source_key(account_id: str, org_id: str, project_id: str) -> str:
    return f"volt:directory_source:{account_id}:{org_id}:{project_id}"


async def get_directory_source_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> Optional[Dict[str, Any]]:
    r = await _client()
    if not r:
        return None
    try:
        return await _get_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_source_key(account_id, org_id, project_id),
        )
    except Exception as exc:
        logger.debug("directory source cache get failed: %s", exc)
        return None


async def set_directory_source_cache(
    account_id: str,
    org_id: str,
    project_id: str,
    payload: Dict[str, Any],
    *,
    ttl_seconds: Optional[int] = None,
    expected_generation: int | None | object = _CACHE_GENERATION_UNSET,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await _set_cache_value(
            r,
            generation_key=_generation_key(account_id, org_id, project_id),
            cache_key=_source_key(account_id, org_id, project_id),
            value=json.dumps(payload),
            ttl_seconds=ttl_seconds,
            expected_generation=expected_generation,
        )
    except Exception as exc:
        logger.debug("directory source cache set failed: %s", exc)


async def invalidate_directory_source_cache(
    account_id: str,
    org_id: str,
    project_id: str,
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.delete(_source_key(account_id, org_id, project_id))
    except Exception as exc:
        logger.debug("directory source cache invalidate failed: %s", exc)
