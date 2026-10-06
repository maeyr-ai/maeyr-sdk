from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from bson.int64 import Int64

from maeyr_platform.license_errors import public_license_denial_detail
from maeyr_platform.mongo_storage import (
    MAX_STORAGE_BYTES,
    GuardedMongoChangeStream,
    GuardedMongoClient,
    GuardedMongoCollection,
    GuardedMongoCursor,
    GuardedMongoDatabase,
    GuardedMongoSession,
    GuardedMongoTransaction,
    MongoStorageCapacityError,
    StorageWriteGuard,
    estimate_write_growth,
    guard_platform_mongo_client,
    storage_admission_state,
)


def observation(now: datetime, *, data: int = 0, indexes: int = 0) -> dict[str, Any]:
    return {
        "data_size_bytes": data,
        "index_size_bytes": indexes,
        "total_bytes": data + indexes,
        "observed_at": now,
        "max_age_seconds": 60,
        "committed_growth_total": 0,
        "baseline_committed_growth": 0,
        "pending_growth_bytes": 0,
        "active_operations": 0,
    }


def account(now: datetime, *, limit: int = 500_000_000) -> dict[str, Any]:
    return {"limits": {"max_mongodb_storage_bytes": limit}, "mongodb_storage": observation(now)}


@pytest.mark.parametrize("limit", [500_000_000, 5_000_000_000, 25_000_000_000, MAX_STORAGE_BYTES])
def test_decimal_byte_limits_and_native_bson_long(limit: int) -> None:
    now = datetime.utcnow()
    row = account(now, limit=Int64(limit))
    row["mongodb_storage"].update(
        committed_growth_total=Int64(1000),
        baseline_committed_growth=Int64(100),
        pending_growth_bytes=Int64(500),
    )
    assert storage_admission_state(row, now) == (limit, 1400)


@pytest.mark.parametrize(
    "value", [None, True, False, -1, 1.5, 5_000_000_000.0, "500000000", MAX_STORAGE_BYTES + 1]
)
def test_invalid_or_absent_grant_never_uses_plan_fallback(value: object) -> None:
    now = datetime.utcnow()
    row = account(now)
    row["subscription_plan"] = "team"
    row["limits"]["max_mongodb_storage_bytes"] = value
    with pytest.raises(MongoStorageCapacityError) as error:
        storage_admission_state(row, now)
    assert error.value.detail["code"] == "mongodb_storage_grant_unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_bytes", None),
        ("index_size_bytes", -1),
        ("data_size_bytes", True),
        ("total_bytes", 1),
        ("baseline_committed_growth", 1),
        ("pending_growth_bytes", 0.0),
        ("active_operations", -1),
        ("max_age_seconds", True),
        ("max_age_seconds", 301),
        ("committed_growth_total", MAX_STORAGE_BYTES + 1),
    ],
)
def test_invalid_observations_fail_closed(field: str, value: object) -> None:
    now = datetime.utcnow()
    row = account(now)
    row["mongodb_storage"][field] = value
    with pytest.raises(MongoStorageCapacityError) as error:
        storage_admission_state(row, now)
    assert error.value.detail["code"] == "mongodb_storage_observation_unavailable"


@pytest.mark.parametrize("age", [60, 61, -1])
def test_stale_boundary_and_future_observations_deny(age: int) -> None:
    now = datetime.utcnow()
    row = account(now - timedelta(seconds=age))
    with pytest.raises(MongoStorageCapacityError):
        storage_admission_state(row, now)


def test_utc_aware_observation_and_pending_growth_are_accounted() -> None:
    now = datetime.now(timezone.utc)
    row = account(now - timedelta(seconds=59), limit=25_000_000_000)
    row["mongodb_storage"].update(
        data_size_bytes=100,
        index_size_bytes=200,
        total_bytes=300,
        pending_growth_bytes=400,
        committed_growth_total=800,
        baseline_committed_growth=500,
    )
    assert storage_admission_state(row, now) == (25_000_000_000, 1000)


@pytest.mark.parametrize(
    "code",
    [
        "mongodb_storage_limit_exceeded",
        "mongodb_storage_reservation_exceeds_headroom",
        "mongodb_storage_observation_unavailable",
        "mongodb_storage_grant_unavailable",
        "mongodb_storage_accounting_unavailable",
        "mongodb_storage_operation_unsupported",
    ],
)
def test_public_error_catalog_agrees_with_guard(code: str) -> None:
    error = MongoStorageCapacityError(code)
    assert public_license_denial_detail(error.status_code, error.detail) == error.detail


