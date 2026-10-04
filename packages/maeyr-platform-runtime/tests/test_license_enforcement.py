from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from maeyr_platform.licensing import root_execution_license
from maeyr_platform.llm.licensing import (
    LicensedProviderClient,
    current_funding_event,
    reconcile_funding_event,
)
from maeyr_platform.llm.models import LLMScope
from maeyr_platform.usage_limits import UsageLimitClient

SCOPE = LLMScope(account_id="AC-test", org_id="OI-test", project_id="PI-test")


async def owned_reservation(*args, **kwargs):
    return {
        "reserved": True,
        "lease_owner": kwargs["lease_owner"],
        "lease_generation": kwargs["lease_generation"] + 1,
        "remaining_duration_seconds": 300,
        "max_orchestration_iterations": 10,
    }


def authority():
    client = UsageLimitClient(
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth", AUTH_INTERNAL_KEY="key"),
        caller_service="chat-service",
        logger=Mock(),
    )
    client.authorize_ai_call = AsyncMock(
        return_value={
            "authorized": True,
            "funding": "tokens",
            "max_output_tokens": 50,
            "rate_card_version": "approved-v1",
        }
    )
    client.settle_usage = AsyncMock(return_value={"consumed": True})
    client.release_usage = AsyncMock(return_value={"released": True})
    client.queue_usage = AsyncMock(
        return_value={
            "enqueued": True,
            "removed": True,
            "remaining_duration_seconds": 300,
            "max_orchestration_iterations": 10,
        }
    )
    return client


def provider(response):
    create = AsyncMock(return_value=response)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return sdk, create


def response():
    return SimpleNamespace(
        id="provider-1",
        usage=SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
        ),
    )


@pytest.mark.asyncio
async def test_authority_denial_prevents_provider_spend():
    auth = authority()
    auth.authorize_ai_call.side_effect = HTTPException(429, "Quota reached")
    sdk, create = provider(response())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    with pytest.raises(HTTPException) as error:
        await client.chat.completions.create(
            model="published-model",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=100,
        )
    assert error.value.status_code == 429
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_applies_authoritative_output_cap_and_settles_observed_actuals():
    auth = authority()
    sdk, create = provider(response())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    await client.chat.completions.create(
        model="published-model",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=100,
    )
    assert create.await_args.kwargs["max_tokens"] == 50
    reservation = auth.authorize_ai_call.await_args.kwargs
    assert reservation["input_tokens"] > len("Hello")
    assert reservation["turn_operation_id"].startswith("ai-turn:")
    actual = auth.settle_usage.await_args
    assert actual.args[2] == reservation["operation_id"]
    assert actual.kwargs["input_tokens"] == 20
    assert actual.kwargs["output_tokens"] == 10
    assert actual.kwargs["cached_input_tokens"] == 5
    assert current_funding_event()["operation_id"] == actual.args[2]


@pytest.mark.asyncio
async def test_unknown_provider_usage_keeps_budget_hold():
    auth = authority()
    sdk, create = provider(SimpleNamespace(usage=None))
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    await client.chat.completions.create(
        model="published-model",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=100,
    )
    create.assert_awaited_once()
    auth.settle_usage.assert_not_awaited()
    auth.release_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_timeout_does_not_refund_ambiguous_spend():
    auth = authority()
    sdk, create = provider(None)
    create.side_effect = TimeoutError()
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    with pytest.raises(TimeoutError):
        await client.chat.completions.create(
            model="published-model",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=100,
        )
    auth.release_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_replays_same_spend_identity_and_ignores_byok():
    auth = authority()
    event = {
        "account_id": SCOPE.account_id,
        "org_id": SCOPE.org_id,
        "project_id": SCOPE.project_id,
        "credential_source": "platform",
        "usage_status": "observed",
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "metadata": {
            "license_funding": {
                "operation_id": "call-1",
                "funding": "tokens",
                "model_id": "published-model",
                "rate_card_version": "approved-v1",
            }
        },
    }
    await reconcile_funding_event(auth, event)
    await reconcile_funding_event(auth, event)
    assert [call.args[2] for call in auth.settle_usage.await_args_list] == ["call-1", "call-1"]
    event["credential_source"] = "customer"
    await reconcile_funding_event(auth, event)
    assert auth.settle_usage.await_count == 2


@pytest.mark.asyncio
async def test_root_execution_counts_before_start_and_keeps_charge_on_failure():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "effective_limits": {
                "max_orchestration_iterations": 10,
                "max_execution_duration_seconds": 300,
            }
        }
    )
    auth.admit = AsyncMock(return_value={"admitted": True})
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    with pytest.raises(RuntimeError):
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run-1",
        ) as permit:
            assert auth.settle_usage.await_count == 1
            assert permit.max_iterations == 10
            raise RuntimeError("Application failed")
    assert auth.release_usage.await_args.kwargs["refund"] is False
    assert auth.release_usage.await_args.args[2] == auth.reserve_usage.await_args.args[2]


