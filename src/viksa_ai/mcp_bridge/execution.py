"""Execution backend protocol for MCP bridge (stdio or remote gateway)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from viksa_ai.models.executor import EndpointExecutionRequest, EndpointExecutionResponse


class MCPExecutionBackend(Protocol):
    async def execute(self, request: EndpointExecutionRequest) -> EndpointExecutionResponse:
        """Execute a Viksa agent endpoint."""


class ViksaClientExecutionBackend:
    """Default backend: Viksa HTTP client pulse executor."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def execute(self, request: EndpointExecutionRequest) -> EndpointExecutionResponse:
        return await self._client.pulse.execute(request)