def test_byte_estimator_is_linear_in_bounded_serialized_input() -> None:
    small = estimate_write_growth({"value": "small"})
    big = estimate_write_growth({"value": "x" * 50_000})
    assert small > 0 and big > 50_000
    assert estimate_write_growth({"value": "small"}, 1000) == small * 1000
    with pytest.raises(MongoStorageCapacityError):
        estimate_write_growth({"value": "small"}, 1001)
    with pytest.raises(MongoStorageCapacityError):
        estimate_write_growth({"value": "x" * (16 * 1024 * 1024)})


@pytest.mark.parametrize("database", ["AC-1234ABCD", "user-management", "admin"])
@pytest.mark.parametrize(
    "command,kwargs",
    [
        ({"aggregate": "people", "pipeline": [{"$out": "other"}], "cursor": {}}, {}),
        ({"aggregate": "people", "pipeline": [], "cursor": {}}, {"pipeline": [{"$out": "other"}]}),
        ("aggregate", {"pipeline": [{"$merge": {"into": {"db": "AC-FFFFFFFF", "coll": "other"}}}]}),
        ({"aggregate": "people", "pipeline": [{"$facet": {"nested": [{"$out": "other"}]}}]}, {}),
        ({"ping": 1}, {"applyOps": [{"op": "i", "ns": "AC-1234ABCD.other"}]}),
        ({"renameCollection": "AC-1234ABCD.people", "to": "AC-FFFFFFFF.people"}, {}),
    ],
)
async def test_raw_command_paths_cannot_bypass_guard(
    database: str, command: object, kwargs: dict[str, Any]
) -> None:
    delegate_db = SimpleNamespace(name=database, command=AsyncMock())
    raw = SimpleNamespace(get_database=lambda _: delegate_db)
    with pytest.raises(MongoStorageCapacityError):
        await guard_platform_mongo_client(raw)[database].command(command, **kwargs)
    delegate_db.command.assert_not_awaited()


async def test_ambiguous_write_retains_charge_and_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = object.__new__(StorageWriteGuard)
    reserve, settle = AsyncMock(), AsyncMock()
    monkeypatch.setattr(guard, "reserve", reserve)
    monkeypatch.setattr(guard, "settle", settle)
    write = AsyncMock(side_effect=TimeoutError("private driver diagnostic"))
    with pytest.raises(TimeoutError):
        await guard.execute(write, (), {}, 1234)
    reserve.assert_awaited_once_with(1234, session=None)
    settle.assert_awaited_once_with(1234, session=None)


async def test_cancellation_retains_charge_even_if_settlement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = object.__new__(StorageWriteGuard)
    monkeypatch.setattr(guard, "reserve", AsyncMock())
    settle = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    monkeypatch.setattr(guard, "settle", settle)
    write = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await guard.execute(write, (), {}, 1234)
    settle.assert_awaited_once()


TEST_URI = os.getenv("MONGODB_STORAGE_TEST_URI")


@pytest.fixture
async def live_storage() -> AsyncIterator[SimpleNamespace]:
    if not TEST_URI:
        pytest.skip("Disposable loopback MongoDB replica set required")
    parsed = urlsplit(TEST_URI)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "mongodb" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    assert parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
    assert query.get("replicaSet") == ["rs-storage-tests"] and query.get("directConnection") == [
        "true"
    ]
    from motor.motor_asyncio import AsyncIOMotorClient

    raw: Any = AsyncIOMotorClient(TEST_URI, serverSelectionTimeoutMS=5000)
    hello = await raw.admin.command("hello")
    assert hello.get("isWritablePrimary") and hello.get("setName") == "rs-storage-tests"
    account_id = "AC-" + uuid4().hex.upper()
    guarded = guard_platform_mongo_client(raw)
    accounts = raw["user-management"]["accounts"]
    stats = await raw[account_id].command({"dbStats": 1, "scale": 1})
    # Command metrics can be integral BSON doubles even with scale=1. Only
    # provider measurements are normalized; stored grants stay strict integers.
    assert int(stats["dataSize"]) == stats["dataSize"]
    assert int(stats["indexSize"]) == stats["indexSize"]
    now = datetime.utcnow()
    row = {"_id": account_id, **account(now, limit=100_000)}
    row["mongodb_storage"] = observation(
        now, data=int(stats["dataSize"]), indexes=int(stats["indexSize"])
    )
    await accounts.insert_one(row)
    try:
        yield SimpleNamespace(
            raw=raw,
            guarded=guarded,
            account_id=account_id,
            accounts=accounts,
            collection=guarded[account_id]["records"],
        )
    finally:
        await accounts.delete_one({"_id": account_id})
        await raw.drop_database(account_id)
        raw.close()


