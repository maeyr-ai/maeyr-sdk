"""Resolve Viksa mapping_id references for MCP tool schemas."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from viksa_ai.client import ViksaClient


def collect_mapping_ids(agent_docs: List[Dict[str, Any]]) -> List[str]:
    """Collect unique mapping_id values from agent manifests."""
    seen: Dict[str, None] = {}

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            seen.setdefault(value.strip(), None)

    for doc in agent_docs:
        for inp in doc.get("inputs") or []:
            if isinstance(inp, dict):
                _add(inp.get("mapping_id"))
        cq = doc.get("chrona_queue")
        if isinstance(cq, dict):
            _add(cq.get("mapping_id"))
        for ep in doc.get("agent_endpoints") or []:
            if not isinstance(ep, dict):
                continue
            ep_cq = ep.get("chrona_queue")
            if isinstance(ep_cq, dict):
                _add(ep_cq.get("mapping_id"))
            for inp in ep.get("inputs") or []:
                if isinstance(inp, dict):
                    _add(inp.get("mapping_id"))

    return list(seen.keys())


def mappings_from_docs(docs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index mapping documents keyed by mapping_id."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        mid = str(doc.get("mapping_id") or doc.get("_id") or doc.get("id") or "")
        if mid:
            by_id[mid] = doc
    return by_id


async def fetch_mappings(client: ViksaClient, mapping_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch mapping documents keyed by mapping_id."""
    if not mapping_ids:
        return {}
    docs = await client.builder.mappings.get_many(mapping_ids)
    by_id: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        mid = str(doc.get("mapping_id") or doc.get("_id") or doc.get("id") or "")
        if mid:
            by_id[mid] = doc
    return by_id


def mapping_hint_text(mapping_doc: Optional[Dict[str, Any]]) -> str:
    """Human-readable mapping context for tool input descriptions."""
    if not mapping_doc:
        return ""
    mapping_type = str(mapping_doc.get("mapping_type") or "")
    body = mapping_doc.get("mapping") or {}
    name = mapping_doc.get("name") or mapping_doc.get("mapping_id") or "mapping"
    if not isinstance(body, dict) or not body:
        return f"Mapping '{name}' ({mapping_type}): (empty)"
    pairs = ", ".join(f"{k}→{json.dumps(v, default=str)}" for k, v in list(body.items())[:12])
    suffix = " …" if len(body) > 12 else ""
    return f"Mapping '{name}' ({mapping_type}): {pairs}{suffix}"


def format_mappings_catalog(mappings: Dict[str, Dict[str, Any]]) -> str:
    """JSON catalog of all mappings for MCP resources / server instructions."""
    if not mappings:
        return ""
    slim = []
    for mid, doc in sorted(mappings.items()):
        slim.append(
            {
                "mapping_id": mid,
                "name": doc.get("name"),
                "mapping_type": doc.get("mapping_type"),
                "mapping": doc.get("mapping") or {},
            }
        )
    return json.dumps(slim, indent=2, default=str)
