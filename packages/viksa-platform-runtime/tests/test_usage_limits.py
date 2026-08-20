from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fastapi import HTTPException
from viksa_platform.usage_limits import UsageLimitClient, enforce_limit, usage_control


def _client() -> UsageLimitClient:
    return UsageLimitClient(
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth", AUTH_INTERNAL_KEY="key"),
        caller_service="test-service",
        logger=Mock(),
    )


@pytest.mark.asyncio
async def test_increment_usage_accepts_only_the_current_account_counter_contract() -> None:
    client = _client()
    post = AsyncMock(return_value=True)
    client.post_usage_request = post  # type: ignore[method-assign]

    assert await client.increment_usage(
        "account-1",
        [{"resource": "executions", "amount": 1}],
    )
    post.assert_awaited_once_with(
        "/internal/usage/increment",
        {
            "account_id": "account-1",
            "operation_id": post.await_args.args[1]["operation_id"],
            "updates": [{"resource": "executions", "amount": 1}],
        },
        "update",
    )
    assert post.await_args.args[1]["operation_id"].startswith("usage:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"resource": "agents", "amount": 1},
        {"resource": "cloud_worker_cpu", "amount": 100},
        {"resource": "executions", "amount": 1, "absolute": True},
    ],
)
async def test_increment_usage_rejects_retired_update_variants(
    update: dict[str, object],
) -> None:
    client = _client()
    post = AsyncMock()
    client.post_usage_request = post  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="Unsupported account usage update"):
        await client.increment_usage("account-1", [update])

    post.assert_not_awaited()


@pytest.mark.parametrize("resource", ["agents", "cloud_worker_cpu", "unknown"])
def test_usage_control_rejects_resources_with_dedicated_quota_apis(
    resource: str,
) -> None:
    with pytest.raises(ValueError, match="Unsupported account usage resource"):
        usage_control(resource, enforce=AsyncMock(), increment=AsyncMock())


@pytest.mark.asyncio
async def test_usage_control_consumes_before_the_operation() -> None:
    order: list[str] = []

    async def enforce(*_args):
        order.append("advisory")

    async def increment(_account_id, _updates, *, operation_id):
        assert operation_id.startswith("usage:chats:")
        order.append("consume")
        return True

    @usage_control("chats", enforce=enforce, increment=increment)
    async def operation(*, current_user):
        del current_user
        order.append("operation")
        return "ok"

    result = await operation(
        current_user={
            "account_id": "AC-1",
            "usage": {"chats_today_count": 0},
            "limits": {"max_chats_per_day": 1},
        }
    )

    assert result == "ok"
    assert order == ["advisory", "consume", "operation"]


@pytest.mark.asyncio
async def test_usage_control_fails_closed_without_running_operation() -> None:
    operation = AsyncMock(return_value="must-not-run")
    decorated = usage_control(
        "executions",
        enforce=AsyncMock(),
        increment=AsyncMock(return_value=False),
    )(operation)

    with pytest.raises(HTTPException) as exc_info:
        await decorated(
            current_user={
                "account_id": "AC-1",
                "usage": {"executions_today_count": 0},
                "limits": {"max_executions_per_day": 1},
            }
        )

    assert exc_info.value.status_code == 503
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_limit_allows_exact_last_unit_and_rejects_next() -> None:
    logger = Mock()
    user = {
        "account_id": "AC-1",
        "usage": {"chats_today_count": 0},
        "limits": {"max_chats_per_day": 1},
    }
    await enforce_limit(user, "chats", 1, logger=logger)

    user["usage"]["chats_today_count"] = 1
    with pytest.raises(HTTPException) as exc_info:
        await enforce_limit(user, "chats", 1, logger=logger)
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_enforce_limit_treats_zero_as_no_capacity_and_minus_one_as_unlimited() -> None:
    logger = Mock()
    zero = {
        "usage": {"executions_today_count": 0},
        "limits": {"max_executions_per_day": 0},
    }
    with pytest.raises(HTTPException) as exc_info:
        await enforce_limit(zero, "executions", 1, logger=logger)
    assert exc_info.value.status_code == 429

    unlimited = {
        "usage": {"executions_today_count": 123},
        "limits": {"max_executions_per_day": -1},
    }
    await enforce_limit(unlimited, "executions", 1, logger=logger)

    overflow = {
        "usage": {"executions_today_count": 2_147_483_647},
        "limits": {"max_executions_per_day": -1},
    }
    with pytest.raises(HTTPException) as invalid_total:
        await enforce_limit(overflow, "executions", 1, logger=logger)
    assert invalid_total.value.status_code == 503