@pytest.mark.asyncio
async def test_completed_root_cannot_run_again():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {
                "max_orchestration_iterations": 10,
                "max_execution_duration_seconds": 300,
            }
        }
    )
    auth.admit = AsyncMock(return_value={"admitted": True})
    auth.reserve_usage = AsyncMock(return_value={"reserved": True, "completed": True})
    with pytest.raises(HTTPException) as error:
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run-1",
        ):
            pytest.fail("A completed root must not restart")
    assert error.value.status_code == 409
    auth.settle_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_start_denial_refunds_confirmed_undispatched_root():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock(return_value={"admitted": True})
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    auth.queue_usage.side_effect = [
        {"enqueued": True},
        HTTPException(409, "Start rejected"),
        {"removed": True},
    ]
    with pytest.raises(HTTPException):
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="never-dispatched",
        ):
            pytest.fail("Rejected queue start must not dispatch")
    assert auth.release_usage.await_args.kwargs["refund"] is True


@pytest.mark.asyncio
async def test_foreign_root_lease_is_not_dispatched_or_released():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(
        return_value={
            "reserved": True,
            "lease_owner": "another-attempt",
            "lease_generation": 1,
            "remaining_duration_seconds": 300,
            "max_orchestration_iterations": 10,
        }
    )
    with pytest.raises(HTTPException) as error:
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run",
        ):
            pytest.fail("Foreign reservation must not dispatch")
    assert error.value.status_code == 503
    auth.settle_usage.assert_not_awaited()
    auth.release_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_preserves_generation_and_transactional_runtime_bounds():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 40, "max_execution_duration_seconds": 1800}
        }
    )
    auth.admit = AsyncMock()

    async def reserve(*args, **kwargs):
        return {
            "reserved": True,
            "lease_owner": kwargs["lease_owner"],
            "lease_generation": 3,
            "remaining_duration_seconds": 19,
            "max_orchestration_iterations": 10,
        }

    auth.reserve_usage = AsyncMock(side_effect=reserve)
    async with root_execution_license(
        auth,
        account_id=SCOPE.account_id,
        org_id=SCOPE.org_id,
        project_id=SCOPE.project_id,
        execution_id="run",
        resume=True,
        resume_generation=2,
    ) as permit:
        assert permit.lease_generation == 3
        assert 18 < permit.max_duration_seconds <= 19
        assert permit.max_iterations == 10
    assert auth.reserve_usage.await_args.kwargs["lease_generation"] == 2
    assert auth.settle_usage.await_args.kwargs["lease_generation"] == 3
    assert auth.queue_usage.await_args_list[1].kwargs["lease_generation"] == 3
    assert auth.release_usage.await_args.kwargs["lease_generation"] == 3
    assert auth.release_usage.await_args.kwargs["lease_owner"] == permit.lease_owner


@pytest.mark.asyncio
async def test_malformed_account_admission_never_grants_permission():
    auth = authority()
    auth.post_license_request = AsyncMock(return_value={})
    with pytest.raises(HTTPException) as error:
        await auth.admit(
            SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            kind="mcp",
            operation_id="mcp-call",
        )
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_prequeued_ai_turn_keeps_queue_kind_at_root_start_and_cleanup():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    async with root_execution_license(
        auth,
        account_id=SCOPE.account_id,
        org_id=SCOPE.org_id,
        project_id=SCOPE.project_id,
        execution_id="run",
        queue_operation_id="queue:volt:server",
        queue_kind="ai_turn",
    ):
        pass
    assert [call.kwargs["queue_kind"] for call in auth.queue_usage.await_args_list] == [
        "ai_turn"
    ] * 3
    assert [call.args[1] for call in auth.queue_usage.await_args_list] == ["queue:volt:server"] * 3


@pytest.mark.asyncio
async def test_signed_usage_retry_preserves_body_and_refreshes_nonce():
    import asyncio
    import json

    from maeyr_platform.usage_limits import post_signed_license_request

    calls = []

    class Reply:
        status = 200

        async def __aenter__(self):
            if len(calls) == 1:
                raise asyncio.TimeoutError()
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"reserved": True}

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Reply()

    get_session = AsyncMock(return_value=Session())
    payload = {
        "account_id": SCOPE.account_id,
        "org_id": SCOPE.org_id,
        "project_id": SCOPE.project_id,
        "operation_id": "root:stable",
        "resource": "executions",
        "lease_owner": "queue:stable",
        "lease_generation": 2,
        "resume": True,
        "amount": 1,
    }
    result = await post_signed_license_request(
        endpoint_path="/internal/usage/reserve",
        payload=payload,
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth", AUTH_INTERNAL_KEY="test-key"),
        caller_service="chat-service",
        get_session=get_session,
        logger=Mock(),
    )
    assert result["reserved"] is True
    assert len(calls) == 2
    assert calls[0][1]["data"] == calls[1][1]["data"]
    assert json.loads(calls[0][1]["data"]) == payload
    assert calls[0][1]["headers"]["x-internal-nonce"] != calls[1][1]["headers"]["x-internal-nonce"]


@pytest.mark.asyncio
async def test_funded_stream_requests_actual_usage_and_settles_once_on_finish():
    auth = authority()

    class Stream:
        close = AsyncMock()

        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(usage=None)
                yield response()

            return chunks()

    sdk, create = provider(Stream())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    stream = await client.chat.completions.create(
        model="published-model",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=100,
        stream=True,
        stream_options={"include_usage": False},
    )
    assert create.await_args.kwargs["stream_options"]["include_usage"] is True
    assert len([chunk async for chunk in stream]) == 2
    await stream.close()
    auth.settle_usage.assert_awaited_once()
    assert auth.settle_usage.await_args.kwargs["input_tokens"] == 20
    assert auth.settle_usage.await_args.kwargs["output_tokens"] == 10
    auth.release_usage.assert_not_awaited()
