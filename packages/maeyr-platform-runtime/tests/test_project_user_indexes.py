"""Profile policy tests; native execution coverage lives with the owning service."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maeyr_platform.directory import project_user_indexes as module
from maeyr_platform.directory.project_user_indexes import (
    IDENTITY_INDEX,
    SCOPE_KEYS,
    ProjectUserIndexesUnavailable,
    ProjectUserIndexPreparationRequired,
    assess_profile_index_contract,
    collect_unique_profile_fields,
    prepare_project_user_indexes,
    profile_index_manifest,
)

SCHEMA = {"fields": [{"name": "staff", "unique": True}]}


def metadata(schemas=None):
    return {
        index.name: {"key": index.keys, **index.options()}
        for index in profile_index_manifest(schemas or [SCHEMA])
    }


def test_all_advisory_unique_columns_are_native_constraints():
    schema = {
        "primary_key": "key",
        "fields": [{"name": "declared", "unique": True}],
        "connector_bindings": [{"profile_field": "bound"}],
        "match_rules": [
            {"channel": "slack", "field": "matched"},
            {"channel": "*", "field": "lookup"},
        ],
        "agent_connectors": [{"primary_key_field": "connector"}],
    }
    assert collect_unique_profile_fields(schema) == {"key", "declared", "bound", "matched"}
    manifest = profile_index_manifest([schema])
    assert {index.name for index in manifest if index.unique} == {
        IDENTITY_INDEX,
        "unique_profile_key_v2",
        "unique_profile_declared_v2",
        "unique_profile_bound_v2",
        "unique_profile_matched_v2",
    }
    assert any(index.name == "profile_lookup_lookup_v2" for index in manifest)
    assert any(index.name == "profile_lookup_connector_v2" for index in manifest)


@pytest.mark.parametrize("field", ["", "a.b", "$field", "x" * 65, None, 7])
def test_unsafe_field_identities_are_rejected(field):
    with pytest.raises(ValueError):
        profile_index_manifest([{"fields": [{"name": field}]}])


@pytest.mark.parametrize(
    "schema",
    [
        {"fields": "not-list"},
        {"fields": [None]},
        {"fields": [{"name": "a", "unique": "false"}]},
        {"fields": [{"name": "a", "required": 1}]},
        {"fields": [{"name": "a", "type": "unknown"}]},
        {"fields": [{"name": f"f{number}", "unique": True} for number in range(9)]},
    ],
)
def test_malformed_or_excessive_schema_is_not_reinterpreted(schema):
    with pytest.raises(ValueError):
        profile_index_manifest([schema])


@pytest.mark.parametrize(
    "alteration",
    [
        {"unique": False},
        {"sparse": True},
        {"hidden": True},
        {"expireAfterSeconds": 1},
        {"buildUUID": "unfinished"},
        {"partialFilterExpression": {"profile.staff": {"$exists": True}}},
        {"collation": {"locale": "en"}},
        {"key": [("org_id", 1), ("project_id", 1), ("profile.staff", 1)]},
    ],
)
async def test_conflicting_or_uncommitted_indexes_never_pass_or_mutate(monkeypatch, alteration):
    indexes = metadata()
    indexes["unique_profile_staff_v2"].update(alteration)
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=indexes))
    coll = SimpleNamespace(create_index=AsyncMock(), drop_index=AsyncMock(), find_one=AsyncMock())
    with pytest.raises((ProjectUserIndexPreparationRequired, ProjectUserIndexesUnavailable)):
        await prepare_project_user_indexes(
            coll, [SCHEMA], allow_populated=True, retire_obsolete=True
        )
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()


async def test_fifty_indexes_never_switch_uniqueness_to_advisory_only(monkeypatch):
    indexes = {f"customer_{number}": {"key": [(f"other{number}", 1)]} for number in range(50)}
    indexes["_id_"] = {"key": [("_id", 1)]}
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=indexes))

    async def create(keys, **options):
        indexes[options["name"]] = {"key": keys, **options}

    coll = SimpleNamespace(
        create_index=AsyncMock(side_effect=create),
        drop_index=AsyncMock(),
        find_one=AsyncMock(return_value=None),
    )
    await prepare_project_user_indexes(coll, [SCHEMA])
    assert "unique_profile_staff_v2" in indexes
    assert indexes["unique_profile_staff_v2"]["unique"] is True


async def test_native_index_limit_refuses_without_any_creation(monkeypatch):
    indexes = {f"customer_{number}": {"key": [(f"other{number}", 1)]} for number in range(60)}
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=indexes))
    coll = SimpleNamespace(
        create_index=AsyncMock(), drop_index=AsyncMock(), find_one=AsyncMock(return_value=None)
    )
    with pytest.raises(ProjectUserIndexPreparationRequired):
        await prepare_project_user_indexes(coll, [SCHEMA], allow_populated=True)
    coll.create_index.assert_not_awaited()


async def test_missing_constraint_check_only_never_bootstraps(monkeypatch):
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value={}))
    coll = SimpleNamespace(create_index=AsyncMock(), find_one=AsyncMock(return_value=None))
    with pytest.raises(ProjectUserIndexPreparationRequired):
        await prepare_project_user_indexes(coll, [SCHEMA], check_only=True)
    coll.create_index.assert_not_awaited()
    coll.find_one.assert_not_awaited()


async def test_old_field_named_v2_is_not_mistaken_for_a_current_constraint(monkeypatch):
    schema = {"fields": [{"name": "staff_v2", "unique": True}]}
    indexes = metadata([schema])
    indexes["unique_profile_staff_v2"] = {
        "key": SCOPE_KEYS + [("profile.staff_v2", 1)],
        "unique": True,
        "sparse": True,
    }
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value=indexes))
    coll = SimpleNamespace(
        create_index=AsyncMock(),
        drop_index=AsyncMock(side_effect=lambda name, **kwargs: indexes.pop(name)),
    )
    await prepare_project_user_indexes(coll, [schema], retire_obsolete=True)
    assert "unique_profile_staff_v2" not in indexes
    assert "unique_profile_staff_v2_v2" in indexes


async def test_lease_fence_refusal_prevents_every_ddl_mutation(monkeypatch):
    monkeypatch.setattr(module, "mongodb_index_information", AsyncMock(return_value={}))
    coll = SimpleNamespace(
        create_index=AsyncMock(), drop_index=AsyncMock(), find_one=AsyncMock(return_value=None)
    )
    fence = AsyncMock(side_effect=ProjectUserIndexesUnavailable())
    with pytest.raises(ProjectUserIndexesUnavailable):
        await prepare_project_user_indexes(
            coll, [SCHEMA], allow_populated=True, before_mutation=fence
        )
    coll.create_index.assert_not_awaited()
    coll.drop_index.assert_not_awaited()


def test_unknown_old_specification_is_never_a_retirement_permission():
    indexes = metadata()
    indexes["unique_profile_staff"] = {
        "key": SCOPE_KEYS + [("profile.staff", 1)],
        "unique": False,
        "sparse": True,
    }
    before = deepcopy(indexes)
    with pytest.raises(ProjectUserIndexPreparationRequired):
        assess_profile_index_contract(indexes, [SCHEMA])
    assert indexes == before
