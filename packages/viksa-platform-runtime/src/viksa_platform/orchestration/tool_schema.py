"""Convert Viksa agent documents into canonical OpenAI tool schemas.

This mirrors ``mcp-gateway-service/mcp_bridge/tools.py`` so the tools the
internal orchestrator calls are shaped identically to the ones the external MCP
gateway advertises (same names, same JSON Schemas). Tool name convention:
``{agent_alias}_{endpoint_name}`` with sanitized characters, matching the MCP
gateway exactly (many MCP clients reject dots in tool names).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .protocols import ToolSpec

# OpenAI accepts at most 128 function tools per request. Cap to stay within
# that hard limit (and to keep the prompt/context bounded).
MAX_OPENAI_TOOLS = 128

# Tool names: alphanumeric + underscore only (parity with MCP gateway / clients).
_TOOL_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_]")

_JSON_TYPE_MAP: Dict[str, Dict[str, Any]] = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "float": {"type": "number"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "dict": {"type": "object"},
    "object": {"type": "object"},
    "list": {"type": "array"},
    "array": {"type": "array"},
}


def sanitize_tool_segment(value: str) -> str:
    cleaned = _TOOL_SEGMENT_RE.sub("_", (value or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unnamed"


def _short_agent_id(agent_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]", "", agent_id or "")
    return (token[-8:] if len(token) >= 4 else token) or "agent"


def make_tool_name(
    agent_alias: str,
    endpoint_name: str,
    *,
    agent_id: Optional[str] = None,
    disambiguate: bool = False,
) -> str:
    parts = [sanitize_tool_segment(agent_alias), sanitize_tool_segment(endpoint_name)]
    if disambiguate and agent_id:
        parts.append(sanitize_tool_segment(_short_agent_id(agent_id)))
    return "_".join(parts)


def _endpoint_path(agent_alias: str, module: str, endpoint_name: str) -> str:
    return f"{agent_alias}.{module}.{endpoint_name}"


def _input_parameters_schema(
    agent_inputs: List[Dict[str, Any]],
    endpoint_inputs: List[Dict[str, Any]],
    mapping_hints: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build a JSON Schema ``parameters`` object for one endpoint's inputs."""
    inputs_by_name = {item["name"]: item for item in agent_inputs if item.get("name")}
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for ref in endpoint_inputs:
        if isinstance(ref, str):
            ref = {"input_ref": ref, "required": True}
        input_ref = ref.get("input_ref") or ref.get("name")
        if not input_ref:
            continue
        agent_input = inputs_by_name.get(input_ref)
        if not agent_input:
            properties[input_ref] = {"type": "string", "description": f"Agent input '{input_ref}'"}
            if ref.get("required", True):
                required.append(input_ref)
            continue

        raw_type = str(agent_input.get("type", "string")).lower()
        prop: Dict[str, Any] = dict(_JSON_TYPE_MAP.get(raw_type, {"type": "string"}))

        desc_parts: List[str] = []
        if agent_input.get("description"):
            desc_parts.append(str(agent_input["description"]))
        if agent_input.get("ai_note"):
            desc_parts.append(f"Note: {agent_input['ai_note']}")
        mapping_id = agent_input.get("mapping_id")
        if mapping_id:
            hint = (mapping_hints or {}).get(str(mapping_id))
            if hint:
                desc_parts.append(hint)
            else:
                desc_parts.append(
                    f"Resolve via mapping_id '{mapping_id}' (see project mappings)."
                )
        if desc_parts:
            prop["description"] = " ".join(desc_parts)

        if agent_input.get("default") is not None:
            prop["default"] = agent_input["default"]
        if agent_input.get("allowed_values"):
            prop["enum"] = agent_input["allowed_values"]
        if agent_input.get("min_value") is not None:
            prop["minimum"] = agent_input["min_value"]
        if agent_input.get("max_value") is not None:
            prop["maximum"] = agent_input["max_value"]
        if agent_input.get("min_length") is not None:
            prop["minLength"] = agent_input["min_length"]
        if agent_input.get("max_length") is not None:
            prop["maxLength"] = agent_input["max_length"]
        if agent_input.get("pattern"):
            prop["pattern"] = agent_input["pattern"]

        properties[input_ref] = prop
        if ref.get("required", True):
            required.append(input_ref)

    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_openai_tools(
    agents: List[Dict[str, Any]],
    *,
    mapping_hints: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, ToolSpec]]:
    """Return ``(openai_tools, registry)`` for the given agent documents.

    ``openai_tools`` is the list passed as ``tools=`` to the LLM.
    ``registry`` maps tool name -> :class:`ToolSpec` for execution dispatch.

    Names are disambiguated with a short agent-id suffix only on collision, so
    the common case keeps clean ``{alias}_{endpoint}`` names.
    """
    # First pass: collect raw pre-spec entries to detect name collisions.
    raw: List[Dict[str, Any]] = []
    seen_counts: Dict[str, int] = {}
    for agent in agents or []:
        agent_id = str(agent.get("_id") or agent.get("id") or "")
        agent_alias = str(agent.get("agent_alias") or "")
        agent_type = str(agent.get("agent_type") or "cloud").lower()
        agent_inputs = list(agent.get("inputs") or [])
        chrona_queue = agent.get("chrona_queue")
        endpoints = list(agent.get("agent_endpoints") or agent.get("endpoints") or [])

        for ep in endpoints:
            if str(ep.get("status", "enabled")).lower() == "disabled":
                continue
            endpoint_name = str(ep.get("name") or "")
            module = str(ep.get("module") or "main")
            if not endpoint_name or not agent_alias:
                continue
            full_endpoint = str(ep.get("endpoint") or "") or _endpoint_path(
                agent_alias, module, endpoint_name
            )
            base_name = make_tool_name(agent_alias, endpoint_name)
            seen_counts[base_name] = seen_counts.get(base_name, 0) + 1

            annotations = ep.get("annotations") or {}
            read_only = annotations.get("readOnly")
            if read_only is None:
                read_only = annotations.get("readOnlyHint")
            destructive = annotations.get("destructive")
            if destructive is None:
                destructive = annotations.get("destructiveHint")

            description = str(ep.get("description") or f"Viksa endpoint {full_endpoint}")
            if ep.get("ai_note"):
                description = f"{description} (Note: {ep['ai_note']})"

            raw.append(
                {
                    "agent_id": agent_id,
                    "agent_alias": agent_alias,
                    "agent_type": agent_type,
                    "endpoint": full_endpoint,
                    "endpoint_name": endpoint_name,
                    "description": description,
                    "base_name": base_name,
                    "chrona_queue": chrona_queue,
                    "read_only": read_only,
                    "destructive": destructive,
                    "parameters": _input_parameters_schema(
                        agent_inputs,
                        list(ep.get("inputs") or []),
                        mapping_hints=mapping_hints,
                    ),
                }
            )

    tools: List[Dict[str, Any]] = []
    registry: Dict[str, ToolSpec] = {}
    for entry in raw:
        disambiguate = seen_counts.get(entry["base_name"], 0) > 1
        name = make_tool_name(
            entry["agent_alias"],
            entry["endpoint_name"],
            agent_id=entry["agent_id"],
            disambiguate=disambiguate,
        )
        # Guard against residual collisions (same alias+endpoint+agent shortid).
        if name in registry:
            suffix = 1
            while f"{name}_{suffix}" in registry:
                suffix += 1
            name = f"{name}_{suffix}"

        spec = ToolSpec(
            name=name,
            agent_id=entry["agent_id"],
            agent_alias=entry["agent_alias"],
            agent_type=entry["agent_type"],
            endpoint=entry["endpoint"],
            endpoint_name=entry["endpoint_name"],
            description=entry["description"],
            parameters=entry["parameters"],
            chrona_queue=entry["chrona_queue"],
            read_only=entry["read_only"] if entry["read_only"] is not None else None,
            destructive=entry["destructive"] if entry["destructive"] is not None else None,
        )
        registry[name] = spec
        tools.append(spec.to_openai_tool())

    return tools, registry


def cap_tools(
    tools: List[Dict[str, Any]],
    registry: Dict[str, ToolSpec],
    max_tools: Optional[int] = MAX_OPENAI_TOOLS,
) -> Tuple[List[Dict[str, Any]], Dict[str, ToolSpec], List[str]]:
    """Cap the tool list to ``max_tools`` and prune the registry to match.

    Returns ``(capped_tools, pruned_registry, dropped_names)``. The registry is
    pruned so dispatch can never resolve a tool that was not advertised to the
    model. Caller is responsible for logging ``dropped_names``.
    """
    if not max_tools or len(tools) <= max_tools:
        return tools, registry, []
    kept = tools[:max_tools]
    kept_names = {t["function"]["name"] for t in kept}
    dropped = [name for name in registry if name not in kept_names]
    pruned = {name: spec for name, spec in registry.items() if name in kept_names}
    return kept, pruned, dropped
