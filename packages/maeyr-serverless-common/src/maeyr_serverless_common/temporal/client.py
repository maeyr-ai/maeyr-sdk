"""Internal namespace routing, idempotent starts, and bounded queue sharding."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Mapping

from temporalio.client import Client, TLSConfig, WorkflowExecutionDescription, WorkflowHandle
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from maeyr_serverless_common.temporal.models import (
    BUILD_WORKFLOW_NAME,
    STATUSES,
    WORKFLOW_NAME,
    BuildReference,
    InvocationReference,
    build_workflow_id,
    workflow_id,
)

_INTERNAL_NAME = re.compile(r"^maeyr-serverless(?:[-._][a-z0-9]+)*$", re.ASCII)


@dataclass(frozen=True)
class ServerlessTemporalSettings:
    endpoint: str
    namespace: str = "serverless"
    queue_prefix: str = "maeyr-serverless-v1"
    shard_count: int = 4
    tls_enabled: bool = True
    max_concurrent_activities: int = 128
    max_concurrent_workflow_tasks: int = 32
    activity_pollers: int = 2
    workflow_pollers: int = 2
    server_root_ca_cert: bytes | None = field(default=None, repr=False)
    client_cert: bytes | None = field(default=None, repr=False)
    client_private_key: bytes | None = field(default=None, repr=False)
    server_name: str | None = None
    identity: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.endpoint, str)
            or not self.endpoint
            or len(self.endpoint) > 512
            or any(character.isspace() for character in self.endpoint)
            or "/" in self.endpoint
            or "@" in self.endpoint
        ):
            raise ValueError("Temporal endpoint must be an internal host and port")
        if self.namespace != "serverless":
            raise ValueError("Serverless requires the shared serverless namespace")
        if not _INTERNAL_NAME.fullmatch(self.queue_prefix):
            raise ValueError("Serverless requires internal versioned queues")
        if type(self.tls_enabled) is not bool:
            raise ValueError("Temporal TLS configuration must be boolean")
        certificates = (self.server_root_ca_cert, self.client_cert, self.client_private_key)
        if any(
            value is not None and (not isinstance(value, bytes) or not 1 <= len(value) <= 131_072)
            for value in certificates
        ):
            raise ValueError("Temporal TLS material is invalid")
        if (self.client_cert is None) != (self.client_private_key is None):
            raise ValueError("Temporal mutual TLS requires both certificate and private key")
        if not self.tls_enabled and (
            any(value is not None for value in certificates) or self.server_name is not None
        ):
            raise ValueError("Temporal TLS material cannot be configured without TLS")
        if self.server_name is not None and (
            not isinstance(self.server_name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.server_name)
        ):
            raise ValueError("Temporal TLS server name is invalid")
        for value, maximum in (
            (self.shard_count, 64),
            (self.max_concurrent_activities, 4096),
            (self.max_concurrent_workflow_tasks, 256),
            (self.activity_pollers, 16),
            (self.workflow_pollers, 16),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Serverless Temporal capacity setting is outside its bounds")
        if self.shard_count > self.max_concurrent_workflow_tasks:
            raise ValueError("Queue shards cannot multiply the workflow task budget")

    def queue_for(self, account_id: str) -> str:
        shard = int.from_bytes(hashlib.sha256(account_id.encode()).digest()[:8], "big")
        return self.queue(shard % self.shard_count)

    def queue(self, shard: int) -> str:
        if type(shard) is not int or not 0 <= shard < self.shard_count:
            raise ValueError("Invalid serverless queue shard")
        return f"{self.queue_prefix}-{shard:02d}"


class TemporalDispatchError(RuntimeError):
    """A finite safe service error; underlying transport details stay private."""


class TemporalServerlessDispatcher:
    _reference_type = InvocationReference
    _workflow_identity = staticmethod(workflow_id)
    _workflow_name = WORKFLOW_NAME
    _identity_field = "execution_id"

    def _public_id(self, reference: InvocationReference) -> str:
        return reference.execution_id

    def __init__(
        self,
        settings: ServerlessTemporalSettings,
        *,
        client_provider: Callable[[], Awaitable[Client]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._client_provider = client_provider
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def client(self) -> Client:
        async with self._lock:
            if self._client is None:
                if self._client_provider is not None:
                    client = await self._client_provider()
                else:
                    client = await Client.connect(
                        self.settings.endpoint,
                        namespace=self.settings.namespace,
                        identity=self.settings.identity,
                        tls=TLSConfig(
                            server_root_ca_cert=self.settings.server_root_ca_cert,
                            client_cert=self.settings.client_cert,
                            client_private_key=self.settings.client_private_key,
                            domain=self.settings.server_name,
                        )
                        if self.settings.tls_enabled
                        else False,
                    )
                if client.namespace != self.settings.namespace:
                    raise TemporalDispatchError("Serverless Temporal namespace is invalid")
                self._client = client
            return self._client

    async def preflight(self) -> None:
        from temporalio.api.enums.v1 import NamespaceState
        from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

        client = await self.client()
        response = await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=self.settings.namespace),
            timeout=timedelta(seconds=5),
        )
        info = getattr(response, "namespace_info", None)
        if (
            info is None
            or getattr(info, "name", None) != self.settings.namespace
            or getattr(info, "state", None) != NamespaceState.NAMESPACE_STATE_REGISTERED
        ):
            raise TemporalDispatchError("Serverless Temporal namespace is not registered")

    async def start(self, payload: Mapping[str, object], *, route: Mapping[str, str]) -> None:
        reference = self._reference_type.parse(payload)
        queue = self.validate_route(route)
        remaining = reference.deadline - self._now()
        if remaining <= timedelta():
            raise TemporalDispatchError("Serverless execution deadline has elapsed")
        try:
            client = await self.client()
            await client.start_workflow(
                self._workflow_name,
                reference.payload(),
                id=self._workflow_identity(reference),
                task_queue=queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                retry_policy=RetryPolicy(maximum_attempts=1),
                execution_timeout=remaining,
                rpc_timeout=timedelta(seconds=10),
            )
        except WorkflowAlreadyStartedError:
            # The control store has already compared the immutable acceptance
            # identity and fence. Temporal must never create a second run even
            # after the original workflow has completed.
            await self._description(reference, route)
        except Exception:
            raise TemporalDispatchError(
                "Serverless scheduling is temporarily unavailable"
            ) from None

    def route_for(self, account_id: str, plan_code: str) -> dict[str, str]:
        if plan_code not in {"free", "pro", "team", "enterprise", "serverless"}:
            raise TemporalDispatchError("Serverless plan is invalid")
        return {
            "namespace": self.settings.namespace,
            "task_queue": self.settings.queue_for(account_id),
            "platform_version": "v1",
            "pool": plan_code,
        }

    def validate_route(self, route: Mapping[str, str]) -> str:
        if set(route) != {"namespace", "task_queue", "platform_version", "pool"} or (
            route["namespace"] != self.settings.namespace
            or route["platform_version"] != "v1"
            or route["pool"] not in {"free", "pro", "team", "enterprise", "serverless"}
            or route["task_queue"]
            not in {self.settings.queue(index) for index in range(self.settings.shard_count)}
        ):
            raise TemporalDispatchError("Persisted serverless route is invalid")
        return route["task_queue"]

    async def _description(
        self, reference: InvocationReference, route: Mapping[str, str]
    ) -> tuple[WorkflowHandle[object, object], WorkflowExecutionDescription]:
        client = await self.client()
        handle = client.get_workflow_handle(self._workflow_identity(reference))
        description = await handle.describe(rpc_timeout=timedelta(seconds=10))
        if (
            description.workflow_type != self._workflow_name
            or description.task_queue != self.validate_route(route)
        ):
            raise TemporalDispatchError("Serverless workflow identity does not match")
        return handle, description

    async def cancel(self, payload: Mapping[str, object], *, route: Mapping[str, str]) -> None:
        reference = self._reference_type.parse(payload)
        try:
            handle, _ = await self._description(reference, route)
            await handle.cancel(rpc_timeout=timedelta(seconds=10))
        except Exception:
            raise TemporalDispatchError(
                "Serverless cancellation is temporarily unavailable"
            ) from None

    async def status(
        self, payload: Mapping[str, object], *, route: Mapping[str, str]
    ) -> dict[str, object]:
        reference = self._reference_type.parse(payload)
        try:
            handle, description = await self._description(reference, route)
            if description.status is None:
                raise ValueError("Serverless workflow status is unavailable")
            state = description.status.name
            if state == "COMPLETED":
                result = await handle.result(rpc_timeout=timedelta(seconds=10))
                if (
                    not isinstance(result, dict)
                    or result.get(self._identity_field) != self._public_id(reference)
                    or result.get("status") not in STATUSES
                ):
                    raise ValueError("Invalid serverless workflow result")
                return {
                    self._identity_field: self._public_id(reference),
                    "status": result["status"],
                }
            return {
                self._identity_field: self._public_id(reference),
                "status": {
                    "RUNNING": "running",
                    "FAILED": "failed",
                    "TIMED_OUT": "failed",
                    "CANCELED": "cancelled",
                    "TERMINATED": "cancelled",
                }.get(state, "failed"),
            }
        except Exception:
            raise TemporalDispatchError("Serverless status is temporarily unavailable") from None


class TemporalBuildDispatcher(TemporalServerlessDispatcher):
    _reference_type = BuildReference
    _workflow_identity = staticmethod(build_workflow_id)
    _workflow_name = BUILD_WORKFLOW_NAME
    _identity_field = "build_id"

    def _public_id(self, reference: InvocationReference) -> str:
        if not isinstance(reference, BuildReference):
            raise ValueError("Invalid serverless build identity")
        return reference.build_id

    def __init__(
        self,
        settings: ServerlessTemporalSettings,
        *,
        client_provider: Callable[[], Awaitable[Client]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not settings.queue_prefix.startswith("maeyr-serverless-build-"):
            raise ValueError("Prepared builds require their own bounded queue pool")
        super().__init__(settings, client_provider=client_provider, now=now)
