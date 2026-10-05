"""Known denial codes are safe, bounded, and terminal at the signed boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError, ClientSession, ClientTimeout
from fastapi import HTTPException

from maeyr_platform.usage_limits import post_signed_license_request

_PUBLIC_TRIAL_EXPIRY = {
    "code": "trial_expired",
    "message": "The Free trial has expired.",
    "retryable": False,
}
_SCOPE = {"account_id": "AC-test", "org_id": "OI-test", "project_id": "PI-test"}
_GENERIC_DENIAL = "This operation is not authorized by the current license"


def _detail(error: HTTPException) -> object:
    # Older Starlette annotations declare str; the FastAPI transport accepts JSON.
    return error.detail


class _Body(asyncio.StreamReader):
    def __init__(self, body: bytes, error: BaseException | None = None) -> None:
        super().__init__()
        self.error = error
        self.read_sizes: list[int] = []
        self.feed_data(body)
        self.feed_eof()

    async def readexactly(self, n: int) -> bytes:
        self.read_sizes.append(n)
        if self.error is not None:
            raise self.error
        return await super().readexactly(n)


class _Reply:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json",
        body_error: BaseException | None = None,
        transport_error: BaseException | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.content = _Body(body, body_error)
        self.body = body
        self.transport_error = transport_error
        self.headers = headers or {}
        self.json_reads = 0

    async def __aenter__(self) -> _Reply:
        if self.transport_error is not None:
            raise self.transport_error
        return self

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def json(self) -> Any:
        self.json_reads += 1
        return json.loads(self.body)


class _Session:
    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = replies
        self.calls: list[tuple[bytes, dict[str, str]]] = []

    def post(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str],
        timeout: ClientTimeout,
    ) -> _Reply:
        assert url == "https://auth.test/internal/license/queue"
        assert timeout.total == 5
        self.calls.append((data, headers))
        return self.replies.pop(0)


async def _request(session: _Session) -> dict[str, Any]:
    return await post_signed_license_request(
        endpoint_path="/internal/license/queue",
        payload={**_SCOPE, "operation_id": "queue:stable", "action": "enqueue"},
        settings=SimpleNamespace(
            AUTH_SERVICE_URL="https://auth.test", AUTH_INTERNAL_KEY="test-key"
        ),
        caller_service="pulse-service",
        get_session=AsyncMock(return_value=cast(ClientSession, session)),
        logger=Mock(),
    )


async def test_canonical_trial_denial_preserves_only_static_allowlisted_details() -> None:
    reply = _Reply(
        403,
        json.dumps(
            {
                "detail": {
                    "code": "trial_expired",
                    "message": "private-token tenant:AC-private model:private-config",
                    "retryable": True,
                    "credentials": "private-secret",
                },
                "account_id": "AC-private",
            }
        ).encode(),
    )
    session = _Session([reply])

    with pytest.raises(HTTPException) as denied:
        await _request(session)

    assert denied.value.status_code == 403
    assert _detail(denied.value) == _PUBLIC_TRIAL_EXPIRY
    assert len(session.calls) == 1
    assert reply.json_reads == 0
    assert reply.content.read_sizes == [4097]


@pytest.mark.parametrize("status_code", [402, 409, 429])
async def test_trial_code_on_wrong_status_keeps_existing_generic_denial(status_code: int) -> None:
    reply = _Reply(status_code, b'{"detail":{"code":"trial_expired"}}')
    session = _Session([reply])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == status_code
    assert denied.value.detail == (
        "Plan allowance or authorized spending limit reached"
        if status_code == 402
        else "A request or plan allowance limit was reached. Check usage and limits."
        if status_code == 429
        else "This operation conflicts with the current license or execution state"
    )
    assert len(session.calls) == 1
    assert reply.content.read_sizes == ([] if status_code == 402 else [4097])


@pytest.mark.parametrize(
    ("status_code", "code", "message_part"),
    [
        (403, "license_disabled", "Contact your administrator"),
        (403, "managed_ai_not_included", "own LLM key"),
        (403, "spending_not_authorized", "spending limit"),
        (429, "quota_exceeded", "workspace allowance"),
        (429, "concurrency_exceeded", "concurrent execution"),
        (429, "rate_exceeded", "request rate limit"),
        (429, "model_call_limit_exceeded", "model-call limit"),
        (429, "queue_exceeded", "queue is full"),
        (409, "operation_conflict", "Refresh its status"),
        (409, "cleanup_busy", "cleanup is already running"),
        (409, "execution_duration_exceeded", "duration limit"),
        (409, "inventory_changed", "inventory changed"),
        (409, "lease_owner_conflict", "Another worker owns"),
        (409, "operation_already_started", "already started"),
        (409, "operation_completed", "already completed"),
        (409, "operation_expired", "expired"),
        (409, "operation_released", "released"),
        (409, "policy_changed", "policy changed"),
        (409, "queue_not_found", "no longer available"),
        (409, "reservation_exceeded", "reserved allowance"),
        (409, "reservation_not_found", "reservation is no longer available"),
        (409, "root_identity_required", "parent execution identity"),
        (409, "root_not_started", "parent execution has not started"),
        (409, "stale_lease_generation", "lease is outdated"),
        (409, "trial_cleanup_in_progress", "cleanup is in progress"),
        (409, "trial_not_expired", "trial is still active"),
        (409, "turn_identity_required", "conversation-turn identity"),
    ],
)
async def test_license_conditions_keep_distinct_safe_codes_without_retrying_denials(
    status_code: int, code: str, message_part: str
) -> None:
    reply = _Reply(
        status_code,
        json.dumps(
            {"detail": {"code": code, "message": "private-secret", "account_id": "AC-private"}}
        ).encode(),
    )
    session = _Session([reply])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == status_code
    detail = _detail(denied.value)
    assert isinstance(detail, dict)
    assert detail["code"] == code
    assert isinstance(detail["message"], str)
    assert message_part in detail["message"]
    assert detail["retryable"] is (
        code in {"concurrency_exceeded", "rate_exceeded", "queue_exceeded"}
    )
    assert set(detail) == {"code", "message", "retryable"}
    assert "private" not in json.dumps(detail)
    assert len(session.calls) == 1


@pytest.mark.parametrize("value", ["0", "1", "30", "86400", "000030"])
async def test_throttling_preserves_only_bounded_retry_after(value: str) -> None:
    reply = _Reply(
        429,
        b'{"detail":{"code":"rate_exceeded"}}',
        headers={"Retry-After": value, "X-Private-Key": "private-secret"},
    )
    session = _Session([reply])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.headers == {"Retry-After": str(int(value))}
    assert len(session.calls) == 1


@pytest.mark.parametrize("value", ["", "-1", "+30", "1.5", "86401", "9999999", "٣٠", "30\r\n"])
async def test_malformed_retry_hints_are_not_forwarded(value: str) -> None:
    reply = _Reply(429, b'{"detail":{"code":"rate_exceeded"}}', headers={"Retry-After": value})
    with pytest.raises(HTTPException) as denied:
        await _request(_Session([reply]))
    assert denied.value.headers is None


async def test_retry_after_on_terminal_trial_expiry_is_not_forwarded() -> None:
    reply = _Reply(403, b'{"detail":{"code":"trial_expired"}}', headers={"Retry-After": "30"})
    with pytest.raises(HTTPException) as denied:
        await _request(_Session([reply]))
    assert _detail(denied.value) == _PUBLIC_TRIAL_EXPIRY
    assert denied.value.headers is None


@pytest.mark.parametrize(
    "body",
    [
        b"invalid-json",
        b"\xff",
        b"[]",
        b"null",
        b'{"code":"trial_expired"}',
        b'{"detail":"trial_expired"}',
        b'{"detail":{"code":true}}',
        b'{"detail":{"code":["trial_expired"]}}',
        b'{"detail":{"code":"TRIAL_EXPIRED"}}',
        b'{"detail":{"code":"unknown","message":"private-secret"}}',
    ],
)
async def test_unknown_or_malformed_denial_is_generic_without_retry(body: bytes) -> None:
    session = _Session([_Reply(403, body)])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == 403
    assert denied.value.detail == _GENERIC_DENIAL
    assert len(session.calls) == 1


@pytest.mark.parametrize("body_error", [ClientError("private-secret"), asyncio.TimeoutError()])
async def test_denial_body_transport_failure_does_not_retry_a_terminal_rejection(
    body_error: BaseException,
) -> None:
    session = _Session([_Reply(403, b"", body_error=body_error)])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == 403
    assert denied.value.detail == _GENERIC_DENIAL
    assert len(session.calls) == 1


async def test_oversized_denial_is_read_with_a_bound_and_rejected() -> None:
    reply = _Reply(403, b'{"detail":{"code":"trial_expired","padding":"' + b"x" * 4096 + b'"}}')
    session = _Session([reply])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == 403
    assert denied.value.detail == _GENERIC_DENIAL
    assert reply.content.read_sizes == [4097]
    assert not reply.content.at_eof()
    assert len(session.calls) == 1


async def test_non_json_denial_does_not_read_or_expose_the_body() -> None:
    reply = _Reply(403, b'{"detail":{"code":"trial_expired"}}', content_type="text/html")
    session = _Session([reply])
    with pytest.raises(HTTPException) as denied:
        await _request(session)
    assert denied.value.status_code == 403
    assert denied.value.detail == _GENERIC_DENIAL
    assert reply.content.read_sizes == []
    assert len(session.calls) == 1


@pytest.mark.parametrize("transport_error", [ClientError("disconnected"), asyncio.TimeoutError()])
async def test_genuine_request_transport_failure_retries_with_same_operation(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: BaseException,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("maeyr_platform.usage_limits.asyncio.sleep", sleep)
    success = {"ok": True, "enqueued": True, "remaining": 3}
    reply = _Reply(200, json.dumps(success).encode())
    session = _Session([_Reply(200, b"", transport_error=transport_error), reply])

    assert await _request(session) == success
    assert len(session.calls) == 2
    assert session.calls[0][0] == session.calls[1][0]
    assert session.calls[0][1]["x-internal-nonce"] != session.calls[1][1]["x-internal-nonce"]
    assert reply.json_reads == 1
    sleep.assert_awaited_once()


async def test_success_payload_is_unchanged_without_denial_parsing() -> None:
    success = {
        "ok": True,
        "limits": {"max_execution_queue": 3},
        "detail": {"code": "trial_expired"},
    }
    reply = _Reply(200, json.dumps(success).encode())
    session = _Session([reply])
    assert await _request(session) == success
    assert reply.json_reads == 1
    assert reply.content.read_sizes == []
    assert len(session.calls) == 1
