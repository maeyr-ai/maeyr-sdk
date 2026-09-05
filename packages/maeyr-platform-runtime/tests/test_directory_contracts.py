from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from maeyr_platform.directory import invoker_cache, prompt_cache
from maeyr_platform.directory.access_policy import VoltAccessPolicy
from maeyr_platform.directory.project_user_csv import (
    format_project_users_csv,
    parse_project_users_csv,
)
from maeyr_platform.directory.slack_access_grant import (
    coerce_expires_at,
    grant_is_active,
    grant_is_expired,
)
from maeyr_platform.directory.tenant_database import (
    database_for_account,
    document_scope,
    extract_tenant_scope,
    project_filter,
)


def test_project_user_csv_round_trip_preserves_flat_contract() -> None:
    rendered = format_project_users_csv(
        [
            {
                "customer_user_id": "customer-1",
                "profile": {"email": "user@example.test"},
                "enabled": False,
            }
        ],
        fieldnames=["customer_user_id", "email", "enabled"],
    )

    rows, errors = parse_project_users_csv(rendered)

    assert errors == []
    assert rows == [
        {
            "customer_user_id": "customer-1",
            "email": "user@example.test",
            "enabled": "False",
        }
    ]


def test_project_user_csv_rejects_empty_input_and_header_only_input() -> None:
    assert parse_project_users_csv("") == ([], ["empty CSV"])
    assert parse_project_users_csv("customer_user_id,email") == (
        [],
        ["no data rows found"],
    )


def test_slack_grant_policy_normalizes_utc_and_expiry_boundary() -> None:
    now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    expires_at = now + timedelta(seconds=1)
    grant = {"enabled": True, "expires_at": expires_at.isoformat()}

    assert coerce_expires_at("2026-08-04T12:00:01Z") == expires_at
    assert grant_is_active(grant, now)
    assert not grant_is_expired(grant, now)
    assert not grant_is_active(grant, expires_at)
    assert grant_is_expired(grant, expires_at)


def test_access_policy_defaults_are_not_shared_between_instances() -> None:
    first = VoltAccessPolicy(
        account_id="account-1",
        org_id="org-1",
        project_id="project-1",
        name="default",
    )
    second = VoltAccessPolicy(
        account_id="account-1",
        org_id="org-1",
        project_id="project-1",
        name="other",
    )

    first.principals.users.append("user-1")

    assert second.principals.users == []
    assert first.effect == "allow"


def test_tenant_database_policy_validates_and_fences_project_scope() -> None:
    scope = extract_tenant_scope(
        {
            "account_id": " account-1 ",
            "org_id": " org-1 ",
            "project_id": " project-1 ",
        }
    )

    assert database_for_account(scope["account_id"]) == "account-1"
    assert project_filter(scope) == {"org_id": "org-1", "project_id": "project-1"}
    assert document_scope(scope) == scope
    with pytest.raises(ValueError, match="reserved Mongo database"):
        database_for_account("admin")


class _ScanningRedis:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, ...]] = []
        self.patterns: list[str] = []

    async def delete(self, *keys: str) -> int:
        self.deleted.append(tuple(keys))
        return len(keys)

    async def scan(
        self,
        *,
        cursor: int,
        match: str,
        count: int,
    ) -> tuple[int, list[str]]:
        self.patterns.append(match)
        return 0, [f"matched:{match}"]


@pytest.mark.asyncio
async def test_wildcard_identity_invalidation_scans_the_whole_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _ScanningRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(invoker_cache, "_client", get_redis)

    await invoker_cache.invalidate_invoker_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        channel="slack",
        external_user_id="*",
    )

    assert redis.patterns == [
        "volt:invoker:AC-1:OR-1:PR-1:slack:*",
        "volt:project_user_identity:AC-1:OR-1:PR-1:slack:*",
    ]
    assert all(
        literal not in call
        for call in redis.deleted
        for literal in (
            "volt:invoker:AC-1:OR-1:PR-1:slack:*",
            "volt:project_user_identity:AC-1:OR-1:PR-1:slack:*",
        )
    )


