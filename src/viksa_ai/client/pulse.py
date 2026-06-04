from __future__ import annotations

from typing import TYPE_CHECKING

from viksa_ai.models.executor import (
    AgentInvokeRequest,
    AgentInvokeResponse,
    EndpointExecutionRequest,
    EndpointExecutionResponse,
)

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class PulseClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client

    async def execute(self, request: EndpointExecutionRequest) -> EndpointExecutionResponse:
        data = await self._client._arequest(
            "POST",
            "/pulse",
            "/executor/execute",
            json=request.model_dump(mode="json"),
        )
        return EndpointExecutionResponse.model_validate(data)

    def execute_sync(self, request: EndpointExecutionRequest) -> EndpointExecutionResponse:
        data = self._client._request(
            "POST",
            "/pulse",
            "/executor/execute",
            json=request.model_dump(mode="json"),
        )
        return EndpointExecutionResponse.model_validate(data)

    async def invoke(self, request: AgentInvokeRequest) -> AgentInvokeResponse:
        data = await self._client._arequest(
            "POST",
            "/pulse",
            "/executor/invoke",
            json=request.model_dump(mode="json"),
        )
        return AgentInvokeResponse.model_validate(data)
