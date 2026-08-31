from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maeyr_platform.aiohttp_lifecycle import close_session
from maeyr_platform.auth.sso_access import (
    extract_org_ids,
    extract_project_ids,
    get_admin_org_ids,
    has_org_permission,
    is_org_admin_for,
)
from maeyr_platform.mongo import (
    cached_mongo_database,
    connection_usage_log,
    log_mongo_connection_status,
    mongo_collection,
    optimize_projection,
    require_mongo_client,
    warm_mongo_connection_pool,
)
from maeyr_platform.redis.config import (
    build_redis_connection_string,
    redis_connection_kwargs,
    redis_ssl_enabled,
    redis_tls_connection_kwargs,
)
from maeyr_platform.resource_units import (
    parse_cpu_to_millicores,
    parse_memory_to_mb,
)


class _Session:
    def __init__(self) -> None:
        self._closed = False
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True


def test_resource_and_access_projections_are_canonical() -> None:
    assert parse_cpu_to_millicores("500m") == 500
    assert parse_cpu_to_millicores("1.5") == 1500
    assert parse_memory_to_mb("1Gi") == 1024
    access: dict[str, Any] = {
        "orgs": [
            {
                "org_id": "org-1",
                "org_role": {"permissions": [{"module": "access_control", "actions": ["manage"]}]},
                "projects": [{"project_id": "project-1"}],
            }
        ]
    }
    assert extract_project_ids(access) == ["project-1"]
    assert get_admin_org_ids(access) == ["org-1"]
    assert has_org_permission(access, "org-1", "access_control", "manage")
    assert is_org_admin_for(access, "org-1")


def test_sso_access_projections_fail_closed_for_legacy_and_malformed_grants() -> None:
    legacy: dict[str, Any] = {
        "orgs": [
            {
                "org_id": "org-1",
                "org_role": {
                    "permissions": [{"module": "organization", "actions": ["admin", "all"]}]
                },
            }
        ]
    }
    assert not is_org_admin_for(legacy, "org-1")
    assert get_admin_org_ids(legacy) == []

    malformed: dict[str, Any] = {
        "orgs": [
            None,
            "org-2",
            {"org_id": True, "projects": [None, {"project_id": False}]},
            {
                "org_id": "org-3",
                "org_role": {
                    "permissions": {
                        "module": "access_control",
                        "actions": ["manage"],
                    }
                },
            },
        ]
    }
    assert extract_org_ids(malformed) == ["org-3"]
    assert extract_project_ids(malformed) == []
    assert not is_org_admin_for(malformed, "org-3")


def test_redis_policy_covers_urls_tls_and_ca_validation(tmp_path: Path) -> None:
    assert redis_ssl_enabled("rediss://cache:6379", None)
    assert not redis_ssl_enabled("redis://cache:6379", "false")
    assert (
        build_redis_connection_string(
            "cache",
            port=6380,
            database=2,
            ssl_enabled=True,
        )
        == "rediss://cache:6380/2"
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")
    assert redis_tls_connection_kwargs(
        ssl_enabled=True,
        ca_cert_path=str(ca_file),
        environment="production",
    )["ssl_ca_certs"] == str(ca_file)
    assert redis_tls_connection_kwargs(
        ssl_enabled=True,
        ca_cert_path="",
        environment="production",
    ) == {"ssl_cert_reqs": "required", "ssl_check_hostname": True}
    assert redis_connection_kwargs(
        {
            "APP_ENVIRONMENT": "production",
            "REDIS_URL": "rediss://managed-cache.example:6379/0",
            "REDIS_USERNAME": "runtime",
            "REDIS_PASSWORD": "secret",
        }
    ) == {
        "decode_responses": True,
        "username": "runtime",
        "password": "secret",
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }


def test_mongo_policies_are_transport_independent() -> None:
    assert optimize_projection(["name"], None) == {"name": 1, "_id": 0}
    assert optimize_projection(None, ["secret"]) == {"secret": 0}
    assert connection_usage_log({"current_connections": 91, "available_connections": 9}) == (
        40,
        "CRITICAL: MongoDB connection usage: 91/100 (91.0%)",
    )


class _MongoAdmin:
    async def command(self, _name: str) -> dict[str, int]:
        return {"ok": 1}


class _MongoClient:
    admin = _MongoAdmin()

    def __getitem__(self, name: str) -> dict[str, str]:
        return {"database": name}


@pytest.mark.asyncio
async def test_mongo_runtime_helpers_reuse_owned_handles() -> None:
    client = _MongoClient()
    databases: dict[str, Any] = {}
    database = cached_mongo_database(client, databases, "trace")
    assert database is cached_mongo_database(client, databases, "trace")
    assert mongo_collection({"spans": "collection"}, "spans") == "collection"
    assert require_mongo_client(client) is client
    assert await warm_mongo_connection_pool(client, 3) == 3
    with pytest.raises(RuntimeError, match="not initialized"):
        require_mongo_client(None)


@pytest.mark.asyncio
async def test_mongo_connection_status_emits_only_a_meaningful_band() -> None:
    emissions: list[tuple[int, str]] = []

    async def get_stats() -> dict[str, int]:
        return {"current_connections": 85, "available_connections": 15}

    await log_mongo_connection_status(get_stats, lambda *entry: emissions.append(entry))
    assert emissions == [(30, "HIGH: MongoDB connection usage: 85/100 (85.0%)")]


@pytest.mark.asyncio
async def test_session_close_is_idempotent() -> None:
    session = _Session()
    await close_session(session)
    await close_session(session)
    assert session.close_calls == 1
