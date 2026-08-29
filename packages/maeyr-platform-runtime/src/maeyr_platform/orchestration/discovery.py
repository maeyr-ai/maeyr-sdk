# ruff: noqa: E501
"""Canonical tool discovery for the orchestration harness.

At 500+ agent endpoints you cannot expose every tool to the model (OpenAI caps
at 128 tools, and it would be hugely token-heavy). Instead the model starts with
the intent-funnelled toolset plus two always-present meta-tools:

* ``find_tools(query)``  — semantic/keyword search over the FULL catalog; returns
  matching tool names (does NOT activate them).
* ``load_tools(tool_names)`` — activates the named tools so subsequent turns can
  call them (their schemas are injected into the live ``tools`` list).

This gives "discover and use any tool as needed" behaviour while
keeping per-turn token cost proportional to what is actually used.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

from .tool_schema import _endpoint_path, make_tool_name

FIND_TOOLS_NAME = "find_tools"
LOAD_TOOLS_NAME = "load_tools"
DISCOVERY_TOOL_NAMES = frozenset({FIND_TOOLS_NAME, LOAD_TOOLS_NAME})

DISCOVERY_PROMPT = (
    "## Discovering more tools\n"
    "You start with the tools most relevant to the request. If you need a "
    "capability none of your current tools provide, call `find_tools` with a "
    "description of what you need, then `load_tools` with the exact tool names "
    "you want to activate. After loading, those tools become callable on the "
    "next turn. Prefer your already-loaded tools; only discover when needed."
)


def _canonical_endpoint(agent_alias: str, endpoint: Dict[str, Any]) -> str:
    explicit = str(endpoint.get("endpoint") or "")
    if explicit:
        return explicit
    return _endpoint_path(
        agent_alias,
        str(endpoint.get("module") or "main"),
        str(endpoint.get("name") or ""),
    )


def discovery_tool_schemas() -> List[Dict[str, Any]]:
    """OpenAI tool schemas for the two discovery meta-tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": FIND_TOOLS_NAME,
                "description": (
                    "Search the full catalog of available tools (agent endpoints) by "
                    "capability when your current tools cannot do what the user needs. "
                    "Returns matching tool names + descriptions; activate them with load_tools."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What capability you need (e.g. 'currency conversion', 'send email').",
                        },
                        "limit": {"type": "integer", "description": "Max results (default 10)."},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": LOAD_TOOLS_NAME,
                "description": (
                    "Activate one or more tools by their exact tool name (from find_tools) so "
                    "you can call them on subsequent turns. Only load tools you intend to use."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exact tool names to activate.",
                        }
                    },
                    "required": ["tool_names"],
                },
            },
        },
    ]


def build_catalog_entries(agent_docs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lightweight searchable index over all endpoints (never sent to the model wholesale).

    Tool names are made UNIQUE (suffixing a short agent id on collision) so the
    names advertised by find_tools always resolve to exactly one endpoint and
    match what load_tools activates.
    """
    entries: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for a in agent_docs or []:
        alias = a.get("agent_alias") or ""
        agent_id = str(a.get("_id") or a.get("id") or "")
        agent_name = a.get("agent_name") or ""
        for ep in a.get("agent_endpoints") or a.get("endpoints") or []:
            if str(ep.get("status", "enabled")).lower() == "disabled":
                continue
            name = ep.get("name") or ""
            if not name or not alias:
                continue
            endpoint = _canonical_endpoint(alias, ep)
            tool_name = make_tool_name(alias, name)
            if tool_name in seen:
                tool_name = make_tool_name(alias, name, agent_id=agent_id, disambiguate=True)
                while tool_name in seen:
                    seen[tool_name] = seen.get(tool_name, 0) + 1
                    tool_name = f"{tool_name}_{seen[tool_name]}"
            seen[tool_name] = seen.get(tool_name, 0)
            entries.append(
                {
                    "tool_name": tool_name,
                    "agent_alias": alias,
                    "agent_id": agent_id,
                    "endpoint": endpoint,
                    "endpoint_name": name,
                    "description": ep.get("description") or "",
                    "agent_name": agent_name,
                }
            )
    return entries


def search_catalog(
    entries: List[Dict[str, Any]],
    query: str,
    *,
    limit: int = 10,
    exclude: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Keyword-score the catalog for a capability query (no embeddings needed)."""
    q = (query or "").lower().strip()
    terms = [t for t in re.split(r"\W+", q) if t]
    exclude = exclude or set()
    scored = []
    for e in entries:
        if e["tool_name"] in exclude:
            continue
        hay = " ".join(
            [
                e.get("tool_name", ""),
                e.get("agent_alias", ""),
                e.get("endpoint_name", ""),
                e.get("description", ""),
                e.get("agent_name", ""),
            ]
        ).lower()
        score = sum(hay.count(t) for t in terms)
        if q and q in hay:
            score += 5
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[: max(1, int(limit or 10))]]


def agent_docs_for_endpoints(
    catalog_docs: List[Dict[str, Any]],
    wanted: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Build per-agent docs containing ONLY the requested endpoints.

    ``wanted`` is a list of {agent_alias, endpoint}. Returns agent doc clones
    suitable for ``build_openai_tools`` so only those endpoints become tools.
    """
    by_alias: Dict[str, Set[str]] = {}
    for w in wanted:
        by_alias.setdefault(w["agent_alias"], set()).add(w["endpoint"])

    out: List[Dict[str, Any]] = []
    for doc in catalog_docs:
        alias = doc.get("agent_alias")
        if alias not in by_alias:
            continue
        eps = doc.get("agent_endpoints") or doc.get("endpoints") or []
        kept = []
        for ep in eps:
            endpoint = _canonical_endpoint(alias, ep)
            if endpoint not in by_alias[alias]:
                continue
            normalized = dict(ep)
            normalized["endpoint"] = endpoint
            kept.append(normalized)
        if not kept:
            continue
        clone = dict(doc)
        clone["agent_endpoints"] = kept
        clone.pop("endpoints", None)
        out.append(clone)
    return out
