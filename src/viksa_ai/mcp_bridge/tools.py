"""Map Viksa agent definitions to MCP tool specs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from viksa_ai.mcp_bridge.mappings import mapping_hint_text

# MCP tool names: alphanumeric + underscores only (Cursor and other clients reject dots).
_TOOL_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_]")
_TOOL_PREFIX = "viksa"

_JSON_TYPE_MAP: Dict[str, Dict[str, Any]] = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "float": {"type": "number"},
    "boolean": {"type": "boolean"},
    "dict": {"type": "object"},
    "list": {"type": "array"},
}


def sanitize_tool_segment(value: str) -> str:
    """Keep only characters allowed in MCP tool names."""
    cleaned = _TOOL_SEGMENT_RE.sub("_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unnamed"


def short_agent_id(agent_id: str) -> str:
    """Short stable suffix for disambiguating tool names."""
    token = re.sub(r"[^A-Za-z0-9]", "", agent_id)
    return (token[-8:] if len(token) >= 4 else token) or "agent"


def make_tool_name(
    agent_alias: str,
    endpoint_name: str,
    *,
    agent_id: Optional[str] = None,
    disambiguate: bool = False,
) -> str:
    """Build a stable MCP tool name for a Viksa endpoint."""
    alias = sanitize_tool_segment(agent_alias)
    name = sanitize_tool_segment(endpoint_name)
    parts = [_TOOL_PREFIX, alias, name]
    if disambiguate and agent_id:
        parts.append(sanitize_tool_segment(short_agent_id(agent_id)))
    return "_".join(parts)


def endpoint_path(agent_alias: str, module: str, endpoint_name: str) -> str:
    return f"{agent_alias}.{module}.{endpoint_name}"


@dataclass(frozen=True)
class ViksaToolSpec:
    """Execution metadata for one Viksa endpoint exposed as an MCP tool."""

    mcp_name: str
    agent_id: str
    agent_alias: str
    agent_type: str
    endpoint: str
    endpoint_name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Optional[Dict[str, Any]] = None
    task_queue: Optional[str] = None
    timeout: Optional[int] = None
    read_only: Optional[bool] = None
    destructive: Optional[bool] = None

    def to_mcp_tool(self) -> Any:
        from mcp import types

        annotations = None
        if self.read_only is not None or self.destructive is not None:
            annotations = types.ToolAnnotations(
                title=f"{self.agent_alias}: {self.endpoint_name}",
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
            )

        return types.Tool(
            name=self.mcp_name,
            description=self.description,
            inputSchema=self.input_schema,
            outputSchema=self.output_schema,
            annotations=annotations,
        )


def _output_schema_for_endpoint(
    agent_outputs: List[Dict[str, Any]],
    endpoint_output_names: List[str],
) -> Optional[Dict[str, Any]]:
    if not endpoint_output_names:
        return None

    outputs_by_name = {item["name"]: item for item in agent_outputs if item.get("name")}
    properties: Dict[str, Any] = {}
    for out_name in endpoint_output_names:
        agent_output = outputs_by_name.get(out_name)
        if agent_output:
            raw_type = str(agent_output.get("type", "string")).lower()
            prop: Dict[str, Any] = dict(_JSON_TYPE_MAP.get(raw_type, {"type": "string"}))
            if agent_output.get("description"):
                prop["description"] = agent_output["description"]
        else:
            prop = {"type": "string", "description": f"Agent output '{out_name}'"}
        properties[out_name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": list(endpoint_output_names),
    }


def _input_schema_for_endpoint(
    agent_inputs: List[Dict[str, Any]],
    endpoint_inputs: List[Dict[str, Any]],
    mappings_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    inputs_by_name = {item["name"]: item for item in agent_inputs if item.get("name")}
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for ref in endpoint_inputs:
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
        description_parts: List[str] = []
        if agent_input.get("description"):
            description_parts.append(str(agent_input["description"]))
        if agent_input.get("ai_note"):
            description_parts.append(f"Note: {agent_input['ai_note']}")
        mapping_id = agent_input.get("mapping_id")
        if mapping_id and mappings_by_id:
            hint = mapping_hint_text(mappings_by_id.get(str(mapping_id)))
            if hint:
                description_parts.append(hint)
        elif mapping_id:
            description_parts.append(
                f"Resolve via mapping_id '{mapping_id}' (see viksa://mappings)."
            )
        if description_parts:
            prop["description"] = " ".join(description_parts)

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


def agent_doc_to_tools(
    agent_doc: Dict[str, Any],
    *,
    mappings_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[ViksaToolSpec]:
    """Convert a builder agent detail document into MCP tool specs."""
    agent_id = str(agent_doc.get("_id") or agent_doc.get("id") or "")
    agent_alias = str(agent_doc.get("agent_alias") or "")
    agent_type = str(agent_doc.get("agent_type") or "cloud").lower()
    agent_inputs = list(agent_doc.get("inputs") or [])
    agent_outputs = list(agent_doc.get("outputs") or [])
    endpoints = list(agent_doc.get("agent_endpoints") or [])

    task_queue: Optional[str] = None
    chrona_queue = agent_doc.get("chrona_queue") or {}
    queues = chrona_queue.get("chrona_queues") or []
    if queues:
        task_queue = str(queues[0])

    tools: List[ViksaToolSpec] = []
    for ep in endpoints:
        if str(ep.get("status", "enabled")).lower() == "disabled":
            continue

        endpoint_name = str(ep.get("name") or "")
        module = str(ep.get("module") or "main")
        if not endpoint_name or not agent_alias:
            continue

        full_endpoint = str(ep.get("endpoint") or "")
        if not full_endpoint:
            full_endpoint = endpoint_path(agent_alias, module, endpoint_name)
        description = str(ep.get("description") or f"Viksa endpoint {full_endpoint}")

        exec_config = ep.get("execution_config") or {}
        timeout = exec_config.get("timeout_seconds") or ep.get("timeout")

        annotations = ep.get("annotations") or {}
        read_only = annotations.get("readOnly")
        if read_only is None:
            read_only = annotations.get("readOnlyHint")
        destructive = annotations.get("destructive")
        if destructive is None:
            destructive = annotations.get("destructiveHint")

        endpoint_outputs = list(ep.get("outputs") or [])

        tools.append(
            ViksaToolSpec(
                mcp_name=make_tool_name(agent_alias, endpoint_name),
                agent_id=agent_id,
                agent_alias=agent_alias,
                agent_type=agent_type,
                endpoint=full_endpoint,
                endpoint_name=endpoint_name,
                description=description,
                input_schema=_input_schema_for_endpoint(
                    agent_inputs,
                    list(ep.get("inputs") or []),
                    mappings_by_id=mappings_by_id,
                ),
                output_schema=_output_schema_for_endpoint(agent_outputs, endpoint_outputs),
                task_queue=task_queue,
                timeout=int(timeout) if timeout is not None else None,
                read_only=read_only if read_only is not None else None,
                destructive=destructive if destructive is not None else None,
            )
        )
    return tools


def format_execution_result(payload: Any) -> str:
    """Serialize pulse execution output for MCP text content."""
    if payload is None:
        return "null"
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, indent=2, default=str)