async def test_real_atomic_reservations_prevent_concurrent_oversubscription(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    outcomes = await asyncio.gather(
        *(s.collection.insert_one({"_id": str(i)}) for i in range(80)), return_exceptions=True
    )
    accepted = sum(not isinstance(value, BaseException) for value in outcomes)
    assert accepted == 100_000 // estimate_write_growth({"_id": "0"})
    assert all(
        not isinstance(value, BaseException)
        or (isinstance(value, MongoStorageCapacityError) and value.status_code == 429)
        for value in outcomes
    )
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["pending_growth_bytes"] == 0 and state["active_operations"] == 0
    assert state["committed_growth_total"] <= 100_000
    assert await s.collection.count_documents({}) == accepted


@pytest.mark.parametrize("used", [100_000, 100_001])
async def test_real_exact_and_over_capacity_deny_writes_preserve_reads(
    live_storage: SimpleNamespace, used: int
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id},
        {"$set": {"mongodb_storage.data_size_bytes": used, "mongodb_storage.total_bytes": used}},
    )
    mutations: tuple[Callable[[], Awaitable[Any]], ...] = (
        lambda: s.collection.insert_one({"_id": "no"}),
        lambda: s.collection.update_one({"_id": "no"}, {"$set": {"x": 1}}, upsert=True),
        lambda: s.collection.delete_one({"_id": "no"}),
    )
    for mutation in mutations:
        with pytest.raises(MongoStorageCapacityError) as error:
            await mutation()
        assert error.value.status_code == 429
    assert await s.collection.find_one({}) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("limits.max_mongodb_storage_bytes", [100_000]),
        ("limits.max_mongodb_storage_bytes", [0, 100_000]),
        ("limits", [{"max_mongodb_storage_bytes": 100_000}]),
    ],
)
async def test_real_array_shaped_grants_never_authorize_storage_mutations(
    live_storage: SimpleNamespace, field: str, value: object
) -> None:
    s = live_storage
    await s.accounts.update_one({"_id": s.account_id}, {"$set": {field: value}})
    original = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.insert_one({"_id": "must-not-write"})
    assert error.value.detail["code"] == "mongodb_storage_grant_unavailable"
    current = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    for counter in ("committed_growth_total", "pending_growth_bytes", "active_operations"):
        assert current[counter] == original[counter]
    assert await s.collection.count_documents({}) == 0


async def test_real_stale_authority_refreshes_once_before_mutation(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id},
        {"$set": {"mongodb_storage.observed_at": datetime.utcnow() - timedelta(seconds=60)}},
    )
    await s.collection.insert_one({"_id": "freshly_measured"})
    measured = await s.accounts.find_one({"_id": s.account_id})
    assert measured["mongodb_storage"]["observed_at"] > datetime.utcnow() - timedelta(seconds=10)
    assert measured["mongodb_storage"]["committed_growth_total"] == 4096
    assert await s.collection.count_documents({}) == 1


async def test_real_stale_authority_with_live_observation_lease_denies(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id},
        {
            "$set": {
                "mongodb_storage.observed_at": datetime.utcnow() - timedelta(seconds=60),
                "mongodb_storage.observation_lease_owner": "other_process",
                "mongodb_storage.observation_lease_until": datetime.utcnow()
                + timedelta(seconds=15),
            }
        },
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.insert_one({"_id": "blocked"})
    assert error.value.detail["code"] == "mongodb_storage_observation_unavailable"
    assert await s.collection.count_documents({}) == 0


async def test_real_invalid_authority_never_uses_measurement_to_create_grant(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id},
        {
            "$set": {
                "mongodb_storage.observed_at": datetime.utcnow(),
                "limits.max_mongodb_storage_bytes": 1.0,
            }
        },
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.insert_one({"_id": "invalid"})
    assert error.value.detail["code"] == "mongodb_storage_grant_unavailable"
    assert await s.collection.count_documents({}) == 0


