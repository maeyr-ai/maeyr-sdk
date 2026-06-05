"""Expose Viksa agents as MCP tools for Cursor, Claude Desktop, and other MCP clients."""

from viksa_ai.mcp_bridge.registry import BridgeRegistry, build_registry, load_tool_registry
from viksa_ai.mcp_bridge.tools import ViksaToolSpec, agent_doc_to_tools, make_tool_name

__all__ = [
    "BridgeRegistry",
    "ViksaToolSpec",
    "agent_doc_to_tools",
    "build_registry",
    "load_tool_registry",
    "make_tool_name",
]
