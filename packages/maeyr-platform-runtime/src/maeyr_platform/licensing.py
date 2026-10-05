"""Root-execution admission shared by interactive and automated ingress."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypedDict

from fastapi import HTTPException

from maeyr_platform.usage_limits import UsageLimitClient


class _TenantScope(TypedDict):
    org_id: str
    project_id: str


@dataclass(frozen=True)
class ExecutionPermit:
    operation_id: str
    max_iterations: int
    max_duration_seconds: float
    lease_owner: str
    lease_generation: int


def _bounded_limit(limits: dict[str, Any], field: str, fallback: int) -> int:
    value = limits.get(field)
    if type(value) is not int or value < -1 or value == 0:
        raise HTTPException(503, "Execution license limits are unavailable")
    return fallback if value == -1 else value


@asynccontextmanager
async def root_execution_license(
    authority: UsageLimitClient,
    *,
    account_id: str,
    org_id: str,
    project_id: str,
    execution_id: str,
    resume: bool = False,
    queue_operation_id: str | None = None,
    resume_generation: int = 0,
    queue_kind: str = "execution",
) -> AsyncIterator[ExecutionPermit]:
    """Count a started root once; keep child/MCP calls outside this counter.

    Auth serializes quota and active leases across every replica. Queued work
    calls this at actual start, so an earlier enqueue or warm JWT cannot retain
    permission after trial expiry or a subscription change.
    """
    if not all((account_id, org_id, project_id, execution_id)):
        raise HTTPException(503, "Execution requires a complete tenant scope")
    operation_id = "root:" + hashlib.sha256(execution_id.encode()).hexdigest()
    queue_id = queue_operation_id or "queue:" + secrets.token_hex(24)
    scope: _TenantScope = {"org_id": org_id, "project_id": project_id}
    reserved = False
    dispatched = False
    generation = 0
    try:
        # Auth can commit capacity before an interrupted reply or cancellation.
        # The same signed queue identity must be dropped for every failed attempt.
        enqueued = await authority.queue_usage(
            account_id, queue_id, action="enqueue", queue_kind=queue_kind, **scope
        )
        if enqueued.get("enqueued") is not True:
            raise HTTPException(503, "Execution queue admission was not confirmed")
        policy = await authority.license_grants(account_id, **scope)
        limits = policy.get("effective_limits", policy.get("limits"))
        if not isinstance(limits, dict):
            raise HTTPException(503, "Execution license limits are unavailable")
        iterations = _bounded_limit(limits, "max_orchestration_iterations", 100)
        duration = _bounded_limit(limits, "max_execution_duration_seconds", 3600)
        await authority.admit(account_id, kind="execution", operation_id=queue_id, **scope)
        reservation = await authority.reserve_usage(
            account_id,
            "executions",
            operation_id,
            **scope,
            resume=resume,
            lease_owner=queue_id,
            lease_generation=resume_generation,
        )
        if reservation.get("reserved") is not True:
            raise HTTPException(503, "Execution reservation was not confirmed")
        if reservation.get("completed"):
            raise HTTPException(409, "This execution has already completed")
        generation = reservation.get("lease_generation", 0)
        remaining = reservation.get("remaining_duration_seconds", 0)
        if (
            reservation.get("lease_owner") != queue_id
            or type(generation) is not int
            or generation < 1
            or type(remaining) is not int
            or remaining < 1
        ):
            raise HTTPException(503, "Execution lease ownership was not confirmed")
        reserved = True
        # Bind runtime limits to the same transaction that acquired capacity;
        # an account downgrade between the policy read and reserve still wins.
        iterations = min(
            iterations,
            _bounded_limit(reservation, "max_orchestration_iterations", 100),
        )
        result = await authority.settle_usage(
            account_id,
            "executions",
            operation_id,
            **scope,
            lease_owner=queue_id,
            lease_generation=generation,
        )
        if result.get("consumed") is not True:
            raise HTTPException(503, "Execution start was not confirmed")
        start_requested_at = time.monotonic()
        started = await authority.queue_usage(
            account_id,
            queue_id,
            action="start",
            root_operation_id=operation_id,
            lease_generation=generation,
            queue_kind=queue_kind,
            **scope,
        )
        if started.get("removed") is not True:
            raise HTTPException(503, "Execution queue start was not confirmed")
        final_remaining = started.get("remaining_duration_seconds", 0)
        if type(final_remaining) is not int or final_remaining < 1:
            raise HTTPException(503, "Execution runtime bound was not confirmed")
        iterations = min(iterations, _bounded_limit(started, "max_orchestration_iterations", 100))
        # Subtract the complete RPC round trip conservatively so local work
        # finishes before the server's active lease expires, without relying
        # on synchronized clocks across the runtime and Auth hosts.
        runtime = min(duration, remaining, final_remaining) - (
            time.monotonic() - start_requested_at
        )
        if runtime <= 0:
            raise HTTPException(409, "Execution lease elapsed before dispatch")
        permit = ExecutionPermit(
            operation_id=operation_id,
            max_iterations=iterations,
            max_duration_seconds=runtime,
            lease_owner=queue_id,
            lease_generation=generation,
        )
        dispatched = True
        yield permit
    finally:
        # Once a provider/runtime request has started, user errors and network
        # ambiguity cannot refund the root or let a retry execute it twice.
        async def finish() -> None:
            try:
                if reserved:
                    await authority.release_usage(
                        account_id,
                        "executions",
                        operation_id,
                        **scope,
                        refund=not dispatched,
                        lease_owner=queue_id,
                        lease_generation=generation,
                    )
            finally:
                await authority.queue_usage(
                    account_id, queue_id, action="drop", queue_kind=queue_kind, **scope
                )

        release = asyncio.create_task(finish())
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            await release
            raise
