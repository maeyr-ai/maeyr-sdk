from __future__ import annotations

from typing import TYPE_CHECKING

from maeyr.models.executor import (
    AgentInvokeRequest,
    AgentInvokeResponse,
    EndpointExecutionRequest,
    EndpointExecutionResponse,
)

if TYPE_CHECKING:
    from maeyr.client.base import MaeyrClient


class PulseClient:
    """
    Direct pulse executor API (``/pulse/executor/*``).

    For MCP agent tool calls from Python, prefer :class:`~maeyr.client.mcp.McpClient`
    so execution enters through mcp-gateway-service and traces stay
    ``mcp.tools.call`` → ``pulse.invoke``.
    """

    def __init__(self, client: MaeyrClient) -> None:
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
