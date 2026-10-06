"""Typed, service-neutral usage-limit and Auth quota-client policies."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout, TCPConnector
from fastapi import HTTPException, status

from maeyr_platform.license_errors import public_license_denial_detail
from maeyr_platform.resource_allocation import (
    MAX_RESOURCE_VALUE,
    PROJECT_RESOURCES,
    UNLIMITED,
    validate_resource_limit,
)
from maeyr_platform.security.internal_request_signing import sign_internal_request

RESOURCE_KEYS: dict[str, tuple[str, str]] = {
    **{
        name: (resource.usage_key, resource.allocation_key)
        for name, resource in PROJECT_RESOURCES.items()
    },
    "chats": ("chats_today_count", "max_chats_per_day"),
    "executions": ("executions_today_count", "max_executions_per_day"),
}

ACCOUNT_USAGE_RESOURCES = frozenset({"chats", "executions"})
RESERVATION_RESOURCES = frozenset(
    {"executions", "platform_ai_turns", "ai_token_spend", "trace_ingestion_bytes"}
)


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
        logger.error("Unsupported usage resource: %s", resource)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage policy is unavailable",
        )
    usage_key, limit_key = keys
    usage = current_user.get("usage")
    limits = current_user.get("limits")
    if not isinstance(usage, Mapping) or not isinstance(limits, Mapping):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage policy is unavailable",
        )
    used = usage.get(usage_key)
    maximum = limits.get(limit_key)
    if (
        type(used) is not int
        or used < 0
        or used > MAX_RESOURCE_VALUE
        or type(maximum) is not int
        or maximum < UNLIMITED
        or maximum > MAX_RESOURCE_VALUE
        or type(requested_amount) is not int
        or requested_amount < 0
        or requested_amount > MAX_RESOURCE_VALUE
        or used + requested_amount > MAX_RESOURCE_VALUE
    ):
        logger.error(
            "Invalid %s usage policy for principal=%s",
            resource,
            current_user.get("user_id"),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Usage policy is unavailable",
        )
    total = used + requested_amount
    if maximum != UNLIMITED and total > maximum:
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
    limits = current_user.get("limits")
    if not isinstance(limits, Mapping):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker resource policy is unavailable",
        )
    maximum_cpu = limits.get("max_cloud_worker_cpu_millicores")
    maximum_memory = limits.get("max_cloud_worker_memory_mb")
    if (
        not validate_resource_limit(maximum_cpu)
        or not validate_resource_limit(maximum_memory)
        or type(total_cpu_millicores) is not int
        or not 0 <= total_cpu_millicores <= MAX_RESOURCE_VALUE
        or type(total_memory_mb) is not int
        or not 0 <= total_memory_mb <= MAX_RESOURCE_VALUE
    ):
        logger.error("Invalid cloud worker resource policy or requested capacity")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker resource policy is unavailable",
        )
    maximum_cpu = cast(int, maximum_cpu)
    maximum_memory = cast(int, maximum_memory)
    logger.debug(
        "Checking cloud worker limits principal=%s cpu=%sm/%sm memory=%sMi/%sMi",
        current_user.get("user_id", "unknown"),
        total_cpu_millicores,
        maximum_cpu,
        total_memory_mb,
        maximum_memory,
    )
    if maximum_cpu != UNLIMITED and total_cpu_millicores > maximum_cpu:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Total CPU ({total_cpu_millicores}m) exceeds plan limit "
                f"({maximum_cpu}m). Please upgrade your plan."
            ),
        )
    if maximum_memory != UNLIMITED and total_memory_mb > maximum_memory:
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
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
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
            session = await get_session()
            async with session.post(
                endpoint,
                data=body,
                headers=headers,
                timeout=ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    return True
                if response.status == status.HTTP_429_TOO_MANY_REQUESTS:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Plan limit reached. Please upgrade your plan.",
                    )
                if 400 <= response.status < 500:
                    logger.error(
                        "Usage %s rejected with terminal status=%s",
                        operation_name,
                        response.status,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Usage authorization is unavailable",
                    )
                logger.warning(
                    "Usage %s failed attempt=%s/%s status=%s",
                    operation_name,
                    attempt,
                    max_attempts,
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
        if attempt < max_attempts:
            await asyncio.sleep(min(0.5, 0.1 * (2 ** (attempt - 1))))
    logger.error("All attempts to %s usage failed", operation_name)
    return False


_MAX_LICENSE_DENIAL_BYTES = 4096


async def _public_license_denial(response: ClientResponse) -> dict[str, object] | None:
    """Recognize bounded canonical denials without copying authority details."""
    if (
        response.status not in {403, 409, 429}
        or getattr(response, "content_type", None) != "application/json"
    ):
        return None
    try:
        try:
            body = await response.content.readexactly(_MAX_LICENSE_DENIAL_BYTES + 1)
        except asyncio.IncompleteReadError as exc:
            body = exc.partial
        if len(body) > _MAX_LICENSE_DENIAL_BYTES:
            return None
        payload = json.loads(body)
    except (ClientError, asyncio.TimeoutError, ValueError, RecursionError):
        # A denial remains terminal even if its error body cannot be read.
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("detail"), dict):
        return None
    return public_license_denial_detail(response.status, payload["detail"])


def _license_retry_headers(response: ClientResponse) -> dict[str, str] | None:
    """Retain Auth's bounded delta-second retry hint, never arbitrary headers."""
    if response.status != 429:
        return None
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    if len(value) > 6 or int(value) > 86_400:
        return None
    return {"Retry-After": str(int(value))}


