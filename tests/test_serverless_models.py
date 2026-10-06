import pytest
from pydantic import ValidationError

from maeyr.models.agent import AgentCreationRequest, AgentType
from maeyr.models.executor import AgentInvokeRequest, EndpointExecutionRequest
from maeyr.models.executor import AgentType as ExecutorAgentType


def test_serverless_creation_and_execution_preserve_runtime_without_worker_queue():
    created = AgentCreationRequest(
        agent_name="Demo", agent_alias="demo", agent_description="Demo", agent_type="serverless"
    )
    assert created.agent_type == AgentType.SERVERLESS
    for request_type in (EndpointExecutionRequest, AgentInvokeRequest):
        request = request_type(agent_id="AI-1", agent_type="serverless", endpoint="demo.main.run")
        assert request.agent_type == ExecutorAgentType.SERVERLESS
        assert request.task_queue is None
        assert request.model_dump(mode="json")["agent_type"] == "serverless"


def test_secure_execution_still_requires_selected_queue():
    with pytest.raises(ValidationError, match="task_queue is mandatory"):
        EndpointExecutionRequest(agent_id="AI-1", agent_type="secure", endpoint="demo.main.run")


@pytest.mark.parametrize("request_type", [EndpointExecutionRequest, AgentInvokeRequest])
@pytest.mark.parametrize(
    "override", [{"task_queue": "customer-queue"}, {"timeout": 901}, {"timeout": 0}]
)
def test_serverless_requests_cannot_select_worker_queue_or_exceed_deadline(request_type, override):
    with pytest.raises(ValidationError):
        request_type(agent_id="AI-1", agent_type="serverless", endpoint="main.run", **override)


@pytest.mark.parametrize(
    "override", [
        {"namespace": "pro"}, {"generation": True}, {"generation": 0}, {"pool": "team"},
        {"service_url": "https://serverless-service:8443"},
    ]
)
def test_serverless_placement_rejects_old_or_invalid_worker_authority(override):
    from maeyr.models.serverless import ServerlessPlacement

    with pytest.raises(ValidationError):
        ServerlessPlacement.model_validate(
            {
                "worker_id": "SW-1",
                "worker_name": "Autumn Lynx",
                "namespace": "serverless",
                "task_queue": "maeyr-serverless-SW-1",
                "generation": 1,
                **override,
            }
        )


@pytest.mark.asyncio
async def test_builder_assignment_client_reads_without_customer_routing_input():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from maeyr.client.builder import BuilderClient

    response = {
        "account_id": "AC-1",
        "org_id": "OI-1",
        "project_id": "PI-1",
        "agent_id": "AI-1",
        "assignment": None,
    }
    request = AsyncMock(return_value=response)
    client = BuilderClient(SimpleNamespace(_arequest=request, org_id="OI-1", project_id="PI-1"))
    assert (await client.agents.serverless_assignment("AI-1")).model_dump() == response
    assert request.await_args.args == ("GET", "/builder", "/agent/AI-1/serverless-assignment")
    request.return_value = {**response, "agent_id": "AI-OTHER"}
    with pytest.raises(ValueError, match="does not match"):
        await client.agents.serverless_assignment("AI-1")
    for field in ("org_id", "project_id"):
        request.return_value = {**response, field: "OTHER"}
        with pytest.raises(ValueError, match="does not match"):
            await client.agents.serverless_assignment("AI-1")
