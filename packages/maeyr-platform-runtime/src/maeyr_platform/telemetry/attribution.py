"""Agent identity helpers for cost and execution telemetry attribution."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

_AGENT_ID_KEYS = ("_id", "id", "agent_id")
_AGENT_ALIAS_KEYS = ("agent_alias", "alias")


def agent_id_from_document(agent: Mapping[str, Any] | None) -> str | None:
    """Return the durable agent id from a catalog or task document.

    Aliases are never used as ids. Cost and trace filters match ``AI-…``
    identifiers, not ``weatherinsightagent``.
    """
    if not agent:
        return None
    for key in _AGENT_ID_KEYS:
        value = agent.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def catalog_agent_ids(agents: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Stable unique agent ids from catalog documents, skipping rows without ``_id``."""
    seen: set[str] = set()
    out: list[str] = []
    for agent in agents or []:
        agent_id = agent_id_from_document(agent)
        if agent_id and agent_id not in seen:
            seen.add(agent_id)
            out.append(agent_id)
    return out


def stamp_catalog_agent_ids(
    agents: Sequence[MutableMapping[str, Any]],
    catalog_docs: Sequence[Mapping[str, Any]] | None,
) -> None:
    """Copy catalog ``_id`` onto in-memory agent docs matched by alias."""
    alias_to_id: dict[str, str] = {}
    for doc in catalog_docs or []:
        agent_id = agent_id_from_document(doc)
        alias = next((str(doc.get(key)).strip() for key in _AGENT_ALIAS_KEYS if doc.get(key)), "")
        if agent_id and alias:
            alias_to_id[alias] = agent_id
    for agent in agents:
        if agent_id_from_document(agent):
            continue
        alias = next(
            (str(agent.get(key)).strip() for key in _AGENT_ALIAS_KEYS if agent.get(key)),
            "",
        )
        if alias and alias in alias_to_id:
            agent["_id"] = alias_to_id[alias]


__all__ = [
    "agent_id_from_document",
    "catalog_agent_ids",
    "stamp_catalog_agent_ids",
]
