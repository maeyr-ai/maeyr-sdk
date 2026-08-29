from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from maeyr.client import MaeyrClient
from maeyr.models import WorkerKeyCreateRequest, WorkerKeyRateLimit


def _created_worker_key() -> dict:
    return {
        "key_id": "release_worker",
        "name": "Release Worker",
        "worker_key": "wk_created_once",
        "created_at": "2026-08-05T00:00:00+00:00",
        "expires_at": None,
        "key_type": "worker",
        "account_id": "account",
        "org_id": "org",
        "project_id": "project",
        "status": "active",
        "scopes": ["read", "write"],
    }


def test_worker_key_request_is_typed_and_worker_only() -> None:
    request = WorkerKeyCreateRequest(name="  Release Worker  ")
    assert request.name == "Release Worker"
    assert request.key_type == "worker"
    assert request.scopes == ["read", "write"]

    with pytest.raises(ValidationError):
        WorkerKeyCreateRequest(name="Release Worker", key_type="api")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        WorkerKeyCreateRequest(name="Release Worker", expires_in_days=0)
    with pytest.raises(ValidationError):
        WorkerKeyCreateRequest(name="   ")


@pytest.mark.asyncio
async def test_create_worker_key_preserves_legacy_call_and_sends_current_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_created_worker_key())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = MaeyrClient("token", base_url="https://api.test")
        client._transport._async_client = http
        response = await client.auth.create_worker_key(
            " Release Worker ",
            "legacy description is intentionally not transmitted",
            project_id="project",
            scopes=["read", "write"],
            expires_in_days=30,
        )

    assert response["worker_key"] == "wk_created_once"
    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/auth/key/worker"
    assert json.loads(captured[0].read()) == {
        "name": "Release Worker",
        "key_type": "worker",
        "expires_in_days": 30,
        "project_id": "project",
        "scopes": ["read", "write"],
    }


@pytest.mark.asyncio
async def test_create_worker_key_accepts_typed_request_without_field_mixing() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_created_worker_key())

    request = WorkerKeyCreateRequest(
        key_id="release_worker",
        name="Release Worker",
        project_id="project",
        scopes=["admin"],
        expires_in_days=7,
        rate_limit=WorkerKeyRateLimit(rpm=120, burst=10),
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = MaeyrClient("token", base_url="https://api.test")
        client._transport._async_client = http
        await client.auth.create_worker_key(request)
        with pytest.raises(ValueError, match="cannot be combined"):
            await client.auth.create_worker_key(request, project_id="different")

    assert json.loads(captured[0].read()) == {
        "key_id": "release_worker",
        "name": "Release Worker",
        "key_type": "worker",
        "expires_in_days": 7,
        "project_id": "project",
        "scopes": ["admin"],
        "rate_limit": {"rpm": 120, "burst": 10},
    }


@pytest.mark.asyncio
async def test_list_revoke_and_delete_worker_keys_match_auth_routes() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"items": [], "total": 0, "skip": 5, "limit": 25},
            )
        if request.method == "POST":
            return httpx.Response(200, json={"revoked": True})
        return httpx.Response(200, json={"deleted": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = MaeyrClient("token", base_url="https://api.test")
        client._transport._async_client = http
        listed = await client.auth.list_worker_keys(
            project_id="project",
            include_revoked=True,
            skip=5,
            limit=25,
            search="release",
        )
        revoked = await client.auth.revoke_worker_key("key/with space", project_id="project")
        deleted = await client.auth.delete_worker_key("key/with space", project_id="project")

    assert listed["total"] == 0
    assert revoked == {"revoked": True}
    assert deleted == {"deleted": True}
    assert captured[0].url.path == "/auth/key/worker"
    assert dict(captured[0].url.params) == {
        "include_revoked": "true",
        "skip": "5",
        "limit": "25",
        "project_id": "project",
        "search": "release",
    }
    assert captured[1].url.raw_path == (
        b"/auth/key/worker/key%2Fwith%20space/revoke?project_id=project"
    )
    assert dict(captured[1].url.params) == {"project_id": "project"}
    assert captured[2].url.raw_path == b"/auth/key/worker/key%2Fwith%20space?project_id=project"
    assert dict(captured[2].url.params) == {"project_id": "project"}


@pytest.mark.asyncio
async def test_worker_key_list_and_identifiers_fail_before_transport() -> None:
    client = MaeyrClient("token", base_url="https://api.test")
    with pytest.raises(ValueError, match="skip"):
        await client.auth.list_worker_keys(skip=-1)
    with pytest.raises(ValueError, match="limit"):
        await client.auth.list_worker_keys(limit=501)
    with pytest.raises(ValueError, match="key_id"):
        await client.auth.revoke_worker_key("   ")
    await client.aclose()
