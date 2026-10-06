"""Native command boundaries prevent unfinished or partial index readiness."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from bson.int64 import Int64
from pymongo import IndexModel
from pymongo.errors import OperationFailure
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from maeyr_platform.mongo_storage import GuardedMongoCollection, MongoStorageCapacityError
from maeyr_platform.mongodb_indexes import mongodb_index_information


def response(entries: list[Any], *, identity: Any = 0, first: bool = True) -> dict[str, Any]:
    return {
        "cursor": {
            "id": identity,
            "ns": "AC-index-test.people",
            "firstBatch" if first else "nextBatch": entries,
        },
        "ok": 1,
    }


def collection(*responses: Any) -> SimpleNamespace:
    database = SimpleNamespace(name="AC-index-test", command=AsyncMock(side_effect=responses))
    database.with_options = Mock(return_value=database)
    return SimpleNamespace(
        name="people",
        full_name="AC-index-test.people",
        database=database,
        write_concern=WriteConcern(),
        create_indexes=AsyncMock(),
    )


INDEX = {"v": 2, "key": {"org_id": 1, "email": 1}, "name": "email_v1", "unique": True}


async def test_completed_native_metadata_preserves_semantics_and_reads_primary() -> None:
    coll = collection(response([INDEX]))
    result = await mongodb_index_information(coll)
    assert result == {"email_v1": {"v": 2, "key": [("org_id", 1), ("email", 1)], "unique": True}}
    coll.database.with_options.assert_called_once_with(read_preference=ReadPreference.PRIMARY)
    coll.database.command.assert_awaited_once_with(
        {
            "listIndexes": "people",
            "includeBuildUUIDs": True,
            "cursor": {"batchSize": 64},
            "maxTimeMS": 5000,
        }
    )


async def test_native_unfinished_wrapper_keeps_build_identity_not_usable_spec_only() -> None:
    coll = collection(response([{"spec": INDEX, "buildUUID": b"native-build"}]))
    result = await mongodb_index_information(coll)
    assert result["email_v1"]["buildUUID"] == b"native-build"
    assert result["email_v1"]["key"] == [("org_id", 1), ("email", 1)]


async def test_two_bounded_cursor_batches_are_complete_before_return() -> None:
    coll = collection(response([], identity=Int64(17)), response([INDEX], first=False))
    assert "email_v1" in await mongodb_index_information(coll)
    assert coll.database.command.await_args_list[1].args == (
        {
            "getMore": 17,
            "collection": "people",
            "batchSize": 64,
        },
    )
    assert coll.database.command.await_count == 2


@pytest.mark.parametrize("code", [18, 50, 59])
async def test_provider_failure_is_not_misreported_as_empty_metadata(code: int) -> None:
    failure = OperationFailure("private diagnostic", code)
    coll = collection(failure)
    with pytest.raises(OperationFailure) as raised:
        await mongodb_index_information(coll)
    assert raised.value is failure


async def test_only_native_namespace_not_found_is_empty() -> None:
    assert await mongodb_index_information(collection(OperationFailure("missing", 26))) == {}


@pytest.mark.parametrize(
    "native_response",
    [
        {},
        {"cursor": {}},
        response([], identity=True),
        response([], identity=-1),
        {"cursor": {"id": 0, "ns": "AC-other.people", "firstBatch": [INDEX]}},
        response([INDEX, INDEX]),
        response([{"spec": INDEX}]),
        response([{"spec": {}, "buildUUID": b"native-build"}]),
        response([{"spec": INDEX, "indexBuildInfo": {"phase": "building"}}]),
        response([{**INDEX, "indexBuildInfo": {"phase": "building"}}]),
        response([{"name": "email_v1", "key": []}]),
        response([{**INDEX, "name": f"index_{i}"} for i in range(65)]),
    ],
)
async def test_malformed_or_over_bound_metadata_cannot_be_ready(native_response: Any) -> None:
    with pytest.raises(ValueError):
        await mongodb_index_information(collection(native_response))


async def test_unfinished_cursor_is_closed_and_never_returned_as_partial_readiness() -> None:
    coll = collection(response([], identity=17), response([], identity=17, first=False), {})
    with pytest.raises(ValueError, match="bounded batch limit"):
        await mongodb_index_information(coll)
    assert coll.database.command.await_args_list[-1].args == (
        {"killCursors": "people", "cursors": [17]},
    )


async def test_metadata_total_timeout_does_not_hang_readiness() -> None:
    coll = collection()

    async def delayed(_: Any) -> None:
        await asyncio.sleep(1)

    coll.database.command.side_effect = delayed
    with pytest.raises(TimeoutError):
        await mongodb_index_information(coll, max_time_ms=1)


@pytest.mark.parametrize("building", [False, True])
async def test_guard_noop_requires_completed_definition_before_admission(building: bool) -> None:
    native = {"spec": INDEX, "buildUUID": b"native-build"} if building else INDEX
    raw = collection(response([native]))
    database = SimpleNamespace(name="AC-index-test", _write=AsyncMock())
    guarded = GuardedMongoCollection(database, raw)
    if building:
        with pytest.raises(MongoStorageCapacityError) as raised:
            await guarded.create_indexes(
                [IndexModel([("org_id", 1), ("email", 1)], name="email_v1", unique=True)]
            )
        assert raised.value.detail["code"] == "mongodb_storage_observation_unavailable"
    else:
        assert await guarded.create_indexes(
            [
                IndexModel([("org_id", 1), ("email", 1)], name="email_v1", unique=True),
            ]
        ) == ["email_v1"]
    database._write.assert_not_awaited()
    raw.create_indexes.assert_not_awaited()
