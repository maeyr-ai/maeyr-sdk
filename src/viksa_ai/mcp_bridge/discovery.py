"""Resolve which Viksa agent documents to expose."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from viksa_ai.client import ViksaClient


@dataclass(frozen=True)
class BridgeTarget:
    """Which Viksa agents to expose."""

    agent_id: Optional[str] = None
    agent_alias: Optional[str] = None
    all_deployed: bool = False


async def resolve_agent_docs(client: ViksaClient, target: BridgeTarget) -> List[dict]:
    if target.agent_id:
        return [await client.builder.agents.get(target.agent_id)]

    if target.agent_alias:
        async for summary in client.builder.agents.iter_all():
            if summary.get("agent_alias") == target.agent_alias:
                agent_id = summary.get("_id") or summary.get("id")
                if agent_id:
                    return [await client.builder.agents.get(str(agent_id))]
        raise ValueError(f"No agent found with alias '{target.agent_alias}'")

    if target.all_deployed:
        docs: List[dict] = []
        async for summary in client.builder.agents.iter_all():
            if summary.get("deploy_status") != "deployed":
                continue
            if summary.get("agent_status") not in (None, "enabled"):
                continue
            agent_id = summary.get("_id") or summary.get("id")
            if not agent_id:
                continue
            docs.append(await client.builder.agents.get(str(agent_id)))
        if not docs:
            raise ValueError("No deployed agents found in this project")
        return docs

    raise ValueError("Specify --agent-id, --agent-alias, or --all-deployed")