async def test_real_quota_charges_roll_back_with_customer_transaction(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    async with await s.guarded.start_session() as session:
        session.start_transaction()
        await s.collection.insert_one({"_id": "aborted"}, session=session)
        await session.abort_transaction()
    assert await s.collection.find_one({}) is None
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["committed_growth_total"] == state["pending_growth_bytes"] == 0
    async with await s.guarded.start_session() as session:
        session.start_transaction()
        await s.collection.insert_one({"_id": "committed"}, session=session)
        await session.commit_transaction()
    assert (await s.collection.find_one({}))["_id"] == "committed"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["committed_growth_total"] > 0 and state["pending_growth_bytes"] == 0


async def test_real_parent_handles_options_and_unknown_databases_do_not_escape(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    assert isinstance(s.collection.database.client, GuardedMongoClient)
    await s.collection.with_options().insert_one({"_id": "scoped"})
    await s.collection.database.client[s.account_id]["records"]["nested"].insert_one(
        {"_id": "nested"}
    )
    with pytest.raises(MongoStorageCapacityError):
        await s.guarded["unrecognized_database"]["records"].insert_one({"_id": "escape"})
    with pytest.raises(AttributeError):
        getattr(s.collection, "delegate")


def test_motor_cursor_and_stream_public_handles_never_expose_raw_driver() -> None:
    motor = pytest.importorskip("motor.motor_asyncio")
    raw = motor.AsyncIOMotorClient("mongodb://127.0.0.1:27119/", connect=False)
    guarded = guard_platform_mongo_client(raw)
    database = guarded["AC-HANDLES"]
    collection = database["records"]
    try:
        # Motor synthesizes absent properties into database/collection handles.
        # Only genuine properties may be copied from each specific handle type.
        for name in ("max_pool_size", "min_pool_size"):
            child = getattr(guarded, name)
            assert isinstance(child, GuardedMongoDatabase)
            assert child.client is guarded
        for name in ("options", "address", "nodes", "is_primary", "is_mongos"):
            child = getattr(database, name)
            assert isinstance(child, GuardedMongoCollection)
            assert child.database.client is guarded
        for name in ("address", "nodes", "is_primary", "is_mongos", "max_pool_size"):
            with pytest.raises(AttributeError):
                getattr(collection, name)
        cursors = [
            collection.find({}),
            collection.find_raw_batches({}),
            collection.aggregate([]),
            collection.aggregate_raw_batches([]),
            collection.list_indexes(),
            collection.list_search_indexes(),
            database.aggregate([]),
        ]
        for cursor in cursors:
            assert isinstance(cursor, GuardedMongoCursor)
            assert isinstance(cursor.collection, GuardedMongoCollection)
            assert cursor.collection.database.client is guarded
            for name in ("delegate", "insert_one", "unknown_mutation"):
                with pytest.raises(AttributeError):
                    getattr(cursor, name)
        cursor = collection.find({})
        assert (
            cursor.sort("_id", 1).limit(5).skip(0).hint("_id_").batch_size(2).max_time_ms(1000)
            is cursor
        )
        for derived in (cursor.clone(), cursor.rewind(), copy(cursor), deepcopy(cursor)):
            assert isinstance(derived, GuardedMongoCursor)
            assert derived.collection is collection
            with pytest.raises(AttributeError):
                getattr(derived, "delegate")
        for stream in (collection.watch(), database.watch(), guarded.watch()):
            assert isinstance(stream, GuardedMongoChangeStream)
            for name in ("delegate", "client", "collection", "session", "unknown_mutation"):
                with pytest.raises(AttributeError):
                    getattr(stream, name)
        with pytest.raises(MongoStorageCapacityError):
            collection.watch([{"$merge": "other"}])
    finally:
        raw.close()


async def test_fake_session_is_unwrapped_only_for_driver_and_callbacks_keep_guard() -> None:
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def with_transaction(self, callback: Any, **kwargs: Any) -> Any:
            return await callback(self)

    session = Session()
    raw_cursor = SimpleNamespace(session=session)
    driver_collection = SimpleNamespace(
        name="records",
        full_name="user-management.records",
        find_one=AsyncMock(return_value={"_id": "one"}),
        insert_one=AsyncMock(return_value="inserted"),
        find=lambda *args, **kwargs: raw_cursor,
    )
    driver_database = SimpleNamespace(
        name="user-management", get_collection=lambda name: driver_collection
    )
    raw_client = SimpleNamespace(
        get_database=lambda name: driver_database,
        start_session=AsyncMock(return_value=session),
    )
    client = guard_platform_mongo_client(raw_client)
    collection = client["user-management"]["records"]
    async with await client.start_session() as guarded_session:
        assert guarded_session.client is client
        assert await collection.find_one({}, session=guarded_session) == {"_id": "one"}
        driver_collection.find_one.assert_awaited_once_with({}, session=session)
        cursor_session = collection.find({}, session=guarded_session).session
        assert isinstance(cursor_session, GuardedMongoSession)
        assert cursor_session.client is client

        async def operation(callback_session: Any) -> Any:
            assert callback_session is guarded_session
            return await collection.insert_one({"_id": "two"}, session=callback_session)

        assert await guarded_session.with_transaction(operation) == "inserted"
        driver_collection.insert_one.assert_awaited_once_with({"_id": "two"}, session=session)
        for handle in (guarded_session, cursor_session):
            for name in ("delegate", "unknown_mutation"):
                with pytest.raises(AttributeError):
                    getattr(handle, name)


async def test_real_read_cursor_parents_cannot_write_at_capacity(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.raw[s.account_id]["records"].insert_one({"_id": "seed"})
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 0}}
    )
    cursor = s.collection.find({}).sort("_id", 1).hint("_id_").limit(1).max_time_ms(1000)
    async with cursor as entered:
        assert entered is cursor
        assert [row["_id"] async for row in cursor] == ["seed"]
    assert await s.collection.aggregate([]).to_list(length=1) == [{"_id": "seed"}]
    cursors = [
        s.collection.find({}),
        s.collection.list_indexes(),
        await s.collection.database.list_collections(),
        await s.guarded.list_databases(),
    ]
    for cursor in cursors:
        assert isinstance(cursor, GuardedMongoCursor)
        parent = cursor.collection
        client = parent if isinstance(parent, GuardedMongoClient) else parent.database.client
        with pytest.raises(MongoStorageCapacityError) as error:
            await client[s.account_id]["records"].insert_one({"_id": "escape"})
        assert error.value.detail["code"] == "mongodb_storage_limit_exceeded"
        with pytest.raises(AttributeError):
            getattr(cursor, "delegate")
        await cursor.close()
    assert await s.collection.count_documents({}) == 1