class _GenerationRedis:
    def __init__(self) -> None:
        self.generation = 0
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        if key.startswith("volt:cache_generation:"):
            return None if self.generation == 0 else str(self.generation)
        return self.values.get(key)

    async def eval(self, script: str, key_count: int, *args: str) -> int:
        assert key_count == 2
        if script == invoker_cache._VERSIONED_CACHE_READ:
            _generation_key, cache_key = args
            return [str(self.generation), self.values.get(cache_key)]  # type: ignore[return-value]
        assert script == invoker_cache._VERSIONED_CACHE_WRITE
        generation_key, cache_key, expected, _ttl, value = args
        assert generation_key.startswith("volt:cache_generation:")
        if expected and int(expected) != self.generation:
            return 0
        self.values[cache_key] = json.dumps(
            {
                "__maeyr_cache_generation": self.generation,
                "payload": json.loads(value),
            }
        )
        return 1


@pytest.mark.asyncio
async def test_generation_fence_rejects_a_delayed_no_ttl_source_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _GenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(invoker_cache, "_client", get_redis)
    generation_before_mongo_read = await invoker_cache.get_cache_generation(
        "AC-1",
        "OR-1",
        "PR-1",
    )
    assert generation_before_mongo_read == 0

    # Directory commits and Volt advances the generation while the old read is in flight.
    redis.generation = 1
    await invoker_cache.set_directory_source_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        {"status": "stale"},
        expected_generation=generation_before_mongo_read,
    )

    assert "volt:directory_source:AC-1:OR-1:PR-1" not in redis.values


@pytest.mark.asyncio
async def test_getter_rejects_old_generation_and_legacy_payload_during_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _GenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(invoker_cache, "_client", get_redis)
    await invoker_cache.set_directory_source_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        {"status": "old"},
    )
    redis.generation = 1

    assert await invoker_cache.get_directory_source_cache("AC-1", "OR-1", "PR-1") is None

    redis.values["volt:directory_source:AC-1:OR-1:PR-1"] = json.dumps(
        {"status": "legacy-unversioned"}
    )
    assert await invoker_cache.get_directory_source_cache("AC-1", "OR-1", "PR-1") is None


class _UserCacheGenerationRedis:
    def __init__(self) -> None:
        self.generation = 0
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        if key.startswith("volt:cache_generation:"):
            return None if self.generation == 0 else str(self.generation)
        return self.values.get(key)

    async def eval(self, script: str, key_count: int, *args: str) -> Any:
        assert key_count == 2
        if script == prompt_cache._VERSIONED_USER_CACHE_READ:
            _generation_key, cache_key = args
            return [str(self.generation), self.values.get(cache_key)]
        assert script == prompt_cache._VERSIONED_USER_CACHE_WRITE
        generation_key, cache_key, expected, _ttl, value = args
        assert generation_key.startswith("volt:cache_generation:")
        if expected and int(expected) != self.generation:
            return 0
        self.values[cache_key] = json.dumps(
            {
                "__maeyr_cache_generation": self.generation,
                "payload": json.loads(value),
            }
        )
        return 1


@pytest.mark.asyncio
async def test_user_cache_generation_rejects_delayed_authorization_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _UserCacheGenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(prompt_cache, "_client", get_redis)
    generation_before_authorization_read = await prompt_cache.get_cache_generation(
        "AC-1",
        "OR-1",
        "PR-1",
    )
    assert generation_before_authorization_read == 0

    # A policy mutation completes its generation advance and replica ACK while
    # the old catalog lookup remains in flight.
    redis.generation = 1
    await prompt_cache.set_user_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        "person@example.test",
        {"accessible_agents": ["stale-agent"]},
        expected_generation=generation_before_authorization_read,
    )

    assert "volt:user_cache:AC-1:OR-1:PR-1:person@example.test" not in redis.values


