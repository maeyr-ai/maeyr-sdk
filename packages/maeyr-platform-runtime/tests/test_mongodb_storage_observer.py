"""Provider normalization and real, fenced observations on disposable MongoDB."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from bson.int64 import Int64

from maeyr_platform.mongodb_storage_observer import (
    ACTIVE_DUE_FILTER,
    ACTIVE_DUE_INDEX,
    DORMANT_AT,
    DUE_INDEX,
    DUE_INDEX_FILTER,
    DUE_INDEX_KEYS,
    MongoDBStorageObserver,
    measured_storage_fields,
    refresh_account_storage,
    valid_storage_counters,
)


def observer_metadata_entries() -> list[dict[str, Any]]:
    return [
        {"name": name, "key": dict(DUE_INDEX_KEYS), "partialFilterExpression": condition}
        for name, condition in (
            (DUE_INDEX, DUE_INDEX_FILTER),
            (ACTIVE_DUE_INDEX, ACTIVE_DUE_FILTER),
        )
    ]


def metadata_observer(entries: list[Any]) -> MongoDBStorageObserver:
    database = SimpleNamespace(
        name="user-management",
        command=AsyncMock(
            return_value={
                "cursor": {"ns": "user-management.accounts", "id": 0, "firstBatch": entries}
            }
        ),
    )
    database.with_options = Mock(return_value=database)
    accounts = SimpleNamespace(name="accounts", database=database)
    accounts.with_options = Mock(return_value=accounts)
    return MongoDBStorageObserver(accounts, {})


async def test_observer_requires_committed_native_index_definitions() -> None:
    worker = metadata_observer(observer_metadata_entries())
    await worker.require_indexes()
    worker.accounts.database.command.assert_awaited_once_with(
        {
            "listIndexes": "accounts",
            "includeBuildUUIDs": True,
            "cursor": {"batchSize": 64},
            "maxTimeMS": 5000,
        }
    )


@pytest.mark.parametrize("index_name", [DUE_INDEX, ACTIVE_DUE_INDEX])
@pytest.mark.parametrize("problem", ["missing", "building", "ttl", "keys", "filter", "hidden"])
async def test_observer_never_accepts_unusable_due_index(index_name: str, problem: str) -> None:
    entries: list[Any] = observer_metadata_entries()
    index = next(i for i, item in enumerate(entries) if item["name"] == index_name)
    if problem == "missing":
        entries.pop(index)
    elif problem == "building":
        entries[index] = {"spec": entries[index], "buildUUID": b"unfinished-native-build"}
    elif problem == "ttl":
        entries[index]["expireAfterSeconds"] = 0
    elif problem == "keys":
        entries[index]["key"] = {"_id": 1}
    elif problem == "filter":
        entries[index]["partialFilterExpression"] = {"_id": {"$exists": True}}
    else:
        entries[index]["hidden"] = True
    with pytest.raises(RuntimeError, match="requires explicit setup"):
        await metadata_observer(entries).require_indexes()


@pytest.mark.parametrize("data,indexes", [(0.0, 0.0), (25.0, 75.0), (Int64(5_000_000_000), 100)])
def test_exact_provider_measurements_normalize_native_double_bytes(
    data: int | float, indexes: int | float
) -> None:
    result = measured_storage_fields(
        {"dataSize": data, "indexSize": indexes}, observed_at=datetime(2026, 10, 6)
    )
    assert result["total_bytes"] == data + indexes
    assert all(
        type(result[key]) is int for key in ("data_size_bytes", "index_size_bytes", "total_bytes")
    )


@pytest.mark.parametrize(
    "value", [True, "5", None, -1, -0.5, 1.5, float("inf"), float("nan"), float(2**53)]
)
def test_provider_measurement_rejects_inexact_unsafe_or_missing_values(value: object) -> None:
    with pytest.raises(ValueError):
        measured_storage_fields(
            {"dataSize": value, "indexSize": 0}, observed_at=datetime(2026, 10, 6)
        )


def test_provider_normalization_does_not_coerce_persisted_accounting_counters() -> None:
    state = {
        "max_age_seconds": 60,
        "committed_growth_total": 0,
        "baseline_committed_growth": 0,
        "pending_growth_bytes": 0,
        "active_operations": 0,
    }
    assert valid_storage_counters(state)
    assert valid_storage_counters({**state, "max_age_seconds": Int64(60)})
    assert not valid_storage_counters({**state, "pending_growth_bytes": 0.0})


@pytest.fixture
async def live_observer() -> AsyncIterator[SimpleNamespace]:
    uri = os.getenv("MONGODB_STORAGE_TEST_URI")
    if not uri:
        pytest.skip("Disposable loopback MongoDB replica set required")
    parsed = urlsplit(uri)
    assert parsed.scheme == "mongodb" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    assert parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
    assert parse_qs(parsed.query) == {
        "replicaSet": ["rs-storage-tests"],
        "directConnection": ["true"],
    }
    from motor.motor_asyncio import AsyncIOMotorClient

    client: Any = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    hello = await client.admin.command("hello")
    assert hello.get("isWritablePrimary") and hello.get("setName") == "rs-storage-tests"
    account_id = "AC-" + uuid4().hex.upper()
    accounts = client["user-management"]["accounts"]
    now = datetime.utcnow()
    await accounts.insert_one(
        {
            "_id": account_id,
            "limits": {"max_mongodb_storage_bytes": 5_000_000_000},
            "mongodb_storage": {
                "data_size_bytes": None,
                "index_size_bytes": None,
                "total_bytes": None,
                "observed_at": None,
                "max_age_seconds": 60,
                "committed_growth_total": 0,
                "baseline_committed_growth": 0,
                "pending_growth_bytes": 0,
                "active_operations": 0,
                "next_observation_at": now,
            },
        }
    )
    try:
        yield SimpleNamespace(client=client, accounts=accounts, account_id=account_id, now=now)
    finally:
        await accounts.delete_one({"_id": account_id})
        await client.drop_database(account_id)
        client.close()


async def test_real_shared_refresh_measures_empty_database_integers_and_canonical_indexes(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    worker = MongoDBStorageObserver(s.accounts, s.client)
    await worker.ensure_indexes()
    await worker.require_indexes()
    assert await refresh_account_storage(s.client, s.account_id) == "observed"
    row = await s.accounts.find_one({"_id": s.account_id})
    state = row["mongodb_storage"]
    assert state["data_size_bytes"] == state["index_size_bytes"] == state["total_bytes"] == 0
    assert type(state["total_bytes"]) is int
    assert state["next_observation_at"] == DORMANT_AT
    assert "observation_lease_owner" not in state


@pytest.mark.parametrize(
    "field,value",
    [
        ("limits", None),
        ("limits.max_mongodb_storage_bytes", None),
        ("limits.max_mongodb_storage_bytes", [5_000_000_000]),
        ("limits.max_mongodb_storage_bytes", 5_000_000_000.0),
        ("limits.max_mongodb_storage_bytes", True),
        ("mongodb_storage", None),
        ("mongodb_storage.pending_growth_bytes", None),
        ("mongodb_storage.pending_growth_bytes", [0]),
        ("mongodb_storage.pending_growth_bytes", -1),
        ("mongodb_storage.pending_growth_bytes", 0.0),
        ("mongodb_storage.max_age_seconds", [60]),
        ("mongodb_storage.max_age_seconds", 60.0),
        ("mongodb_storage.baseline_committed_growth", 1),
    ],
)
async def test_real_forced_observation_cannot_create_or_change_unprepared_accounting(
    live_observer: SimpleNamespace, field: str, value: Any
) -> None:
    s = live_observer
    mutation = {"$unset": {field: ""}} if value is None else {"$set": {field: value}}
    await s.accounts.update_one({"_id": s.account_id}, mutation)
    before = await s.accounts.find_one({"_id": s.account_id})
    entered, release = asyncio.Event(), asyncio.Event()
    worker = MongoDBStorageObserver(
        s.accounts, {s.account_id: DelayedDatabase(s.client[s.account_id], entered, release)}
    )
    outcome = await asyncio.wait_for(worker.observe_account(s.account_id, force=True), 5)
    assert outcome == "unavailable"
    assert not entered.is_set()
    assert await s.accounts.find_one({"_id": s.account_id}) == before


async def test_real_deleted_grant_during_observation_prevents_publication(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    entered, release = asyncio.Event(), asyncio.Event()
    worker = MongoDBStorageObserver(
        s.accounts, {s.account_id: DelayedDatabase(s.client[s.account_id], entered, release)}
    )
    running = asyncio.create_task(worker.observe_account(s.account_id, force=True))
    await asyncio.wait_for(entered.wait(), 5)
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$unset": {"limits.max_mongodb_storage_bytes": ""}}
    )
    before = await s.accounts.find_one({"_id": s.account_id})
    release.set()
    assert await asyncio.wait_for(running, 5) == "lease_lost"
    assert await s.accounts.find_one({"_id": s.account_id}) == before


async def test_real_invalid_accounting_cannot_starve_valid_due_page(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    database_name = "observer-due-qa-" + uuid4().hex
    accounts = s.client[database_name]["accounts"]
    canonical = await s.accounts.find_one({"_id": s.account_id})
    invalid_rows = []
    for value in ([5_000_000_000], True, 5_000_000_000.0):
        row = {
            **canonical,
            "_id": "AC-" + uuid4().hex.upper(),
            "limits": {"max_mongodb_storage_bytes": value},
            "mongodb_storage": {
                **canonical["mongodb_storage"],
                "next_observation_at": s.now - timedelta(seconds=1),
            },
        }
        invalid_rows.append(row)
    for identity in (19, "user-management", "AC-BAD\n", "AC-" + "x" * 61):
        invalid_rows.append({
            **canonical,
            "_id": identity,
            "mongodb_storage": {
                **canonical["mongodb_storage"],
                "next_observation_at": s.now - timedelta(seconds=1),
            },
        })
    try:
        await accounts.insert_many([*invalid_rows, canonical])
        invalid_before = [
            await accounts.find_one({"_id": row["_id"]}) for row in invalid_rows
        ]
        from maeyr_platform.mongodb_storage_observer import StorageObservationSettings

        worker = MongoDBStorageObserver(
            accounts, s.client, settings=StorageObservationSettings(BATCH_SIZE=1),
            clock=lambda: s.now,
        )
        await worker.ensure_indexes()
        result = await worker.run_once()
        assert result["due"] == result["observed"] == 1
        for row in invalid_before:
            assert await accounts.find_one({"_id": row["_id"]}) == row
        state = (await accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
        assert state["total_bytes"] == 0 and state["next_observation_at"] == DORMANT_AT
    finally:
        await s.client.drop_database(database_name)


async def test_real_observer_accepts_native_bson_long_freshness_and_counters(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    await s.accounts.update_one({"_id": s.account_id}, {"$set": {
        "mongodb_storage.max_age_seconds": Int64(60),
        "mongodb_storage.pending_growth_bytes": Int64(10),
        "mongodb_storage.committed_growth_total": Int64(20),
    }})
    assert await refresh_account_storage(s.client, s.account_id) == "observed"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["pending_growth_bytes"] == 10 and state["committed_growth_total"] == 20
    assert state["baseline_committed_growth"] == 20
    assert state["next_observation_at"] < DORMANT_AT


class DelayedDatabase:
    def __init__(self, database: Any, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.database = database
        self.entered = entered
        self.release = release

    def with_options(self, **kwargs: Any) -> DelayedDatabase:
        return DelayedDatabase(self.database.with_options(**kwargs), self.entered, self.release)

    async def command(self, value: dict[str, Any]) -> dict[str, Any]:
        self.entered.set()
        await self.release.wait()
        return cast(dict[str, Any], await self.database.command(value))


async def test_real_observer_preserves_growth_committed_after_baseline_capture(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    entered, release = asyncio.Event(), asyncio.Event()
    database = DelayedDatabase(s.client[s.account_id], entered, release)
    worker = MongoDBStorageObserver(s.accounts, {s.account_id: database})
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"mongodb_storage.committed_growth_total": 100}}
    )
    running = asyncio.create_task(worker.observe_account(s.account_id, force=True))
    await asyncio.wait_for(entered.wait(), 5)
    await s.accounts.update_one(
        {"_id": s.account_id},
        {
            "$inc": {
                "mongodb_storage.committed_growth_total": 40,
                "mongodb_storage.pending_growth_bytes": 20,
                "mongodb_storage.active_operations": 1,
            }
        },
    )
    release.set()
    assert await asyncio.wait_for(running, 5) == "observed"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["baseline_committed_growth"] == 100 and state["committed_growth_total"] == 140
    assert state["pending_growth_bytes"] == 20 and state["active_operations"] == 1
    assert state["next_observation_at"] < DORMANT_AT


async def test_real_expired_publisher_cannot_overwrite_new_owner_measurement(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    entered, release = asyncio.Event(), asyncio.Event()
    worker = MongoDBStorageObserver(
        s.accounts,
        {s.account_id: DelayedDatabase(s.client[s.account_id], entered, release)},
        clock=lambda: s.now,
    )
    old = asyncio.create_task(worker.observe_account(s.account_id, force=True))
    await asyncio.wait_for(entered.wait(), 5)
    assert await refresh_account_storage(s.client, s.account_id) == "not_due"
    await s.accounts.update_one(
        {"_id": s.account_id},
        {
            "$set": {
                "mongodb_storage.observation_lease_until": s.now - timedelta(seconds=1),
                "mongodb_storage.committed_growth_total": 200,
            }
        },
    )
    new = MongoDBStorageObserver(s.accounts, s.client, clock=lambda: s.now + timedelta(seconds=1))
    assert await new.observe_account(s.account_id, force=True) == "observed"
    release.set()
    assert await asyncio.wait_for(old, 5) == "lease_lost"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["baseline_committed_growth"] == 200
    assert state["observed_at"] == (s.now + timedelta(seconds=1)).replace(
        microsecond=(s.now.microsecond // 1000) * 1000
    )


async def test_real_newer_external_publication_cannot_regress_commit_baseline(
    live_observer: SimpleNamespace,
) -> None:
    s = live_observer
    entered, release = asyncio.Event(), asyncio.Event()
    worker = MongoDBStorageObserver(
        s.accounts,
        {s.account_id: DelayedDatabase(s.client[s.account_id], entered, release)},
        clock=lambda: s.now,
    )
    old = asyncio.create_task(worker.observe_account(s.account_id, force=True))
    await asyncio.wait_for(entered.wait(), 5)
    await s.accounts.update_one(
        {"_id": s.account_id},
        {
            "$set": {
                "mongodb_storage.baseline_committed_growth": 10,
                "mongodb_storage.committed_growth_total": 10,
                "mongodb_storage.observed_at": s.now + timedelta(seconds=1),
            }
        },
    )
    release.set()
    assert await asyncio.wait_for(old, 5) == "lease_lost"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["baseline_committed_growth"] == 10