async def test_real_session_callbacks_context_and_reads_keep_guarded_parent(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    async with await s.guarded.start_session() as session:
        assert isinstance(session, GuardedMongoSession) and session.client is s.guarded
        with pytest.raises(AttributeError):
            getattr(session, "delegate")
        with pytest.raises(AttributeError):
            getattr(session, "unknown_mutation")
        async with session.start_transaction() as transaction:
            assert isinstance(transaction, GuardedMongoTransaction)
            with pytest.raises(AttributeError):
                getattr(transaction, "delegate")
            await session.client[s.account_id]["records"].insert_one(
                {"_id": "context"}, session=session
            )
            assert await s.collection.find_one({"_id": "context"}, session=session) is not None
            cursor = s.collection.find({}, session=session)
            assert await cursor.to_list(length=1) == [{"_id": "context"}]
            cursor_session = cursor.session
            assert isinstance(cursor_session, GuardedMongoSession)
            assert cursor_session.client is s.guarded

        async def operation(callback_session: Any) -> str:
            assert callback_session is session
            await callback_session.client[s.account_id]["records"].insert_one(
                {"_id": "callback"}, session=callback_session
            )
            return "committed"

        assert await session.with_transaction(operation) == "committed"
    assert await s.collection.count_documents({}) == 2
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["active_operations"] == state["pending_growth_bytes"] == 0
    assert state["committed_growth_total"] == 2 * estimate_write_growth({"_id": "context"})


async def test_real_change_stream_context_lifecycle_blocks_public_delegate(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    async with s.collection.watch(max_await_time_ms=50) as stream:
        assert isinstance(stream, GuardedMongoChangeStream)
        assert stream.alive
        with pytest.raises(AttributeError):
            getattr(stream, "delegate")
        assert await stream.try_next() is None
    assert not stream.alive


async def test_real_headroom_refusal_is_distinct_from_account_at_capacity(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 1000}}
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.insert_one({"_id": "too_large"})
    assert error.value.detail["code"] == "mongodb_storage_reservation_exceeds_headroom"
    assert "limit has been reached" not in error.value.detail["message"]
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.create_index("email", name="email_v1")
    assert error.value.detail["code"] == "mongodb_storage_reservation_exceeds_headroom"
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 0}}
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.create_index("email", name="email_v1")
    assert error.value.detail["code"] == "mongodb_storage_limit_exceeded"