@pytest.mark.asyncio
async def test_user_cache_getter_rejects_old_and_legacy_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _UserCacheGenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(prompt_cache, "_client", get_redis)
    await prompt_cache.set_user_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        "person@example.test",
        {"accessible_agents": ["old-agent"]},
        expected_generation=0,
    )
    redis.generation = 1

    assert (
        await prompt_cache.get_user_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            "person@example.test",
        )
        is None
    )

    redis.values["volt:user_cache:AC-1:OR-1:PR-1:person@example.test"] = json.dumps(
        {"accessible_agents": ["legacy-agent"]}
    )
    assert (
        await prompt_cache.get_user_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            "person@example.test",
        )
        is None
    )


class _SharedProjectCacheGenerationRedis:
    def __init__(self) -> None:
        self.generation = 0
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        if key.startswith("volt:cache_generation:"):
            return None if self.generation == 0 else str(self.generation)
        return self.values.get(key)

    async def eval(self, script: str, key_count: int, *args: str) -> Any:
        keys = args[:key_count]
        argv = args[key_count:]
        assert keys[0].startswith("volt:cache_generation:")
        if script == prompt_cache._VERSIONED_SHARED_CACHE_READ_MANY:
            return [str(self.generation), *(self.values.get(key) for key in keys[1:])]
        assert script == prompt_cache._VERSIONED_SHARED_CACHE_WRITE_MANY
        expected, _ttl, *payloads = argv
        assert len(payloads) == len(keys) - 1
        if int(expected) != self.generation:
            return 0
        for key, payload in zip(keys[1:], payloads):
            self.values[key] = json.dumps(
                {
                    "__maeyr_cache_generation": self.generation,
                    "payload": json.loads(payload),
                }
            )
        return len(payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_kind", ["agents", "mappings"])
async def test_project_cache_generation_rejects_delayed_builder_writers(
    monkeypatch: pytest.MonkeyPatch,
    cache_kind: str,
) -> None:
    redis = _SharedProjectCacheGenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(prompt_cache, "_client", get_redis)
    generation_before_builder_read = await prompt_cache.get_cache_generation(
        "AC-1",
        "OR-1",
        "PR-1",
    )
    assert generation_before_builder_read == 0

    redis.generation = 1
    if cache_kind == "agents":
        await prompt_cache.set_project_agents_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            "summary:enabled:deployed",
            {"agents": [{"agent_id": "stale-agent"}]},
            expected_generation=generation_before_builder_read,
        )
    else:
        await prompt_cache.set_project_mappings_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            [{"mapping_id": "stale-mapping", "mapping": {"old": True}}],
            expected_generation=generation_before_builder_read,
        )

    assert redis.values == {}


@pytest.mark.asyncio
async def test_project_cache_getters_reject_old_and_legacy_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _SharedProjectCacheGenerationRedis()

    async def get_redis() -> Any:
        return redis

    monkeypatch.setattr(prompt_cache, "_client", get_redis)
    await prompt_cache.set_project_agents_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        "summary:enabled:deployed",
        {"agents": [{"agent_id": "old-agent"}]},
        expected_generation=0,
    )
    await prompt_cache.set_project_mappings_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        [{"mapping_id": "old-mapping", "mapping": {"old": True}}],
        expected_generation=0,
    )
    redis.generation = 1

    assert (
        await prompt_cache.get_project_agents_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            "summary:enabled:deployed",
        )
        is None
    )
    assert await prompt_cache.get_project_mappings_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        ["old-mapping"],
    ) == ([], ["old-mapping"])

    redis.values["volt:project_cache:AC-1:OR-1:PR-1:agents:summary:enabled:deployed"] = json.dumps(
        {"agents": [{"agent_id": "legacy-agent"}]}
    )
    redis.values["volt:mapping:AC-1:OR-1:PR-1:legacy-mapping"] = json.dumps(
        {"mapping_id": "legacy-mapping"}
    )
    assert (
        await prompt_cache.get_project_agents_cache(
            "AC-1",
            "OR-1",
            "PR-1",
            "summary:enabled:deployed",
        )
        is None
    )
    assert await prompt_cache.get_project_mappings_cache(
        "AC-1",
        "OR-1",
        "PR-1",
        ["legacy-mapping"],
    ) == ([], ["legacy-mapping"])
