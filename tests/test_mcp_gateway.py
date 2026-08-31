"""Tests for Maeyr MCP gateway stdio proxy helpers."""

from __future__ import annotations

import pytest

from maeyr.mcp_bridge.gateway import resolve_gateway_url, resolve_mcp_token


def test_resolve_gateway_url_default():
    assert resolve_gateway_url() == "https://api.maeyr.com/mcp"


def test_resolve_gateway_url_with_agent_alias():
    url = resolve_gateway_url(base_url="https://api.test", agent_alias="my_agent")
    assert url == "https://api.test/mcp/agents/my_agent"


def test_resolve_gateway_url_explicit_override():
    assert (
        resolve_gateway_url(gateway_url="https://custom.example/mcp/")
        == "https://custom.example/mcp"
    )


def test_resolve_mcp_token_from_explicit():
    assert resolve_mcp_token("mcp_test_token") == "mcp_test_token"


def test_resolve_mcp_token_missing(monkeypatch):
    monkeypatch.delenv("MAEYR_MCP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MCP token required"):
        resolve_mcp_token(None)
