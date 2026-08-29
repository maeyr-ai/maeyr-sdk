"""Tests for SDK McpClient gateway routing."""

from __future__ import annotations

from maeyr.client.mcp import McpClient


def test_mcp_client_headers_identify_maeyr_sdk():
    client = McpClient("mcp_test", client_slug="maeyr_sdk")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer mcp_test"
    assert headers["X-MCP-Client"] == "maeyr-sdk"
    assert headers["User-Agent"].startswith("maeyr/")


def test_mcp_client_gateway_url_scoped():
    client = McpClient(
        "mcp_test",
        base_url="https://api.test",
        agent_alias="github_mcp_agent",
    )
    assert client.gateway_url == "https://api.test/mcp/agents/github_mcp_agent"
