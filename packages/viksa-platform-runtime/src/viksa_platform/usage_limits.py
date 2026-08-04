"""Typed, service-neutral usage-limit and Auth quota-client policies."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from fastapi import HTTPException, status

from viksa_platform.security.internal_request_signing import (
    requires_internal_signature,
    sign_internal_request,
)

RESOURCE_KEYS: dict[str, tuple[str, str]] = {
    "agents": ("agents_count", "max_agents"),
    "chats": ("chats_today_count", "max_chats_per_day"),
    "executions": ("executions_today_count", "max_executions_per_day"),
    "organizations": ("organizations_count", "max_organizations"),
    "cloud_worker_cpu": (
        "cloud_worker_cpu_millicores_usage",
        "max_cloud_worker_cpu_millicores",
    ),
    "cloud_worker_memory": (
        "cloud_worker_memory_mb_usage",
        "max_cloud_worker_memory_mb",
    ),
}


class UsageAuthSettings(Protocol):
    AUTH_SERVICE_URL: str
    AUTH_INTERNAL_KEY: str


class UsageLogger(Protocol):
    def debug(self, message: object, *args: object) -> Any: ...
    def warning(self, message: object, *args: object) -> Any: ...
    def error(self, message: object, *args: object) -> Any: ...


class AiohttpSessionPool:
    """Own one bounded aiohttp session and close it deterministically."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()

    @property
    def session(self) -> ClientSession | None:
        return self._session

    async def get(self, settings: UsageAuthSettings) -> ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = ClientSession(
                    connector=TCPConnector(
                        limit=max(1, int(getattr(settings, "AUTH_API_POOL_SIZE", 100))),
                        limit_per_host=max(
                            1,
                            int(getattr(settings, "AUTH_API_POOL_SIZE_PER_HOST", 50)),
                        ),
                        ttl_dns_cache=300,
                    ),
                    timeout=ClientTimeout(total=5),
                )
        return self._session

    async def close(self) -> None:
        async with self._lock:
            session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


async def enforce_limit(
    current_user: dict[str, Any],
    resource: str,
    requested_amount: int = 0,
    *,
    logger: UsageLogger,
) -> None:
    """Raise HTTP 429 when a tenant's plan cannot cover an operation."""
    keys = RESOURCE_KEYS.get(resource)
    if keys is None:
        return
    usage_key, limit_key = keys
    used = current_user.get("usage", {}).get(usage_key, 0)
    maximum = current_user.get("limits", {}).get(limit_key, 0)
    total = used + requested_amount if requested_amount > 0 else used
    if maximum > 0 and total >= maximum:
        logger.warning(
            "%s limit reached: %s/%s for principal=%s",
            resource,
            total,
            maximum,
            current_user.get("user_id"),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"{resource.replace('_', ' ').capitalize()} limit reached. "
                "Please upgrade your plan."
            ),
        )


async def enforce_cloud_worker_limit(
    current_user: dict[str, Any],
    total_cpu_millicores: int,
    total_memory_mb: int,
    *,
    logger: UsageLogger,
) -> None:
    """Enforce aggregate Worker CPU and memory plan ceilings."""
    limits = current_user.get("limits", {})
    maximum_cpu = limits.get("max_cloud_worker_cpu_millicores", 100)
    maximum_memory = limits.get("max_cloud_worker_memory_mb", 128)
    logger.debug(
        "Checking cloud worker limits principal=%s cpu=%sm/%sm memory=%sMi/%sMi",
        current_user.get("user_id", "unknown"),
        total_cpu_millicores,
        maximum_cpu,
        total_memory_mb,
        maximum_memory,
    )
    if total_cpu_millicores > maximum_cpu:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Total CPU ({total_cpu_millicores}m) exceeds plan limit "
                f"({maximum_cpu}m). Please upgrade your plan."
            ),
        )
    if total_memory_mb > maximum_memory:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Total memory ({total_memory_mb}Mi) exceeds plan limit "
                f"({maximum_memory}Mi). Please upgrade your plan."
            ),
        )


