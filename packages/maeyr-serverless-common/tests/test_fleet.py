"""Real Redis admission/termination gates for shared in-Pod execution workers."""

import asyncio
import hashlib
import json
import os
import secrets
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from redis.asyncio import Redis

from maeyr_serverless_common.errors import ServiceError
from maeyr_serverless_common.fleet import FleetRegistry, WorkerDescriptor
from maeyr_serverless_common.telemetry import receipt_identity


def worker(**updates):
    value = dict(
        incarnation=str(uuid4()),
        artifact_store_id="artifacts_1",
        image_ref="registry/worker@sha256:" + "a" * 64,
        runtime_abi={"python": "3.11", "architecture": "amd64", "platform": "linux"},
        slots=8,
        cpu_millis=1000,
        memory_bytes=256 * 1024**2,
    )
    return WorkerDescriptor.create(**(value | updates))


@pytest_asyncio.fixture
async def registry():
    url = os.environ.get("SERVERLESS_TEST_REDIS_URL")
    if not url:
        pytest.skip("Owned persistent Redis fixture required")
    redis = Redis.from_url(url)
    registry = FleetRegistry(redis, "fleet_test_" + secrets.token_hex(8))
    await registry.preflight()
    yield registry
    # Only this fixture's unique namespace, never production tenant data.
    keys = [key async for key in redis.scan_iter(match=registry.prefix + ":*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


def usage(descriptor, **updates):
    value = (
        dict(
            schema="maeyr-runtime-usage-v1",
            account_id="AC-a",
            org_id="OI-a",
            project_id="PI-a",
            execution_id="EX-a",
            attempt=1,
            broker_id=descriptor.worker_id,
            boot_id=descriptor.incarnation,
            sandbox_id="maeyr-" + "b" * 48,
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:00:01+00:00",
            occupied_micros=1000000,
            cpu_usage_micros=None,
            memory_peak_bytes=None,
            cpu_millis=1000,
            memory_bytes=256 * 1024**2,
            termination_confirmed=True,
            outcome="succeeded",
        )
        | updates
    )
    value["receipt_id"] = receipt_identity(value)
    return value


def start(receipt):
    return {k: receipt[k] for k in ("broker_id", "boot_id", "sandbox_id", "started_at")}


async def reserve(registry, descriptor, token, receipt):
    return await registry.reserve_attempt(
        descriptor.worker_id,
        token,
        start(receipt),
        scope=receipt,
        execution_id=receipt["execution_id"],
        attempt=receipt["attempt"],
    )


async def expire(registry, descriptor):
    key = registry.prefix + ":workers"
    record = json.loads(await registry.redis.hget(key, descriptor.worker_id))
    record["expires_at_ms"] = 0
    await registry.redis.hset(key, descriptor.worker_id, json.dumps(record))
    await registry.redis.zadd(registry.prefix + ":live", {descriptor.worker_id: 0})
    await registry.redis.zadd(registry.prefix + ":expiry", {descriptor.worker_id: 0})


@pytest.mark.parametrize(
    "field,value",
    [
        ("incarnation", "worker1"),
        ("worker_id", "worker_" + "0" * 40),
        ("task_queue", "other"),
        ("slots", True),
        ("slots", 257),
        ("cpu_millis", 8001),
        ("memory_bytes", 8589934593),
        ("image_ref", "worker:latest"),
        ("runtime_abi", {"python": "3.14", "architecture": "amd64", "platform": "linux"}),
        (
            "runtime_abi",
            {"python": "3.11", "architecture": "amd64", "platform": "linux", "extra": "x"},
        ),
        ("endpoint", "https://old-external-runner"),
        ("node_id", "old-node"),
    ],
)
def test_descriptor_rejects_unbound_identity_and_old_external_runner_fields(field, value):
    raw = worker().model_dump() | {field: value}
    with pytest.raises((ValidationError, ValueError)):
        WorkerDescriptor.model_validate(raw)


def test_worker_id_name_and_queue_are_reproducible_but_boot_incarnations_are_distinct():
    first = worker()
    same = worker(incarnation=first.incarnation)
    another = worker()
    assert first == same
    assert first.worker_id != another.worker_id and first.task_queue != another.task_queue
    assert (
        first.worker_id == "worker_" + hashlib.sha256(first.incarnation.encode()).hexdigest()[:40]
    )
    assert first.task_queue == "maeyr-serverless-agent-" + first.worker_id


@pytest.mark.parametrize(
    "values",
    [
        {"lease_seconds": True},
        {"lease_seconds": 5},
        {"lease_seconds": 121},
        {"max_workers": 0},
        {"max_workers": 10001},
        {"max_workers": True},
        {"max_pending_receipts": 0},
        {"max_pending_receipts": 1000001},
        {"max_tombstones": 3},
        {"max_tombstones": 20000001},
        {"tombstone_seconds": 959},
        {"tombstone_seconds": 3601},
        {"max_tombstones": 200000},
    ],
)
def test_registry_configuration_bounds_are_strict(values):
    with pytest.raises(ValueError):
        FleetRegistry(AsyncMock(), "fixture", **values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("aof_enabled", 0),
        ("aof_last_write_status", "err"),
        ("maxmemory_policy", "allkeys-lru"),
        ("appendfsync", "everysec"),
    ],
)
async def test_durable_authority_preflight_fails_closed(field, value):
    persistence = {"aof_enabled": 1, "aof_last_write_status": "ok"}
    memory = {"maxmemory_policy": "noeviction"}
    config = {"appendfsync": "always"}
    (persistence if field in persistence else memory if field in memory else config)[field] = value
    redis = AsyncMock()
    redis.info.side_effect = [persistence, memory]
    redis.config_get.return_value = config
    with pytest.raises(ServiceError) as error:
        await FleetRegistry(redis, "fixture").preflight()
    assert error.value.code == "durable_authority_unavailable"


async def test_concurrent_duplicate_registration_is_fenced_and_token_is_not_stored(registry):
    descriptor = worker()
    results = await asyncio.gather(
        *(registry.register(descriptor) for _ in range(20)), return_exceptions=True
    )
    tokens = [value for value in results if isinstance(value, str)]
    assert len(tokens) == 1
    assert len(await registry.list_all()) == 1
    raw = await registry.redis.hget(registry.prefix + ":workers", descriptor.worker_id)
    assert tokens[0].encode() not in raw
    assert await registry.owns(descriptor.worker_id, tokens[0])
    assert not await registry.owns(descriptor.worker_id, "0" * 64)
    assert not await registry.heartbeat(descriptor.worker_id, "0" * 64)


async def test_heartbeat_cannot_resurrect_expired_worker_or_reset_physical_count(registry):
    descriptor = worker(slots=1)
    token = await registry.register(descriptor)
    receipt = usage(descriptor)
    assert await reserve(registry, descriptor, token, receipt)
    assert await registry.heartbeat(descriptor.worker_id, token, active_runs=0)
    assert (await registry.get(descriptor.worker_id)).active_runs == 1
    await expire(registry, descriptor)
    assert not await registry.heartbeat(descriptor.worker_id, token)
    assert not await registry.owns(descriptor.worker_id, token)
    assert await registry.owns(descriptor.worker_id, token, admit=False)
    assert not await reserve(registry, descriptor, token, receipt)
    assert await registry.list_all() == []
    assert await registry.retire_expired() == 0
    assert (await registry.get(descriptor.worker_id)).active_runs == 1


async def test_drain_is_sticky_blocks_admission_and_retains_completion_ownership(registry):
    descriptor = worker()
    token = await registry.register(descriptor)
    assert await registry.heartbeat(descriptor.worker_id, token, draining=True)
    assert await registry.heartbeat(descriptor.worker_id, token, draining=False)
    lease = await registry.get(descriptor.worker_id)
    assert lease.draining and lease.live
    assert not await registry.owns(descriptor.worker_id, token)
    assert await registry.owns(descriptor.worker_id, token, admit=False)
    assert not await reserve(registry, descriptor, token, usage(descriptor))
    assert not await registry.retire(descriptor.worker_id, token, journals_drained=False)
    assert not await registry.retire(descriptor.worker_id, "0" * 64, journals_drained=True)
    assert await registry.retire(descriptor.worker_id, token, journals_drained=True)
    assert await registry.get(descriptor.worker_id) is None
    with pytest.raises(ServiceError):
        await registry.register(descriptor)


async def test_atomic_physical_admission_is_idempotent_bounded_and_scoped(registry):
    descriptor = worker(slots=2)
    token = await registry.register(descriptor)
    first = usage(descriptor)
    assert all(
        await asyncio.gather(*(reserve(registry, descriptor, token, first) for _ in range(20)))
    )
    assert (await registry.get(descriptor.worker_id)).active_runs == 1
    assert not await reserve(
        registry, descriptor, token, usage(descriptor, sandbox_id="maeyr-" + "c" * 48)
    )
    second = usage(descriptor, account_id="AC-b")
    assert await reserve(registry, descriptor, token, second)
    assert not await reserve(registry, descriptor, token, usage(descriptor, account_id="AC-c"))
    stats = await registry.stats()
    assert stats["physical_attempts"] == stats["active_slots"] == stats["total_slots"] == 2
    assert stats["available_slots"] == 0


async def test_physical_release_requires_exact_durable_confirmed_termination(registry):
    descriptor = worker(slots=1)
    token = await registry.register(descriptor)
    receipt = usage(descriptor)
    assert await reserve(registry, descriptor, token, receipt)
    with pytest.raises(ServiceError):
        await registry.finish_attempt(descriptor.worker_id, receipt)
    with pytest.raises(ServiceError):
        await registry.finish_attempt(
            descriptor.worker_id, {**receipt, "termination_confirmed": False}
        )
    altered = usage(descriptor, started_at="2026-10-06T00:00:00.000001+00:00")
    with pytest.raises(ServiceError):
        await registry.persist_usage(altered)
    await registry.persist_usage(receipt)
    with pytest.raises(ServiceError):
        await registry.acknowledge_usage(receipt["receipt_id"])
    assert (await registry.get(descriptor.worker_id)).active_runs == 1
    assert all(
        await asyncio.gather(
            *(registry.finish_attempt(descriptor.worker_id, receipt) for _ in range(20))
        )
    )
    assert (await registry.get(descriptor.worker_id)).active_runs == 0
    assert not await reserve(registry, descriptor, token, receipt)
    assert await registry.acknowledge_usage(receipt["receipt_id"])
    assert await registry.finish_attempt(descriptor.worker_id, receipt)  # Lost ACK/reply is safe.
    assert not await registry.acknowledge_usage(receipt["receipt_id"])
    await registry.persist_usage(receipt)  # Replayed local journal does not requeue.
    assert await registry.pending_usage() == []
    with pytest.raises(ServiceError):
        await registry.persist_usage({**receipt, "occupied_micros": 2})


async def test_same_scope_cannot_finish_another_attempt_or_worker_and_expiry_is_not_termination(
    registry,
):
    descriptor, other = worker(), worker()
    token = await registry.register(descriptor)
    await registry.register(other)
    receipt = usage(descriptor)
    assert await reserve(registry, descriptor, token, receipt)
    for altered in (usage(descriptor, attempt=2), usage(descriptor, account_id="AC-other")):
        await registry.persist_usage(altered)
        with pytest.raises(ServiceError):
            await registry.finish_attempt(altered["broker_id"], altered)
    with pytest.raises(ServiceError):
        await registry.persist_usage(usage(other))
    await expire(registry, descriptor)
    assert await registry.retire_expired() == 0
    await registry.persist_usage(receipt)
    assert await registry.finish_attempt(descriptor.worker_id, receipt)
    assert await registry.retire_expired() == 0  # Billing receipt remains pending.
    await registry.acknowledge_usage(receipt["receipt_id"])
    # A prior failed retirement fairly deferred this worker; make just its due score current.
    await registry.redis.zadd(registry.prefix + ":expiry", {descriptor.worker_id: 0})
    assert await registry.retire_expired() == 0  # Other unacknowledged receipts remain.
    for pending in await registry.pending_usage():
        await registry.acknowledge_usage(pending["receipt_id"])
    await registry.redis.zadd(registry.prefix + ":expiry", {descriptor.worker_id: 0})
    assert await registry.retire_expired() == 1
    assert await registry.get(descriptor.worker_id) is None
    assert (await registry.get(other.worker_id)).live


async def test_unstarted_authority_failure_finishes_without_negative_occupancy(registry):
    descriptor = worker()
    await registry.register(descriptor)
    receipt = usage(
        descriptor,
        outcome="not_started",
        occupied_micros=0,
        finished_at="2026-10-06T00:00:00+00:00",
    )
    await registry.persist_usage(receipt)
    assert await registry.finish_attempt(descriptor.worker_id, receipt)
    assert await registry.finish_attempt(descriptor.worker_id, receipt)
    assert (await registry.get(descriptor.worker_id)).active_runs == 0
    await registry.acknowledge_usage(receipt["receipt_id"])


async def test_outbox_backpressure_reserves_capacity_for_already_admitted_work(registry):
    bounded = FleetRegistry(
        registry.redis, "bounded_" + secrets.token_hex(8), max_pending_receipts=1
    )
    try:
        descriptor = worker(slots=2)
        token = await bounded.register(descriptor)
        receipt = usage(descriptor)
        assert await reserve(bounded, descriptor, token, receipt)
        second = usage(descriptor, execution_id="EX-b")
        assert not await reserve(bounded, descriptor, token, second)
        with pytest.raises(ServiceError):
            await bounded.persist_usage(second)
        await bounded.persist_usage(receipt)
        assert await bounded.finish_attempt(descriptor.worker_id, receipt)
        assert not await reserve(bounded, descriptor, token, second)
        await bounded.acknowledge_usage(receipt["receipt_id"])
        assert await reserve(bounded, descriptor, token, second)
    finally:
        keys = [key async for key in registry.redis.scan_iter(match=bounded.prefix + ":*")]
        await registry.redis.delete(*keys)


async def test_outbox_is_durable_order_independent_conflict_checked_and_claims_are_fair(registry):
    descriptor = worker()
    await registry.register(descriptor)
    receipts = [usage(descriptor, execution_id=f"EX-{i}") for i in range(3)]
    for receipt in receipts:
        await asyncio.gather(
            *(registry.persist_usage(dict(reversed(list(receipt.items())))) for _ in range(5))
        )
    restarted = FleetRegistry(registry.redis, registry.prefix.split(":")[1].split("}")[0])
    assert len(await restarted.pending_usage()) == 3
    with pytest.raises(ServiceError):
        await registry.persist_usage({**receipts[0], "occupied_micros": 2})
    with pytest.raises(ValidationError):
        await registry.persist_usage({**receipts[0], "credentials": {"password": "private"}})
    batches = await asyncio.gather(*(registry.pending_usage(limit=1, claim=True) for _ in range(3)))
    assert len({batch[0]["receipt_id"] for batch in batches}) == 3
    assert await registry.pending_usage(limit=1, claim=True) == []
    assert len(await registry.pending_usage()) == 3
    assert await registry.redis.ttl(registry.prefix + ":usage") == -1
    assert await registry.redis.ttl(registry.prefix + ":physical") == -2  # Never created.


async def test_expired_idle_cleanup_is_bounded_and_full_registry_never_discards_active_work(
    registry,
):
    bounded = FleetRegistry(registry.redis, "retirement_" + secrets.token_hex(8), max_workers=1)
    try:
        descriptor = worker()
        token = await bounded.register(descriptor)
        receipt = usage(descriptor)
        assert await reserve(bounded, descriptor, token, receipt)
        await expire(bounded, descriptor)
        with pytest.raises(ServiceError) as rejected:
            await bounded.register(worker())
        assert rejected.value.code == "worker_registry_capacity"
        assert await bounded.get(descriptor.worker_id) is not None
        await bounded.persist_usage(receipt)
        await bounded.finish_attempt(descriptor.worker_id, receipt)
        await bounded.acknowledge_usage(receipt["receipt_id"])
        await bounded.redis.zadd(bounded.prefix + ":expiry", {descriptor.worker_id: 0})
        replacement = worker()
        assert await bounded.register(replacement)
        assert await bounded.get(descriptor.worker_id) is None
        assert await bounded.list_all() == [replacement]
    finally:
        keys = [key async for key in registry.redis.scan_iter(match=bounded.prefix + ":*")]
        await registry.redis.delete(*keys)


async def test_observed_unreserved_build_work_prevents_unsafe_expired_retirement(registry):
    descriptor = worker()
    token = await registry.register(descriptor)
    await registry.heartbeat(descriptor.worker_id, token, active_runs=1)
    await expire(registry, descriptor)
    assert await registry.retire_expired() == 0
    assert await registry.get(descriptor.worker_id) is not None


async def test_completion_tombstones_are_bounded_and_expired_pruning_is_paged(registry):
    for i in range(300):
        await registry.redis.hset(registry.prefix + ":tombstones", f"fixture-{i}", "1")
        await registry.redis.zadd(registry.prefix + ":tombstone_expiry", {f"fixture-{i}": 0})
    await registry.stats()
    assert await registry.redis.hlen(registry.prefix + ":tombstones") == 44
    await registry.stats()
    assert await registry.redis.hlen(registry.prefix + ":tombstones") == 0


@pytest.mark.parametrize(
    "method,values",
    [
        ("retire_expired", {"limit": True}),
        ("retire_expired", {"limit": 257}),
        ("pending_usage", {"limit": 0}),
        ("pending_usage", {"limit": 1001}),
        ("pending_usage", {"claim": 1}),
        ("list_all", {"limit": True}),
        ("list_all", {"limit": 10001}),
    ],
)
async def test_collection_and_retirement_bounds_are_validated_without_redis(method, values):
    with pytest.raises(ValueError):
        await getattr(FleetRegistry(AsyncMock(), "fixture"), method)(**values)


async def test_outbox_rejects_two_physical_identities_for_same_logical_attempt(registry):
    descriptor = worker()
    await registry.register(descriptor)
    first = usage(descriptor)
    await registry.persist_usage(first)
    second = usage(descriptor, sandbox_id="maeyr-" + "e" * 48)
    with pytest.raises(ServiceError):
        await registry.persist_usage(second)
    await registry.acknowledge_usage(first["receipt_id"])
    with pytest.raises(ServiceError):
        await registry.persist_usage(second)
    assert await registry.pending_usage() == []


async def test_minimum_completion_capacity_keeps_finish_and_ack_possible(registry):
    bounded = FleetRegistry(
        registry.redis,
        "minimum_" + secrets.token_hex(8),
        max_workers=1,
        max_pending_receipts=1,
        max_tombstones=4,
    )
    try:
        descriptor = worker(slots=1)
        token = await bounded.register(descriptor)
        receipt = usage(descriptor)
        assert await reserve(bounded, descriptor, token, receipt)
        await bounded.persist_usage(receipt)
        assert await bounded.finish_attempt(descriptor.worker_id, receipt)
        assert await bounded.acknowledge_usage(receipt["receipt_id"])
        assert await bounded.heartbeat(descriptor.worker_id, token, draining=True)
        assert await bounded.retire(descriptor.worker_id, token, journals_drained=True)
        assert await bounded.redis.hlen(bounded.prefix + ":tombstones") == 3
    finally:
        keys = [key async for key in registry.redis.scan_iter(match=bounded.prefix + ":*")]
        await registry.redis.delete(*keys)
