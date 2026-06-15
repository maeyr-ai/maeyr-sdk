"""Tests for SDK McpClient gateway routing."""

from __future__ import annotations

from viksa_ai.client.mcp import McpClient


def test_mcp_client_headers_identify_viksa_sdk():
    client = McpClient("mcp_test", client_slug="viksa_sdk")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer mcp_test"
    assert headers["X-MCP-Client"] == "viksa-sdk"
    assert headers["User-Agent"].startswith("viksa-ai/")


def test_mcp_client_gateway_url_scoped():
    client = McpClient(
        "mcp_test",
        base_url="https://api.test",
        agent_alias="github_mcp_agent",
    )
    assert client.gateway_url == "https://api.test/mcp/agents/github_mcp_agent"