async def post_signed_usage_request(
    *,
    endpoint_path: str,
    payload: dict[str, Any],
    operation_name: str,
    settings: UsageAuthSettings,
    caller_service: str,
    get_session: Callable[[], Awaitable[ClientSession]],
    logger: UsageLogger,
    attempts: int = 3,
) -> bool:
    """POST one exactly-signed usage mutation with bounded retries."""
    account_id = str(payload.get("account_id") or "").strip()
    if not account_id:
        logger.warning("No account_id provided for usage %s", operation_name)
        return False
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    endpoint = f"{settings.AUTH_SERVICE_URL.rstrip('/')}{endpoint_path}"
    for attempt in range(1, max(1, attempts) + 1):
        try:
            org_id = str(payload.get("org_id") or "").strip()
            project_id = str(payload.get("project_id") or "").strip()
            headers = {
                "Content-Type": "application/json",
                "X-Internal-Account-Id": account_id,
                **sign_internal_request(
                    settings.AUTH_INTERNAL_KEY,
                    method="POST",
                    path=endpoint_path,
                    body=body,
                    service=caller_service,
                    account_id=account_id,
                    org_id=org_id,
                    project_id=project_id,
                ),
            }
            if org_id:
                headers["X-Internal-Org-Id"] = org_id
            if project_id:
                headers["X-Internal-Project-Id"] = project_id
            if not requires_internal_signature():
                headers["X-Internal-Auth-Key"] = settings.AUTH_INTERNAL_KEY
            session = await get_session()
            async with session.post(
                endpoint,
                data=body,
                headers=headers,
                timeout=ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    return True
                logger.warning(
                    "Usage %s failed attempt=%s/%s status=%s",
                    operation_name,
                    attempt,
                    attempts,
                    response.status,
                )
        except (ClientError, asyncio.TimeoutError) as exc:
            logger.error(
                "Usage %s transport failed attempt=%s/%s error_type=%s",
                operation_name,
                attempt,
                attempts,
                type(exc).__name__,
            )
        if attempt < attempts:
            await asyncio.sleep(1)
    logger.error("All attempts to %s usage failed", operation_name)
    return False


def parse_cpu_to_millicores(cpu: str) -> int:
    """Convert Kubernetes CPU notation to integer millicores."""
    if not cpu:
        return 0
    normalized = cpu.strip().lower()
    if normalized.endswith("m"):
        return int(normalized[:-1])
    return int(float(normalized) * 1000)


def parse_memory_to_mb(memory: str) -> int:
    """Convert common Kubernetes memory notation to integer MiB."""
    if not memory:
        return 0
    normalized = memory.strip()
    if normalized.endswith("Gi"):
        return int(float(normalized[:-2]) * 1024)
    if normalized.endswith("Mi"):
        return int(normalized[:-2])
    if normalized.endswith("G"):
        return int(float(normalized[:-1]) * 1024)
    if normalized.endswith("M"):
        return int(normalized[:-1])
    return int(normalized)


class UsageLimitClient:
    """Service-bound quota client composed from shared transport and policy."""

    def __init__(
        self,
        *,
        settings: UsageAuthSettings,
        caller_service: str,
        logger: UsageLogger,
    ) -> None:
        self._settings = settings
        self._caller_service = caller_service
        self._logger = logger
        self._sessions = AiohttpSessionPool()

    async def get_session(self) -> ClientSession:
        return await self._sessions.get(self._settings)

    async def close(self) -> None:
        await self._sessions.close()

    async def post_usage_request(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        operation_name: str,
    ) -> bool:
        return await post_signed_usage_request(
            endpoint_path=endpoint_path,
            payload=payload,
            operation_name=operation_name,
            settings=self._settings,
            caller_service=self._caller_service,
            get_session=self.get_session,
            logger=self._logger,
        )

    async def increment_usage(
        self,
        account_id: str | None,
        updates: list[dict[str, Any]],
    ) -> bool:
        return await self.post_usage_request(
            "/internal/usage/increment",
            {"account_id": account_id, "updates": updates},
            "update",
        )

    async def update_cloud_worker_usage(
        self,
        account_id: str,
        cpu_millicores: int,
        memory_mb: int,
    ) -> bool:
        return await self.increment_usage(
            account_id,
            [
                {"resource": "cloud_worker_cpu", "amount": cpu_millicores, "absolute": True},
                {"resource": "cloud_worker_memory", "amount": memory_mb, "absolute": True},
            ],
        )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def usage_control(
    resource: str,
    *,
    enforce: Callable[[dict[str, Any], str, int], Awaitable[None]],
    increment: Callable[[str | None, list[dict[str, Any]]], Awaitable[bool]],
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Build the common pre-enforce/post-increment async decorator."""

    def decorator(
        function: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        @wraps(function)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            keyword_arguments = cast(dict[str, Any], kwargs)
            current_user: Any = keyword_arguments.get("current_user")
            if current_user is None:
                try:
                    bound = inspect.signature(function).bind_partial(*args, **kwargs)
                    current_user = bound.arguments.get("current_user")
                except (TypeError, ValueError):
                    current_user = None
            if isinstance(current_user, dict):
                await enforce(current_user, resource, 0)
            result = await function(*args, **kwargs)
            account_id = (
                cast(str | None, current_user.get("account_id"))
                if isinstance(current_user, dict)
                else cast(str | None, getattr(current_user, "account_id", None))
            )
            await increment(account_id, [{"resource": resource, "amount": 1}])
            return result

        return wrapper

    return decorator


__all__ = [
    "AiohttpSessionPool",
    "RESOURCE_KEYS",
    "UsageLimitClient",
    "enforce_cloud_worker_limit",
    "enforce_limit",
    "post_signed_usage_request",
    "parse_cpu_to_millicores",
    "parse_memory_to_mb",
    "usage_control",
]
