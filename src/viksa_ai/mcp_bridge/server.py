"""MCP server that proxies tool calls to Viksa pulse executor."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from viksa_ai.client import ViksaApiError, ViksaClient
from viksa_ai.mcp_bridge.discovery import BridgeTarget
from viksa_ai.mcp_bridge.registry import BridgeRegistry, refresh_registry
from viksa_ai.mcp_bridge.tools import ViksaToolSpec, format_execution_result
from viksa_ai.models.executor import AgentType, EndpointExecutionRequest

logger = logging.getLogger(__name__)

GUIDELINES_URI_PREFIX = "viksa://agent/"
GUIDELINES_URI_SUFFIX = "/guidelines"
MAPPINGS_URI = "viksa://mappings"
MAPPING_URI_PREFIX = "viksa://mapping/"


def create_mcp_server(
    client: ViksaClient,
    registry: BridgeRegistry,
    *,
    target: BridgeTarget,
    refresh_interval_seconds: int = 60,
):
    """Build an MCP ``Server`` wired to a live Viksa registry."""
    from mcp import types
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions

    refresh_task: Optional[asyncio.Task] = None

    @asynccontextmanager
    async def bridge_lifespan(_server: Server):
        nonlocal refresh_task

        async def _loop() -> None:
            interval = max(refresh_interval_seconds, 15)
            while True:
                await asyncio.sleep(interval)
                await refresh_registry(registry, client, target)

        refresh_task = asyncio.create_task(_loop())
        try:
            yield {}
        finally:
            if refresh_task:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

    server = Server(
        "viksa-mcp-bridge",
        instructions=registry.build_instructions(),
        lifespan=bridge_lifespan,
    )

    async def _tools_snapshot() -> dict[str, ViksaToolSpec]:
        async with registry._lock:
            return dict(registry.tools)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        tools = await _tools_snapshot()
        return [spec.to_mcp_tool() for spec in tools.values()]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        async with registry._lock:
            load_error = registry.load_error
        if load_error:
            return [
                types.TextContent(
                    type="text",
                    text=f"Viksa tools not loaded yet: {load_error}",
                )
            ]

        tools = await _tools_snapshot()
        spec = tools.get(name)
        if spec is None:
            raise ValueError(f"Unknown Viksa tool: {name}")

        payload = arguments or {}
        agent_type = AgentType.SECURE if spec.agent_type == "secure" else AgentType.CLOUD

        request = EndpointExecutionRequest(
            agent_id=spec.agent_id,
            agent_type=agent_type,
            endpoint=spec.endpoint,
            inputs=payload,
            task_queue=spec.task_queue,
            timeout=spec.timeout,
        )

        try:
            result = await client.pulse.execute(request)
        except ViksaApiError as exc:
            message = exc.detail_message or str(exc)
            return [
                types.TextContent(
                    type="text",
                    text=f"Viksa API error ({exc.status_code}): {message}",
                )
            ]

        if result.status != "success" or result.error:
            error_text = result.error or f"Execution failed with status '{result.status}'"
            return [types.TextContent(type="text", text=error_text)]

        body = format_execution_result(result.response)
        if result.duration_ms is not None:
            body = f"{body}\n\n(duration_ms: {result.duration_ms})"
        return [types.TextContent(type="text", text=body)]

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        async with registry._lock:
            agents = dict(registry.agents)
            mappings = dict(registry.mappings)
        resources: list[types.Resource] = []
        for meta in agents.values():
            if meta.ai_guidelines:
                resources.append(
                    types.Resource(
                        uri=f"{GUIDELINES_URI_PREFIX}{meta.agent_id}{GUIDELINES_URI_SUFFIX}",
                        name=f"{meta.agent_alias} guidelines",
                        description=f"ai_guidelines for agent {meta.agent_name}",
                        mimeType="text/plain",
                    )
                )
        if mappings:
            resources.append(
                types.Resource(
                    uri=MAPPINGS_URI,
                    name="Viksa mappings catalog",
                    description="Input mapping shortcuts for all exposed agents",
                    mimeType="application/json",
                )
            )
            for mid, doc in mappings.items():
                resources.append(
                    types.Resource(
                        uri=f"{MAPPING_URI_PREFIX}{mid}",
                        name=str(doc.get("name") or mid),
                        description=f"Mapping ({doc.get('mapping_type')})",
                        mimeType="application/json",
                    )
                )
        return resources

    @server.read_resource()
    async def handle_read_resource(uri: str) -> str:
        if uri == MAPPINGS_URI:
            async with registry._lock:
                return registry.mappings_catalog or "{}"

        if uri.startswith(MAPPING_URI_PREFIX):
            mid = uri[len(MAPPING_URI_PREFIX) :]
            async with registry._lock:
                doc = registry.mappings.get(mid)
            if not doc:
                raise ValueError(f"Mapping not found: {mid}")
            import json

            return json.dumps(
                {
                    "mapping_id": mid,
                    "name": doc.get("name"),
                    "mapping_type": doc.get("mapping_type"),
                    "mapping": doc.get("mapping") or {},
                },
                indent=2,
                default=str,
            )

        if uri.startswith(GUIDELINES_URI_PREFIX) and uri.endswith(GUIDELINES_URI_SUFFIX):
            agent_id = uri[len(GUIDELINES_URI_PREFIX) : -len(GUIDELINES_URI_SUFFIX)]
            async with registry._lock:
                meta = registry.agents.get(agent_id)
            if not meta or not meta.ai_guidelines:
                raise ValueError(f"No ai_guidelines for agent {agent_id}")
            return meta.ai_guidelines

        raise ValueError(f"Unknown resource: {uri}")

    async def run_stdio() -> None:
        from mcp.server.stdio import stdio_server

        init_options = InitializationOptions(
            server_name="viksa-mcp-bridge",
            server_version="0.2.3",
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
            instructions=registry.build_instructions(),
        )

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)

    server.run_stdio = run_stdio  # type: ignore[attr-defined]
    return server
