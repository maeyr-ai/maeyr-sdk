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
        raw = await r.get(_invoker_key(account_id, org_id, project_id, channel, external_user_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
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
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(
            _invoker_key(account_id, org_id, project_id, channel, external_user_id),
            max(1, ttl_seconds),
            json.dumps(payload),
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
        if channel and external_user_id:
            await r.delete(_invoker_key(account_id, org_id, project_id, channel, external_user_id))
            await r.delete(_identity_key(account_id, org_id, project_id, channel, external_user_id))
            return
        pattern = f"volt:invoker:{account_id}:{org_id}:{project_id}:*"
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
        pattern = f"volt:project_user_identity:{account_id}:{org_id}:{project_id}:*"
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
        raw = await r.get(_schema_key(account_id, org_id, project_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
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
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(
            _schema_key(account_id, org_id, project_id),
            max(1, ttl_seconds),
            json.dumps(payload),
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
        raw = await r.get(_access_policy_key(account_id, org_id, project_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
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
) -> None:
    r = await _client()
    if not r:
        return
    try:
        key = _access_policy_key(account_id, org_id, project_id)
        val = json.dumps(payload)
        if ttl_seconds is not None and ttl_seconds > 0:
            await r.setex(key, ttl_seconds, val)
        else:
            await r.set(key, val)
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
        raw = await r.get(_identity_key(account_id, org_id, project_id, channel, external_user_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
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
) -> None:
    r = await _client()
    if not r:
        return
    try:
        await r.setex(
            _identity_key(account_id, org_id, project_id, channel, external_user_id),
            max(1, ttl_seconds),
            json.dumps(payload),
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
        raw = await r.get(_source_key(account_id, org_id, project_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
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
) -> None:
    r = await _client()
    if not r:
        return
    try:
        key = _source_key(account_id, org_id, project_id)
        val = json.dumps(payload)
        if ttl_seconds is not None and ttl_seconds > 0:
            await r.setex(key, ttl_seconds, val)
        else:
            await r.set(key, val)
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
