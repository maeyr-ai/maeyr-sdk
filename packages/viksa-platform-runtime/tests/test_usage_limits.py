from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from viksa_platform.usage_limits import UsageLimitClient, usage_control


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
            "updates": [{"resource": "executions", "amount": 1}],
        },
        "update",
    )


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
