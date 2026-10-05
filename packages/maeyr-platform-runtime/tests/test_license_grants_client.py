"""The narrow grants read keeps fresh, signed authority and scope semantics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientTimeout
from fastapi import HTTPException

from maeyr_platform.security.internal_request_signing import verify_internal_signature
from maeyr_platform.usage_limits import UsageLimitClient

_KEY = "license-grants-test-key"
_SCOPE = {"account_id": "AC-test", "org_id": "OI-test", "project_id": "PI-test"}


@dataclass
class _Reply:
    status: int
    payload: dict[str, Any]

    async def __aenter__(self) -> _Reply:
        return self

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def post(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str],
        timeout: ClientTimeout,
    ) -> _Reply:
        assert timeout.total == 5
        path = urlsplit(url).path
        lowered = {name.lower(): value for name, value in headers.items()}
        body = json.loads(data)
        assert body == _SCOPE
        assert lowered["x-internal-account-id"] == body["account_id"]
        assert lowered["x-internal-org-id"] == body["org_id"]
        assert lowered["x-internal-project-id"] == body["project_id"]
        assert verify_internal_signature(
            _KEY,
            method="POST",
            path=path,
            body=data,
            service="trace-service",
            timestamp=lowered["x-internal-timestamp"],
            signature=lowered["x-internal-signature"],
            nonce=lowered["x-internal-nonce"],
            account_id=body["account_id"],
            org_id=body["org_id"],
            project_id=body["project_id"],
        )
        self.calls.append((path, data, lowered))
        return self.replies.pop(0)


def _client(monkeypatch: pytest.MonkeyPatch, session: _Session) -> UsageLimitClient:
    client = UsageLimitClient(
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth.test", AUTH_INTERNAL_KEY=_KEY),
        caller_service="trace-service",
        logger=Mock(),
    )
    monkeypatch.setattr(client, "get_session", AsyncMock(return_value=session))
    return client


async def test_grants_are_fresh_and_exactly_signed_for_each_scoped_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        [
            _Reply(200, {"account_id": "AC-test", "max_trace_retention_days": 7}),
            _Reply(
                200,
                {
                    "account_id": "AC-test",
                    "max_trace_retention_days": 0,
                    "limits": {"trace_retention_days": 0},
                },
            ),
        ]
    )
    client = _client(monkeypatch, session)
    first = await client.license_grants(**_SCOPE)
    second = await client.license_grants(**_SCOPE)
    assert first["max_trace_retention_days"] == 7
    assert second["max_trace_retention_days"] == 0
    assert second["limits"]["trace_retention_days"] == 0
    assert [path for path, _, _ in session.calls] == ["/internal/license/grants"] * 2
    assert session.calls[0][1] == session.calls[1][1]
    assert session.calls[0][2]["x-internal-nonce"] != session.calls[1][2]["x-internal-nonce"]


async def test_authority_denial_never_reuses_a_warm_grant_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_Reply(200, {"hosting_allowed": True}), _Reply(403, {})])
    client = _client(monkeypatch, session)
    assert (await client.license_grants(**_SCOPE))["hosting_allowed"] is True
    with pytest.raises(HTTPException) as denied:
        await client.license_grants(**_SCOPE)
    assert denied.value.status_code == 403
    assert [path for path, _, _ in session.calls] == ["/internal/license/grants"] * 2


async def test_full_runtime_policy_report_remains_a_separate_fresh_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"usage": {"cloud_workers_count": 3}, "usage_periods": {"executions": {}}}
    session = _Session([_Reply(200, snapshot)])
    client = _client(monkeypatch, session)
    assert await client.runtime_policy(**_SCOPE) == snapshot
    assert [path for path, _, _ in session.calls] == ["/internal/license/runtime-policy"]


@pytest.mark.parametrize("account_id,org_id", [("", "OI-test"), ("AC-test", None)])
async def test_incomplete_grant_scope_never_reaches_transport(
    monkeypatch: pytest.MonkeyPatch, account_id: str, org_id: str | None
) -> None:
    session = _Session([])
    client = _client(monkeypatch, session)
    with pytest.raises(ValueError, match="complete license scope"):
        await client.license_grants(account_id, org_id, "PI-test")
    assert session.calls == []
