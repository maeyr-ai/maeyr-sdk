"""Child admission cannot send ambiguous lease ownership or incomplete scope."""

from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import AsyncMock, Mock

import pytest

from maeyr_platform.usage_limits import UsageLimitClient


class _Scope(TypedDict):
    account_id: str
    org_id: str
    project_id: str


class _Parent(TypedDict):
    operation_id: str
    parent_lease_owner: str
    parent_lease_generation: int


SCOPE: _Scope = {"account_id": "AC-test", "org_id": "OI-test", "project_id": "PI-test"}
PARENT: _Parent = {
    "operation_id": "root:agent.call",
    "parent_lease_owner": "parent:attempt-1",
    "parent_lease_generation": 1,
}


def _client() -> UsageLimitClient:
    return UsageLimitClient(
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth", AUTH_INTERNAL_KEY="key"),
        caller_service="pulse-service",
        logger=Mock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": ""},
        {"org_id": ""},
        {"project_id": ""},
        {"org_id": "  "},
        {"project_id": None},
        {"account_id": 1},
        {"operation_id": ""},
        {"operation_id": "bad id"},
        {"operation_id": "x" * 129},
        {"parent_lease_owner": ""},
        {"parent_lease_owner": "/ambiguous"},
        {"parent_lease_owner": "bad owner"},
        {"parent_lease_owner": "x" * 129},
        {"parent_lease_generation": True},
        {"parent_lease_generation": 0},
        {"parent_lease_generation": -1},
        {"parent_lease_generation": 2_147_483_648},
        {"parent_lease_generation": 1.0},
        {"parent_lease_generation": "1"},
    ],
)
async def test_ambiguous_scope_or_parent_is_not_sent_to_auth(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    client = _client()
    post = AsyncMock()
    monkeypatch.setattr(client, "post_license_request", post)
    with pytest.raises(ValueError):
        await client.delegated_execution_lease_state(**{**SCOPE, **PARENT, **changes})
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_check_always_reads_authority_without_reserving_another_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    post = AsyncMock(side_effect=[{"ok": True}, {"ok": False, "reason": "operation_expired"}])
    reserve = AsyncMock()
    monkeypatch.setattr(client, "post_license_request", post)
    monkeypatch.setattr(client, "reserve_usage", reserve)
    assert await client.delegated_execution_lease_state(**SCOPE, **PARENT) == {"ok": True}
    assert await client.delegated_execution_lease_state(**SCOPE, **PARENT) == {
        "ok": False,
        "reason": "operation_expired",
    }
    assert post.await_count == 2
    post.assert_awaited_with(
        "/internal/license/delegated-execution-lease-state", {**SCOPE, **PARENT}
    )
    reserve.assert_not_awaited()
