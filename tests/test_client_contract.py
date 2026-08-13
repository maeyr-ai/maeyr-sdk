import json

import httpx
import pytest

from viksa_ai.client import ViksaClient, ViksaNotFoundError
from viksa_ai.models.agent import AgentCreationRequest, AgentDeletionStatus


def test_builder_sdk_exposes_no_direct_cross_org_share_client() -> None:
    client = ViksaClient("token", base_url="https://api.test")

    assert not hasattr(client.builder.agents, "share")


@pytest.mark.asyncio
async def test_auth_me_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json={"id": "u1", "email": "a@b.com", "account_id": "acc"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", org_id="o1", project_id="p1", base_url="https://api.test")
        client._transport._async_client = http
        user = await client.auth.me()
        assert user.email == "a@b.com"


@pytest.mark.asyncio
async def test_api_error_on_404():
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "not found"}))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._transport._async_client = http
        with pytest.raises(ViksaNotFoundError) as exc:
            await client.builder.agents.get("missing")
        assert exc.value.status_code == 404
        assert exc.value.path == "/agent/missing"
        assert exc.value.request_id is None or isinstance(exc.value.request_id, str)


@pytest.mark.asyncio
async def test_agent_create_reuses_request_idempotency_key_without_serializing_it():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"agent_id": "AI-1", "message": "created"})

    request = AgentCreationRequest(
        agent_name="Example",
        agent_alias="example",
        agent_description="Example agent",
        idempotency_key="agent-create-test-1",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._transport._async_client = http
        await client.builder.agents.create(request)
        await client.builder.agents.create(request)

    assert [item.headers["Idempotency-Key"] for item in captured] == [
        "agent-create-test-1",
        "agent-create-test-1",
    ]
    assert all("idempotency_key" not in item.read().decode("utf-8") for item in captured)


@pytest.mark.asyncio
async def test_schedule_create_supplies_stable_caller_id_without_mutating_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"schedule": {"_id": "SC-CLIENT-1"}})

    body = {"name": "Daily report"}
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._transport._async_client = http
        await client.scheduler.create(body, schedule_id="SC-CLIENT-1")
        await client.scheduler.create(body, schedule_id="SC-CLIENT-1")

    assert body == {"name": "Daily report"}
    assert [json.loads(item.read())["schedule_id"] for item in captured] == [
        "SC-CLIENT-1",
        "SC-CLIENT-1",
    ]


@pytest.mark.asyncio
async def test_schedule_create_rejects_conflicting_identifiers_before_transport():
    client = ViksaClient("token", base_url="https://api.test")
    with pytest.raises(ValueError, match="conflicts"):
        await client.scheduler.create(
            {"schedule_id": "SC-BODY"},
            schedule_id="SC-ARGUMENT",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_chat_schedule_intent_id_is_caller_stable():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"type": "conversation", "message": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._transport._async_client = http
        await client.chat.indent_finder(
            "Schedule a report",
            schedule_id="SC-CHAT-CLIENT-1",
        )
        await client.chat.indent_finder(
            "Schedule a report",
            schedule_id="SC-CHAT-CLIENT-1",
        )

    assert [json.loads(item.read())["schedule_id"] for item in captured] == [
        "SC-CHAT-CLIENT-1",
        "SC-CHAT-CLIENT-1",
    ]


@pytest.mark.asyncio
async def test_agent_delete_exposes_nonterminal_202_state_as_typed_result():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "agent_id": "AI-1",
                "status": "approval_pending",
                "deleted": False,
                "quota_released": False,
                "pending_change_id": "PC-1",
                "message": "Second-party approval required",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._transport._async_client = http
        result = await client.builder.agents.delete("AI-1")

    assert result.status == AgentDeletionStatus.APPROVAL_PENDING
    assert result.complete is False
    assert result.pending_change_id == "PC-1"
