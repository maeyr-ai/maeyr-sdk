"""Live MCP bridge registry with refresh support."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from viksa_ai.client import ViksaClient
from viksa_ai.mcp_bridge.discovery import BridgeTarget, resolve_agent_docs
from viksa_ai.mcp_bridge.mappings import (
    collect_mapping_ids,
    fetch_mappings,
    format_mappings_catalog,
)
from viksa_ai.mcp_bridge.tools import ViksaToolSpec, agent_doc_to_tools, make_tool_name

logger = logging.getLogger(__name__)


@dataclass
class AgentMeta:
    agent_id: str
    agent_alias: str
    agent_name: str
    ai_guidelines: Optional[str] = None


@dataclass
class BridgeRegistry:
    """Mutable registry refreshed from builder-service."""

    tools: Dict[str, ViksaToolSpec] = field(default_factory=dict)
    agents: Dict[str, AgentMeta] = field(default_factory=dict)
    mappings: Dict[str, Dict] = field(default_factory=dict)
    mappings_catalog: str = ""
    load_error: Optional[str] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def build_instructions(self) -> str:
        lines: List[str] = []
        if self.load_error:
            lines.append(
                "Viksa MCP bridge is running but could not load agents from the platform yet.\n"
                f"Error: {self.load_error}\n"
                "The bridge will retry automatically. Check VIKSA_API_KEY, org/project IDs, "
                "and that https://api.viksaai.com is reachable."
            )
            lines.append("")
        lines.extend(
            [
                "Viksa AI agent endpoints exposed as MCP tools.",
                "Each tool maps to a deployed Viksa agent function.",
                "Read viksa://mappings for input shortcut resolution.",
            ]
        )
        for meta in self.agents.values():
            if meta.ai_guidelines:
                lines.append(
                    f"\n## Agent {meta.agent_alias} ({meta.agent_name})\n{meta.ai_guidelines}"
                )
        if self.mappings_catalog:
            lines.append(
                "\n## Mapping context\n"
                "Inputs with mapping_id resolve shortcuts from the mappings resource. "
                "Priority: ai_guidelines → mapping → description → defaults."
            )
        return "\n".join(lines)

    def assign_tool_names(self, specs: List[ViksaToolSpec]) -> Dict[str, ViksaToolSpec]:
        """Assign MCP tool names; disambiguate alias+endpoint collisions across agents."""
        collision_key = Counter(f"{s.agent_alias}\0{s.endpoint_name}" for s in specs)
        registry: Dict[str, ViksaToolSpec] = {}
        for spec in specs:
            key = f"{spec.agent_alias}\0{spec.endpoint_name}"
            disambiguate = collision_key[key] > 1
            mcp_name = make_tool_name(
                spec.agent_alias,
                spec.endpoint_name,
                agent_id=spec.agent_id,
                disambiguate=disambiguate,
            )
            if mcp_name in registry:
                mcp_name = make_tool_name(
                    spec.agent_alias,
                    spec.endpoint_name,
                    agent_id=spec.agent_id,
                    disambiguate=True,
                )
            updated = ViksaToolSpec(
                mcp_name=mcp_name,
                agent_id=spec.agent_id,
                agent_alias=spec.agent_alias,
                agent_type=spec.agent_type,
                endpoint=spec.endpoint,
                endpoint_name=spec.endpoint_name,
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                task_queue=spec.task_queue,
                timeout=spec.timeout,
                read_only=spec.read_only,
                destructive=spec.destructive,
            )
            registry[mcp_name] = updated
        return registry


async def build_registry(client: ViksaClient, target: BridgeTarget) -> BridgeRegistry:
    """Load agents, mappings, and tools from the platform."""
    agent_docs = await resolve_agent_docs(client, target)
    mapping_ids = collect_mapping_ids(agent_docs)
    mappings = await fetch_mappings(client, mapping_ids)

    specs: List[ViksaToolSpec] = []
    agents: Dict[str, AgentMeta] = {}
    for doc in agent_docs:
        agent_id = str(doc.get("_id") or doc.get("id") or "")
        agents[agent_id] = AgentMeta(
            agent_id=agent_id,
            agent_alias=str(doc.get("agent_alias") or ""),
            agent_name=str(doc.get("agent_name") or doc.get("agent_alias") or ""),
            ai_guidelines=doc.get("ai_guidelines"),
        )
        specs.extend(agent_doc_to_tools(doc, mappings_by_id=mappings))

    registry = BridgeRegistry()
    registry.agents = agents
    registry.mappings = mappings
    registry.mappings_catalog = format_mappings_catalog(mappings)
    registry.tools = registry.assign_tool_names(specs)

    if not registry.tools:
        raise ValueError("No enabled endpoints found on the selected agent(s)")
    return registry


async def refresh_registry(
    registry: BridgeRegistry,
    client: ViksaClient,
    target: BridgeTarget,
) -> None:
    """Replace registry contents from builder-service (thread-safe for MCP handlers)."""
    try:
        fresh = await build_registry(client, target)
    except Exception as exc:
        message = str(exc)
        logger.warning("MCP bridge refresh failed: %s", message)
        async with registry._lock:
            registry.load_error = message
        return
    async with registry._lock:
        registry.tools = fresh.tools
        registry.agents = fresh.agents
        registry.mappings = fresh.mappings
        registry.mappings_catalog = fresh.mappings_catalog
        registry.load_error = None
    logger.info("MCP bridge refreshed: %d tool(s)", len(registry.tools))


async def load_tool_registry(client: ViksaClient, target: BridgeTarget) -> BridgeRegistry:
    """Backward-compatible alias returning a full registry."""
    return await build_registry(client, target)