async def test_real_same_account_rename_returns_driver_result(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.collection.insert_one({"_id": "preserved"})
    result = await s.collection.rename("renamed_records")
    assert result["ok"] == 1
    assert await s.guarded[s.account_id]["renamed_records"].find_one({}) == {"_id": "preserved"}
    assert await s.collection.find_one({}) is None


@pytest.mark.parametrize(
    "override", [{"to": "AC-OTHER.records"}, {"renameCollection": "AC-OTHER.records"}]
)
async def test_rename_namespace_overrides_are_rejected_before_driver(
    override: dict[str, Any],
) -> None:
    driver_collection = SimpleNamespace(
        name="records", full_name="AC-1.records", rename=AsyncMock()
    )
    driver_database = SimpleNamespace(name="AC-1", get_collection=lambda name: driver_collection)
    client = guard_platform_mongo_client(SimpleNamespace(get_database=lambda name: driver_database))
    with pytest.raises(MongoStorageCapacityError) as error:
        await client["AC-1"]["records"].rename("new_records", **override)
    assert error.value.detail["code"] == "mongodb_storage_operation_unsupported"
    driver_collection.rename.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    [
        "client_default",
        "database_options",
        "collection_options",
        "create_keyword",
        "create_positional",
        "command_concern",
        "client_drop",
        "index_options",
        "bulk_options",
        "multi_options",
    ],
)
async def test_unacknowledged_writes_rejected_without_network_or_reservation(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    motor = pytest.importorskip("motor.motor_asyncio")
    from pymongo import InsertOne
    from pymongo.write_concern import WriteConcern

    reserve = AsyncMock()
    monkeypatch.setattr(StorageWriteGuard, "reserve", reserve)
    raw = motor.AsyncIOMotorClient(
        "mongodb://127.0.0.1:27119/",
        connect=False,
        w=0 if case in {"client_default", "client_drop"} else 1,
    )
    client = guard_platform_mongo_client(raw)
    database = client["AC-HANDLES"]
    collection = database["records"]
    unacknowledged = WriteConcern(w=0)
    try:
        with pytest.raises(MongoStorageCapacityError) as error:
            if case == "database_options":
                await database.with_options(write_concern=unacknowledged).create_collection(
                    "new_records"
                )
            elif case == "collection_options":
                await collection.with_options(write_concern=unacknowledged).insert_one(
                    {"_id": "one"}
                )
            elif case == "create_keyword":
                await database.create_collection("new_records", write_concern=unacknowledged)
            elif case == "create_positional":
                await database.create_collection("new_records", None, None, unacknowledged)
            elif case == "command_concern":
                await collection.find_one_and_update({}, {"$set": {"a": 1}}, writeConcern={"w": 0})
            elif case == "client_drop":
                await client.drop_database("AC-HANDLES")
            elif case == "index_options":
                await collection.with_options(write_concern=unacknowledged).create_index("name")
            elif case == "bulk_options":
                await collection.with_options(write_concern=unacknowledged).bulk_write(
                    [InsertOne({"_id": "one"})]
                )
            elif case == "multi_options":
                await collection.with_options(write_concern=unacknowledged).update_many(
                    {}, {"$set": {"a": 1}}
                )
            else:
                await collection.insert_one({"_id": "one"})
        assert error.value.detail["code"] == "mongodb_storage_operation_unsupported"
        reserve.assert_not_awaited()
    finally:
        raw.close()


async def test_real_unacknowledged_options_keep_reads_available_and_ledger_unchanged(
    live_storage: SimpleNamespace,
) -> None:
    from pymongo.write_concern import WriteConcern

    s = live_storage
    await s.raw[s.account_id]["records"].insert_one({"_id": "seed"})
    unacknowledged = s.collection.with_options(write_concern=WriteConcern(w=0))
    assert await unacknowledged.find_one({}) == {"_id": "seed"}
    assert await unacknowledged.find({}).to_list(length=1) == [{"_id": "seed"}]
    before = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    with pytest.raises(MongoStorageCapacityError):
        await unacknowledged.insert_one({"_id": "escape"})
    after = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert before == after
    assert await s.collection.count_documents({}) == 1


async def test_real_index_budget_and_existing_index_noop(live_storage: SimpleNamespace) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 5_000_000}}
    )
    name = await s.collection.create_index("name", name="name_v1", unique=True)
    before = await s.accounts.find_one({"_id": s.account_id})
    assert name == "name_v1" and "name_v1" in await s.collection.index_information()
    assert await s.collection.create_index("name", name="name_v1", unique=True) == name
    after = await s.accounts.find_one({"_id": s.account_id})
    assert (
        before["mongodb_storage"]["committed_growth_total"]
        == after["mongodb_storage"]["committed_growth_total"]
    )
    with pytest.raises(MongoStorageCapacityError):
        await s.collection.create_index("name", name="name_v1", unique=False)


@pytest.mark.parametrize("method", ["insert_many", "bulk_write", "create_indexes"])
async def test_infinite_inputs_are_rejected_after_bounded_consumption(method: str) -> None:
    consumed = 0

    def values() -> Any:
        nonlocal consumed
        while True:
            consumed += 1
            yield {"value": consumed}

    delegate_collection = SimpleNamespace(
        name="records", full_name="AC-1.records", write_concern=SimpleNamespace(acknowledged=True)
    )
    delegate_db = SimpleNamespace(name="AC-1", get_collection=lambda _: delegate_collection)
    raw = SimpleNamespace(get_database=lambda _: delegate_db)
    collection = guard_platform_mongo_client(raw)["AC-1"]["records"]
    with pytest.raises(MongoStorageCapacityError):
        await getattr(collection, method)(values())
    assert consumed == (65 if method == "create_indexes" else 1001)


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$set": {"payload": {"$concatArrays": ["$payload", "$payload"]}}}],
        [{"$set": {"payload": ["$payload", "$payload"]}}],
        [{"$set": {"payload": {"nested": "$payload"}}}],
        [{"$set": {"new_field": "$payload"}}],
        [{"$set": {"items.payload": "$items.payload"}}],
        [
            {
                "$set": {
                    "value": {
                        "$function": {"body": "function(){return 1}", "args": [], "lang": "js"}
                    }
                }
            }
        ],
        [{"$replaceRoot": {"newRoot": "$payload"}}],
    ],
)
async def test_unbounded_update_expressions_never_reach_driver(pipeline: object) -> None:
    delegate = SimpleNamespace(name="records", full_name="AC-1.records", update_one=AsyncMock())
    delegate_db = SimpleNamespace(name="AC-1", get_collection=lambda _: delegate)
    collection = guard_platform_mongo_client(SimpleNamespace(get_database=lambda _: delegate_db))[
        "AC-1"
    ]["records"]
    with pytest.raises(MongoStorageCapacityError):
        await collection.update_one({"_id": "one"}, pipeline)
    delegate.update_one.assert_not_awaited()


