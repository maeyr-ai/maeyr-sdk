"""Channel constraints never lose native authority through request-path DDL."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import OperationFailure

from maeyr_platform.directory import channel_connection_indexes as module
from maeyr_platform.directory.project_user_indexes import (
    ProjectUserIndexesUnavailable,
    ProjectUserIndexPreparationRequired,
)


def metadata():
    index = module.CONNECTION_INDEX
    return {index.name: {"key": index.keys, **index.options()}}


@pytest.mark.parametrize(
    "alteration", [{"unique": False}, {"hidden": True}, {"buildUUID": "unfinished"}]
)
async def test_native_conflict_or_uncommitted_constraint_never_mutates(monkeypatch, alteration):
    observed = metadata()
    observed[module.CONNECTION_INDEX.name].update(alteration)
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=observed))
    collection = SimpleNamespace(create_index=AsyncMock(), drop_index=AsyncMock())
    with pytest.raises(ProjectUserIndexesUnavailable):
        await module.prepare_channel_connection_indexes(
            collection, allow_populated=True, retire_obsolete=True
        )
    collection.create_index.assert_not_awaited()
    collection.drop_index.assert_not_awaited()


async def test_populated_request_never_builds_missing_constraint(monkeypatch):
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value={}))
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value={"_id": 1}),
        create_index=AsyncMock(),
        drop_index=AsyncMock(),
    )
    with pytest.raises(ProjectUserIndexPreparationRequired):
        await module.prepare_channel_connection_indexes(collection)
    collection.create_index.assert_not_awaited()
    collection.drop_index.assert_not_awaited()


async def test_unknown_prior_constraint_is_not_dropped(monkeypatch):
    observed = metadata()
    observed[module.OLD_CONNECTION_INDEX] = {
        "key": [("org_id", 1), ("project_id", 1), ("channel", 1)],
        "unique": False,
    }
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=observed))
    collection = SimpleNamespace(create_index=AsyncMock(), drop_index=AsyncMock())
    with pytest.raises(ProjectUserIndexPreparationRequired):
        await module.prepare_channel_connection_indexes(collection, retire_obsolete=True)
    collection.drop_index.assert_not_awaited()


async def test_lease_refusal_prevents_bootstrap_ddl(monkeypatch):
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value={}))
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=None), create_index=AsyncMock(), drop_index=AsyncMock()
    )
    fence = AsyncMock(side_effect=ProjectUserIndexesUnavailable())
    with pytest.raises(ProjectUserIndexesUnavailable):
        await module.prepare_channel_connection_indexes(collection, before_mutation=fence)
    collection.create_index.assert_not_awaited()


async def test_driver_error_has_static_sanitized_response(monkeypatch):
    monkeypatch.setattr(
        module,
        "mongodb_index_information",
        AsyncMock(side_effect=OperationFailure("private driver diagnostic")),
    )
    with pytest.raises(ProjectUserIndexesUnavailable) as raised:
        await module.prepare_channel_connection_indexes(SimpleNamespace())
    assert raised.value.status_code == 503
    assert "private driver diagnostic" not in str(raised.value.detail)
