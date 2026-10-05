"""A failed validation cannot tear down another request's shared Auth pool."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest
from fastapi import HTTPException

from maeyr_platform.auth import fastapi_validator as validator


class Session:
    def __init__(self, **_kwargs: object) -> None:
        self.closed = False
        self.close_count = 0

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1


@pytest.fixture
async def pool(monkeypatch):
    await validator.close_auth_validator()
    sessions: list[Session] = []

    def make_session(**kwargs):
        session = Session(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(validator.aiohttp, "TCPConnector", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(validator.aiohttp, "ClientSession", make_session)
    monkeypatch.setattr(validator, "auth_settings", SimpleNamespace(
        AUTH_SERVICE_URL="http://auth.test", AUTH_INTERNAL_KEY="invented-test-key" * 3,
        AUTH_API_TIMEOUT=1, AUTH_API_RETRIES=2, AUTH_API_RETRY_BACKOFF=0,
        AUTH_API_POOL_SIZE=10, AUTH_API_POOL_SIZE_PER_HOST=5,
    ))
    monkeypatch.setattr(validator, "logger", MagicMock())
    yield sessions
    await validator.close_auth_validator()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientConnectionError(), aiohttp.ServerDisconnectedError()])
async def test_one_transient_failure_keeps_concurrent_validation_and_retry_on_same_pool(pool, monkeypatch, failure):
    healthy_started = asyncio.Event()
    retry_completed = asyncio.Event()
    failed_calls = []

    async def validate(**kwargs):
        credential = json.loads(kwargs["body"])["access_token"]
        if credential == "healthy-token":
            healthy_started.set()
            await retry_completed.wait()
            assert not kwargs["session"].closed
            return {"valid": True, "user_id": "healthy"}
        failed_calls.append(kwargs)
        if len(failed_calls) == 1:
            raise failure
        retry_completed.set()
        return {"valid": True, "user_id": "retried"}

    monkeypatch.setattr(validator, "_call_auth_validate", validate)
    healthy = asyncio.create_task(validator._validate_credential_with_retries("healthy-token"))
    await healthy_started.wait()
    retried = await validator._validate_credential_with_retries("retry-token")
    result = await healthy
    assert result["user_id"] == "healthy" and retried["user_id"] == "retried"
    assert len(pool) == 1
    assert pool[0].close_count == 0
    assert len(failed_calls) == 2
    assert failed_calls[0]["body"] == failed_calls[1]["body"]
    assert failed_calls[0]["session"] is failed_calls[1]["session"]
    assert failed_calls[0]["headers"]["x-internal-nonce"] != failed_calls[1]["headers"]["x-internal-nonce"]


@pytest.mark.asyncio
async def test_exhausted_request_does_not_destroy_pool_for_next_fresh_validation(pool, monkeypatch):
    calls = []

    async def validate(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 2:
            raise TimeoutError()
        return {"valid": True}

    monkeypatch.setattr(validator, "_call_auth_validate", validate)
    with pytest.raises(HTTPException) as error:
        await validator._validate_credential_with_retries("timed-out-token")
    assert error.value.status_code == 503
    assert await validator._validate_credential_with_retries("next-token") == {"valid": True}
    assert len(pool) == 1 and pool[0].close_count == 0
    assert json.loads(calls[-1]["body"])["access_token"] == "next-token"


@pytest.mark.asyncio
async def test_explicitly_closed_pool_is_replaced_and_shutdown_closes_replacement(pool):
    first = await validator._get_auth_session()
    await first.close()
    second = await validator._get_auth_session()
    assert first is not second and len(pool) == 2
    await validator.close_auth_validator()
    assert second.closed and second.close_count == 1


@pytest.mark.asyncio
async def test_cancelling_one_request_preserves_pool_and_next_request_is_fresh(pool, monkeypatch):
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def validate(**kwargs):
        credential = json.loads(kwargs["body"])["access_token"]
        if credential == "cancelled-token":
            started.set()
            await waiting.wait()
        return {"valid": True}

    monkeypatch.setattr(validator, "_call_auth_validate", validate)
    task = asyncio.create_task(validator._validate_credential_with_retries("cancelled-token"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await validator._validate_credential_with_retries("next-token") == {"valid": True}
    assert len(pool) == 1 and pool[0].close_count == 0