async def test_real_bounded_conditional_pipeline_preserves_atomic_trace_versions(
    live_storage: SimpleNamespace,
) -> None:
    from pymongo import UpdateOne

    s = live_storage
    version = {"$ifNull": ["$version", -1]}

    def operation(incoming: int, status: str) -> Any:
        allowed = {"$lte": [version, incoming]}
        return UpdateOne(
            {"_id": "span"},
            [
                {
                    "$set": {
                        "version": {"$cond": [allowed, {"$literal": incoming}, "$version"]},
                        "status": {"$cond": [allowed, {"$literal": status}, "$status"]},
                    }
                }
            ],
            upsert=True,
        )

    await s.collection.bulk_write(
        [operation(1, "running"), operation(2, "complete"), operation(1, "running")]
    )
    row = await s.collection.find_one({"_id": "span"})
    assert row["version"] == 2 and row["status"] == "complete"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["committed_growth_total"] == 3 * 4096
    assert state["pending_growth_bytes"] == 0 and state["active_operations"] == 0


async def test_real_multi_update_is_bounded_and_charges_each_proved_row(
    live_storage: SimpleNamespace,
) -> None:
    from maeyr_platform.mongodb_storage_observer import refresh_account_storage

    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 10_000_000}}
    )
    await s.raw[s.account_id]["records"].insert_many(
        [{"_id": str(i), "state": "pending"} for i in range(1001)]
    )
    assert await refresh_account_storage(s.guarded, s.account_id) == "observed"
    with pytest.raises(MongoStorageCapacityError):
        await s.collection.update_many({"state": "pending"}, {"$set": {"state": "complete"}})
    assert await s.collection.count_documents({"state": "complete"}) == 0
    result = await s.collection.update_many(
        {"_id": {"$ne": "1000"}}, {"$set": {"state": "complete"}}
    )
    assert result.modified_count == 1000
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["committed_growth_total"] == 1000 * 4096
    assert state["pending_growth_bytes"] == 0


async def test_real_accounts_share_capacity_across_projects_and_recheck_grant(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.collection.insert_one({"_id": "project1", "project_id": "PI-1"})
    await s.guarded[s.account_id]["other"].insert_one({"_id": "project2", "project_id": "PI-2"})
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["committed_growth_total"] == 8192
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 8192}}
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.insert_one({"_id": "third"})
    assert error.value.status_code == 429
    assert await s.collection.find_one({"_id": "project1"}) is not None


async def test_real_index_budget_accepts_integral_collstats_double(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 5_000_000}}
    )
    await s.collection.insert_one({"_id": "one", "email": "work@example.test"})
    assert await s.collection.create_index("email", name="email_v1") == "email_v1"
    assert "email_v1" in await s.collection.index_information()


async def test_real_native_text_index_metadata_noops_without_losing_weight_validation(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    await s.accounts.update_one(
        {"_id": s.account_id}, {"$set": {"limits.max_mongodb_storage_bytes": 5_000_000}}
    )
    keys = [("event_type", "text"), ("details", "text")]
    name = await s.collection.create_index(keys, name="audit_text_v1", weights={"event_type": 2})
    before = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"][
        "committed_growth_total"
    ]
    assert await s.collection.create_index(keys, name=name, weights={"event_type": 2}) == name
    after = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"][
        "committed_growth_total"
    ]
    assert before == after
    with pytest.raises(MongoStorageCapacityError):
        await s.collection.create_index(keys, name=name, weights={"event_type": 3})


