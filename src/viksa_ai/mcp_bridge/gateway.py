"""Proxy stdio MCP clients to the hosted Viksa MCP gateway."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional, cast
from urllib.parse import quote

import httpx
from pydantic import AnyUrl

from viksa_ai._constants import DEFAULT_BASE_URL, ENV_MCP_TOKEN
from viksa_ai.client.base import __version__

logger = logging.getLogger(__name__)


def resolve_gateway_url(
    *,
    base_url: Optional[str] = None,
    agent_alias: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> str:
    """
    Resolve the Streamable HTTP MCP endpoint on mcp-gateway-service.

    * ``gateway_url`` — full override (env: ``VIKSA_MCP_GATEWAY_URL``)
    * ``agent_alias`` — scoped path ``/mcp/agents/{alias}``
    * default — ``{base_url}/mcp`` (all agents allowed by the MCP token)
    """
    if gateway_url:
        return gateway_url.rstrip("/")

    root = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if agent_alias:
        safe_alias = quote(agent_alias.strip(), safe="")
        return f"{root}/mcp/agents/{safe_alias}"
    return f"{root}/mcp"


def resolve_mcp_token(explicit: Optional[str] = None) -> str:
    import os

    token = (explicit or os.environ.get(ENV_MCP_TOKEN) or "").strip()
    if not token:
        raise ValueError(
            f"MCP token required: set {ENV_MCP_TOKEN} or pass --mcp-token "
            "(create tokens in the Viksa console → MCP Tokens)"
        )
    return token


async def run_stdio_gateway_proxy(
    *,
    gateway_url: str,
    mcp_token: str,
) -> None:
    """Run a local stdio MCP server that forwards to the hosted gateway."""
    from mcp import types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server

    headers = {
        "Authorization": f"Bearer {mcp_token}",
        "X-MCP-Client": "viksa-mcp-bridge",
        "User-Agent": f"viksa-mcp-bridge/{__version__}",
    }
    timeout = httpx.Timeout(60.0, read=600.0)

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as http:
        async with streamable_http_client(gateway_url, http_client=http) as (
            remote_read,
            remote_write,
            _get_session_id,
        ):
            async with ClientSession(
                remote_read,
                remote_write,
                client_info=types.Implementation(name="viksa-mcp-bridge", version=__version__),
            ) as upstream:
                init_result = await upstream.initialize()
                logger.info("Connected to Viksa MCP gateway: %s", gateway_url)

                server = Server(
                    "viksa-mcp-bridge",
                    instructions=init_result.instructions,
                )

                list_tools_decorator = cast(
                    Callable[
                        [],
                        Callable[
                            [Callable[[], Awaitable[list[types.Tool]]]],
                            Callable[[], Awaitable[list[types.Tool]]],
                        ],
                    ],
                    server.list_tools,
                )
                call_tool_decorator = cast(
                    Callable[
                        [],
                        Callable[
                            [
                                Callable[
                                    [str, dict[str, Any] | None],
                                    Awaitable[types.CallToolResult],
                                ]
                            ],
                            Callable[
                                [str, dict[str, Any] | None],
                                Awaitable[types.CallToolResult],
                            ],
                        ],
                    ],
                    server.call_tool,
                )
                list_resources_decorator = cast(
                    Callable[
                        [],
                        Callable[
                            [Callable[[], Awaitable[list[types.Resource]]]],
                            Callable[[], Awaitable[list[types.Resource]]],
                        ],
                    ],
                    server.list_resources,
                )
                read_resource_decorator = cast(
                    Callable[
                        [],
                        Callable[
                            [Callable[[AnyUrl | str], Awaitable[str]]],
                            Callable[[AnyUrl | str], Awaitable[str]],
                        ],
                    ],
                    server.read_resource,
                )

                @list_tools_decorator()
                async def handle_list_tools() -> list[types.Tool]:
                    result = await upstream.list_tools()
                    return result.tools

                @call_tool_decorator()
                async def handle_call_tool(
                    name: str,
                    arguments: dict[str, Any] | None,
                ) -> types.CallToolResult:
                    return await upstream.call_tool(name, arguments)

                @list_resources_decorator()
                async def handle_list_resources() -> list[types.Resource]:
                    result = await upstream.list_resources()
                    return result.resources

                @read_resource_decorator()
                async def handle_read_resource(uri: AnyUrl | str) -> str:
                    result = await upstream.read_resource(AnyUrl(str(uri)))
                    if not result.contents:
                        raise ValueError(f"Empty resource: {uri}")
                    first = result.contents[0]
                    if isinstance(first, types.TextResourceContents):
                        return first.text
                    if isinstance(first, types.BlobResourceContents):
                        return first.blob
                    raise ValueError(f"Unsupported resource content for: {uri}")

                init_options = InitializationOptions(
                    server_name="viksa-mcp-bridge",
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                    instructions=init_result.instructions,
                )

                async with stdio_server() as (read_stream, write_stream):
                    await server.run(read_stream, write_stream, init_options)
