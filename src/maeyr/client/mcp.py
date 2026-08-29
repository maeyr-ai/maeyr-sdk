"""Call Maeyr agent tools via the hosted MCP gateway (not pulse directly)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from maeyr._constants import DEFAULT_BASE_URL, ENV_MCP_TOKEN
from maeyr.client.base import __version__
from maeyr.mcp_bridge.gateway import resolve_gateway_url, resolve_mcp_token


def _parse_tool_result(result: Any) -> Any:
    """Return structured MCP tool output when present, else parsed text."""
    structured = getattr(result, "structuredContent", None)
    if structured is None and hasattr(result, "model_dump"):
        structured = result.model_dump().get("structuredContent")
    if structured is not None:
        return structured

    from mcp import types

    text_parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, types.TextContent):
            text_parts.append(block.text)
        elif hasattr(block, "text"):
            text_parts.append(str(block.text))
    if not text_parts:
        return None
    raw = "".join(text_parts).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class McpClient:
    """
    Execute Maeyr agent tools through **mcp-gateway-service**.

    Use this (or Cursor/Claude MCP URL config) for agent tool calls — not
    ``MaeyrClient.pulse.execute``, which bypasses the MCP gateway and breaks
    trace hierarchy (``mcp.tools.call`` → ``pulse.invoke``).
    """

    def __init__(
        self,
        mcp_token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        agent_alias: Optional[str] = None,
        gateway_url: Optional[str] = None,
        client_slug: str = "maeyr_sdk",
        timeout: float = 600.0,
    ) -> None:
        self.mcp_token = mcp_token
        self.gateway_url = resolve_gateway_url(
            base_url=base_url,
            agent_alias=agent_alias,
            gateway_url=gateway_url,
        )
        self.client_slug = client_slug
        self.timeout = timeout

    @classmethod
    def from_token(cls, token: Optional[str] = None, **kwargs: Any) -> McpClient:
        return cls(resolve_mcp_token(token), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> McpClient:
        import os

        return cls.from_token(os.environ.get(ENV_MCP_TOKEN), **kwargs)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.mcp_token}",
            "X-MCP-Client": self.client_slug.replace("_", "-"),
            "User-Agent": f"maeyr/{__version__}",
        }

    async def list_tools(self) -> List[Any]:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx.Timeout(60.0, read=self.timeout)
        async with httpx.AsyncClient(
            headers=self._headers(),
            timeout=timeout,
            follow_redirects=True,
        ) as http:
            async with streamable_http_client(self.gateway_url, http_client=http) as (
                read,
                write,
                _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx.Timeout(60.0, read=self.timeout)
        async with httpx.AsyncClient(
            headers=self._headers(),
            timeout=timeout,
            follow_redirects=True,
        ) as http:
            async with streamable_http_client(self.gateway_url, http_client=http) as (
                read,
                write,
                _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments or {})
                    if getattr(result, "isError", False):
                        payload = _parse_tool_result(result)
                        raise RuntimeError(
                            payload if isinstance(payload, str) else json.dumps(payload)
                        )
                    return _parse_tool_result(result)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> McpClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None