async def post_signed_license_request(
    *,
    endpoint_path: str,
    payload: dict[str, Any],
    settings: UsageAuthSettings,
    caller_service: str,
    get_session: Callable[[], Awaitable[ClientSession]],
    logger: UsageLogger,
    attempts: int = 3,
) -> dict[str, Any]:
    """Read or mutate Auth's license authority with an exactly signed body.

    Mutation callers must supply a stable operation ID. Transport retries keep
    that body and ID intact; a fresh signature nonce does not create new usage.
    A missing or malformed authority response never authorizes paid work.
    """
    account_id = str(payload.get("account_id") or "").strip()
    org_id = str(payload.get("org_id") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    if not account_id or (project_id and not org_id):
        raise ValueError("A complete license scope is required")
    if not settings.AUTH_SERVICE_URL or not settings.AUTH_INTERNAL_KEY:
        raise HTTPException(status_code=503, detail="License authority is unavailable")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    endpoint = f"{settings.AUTH_SERVICE_URL.rstrip('/')}{endpoint_path}"
    for attempt in range(max(1, attempts)):
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
                nonce=uuid.uuid4().hex,
            ),
        }
        if org_id:
            headers["X-Internal-Org-Id"] = org_id
        if project_id:
            headers["X-Internal-Project-Id"] = project_id
        try:
            session = await get_session()
            async with session.post(
                endpoint, data=body, headers=headers, timeout=ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    try:
                        result = await response.json()
                    except (ValueError, ClientError) as exc:
                        raise HTTPException(
                            status_code=503, detail="License authority response is invalid"
                        ) from exc
                    if not isinstance(result, dict):
                        raise HTTPException(
                            status_code=503, detail="License authority response is invalid"
                        )
                    return result
                if response.status in {402, 403, 409, 429}:
                    # Do not expose authority exception details, credentials,
                    # model configuration, or tenant identifiers to the client.
                    public_denial = await _public_license_denial(response)
                    raise HTTPException(
                        status_code=response.status,
                        detail=public_denial
                        if public_denial is not None
                        else (
                            "Plan allowance or authorized spending limit reached"
                            if response.status == 402
                            else (
                                "A request or plan allowance limit was reached. "
                                "Check usage and limits."
                            )
                            if response.status == 429
                            else (
                                "This operation conflicts with the current license "
                                "or execution state"
                            )
                            if response.status == 409
                            else "This operation is not authorized by the current license"
                        ),
                        headers=_license_retry_headers(response),
                    )
                if 400 <= response.status < 500:
                    raise HTTPException(
                        status_code=503, detail="License authority rejected this operation"
                    )
                logger.warning("License authority returned status=%s", response.status)
        except (ClientError, asyncio.TimeoutError) as exc:
            logger.warning("License authority transport failed: %s", type(exc).__name__)
        if attempt + 1 < max(1, attempts):
            await asyncio.sleep(min(0.5, 0.1 * (2**attempt)))
    raise HTTPException(status_code=503, detail="License authority is unavailable")


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

    async def post_license_request(
        self, endpoint_path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await post_signed_license_request(
            endpoint_path=endpoint_path,
            payload=payload,
            settings=self._settings,
            caller_service=self._caller_service,
            get_session=self.get_session,
            logger=self._logger,
        )

    @staticmethod
    def _scope(account_id: str, org_id: str | None, project_id: str | None) -> dict[str, Any]:
        if not account_id or (project_id and not org_id):
            raise ValueError("A complete license scope is required")
        return {"account_id": account_id, "org_id": org_id, "project_id": project_id}

    @staticmethod
    def _operation(resource: str, operation_id: str, amount: int) -> dict[str, Any]:
        if resource not in RESERVATION_RESOURCES:
            raise ValueError("Unsupported license usage resource")
        if not operation_id or len(operation_id) > 128:
            raise ValueError("A stable operation ID is required")
        if type(amount) is not int or amount < 0 or amount > MAX_RESOURCE_VALUE:
            raise ValueError("Invalid license usage amount")
        return {"resource": resource, "operation_id": operation_id, "amount": amount}

    async def runtime_policy(
        self, account_id: str, org_id: str | None = None, project_id: str | None = None
    ) -> dict[str, Any]:
        return await self.post_license_request(
            "/internal/license/runtime-policy", self._scope(account_id, org_id, project_id)
        )

    async def license_grants(
        self, account_id: str, org_id: str | None = None, project_id: str | None = None
    ) -> dict[str, Any]:
        """Read fresh tenant grants without building live usage/report snapshots.

        Auth verifies the current authority source on every request. This client
        does not cache grants or fall back to the heavier runtime-policy route.
        """
        return await self.post_license_request(
            "/internal/license/grants", self._scope(account_id, org_id, project_id)
        )

    async def admit(
        self,
        account_id: str,
        *,
        kind: str,
        operation_id: str,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"mcp", "execution", "ai"} or not operation_id:
            raise ValueError("Invalid account admission")
        result = await self.post_license_request(
            "/internal/license/admit",
            {
                **self._scope(account_id, org_id, project_id),
                "operation_id": operation_id,
                "kind": kind,
            },
        )
        if result.get("admitted") is not True:
            raise HTTPException(503, "Account admission was not confirmed")
        return result

    async def authorize_ai_call(
        self,
        account_id: str,
        *,
        operation_id: str,
        turn_operation_id: str,
        model_id: str,
        input_tokens: int,
        max_output_tokens: int,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if not operation_id or not turn_operation_id or not model_id:
            raise ValueError("Stable model-call and turn identities are required")
        if any(type(value) is not int or value < 1 for value in (input_tokens, max_output_tokens)):
            raise ValueError("Positive model-call bounds are required")
        return await self.post_license_request(
            "/internal/license/authorize-ai-call",
            {
                **self._scope(account_id, org_id, project_id),
                "operation_id": operation_id,
                "turn_operation_id": turn_operation_id,
                "model_id": model_id,
                "input_tokens": input_tokens,
                "max_output_tokens": max_output_tokens,
            },
        )

    async def queue_usage(
        self,
        account_id: str,
        operation_id: str,
        *,
        action: str,
        org_id: str | None = None,
        project_id: str | None = None,
        root_operation_id: str | None = None,
        lease_generation: int | None = None,
        queue_kind: str = "execution",
    ) -> dict[str, Any]:
        if action not in {"enqueue", "start", "drop"} or not operation_id:
            raise ValueError("Invalid execution queue operation")
        if queue_kind not in {"execution", "ai_turn"}:
            raise ValueError("Invalid operational queue kind")
        if action == "start" and (
            not root_operation_id or type(lease_generation) is not int or lease_generation < 1
        ):
            raise ValueError("Execution queue start requires its owned lease generation")
        return await self.post_license_request(
            "/internal/license/queue",
            {
                **self._scope(account_id, org_id, project_id),
                "operation_id": operation_id,
                "action": action,
                "root_operation_id": root_operation_id,
                "lease_generation": lease_generation,
                "queue_kind": queue_kind,
            },
        )

    async def execution_lease_state(
        self, account_id: str, operation_id: str, *, org_id: str, project_id: str
    ) -> dict[str, Any]:
        if not operation_id:
            raise ValueError("A stable execution identity is required")
        return await self.post_license_request(
            "/internal/license/execution-lease-state",
            {**self._scope(account_id, org_id, project_id), "operation_id": operation_id},
        )

    async def delegated_execution_lease_state(
        self,
        account_id: str,
        operation_id: str,
        *,
        parent_lease_owner: str,
        parent_lease_generation: int,
        org_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        """Validate a scoped parent lease without consuming another execution."""
        if not all(isinstance(v, str) and v.strip() for v in (account_id, org_id, project_id)):
            raise ValueError("A complete delegated execution scope is required")
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}", value) is None
            for value in (operation_id, parent_lease_owner)
        ):
            raise ValueError("Stable parent execution and lease owner identities are required")
        if (
            type(parent_lease_generation) is not int
            or not 1 <= parent_lease_generation <= MAX_RESOURCE_VALUE
        ):
            raise ValueError("A positive parent lease generation is required")
        return await self.post_license_request(
            "/internal/license/delegated-execution-lease-state",
            {
                **self._scope(account_id, org_id, project_id),
                "operation_id": operation_id,
                "parent_lease_owner": parent_lease_owner,
                "parent_lease_generation": parent_lease_generation,
            },
        )

    async def reserve_usage(
        self,
        account_id: str,
        resource: str,
        operation_id: str,
        *,
        amount: int = 1,
        org_id: str | None = None,
        project_id: str | None = None,
        **request_bounds: Any,
    ) -> dict[str, Any]:
        operation = self._operation(resource, operation_id, amount)
        if set(request_bounds) - {
            "model_id",
            "rate_card_version",
            "input_tokens",
            "max_output_tokens",
            "max_model_calls",
            "turn_operation_id",
            "resume",
            "lease_owner",
            "lease_generation",
        }:
            raise ValueError("Unsupported license usage bound")
        return await self.post_license_request(
            "/internal/usage/reserve",
            {**self._scope(account_id, org_id, project_id), **operation, **request_bounds},
        )

    async def settle_usage(
        self,
        account_id: str,
        resource: str,
        operation_id: str,
        *,
        amount: int = 1,
        org_id: str | None = None,
        project_id: str | None = None,
        **actual_usage: Any,
    ) -> dict[str, Any]:
        operation = self._operation(resource, operation_id, amount)
        if set(actual_usage) - {
            "model_id",
            "rate_card_version",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "provider_request_id",
            "lease_owner",
            "lease_generation",
        }:
            raise ValueError("Unsupported license usage settlement")
        return await self.post_license_request(
            "/internal/usage/settle",
            {**self._scope(account_id, org_id, project_id), **operation, **actual_usage},
        )

    async def release_usage(
        self,
        account_id: str,
        resource: str,
        operation_id: str,
        *,
        refund: bool = True,
        org_id: str | None = None,
        project_id: str | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> dict[str, Any]:
        operation = self._operation(resource, operation_id, 0)
        operation.pop("amount")
        if type(refund) is not bool:
            raise ValueError("Invalid license usage refund")
        if lease_owner is not None and (
            not lease_owner or type(lease_generation) is not int or lease_generation < 1
        ):
            raise ValueError("Execution release requires its owned lease generation")
        return await self.post_license_request(
            "/internal/usage/release",
            {
                **self._scope(account_id, org_id, project_id),
                **operation,
                "refund": refund,
                **(
                    {"lease_owner": lease_owner, "lease_generation": lease_generation}
                    if lease_owner is not None
                    else {}
                ),
            },
        )

    async def increment_usage(
        self,
        account_id: str | None,
        updates: list[dict[str, Any]],
        *,
        operation_id: str | None = None,
    ) -> bool:
        for update in updates:
            if (
                set(update) != {"resource", "amount"}
                or update.get("resource") not in ACCOUNT_USAGE_RESOURCES
                or type(update.get("amount")) is not int
                or int(update["amount"]) <= 0
                or int(update["amount"]) > MAX_RESOURCE_VALUE
            ):
                raise ValueError("Unsupported account usage update")
        stable_operation_id = operation_id or f"usage:{uuid.uuid4().hex}"
        return await self.post_usage_request(
            "/internal/usage/increment",
            {
                "account_id": account_id,
                "operation_id": stable_operation_id,
                "updates": updates,
            },
            "update",
        )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def usage_control(
    resource: str,
    *,
    enforce: Callable[[dict[str, Any], str, int], Awaitable[None]],
    increment: Callable[..., Awaitable[bool]],
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Build an authoritative pre-consumption async quota decorator."""

    if resource not in ACCOUNT_USAGE_RESOURCES:
        raise ValueError("Unsupported account usage resource")

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
                # JWT usage is a fast advisory only. Auth's transactional
                # consumption below remains authoritative under concurrency.
                await enforce(current_user, resource, 1)
            account_id = (
                cast(str | None, current_user.get("account_id"))
                if isinstance(current_user, dict)
                else cast(str | None, getattr(current_user, "account_id", None))
            )
            operation_id = f"usage:{resource}:{uuid.uuid4().hex}"
            committed = await increment(
                account_id,
                [{"resource": resource, "amount": 1}],
                operation_id=operation_id,
            )
            if not committed:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Usage authorization is unavailable",
                )
            return await function(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "AiohttpSessionPool",
    "RESOURCE_KEYS",
    "UsageLimitClient",
    "enforce_cloud_worker_limit",
    "enforce_limit",
    "post_signed_usage_request",
    "post_signed_license_request",
    "parse_cpu_to_millicores",
    "parse_memory_to_mb",
    "usage_control",
]
