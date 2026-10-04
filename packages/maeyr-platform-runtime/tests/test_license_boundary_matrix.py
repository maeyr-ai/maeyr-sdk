"""Provider and root boundaries: denial must precede any physical side effect."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from test_license_enforcement import SCOPE, authority, owned_reservation, provider, response
from test_llm_runtime import _config

from maeyr_platform.licensing import root_execution_license
from maeyr_platform.llm import (
    CredentialSource,
    LLMCapability,
    LLMConfigurationError,
    UniversalLLMClient,
)
from maeyr_platform.llm.licensing import (
    LicensedProviderClient,
    clear_funding_event,
    current_funding_event,
    reconcile_funding_event,
)
from maeyr_platform.security.internal_request_signing import verify_internal_signature
from maeyr_platform.usage_limits import UsageLimitClient


def bounded_request():
    return {
        "model": "published-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 100,
    }


@pytest.fixture(autouse=True)
def isolated_funding_context():
    clear_funding_event()
    yield
    clear_funding_event()


@pytest.mark.parametrize(
    "change",
    [
        {"model": ""},
        {"max_tokens": None},
        {"max_tokens": 0},
        {"max_tokens": -1},
        {"max_tokens": True},
        {"max_tokens": 1.5},
        {"messages": []},
        {"messages": None},
        {"messages": ["not-a-message"]},
        {"n": 0},
        {"n": 2},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "input_audio", "data": "x"}]}]},
        {"tools": [object()]},
        {"stream": True, "stream_options": "invalid"},
    ],
)
async def test_unbounded_or_unpriced_funded_request_never_authorizes_or_dispatches(change):
    auth = authority()
    sdk, create = provider(response())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    with pytest.raises(HTTPException):
        await client.chat.completions.create(**{**bounded_request(), **change})
    auth.authorize_ai_call.assert_not_awaited()
    create.assert_not_awaited()
    auth.settle_usage.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"authorized": False},
        {"funding": "customer"},
        {"funding": None},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
        {"max_output_tokens": 101},
        {"max_output_tokens": None},
    ],
)
async def test_malformed_funding_reply_never_spends(change):
    auth = authority()
    auth.authorize_ai_call.return_value.update(change)
    sdk, create = provider(response())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    with pytest.raises(HTTPException) as denied:
        await client.chat.completions.create(**bounded_request())
    assert denied.value.status_code == 503
    create.assert_not_awaited()
    auth.release_usage.assert_not_awaited()


@pytest.mark.parametrize("path", ["responses", "images", "audio", "embeddings"])
async def test_non_chat_provider_api_cannot_draw_from_funded_chat_allowance(path):
    auth = authority()
    create = AsyncMock()
    sdk = SimpleNamespace(**{path: SimpleNamespace(create=create)})
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE)
    with pytest.raises(HTTPException) as denied:
        await getattr(client, path).create(model="unpriced-model")
    assert denied.value.status_code == 403
    create.assert_not_awaited()
    auth.authorize_ai_call.assert_not_awaited()


async def test_warm_provider_reauthorizes_every_call_after_revocation():
    auth = authority()
    approved = dict(auth.authorize_ai_call.return_value)
    auth.authorize_ai_call.side_effect = [approved, HTTPException(403, "Revoked")]
    sdk, create = provider(response())
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    await client.chat.completions.create(**bounded_request())
    with pytest.raises(HTTPException):
        await client.chat.completions.create(**bounded_request())
    create.assert_awaited_once()
    assert auth.authorize_ai_call.await_count == 2
    calls = auth.authorize_ai_call.await_args_list
    assert calls[0].kwargs["turn_operation_id"] == calls[1].kwargs["turn_operation_id"]
    assert calls[0].kwargs["operation_id"] != calls[1].kwargs["operation_id"]


async def test_successful_provider_response_survives_settlement_outage_for_durable_replay():
    auth = authority()
    auth.settle_usage.side_effect = HTTPException(503, "Authority unavailable")
    actual = response()
    sdk, _ = provider(actual)
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    assert await client.chat.completions.create(**bounded_request()) is actual
    assert current_funding_event()["operation_id"] == auth.settle_usage.await_args.args[2]
    auth.release_usage.assert_not_awaited()


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 10, "completion_tokens": -1},
        {"prompt_tokens": 10, "completion_tokens": 1.5},
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 11},
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": True},
        },
    ],
)
async def test_invalid_provider_actuals_keep_hold_instead_of_creating_token_invoice(usage):
    auth = authority()
    sdk, _ = provider({"usage": usage})
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    await client.chat.completions.create(**bounded_request())
    auth.settle_usage.assert_not_awaited()
    auth.release_usage.assert_not_awaited()


@pytest.mark.parametrize(
    "source,status",
    [
        ("customer", "observed"),
        ("platform", "estimated"),
        ("platform", "unavailable"),
    ],
)
async def test_non_billable_outbox_evidence_cannot_settle_platform_spend(source, status):
    auth = authority()
    await reconcile_funding_event(
        auth,
        {
            "account_id": SCOPE.account_id,
            "org_id": SCOPE.org_id,
            "project_id": SCOPE.project_id,
            "credential_source": source,
            "usage_status": status,
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
        },
    )
    auth.settle_usage.assert_not_awaited()


@pytest.mark.parametrize(
    "field", ["max_orchestration_iterations", "max_execution_duration_seconds"]
)
@pytest.mark.parametrize("value", [None, 0, -2, True, 1.5, "10"])
async def test_invalid_root_bounds_never_reserve_or_dispatch(field, value):
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {
                "max_orchestration_iterations": 10,
                "max_execution_duration_seconds": 300,
                field: value,
            }
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock()
    with pytest.raises(HTTPException):
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run",
        ):
            pytest.fail("Invalid bounds must not dispatch")
    auth.reserve_usage.assert_not_awaited()
    assert auth.queue_usage.await_args.kwargs["action"] == "drop"


async def test_final_start_bounds_override_stale_policy_before_dispatch():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 40, "max_execution_duration_seconds": 1800}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    auth.queue_usage.side_effect = [
        {"enqueued": True},
        {"removed": True, "remaining_duration_seconds": 2, "max_orchestration_iterations": 3},
        {"removed": True},
    ]
    async with root_execution_license(
        auth,
        account_id=SCOPE.account_id,
        org_id=SCOPE.org_id,
        project_id=SCOPE.project_id,
        execution_id="run",
    ) as permit:
        assert 0 < permit.max_duration_seconds <= 2
        assert permit.max_iterations == 3
    assert auth.release_usage.await_args.kwargs["refund"] is False


async def test_cancellation_after_dispatch_releases_concurrency_without_refunding_count():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    entered = asyncio.Event()

    async def run():
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run",
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert auth.release_usage.await_args.kwargs["refund"] is False
    assert auth.release_usage.await_args.kwargs["lease_generation"] == 1
    assert auth.queue_usage.await_args.kwargs["action"] == "drop"


async def test_release_outage_still_attempts_queue_cleanup_without_fake_refund():
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    auth.release_usage.side_effect = HTTPException(503, "Retry bounded lease reconciliation")
    with pytest.raises(HTTPException):
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run",
        ):
            pass
    assert auth.release_usage.await_args.kwargs["refund"] is False
    assert auth.queue_usage.await_args.kwargs["action"] == "drop"


@pytest.mark.parametrize(
    "caller,queue_kind",
    [("chat-service", "execution"), ("volt-engine-service", "ai_turn")],
)
@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "unconfirmed"])
async def test_committed_enqueue_with_failed_reply_drops_same_signed_queue(
    caller, queue_kind, failure
):
    """The real signed client must clean capacity after ambiguous admission."""
    calls = []
    held = set()
    secret = "queue-fault-test-key"
    queue_id = "queue:server-owned"

    class Reply:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            if self.payload["action"] == "enqueue" and failure == "timeout":
                raise asyncio.TimeoutError()
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            if self.payload["action"] == "enqueue":
                if failure == "invalid_json":
                    raise ValueError("reply interrupted")
                return {"enqueued": False}
            return {"removed": True}

    class Session:
        def post(self, url, *, data, headers, timeout):
            assert url == "https://auth.test/internal/license/queue"
            payload = json.loads(data)
            assert payload == {
                "account_id": SCOPE.account_id,
                "org_id": SCOPE.org_id,
                "project_id": SCOPE.project_id,
                "operation_id": queue_id,
                "action": payload["action"],
                "root_operation_id": None,
                "lease_generation": None,
                "queue_kind": queue_kind,
            }
            assert verify_internal_signature(
                secret,
                method="POST",
                path="/internal/license/queue",
                body=data,
                service=caller,
                account_id=SCOPE.account_id,
                org_id=SCOPE.org_id,
                project_id=SCOPE.project_id,
                timestamp=headers["x-internal-timestamp"],
                signature=headers["x-internal-signature"],
                nonce=headers["x-internal-nonce"],
            )
            identity = (caller, queue_kind, queue_id)
            if payload["action"] == "enqueue":
                held.add(identity)
            else:
                assert payload["action"] == "drop"
                held.discard(identity)
            calls.append(payload)
            return Reply(payload)

    auth = UsageLimitClient(
        settings=SimpleNamespace(AUTH_SERVICE_URL="https://auth.test", AUTH_INTERNAL_KEY=secret),
        caller_service=caller,
        logger=Mock(),
    )
    auth.get_session = AsyncMock(return_value=Session())
    auth.runtime_policy = AsyncMock()
    auth.reserve_usage = AsyncMock()
    auth.settle_usage = AsyncMock()
    auth.release_usage = AsyncMock()
    with pytest.raises(HTTPException) as denied:
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="enqueue-never-dispatched",
            queue_operation_id=queue_id,
            queue_kind=queue_kind,
        ):
            pytest.fail("Unconfirmed admission must never dispatch")
    assert denied.value.status_code == 503
    assert calls[-1]["action"] == "drop"
    assert held == set()
    auth.runtime_policy.assert_not_awaited()
    auth.reserve_usage.assert_not_awaited()
    auth.settle_usage.assert_not_awaited()
    auth.release_usage.assert_not_awaited()


async def test_cancelled_enqueue_attempt_waits_for_queue_cleanup():
    auth = authority()
    committed = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    held = set()

    async def queue(_account, operation, *, action, **_scope):
        if action == "enqueue":
            held.add(operation)
            committed.set()
            await asyncio.Event().wait()
        assert action == "drop"
        cleanup_started.set()
        await allow_cleanup.wait()
        held.remove(operation)
        return {"removed": True}

    auth.queue_usage.side_effect = queue
    auth.runtime_policy = AsyncMock()
    auth.reserve_usage = AsyncMock()

    async def run():
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="cancelled-enqueue",
        ):
            pytest.fail("Cancelled admission must never dispatch")

    task = asyncio.create_task(run())
    await committed.wait()
    task.cancel()
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not task.done()
        # A disconnect during cleanup must not cancel the capacity release.
        task.cancel()
    finally:
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert held == set()
    auth.runtime_policy.assert_not_awaited()
    auth.reserve_usage.assert_not_awaited()


async def test_interleaved_provider_calls_keep_tenant_and_funding_evidence_isolated():
    auth = authority()

    async def run(project, turn):
        sdk, _ = provider(response())
        scope = SCOPE.model_copy(update={"project_id": project})
        client = LicensedProviderClient(
            sdk, authority=auth, scope=scope, turn_identity=lambda: turn
        )
        await client.chat.completions.create(**bounded_request())
        await asyncio.sleep(0)
        return current_funding_event()

    first, second = await asyncio.gather(run("PI-one", "turn-one"), run("PI-two", "turn-two"))
    assert first["operation_id"] != second["operation_id"]
    assert first["turn_operation_id"] != second["turn_operation_id"]
    assert {call.kwargs["project_id"] for call in auth.authorize_ai_call.await_args_list} == {
        "PI-one",
        "PI-two",
    }
    assert {call.kwargs["project_id"] for call in auth.settle_usage.await_args_list} == {
        "PI-one",
        "PI-two",
    }
    assert current_funding_event() is None


async def test_byok_client_never_uses_funded_allowance_or_platform_fallback():
    auth = authority()
    auth.authorize_ai_call.side_effect = HTTPException(403, "Free has no platform AI")
    sdk, create = provider(response())
    runtime = UniversalLLMClient(
        AsyncMock(return_value=_config(source=CredentialSource.CUSTOMER)),
        lambda _: sdk,
        funding_authority=auth,
        turn_identity=lambda: "byok-turn",
    )
    resolved = await runtime.for_scope(SCOPE)
    assert resolved.client is sdk
    await resolved.client.chat.completions.create(**bounded_request())
    create.assert_awaited_once()
    auth.authorize_ai_call.assert_not_awaited()
    auth.settle_usage.assert_not_awaited()
    assert current_funding_event() is None
    with pytest.raises(LLMConfigurationError, match="transcription model"):
        await runtime.for_scope(SCOPE, LLMCapability.TRANSCRIPTION)
    auth.authorize_ai_call.assert_not_awaited()


@pytest.mark.parametrize("remaining", [None, 0, -1, True, 1.5, "10"])
async def test_invalid_final_queue_deadline_never_dispatches_and_releases_owned_hold(remaining):
    auth = authority()
    auth.runtime_policy = AsyncMock(
        return_value={
            "limits": {"max_orchestration_iterations": 10, "max_execution_duration_seconds": 300}
        }
    )
    auth.admit = AsyncMock()
    auth.reserve_usage = AsyncMock(side_effect=owned_reservation)
    auth.queue_usage.side_effect = [
        {"enqueued": True},
        {
            "removed": True,
            "remaining_duration_seconds": remaining,
            "max_orchestration_iterations": 10,
        },
        {"removed": True},
    ]
    with pytest.raises(HTTPException):
        async with root_execution_license(
            auth,
            account_id=SCOPE.account_id,
            org_id=SCOPE.org_id,
            project_id=SCOPE.project_id,
            execution_id="run",
        ):
            pytest.fail("Unconfirmed final runtime must not dispatch")
    assert auth.release_usage.await_args.kwargs["refund"] is True
    assert auth.release_usage.await_args.kwargs["lease_generation"] == 1


async def test_partial_stream_without_actuals_keeps_hold_after_client_close():
    auth = authority()

    class Stream:
        close = AsyncMock()

        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(usage=None)
                await asyncio.Event().wait()

            return chunks()

    raw = Stream()
    sdk, _ = provider(raw)
    client = LicensedProviderClient(sdk, authority=auth, scope=SCOPE, turn_identity=lambda: "turn")
    stream = await client.chat.completions.create(**bounded_request(), stream=True)
    await anext(stream)
    await stream.close()
    raw.close.assert_awaited_once()
    auth.settle_usage.assert_not_awaited()
    auth.release_usage.assert_not_awaited()
