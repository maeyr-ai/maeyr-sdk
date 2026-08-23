from __future__ import annotations

from viksa_platform.telemetry import (
    agent_id_from_document,
    catalog_agent_ids,
    stamp_catalog_agent_ids,
)


def test_agent_id_from_document_prefers_durable_id_over_alias() -> None:
    assert agent_id_from_document({"_id": "AI-1", "agent_alias": "weather"}) == "AI-1"
    assert agent_id_from_document({"id": "AI-2"}) == "AI-2"
    assert agent_id_from_document({"agent_id": "AI-3"}) == "AI-3"
    assert agent_id_from_document({"agent_alias": "weatherinsightagent"}) is None
    assert agent_id_from_document({}) is None
    assert agent_id_from_document(None) is None


def test_catalog_agent_ids_skips_projected_out_ids_and_dedupes() -> None:
    assert catalog_agent_ids(
        [
            {"agent_alias": "weatherinsightagent"},
            {"_id": "AI-A1", "agent_alias": "weatherinsightagent"},
            {"_id": "AI-A1", "agent_alias": "weatherinsightagent"},
            {"_id": "AI-B2", "agent_alias": "timezone"},
        ]
    ) == ["AI-A1", "AI-B2"]
    assert catalog_agent_ids([{"agent_alias": "weatherinsightagent"}]) == []
    assert catalog_agent_ids(None) == []


def test_stamp_catalog_agent_ids_fills_missing_ids_by_alias() -> None:
    agents = [
        {"agent_alias": "weatherinsightagent"},
        {"_id": "AI-KEEP", "agent_alias": "timezone"},
    ]
    stamp_catalog_agent_ids(
        agents,
        [
            {"_id": "AI-WEATHER", "agent_alias": "weatherinsightagent"},
            {"_id": "AI-TZ", "agent_alias": "timezone"},
        ],
    )
    assert agents[0]["_id"] == "AI-WEATHER"
    assert agents[1]["_id"] == "AI-KEEP"