@pytest.mark.parametrize(
    "modifier",
    [
        {"$set": {"items.$[].payload": "x" * 1000}},
        {"$set": {"items.$[selected].payload": "x" * 1000}},
        {"$push": {"items.$[].children": {"$each": ["new"]}}},
        {"$rename": {"large_unindexed_array": "indexed_array"}},
        {"$set": {"items.1000000": "new"}},
        {"$unclassifiedFutureModifier": {"value": 1}},
    ],
)
async def test_data_amplifying_modifiers_are_rejected_before_any_reservation(
    modifier: dict[str, Any],
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.update_one({"_id": "one"}, modifier)
    assert error.value.detail["code"] == "mongodb_storage_operation_unsupported"
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["pending_growth_bytes"] == 0 and state["committed_growth_total"] == 0
    assert await s.collection.count_documents({}) == 0


@pytest.mark.parametrize("kind", ["update", "find_and_update", "bulk_update", "replace"])
async def test_upsert_equality_filter_bytes_are_included_before_insert(
    kind: str,
    live_storage: SimpleNamespace,
) -> None:
    from pymongo import ReplaceOne, UpdateOne

    s = live_storage
    predicate = {"_id": "x" * 20_000}
    update = {"$set": {"state": "new"}}
    with pytest.raises(MongoStorageCapacityError) as error:
        if kind == "update":
            await s.collection.update_one(predicate, update, upsert=True)
        elif kind == "find_and_update":
            await s.collection.find_one_and_update(predicate, update, upsert=True)
        elif kind == "bulk_update":
            await s.collection.bulk_write([UpdateOne(predicate, update, upsert=True)])
        else:
            await s.collection.bulk_write([ReplaceOne(predicate, {"state": "new"}, upsert=True)])
    assert error.value.status_code == 429
    state = (await s.accounts.find_one({"_id": s.account_id}))["mongodb_storage"]
    assert state["pending_growth_bytes"] == 0 and state["committed_growth_total"] == 0
    assert await s.collection.count_documents({}) == 0


async def test_search_index_retirement_uses_account_write_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = SimpleNamespace(
        name="traces",
        full_name="AC-SEARCH.traces",
        drop_search_index=AsyncMock(),
        write_concern=SimpleNamespace(acknowledged=True),
    )
    delegate_db = SimpleNamespace(name="AC-SEARCH", get_collection=lambda _: delegate)
    collection = guard_platform_mongo_client(SimpleNamespace(get_database=lambda _: delegate_db))[
        "AC-SEARCH"
    ]["traces"]
    admission = AsyncMock(return_value=None)
    monkeypatch.setattr(GuardedMongoDatabase, "_write", admission)
    await collection.drop_search_index("default", comment="release-owned")
    admission.assert_awaited_once_with(
        delegate.drop_search_index, ("default",), {"comment": "release-owned"}, 0
    )
    delegate.drop_search_index.assert_not_awaited()


@pytest.mark.parametrize(
    "name,timeout",
    [
        ("", 10),
        (None, 10),
        ("default", True),
        ("default", 0),
        ("default", 61),
        ("default", float("inf")),
        ("default", float("nan")),
    ],
)
async def test_search_index_retirement_rejects_invalid_budget_and_name(
    name: Any, timeout: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = SimpleNamespace(
        name="traces",
        full_name="AC-SEARCH.traces",
        drop_search_index=AsyncMock(),
        write_concern=SimpleNamespace(acknowledged=True),
    )
    delegate_db = SimpleNamespace(name="AC-SEARCH", get_collection=lambda _: delegate)
    collection = guard_platform_mongo_client(SimpleNamespace(get_database=lambda _: delegate_db))[
        "AC-SEARCH"
    ]["traces"]
    admission = AsyncMock()
    monkeypatch.setattr(GuardedMongoDatabase, "_write", admission)
    with pytest.raises(MongoStorageCapacityError):
        await collection.drop_search_index(name, timeout_seconds=timeout)
    admission.assert_not_awaited()


async def test_search_index_retirement_has_finite_driver_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = SimpleNamespace(
        name="traces",
        full_name="AC-SEARCH.traces",
        drop_search_index=AsyncMock(),
        write_concern=SimpleNamespace(acknowledged=True),
    )
    delegate_db = SimpleNamespace(name="AC-SEARCH", get_collection=lambda _: delegate)
    collection = guard_platform_mongo_client(SimpleNamespace(get_database=lambda _: delegate_db))[
        "AC-SEARCH"
    ]["traces"]
    cancelled = asyncio.Event()

    async def blocked(*_args: Any, **_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(GuardedMongoDatabase, "_write", blocked)
    with pytest.raises(TimeoutError):
        await collection.drop_search_index("default", timeout_seconds=0.01)
    assert cancelled.is_set()


async def test_real_search_index_retirement_cannot_bypass_at_capacity(
    live_storage: SimpleNamespace,
) -> None:
    s = live_storage
    now = datetime.utcnow()
    await s.accounts.update_one(
        {"_id": s.account_id},
        {"$set": {"mongodb_storage": observation(now, data=100_000)}},
    )
    with pytest.raises(MongoStorageCapacityError) as error:
        await s.collection.drop_search_index("default")
    assert error.value.detail["code"] == "mongodb_storage_limit_exceeded"
