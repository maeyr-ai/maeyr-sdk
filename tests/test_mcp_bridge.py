"""Tests for Viksa MCP bridge tool mapping and discovery."""

from __future__ import annotations

import httpx
import pytest

from viksa_ai.client import ViksaClient
from viksa_ai.mcp_bridge.discovery import BridgeTarget
from viksa_ai.mcp_bridge.mappings import collect_mapping_ids, mapping_hint_text
from viksa_ai.mcp_bridge.registry import AgentMeta, BridgeRegistry, build_registry
from viksa_ai.mcp_bridge.tools import (
    agent_doc_to_tools,
    make_tool_name,
    resolve_task_queue,
    sanitize_tool_segment,
    structured_execution_result,
)

SAMPLE_AGENT = {
    "_id": "agent-123",
    "agent_alias": "github_mcp_agent",
    "agent_name": "GitHub MCP Agent",
    "agent_type": "cloud",
    "ai_guidelines": "Prefer read-only endpoints unless the user asks to mutate.",
    "inputs": [
        {
            "name": "repo",
            "type": "string",
            "description": "GitHub repository owner/name",
            "mapping_id": "repos",
            "ai_note": "Use mapping shortcuts when the user names an environment.",
        },
        {
            "name": "limit",
            "type": "integer",
            "default": 10,
        },
    ],
    "outputs": [
        {"name": "issues", "type": "list", "description": "Matching GitHub issues"},
    ],
    "agent_endpoints": [
        {
            "name": "list_issues",
            "module": "main",
            "description": "List issues from a GitHub repo",
            "status": "enabled",
            "inputs": [
                {"input_ref": "repo", "required": True},
                {"input_ref": "limit", "required": False},
            ],
            "outputs": ["issues"],
            "annotations": {"readOnly": True},
            "execution_config": {"timeout_seconds": 120},
        },
        {
            "name": "post_comment",
            "module": "main",
            "description": "Post a comment on an issue",
            "status": "disabled",
            "inputs": [{"input_ref": "repo", "required": True}],
            "annotations": {"destructive": True},
        },
    ],
}

MAPPINGS = {
    "repos": {
        "mapping_id": "repos",
        "name": "repos",
        "mapping_type": "inputs",
        "mapping": {"prod": "viksa-ai/platform", "staging": "viksa-ai/staging"},
    }
}


def test_make_tool_name_uses_allowed_characters():
    assert make_tool_name("my_agent", "list_items") == "my_agent_list_items"
    assert make_tool_name("github_mcp_agent", "list_issues") == "github_mcp_agent_list_issues"
    assert sanitize_tool_segment("bad name!") == "bad_name"


def test_make_tool_name_disambiguates_with_agent_id():
    name = make_tool_name("dup", "run", agent_id="agent-abc-999", disambiguate=True)
    assert name.endswith("agentabc999") or "999" in name
    assert name.startswith("dup_run_")


def test_agent_doc_to_tools_skips_disabled_and_builds_schemas():
    tools = agent_doc_to_tools(SAMPLE_AGENT, mappings_by_id=MAPPINGS)
    assert len(tools) == 1
    spec = tools[0]
    assert spec.mcp_name == "github_mcp_agent_list_issues"
    assert spec.endpoint == "github_mcp_agent.main.list_issues"
    assert spec.timeout == 120
    assert spec.read_only is True
    assert spec.output_schema is not None
    assert "issues" in spec.output_schema["properties"]
    repo_prop = spec.input_schema["properties"]["repo"]
    assert "Mapping 'repos'" in repo_prop["description"]
    assert "Note:" in repo_prop["description"]
    assert spec.input_schema["required"] == ["repo"]


def test_resolve_task_queue_cloud_agent():
    assert (
        resolve_task_queue(
            agent_type="cloud",
            org_id="OI-ORG",
            project_id="PI-PROJ",
        )
        == "OI-ORG-PI-PROJ-CLOUD"
    )


def test_resolve_task_queue_secure_agent_uses_prefixed_queue():
    assert (
        resolve_task_queue(
            agent_type="secure",
            chrona_queue={"chrona_queues": ["worker-a"]},
            org_id="OI-ORG",
            project_id="PI-PROJ",
        )
        == "OI-ORG-PI-PROJ-worker-a"
    )


def test_agent_doc_to_tools_sets_prefixed_task_queue():
    doc = {
        **SAMPLE_AGENT,
        "agent_type": "cloud",
        "chrona_queue": {"chrona_queues": ["ignored-for-cloud"]},
    }
    tools = agent_doc_to_tools(
        doc,
        org_id="OI-ORG",
        project_id="PI-PROJ",
    )
    assert tools[0].task_queue == "OI-ORG-PI-PROJ-CLOUD"


def test_structured_execution_result_from_agent_dict():
    schema = {
        "type": "object",
        "properties": {"flights": {"type": "array"}},
        "required": ["flights"],
    }
    payload = {"flights": [{"flight": "AI101"}]}
    assert structured_execution_result(payload, schema) == payload


def test_structured_execution_result_unwraps_response_wrapper():
    schema = {
        "type": "object",
        "properties": {"flights": {"type": "array"}},
        "required": ["flights"],
    }
    payload = {"response": {"flights": []}, "meta": "ignored"}
    assert structured_execution_result(payload, schema) == {"flights": []}


def test_structured_execution_result_parses_json_string():
    schema = {
        "type": "object",
        "properties": {"issues": {"type": "array"}},
        "required": ["issues"],
    }
    payload = '{"issues": [{"id": 1}]}'
    assert structured_execution_result(payload, schema) == {"issues": [{"id": 1}]}


def test_collect_mapping_ids_from_agent():
    ids = collect_mapping_ids([SAMPLE_AGENT])
    assert ids == ["repos"]


def test_mapping_hint_text():
    hint = mapping_hint_text(MAPPINGS["repos"])
    assert "prod" in hint
    assert "viksa-ai/platform" in hint


def test_registry_disambiguates_colliding_tools():
    doc_a = {**SAMPLE_AGENT, "_id": "agent-a", "agent_alias": "shared_agent"}
    doc_b = {
        **SAMPLE_AGENT,
        "_id": "agent-b",
        "agent_alias": "shared_agent",
        "agent_endpoints": [
            {
                **SAMPLE_AGENT["agent_endpoints"][0],
                "name": "list_issues",
            }
        ],
    }
    specs = agent_doc_to_tools(doc_a) + agent_doc_to_tools(doc_b)
    registry = BridgeRegistry()
    assigned = registry.assign_tool_names(specs)
    assert len(assigned) == 2
    assert len({s.mcp_name for s in assigned.values()}) == 2


def test_registry_build_instructions_includes_guidelines():
    registry = BridgeRegistry()
    registry.agents = {
        "agent-123": AgentMeta(
            agent_id="agent-123",
            agent_alias="github_mcp_agent",
            agent_name="GitHub MCP Agent",
            ai_guidelines="Be careful with writes.",
        )
    }
    text = registry.build_instructions()
    assert "Be careful with writes." in text
    assert "github_mcp_agent" in text


@pytest.mark.asyncio
async def test_build_registry_by_agent_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/builder/agent/agent-123"):
            return httpx.Response(200, json=SAMPLE_AGENT)
        if request.url.path.endswith("/builder/mappings/repos"):
            return httpx.Response(
                200,
                json={
                    "_id": "repos",
                    "name": "repos",
                    "mapping_type": "inputs",
                    "mapping": {"prod": "viksa-ai/platform"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", org_id="o1", project_id="p1", base_url="https://api.test")
        client._transport._async_client = http
        registry = await build_registry(client, BridgeTarget(agent_id="agent-123"))
        assert "github_mcp_agent_list_issues" in registry.tools
        assert registry.mappings_catalog
        assert registry.agents["agent-123"].ai_guidelines


@pytest.mark.asyncio
async def test_build_registry_all_deployed():
    list_payload = {
        "agents": [
            {
                "_id": "agent-123",
                "agent_alias": "github_mcp_agent",
                "deploy_status": "deployed",
                "agent_status": "enabled",
            },
            {
                "_id": "agent-skip",
                "agent_alias": "draft_agent",
                "deploy_status": "not_deployed",
                "agent_status": "enabled",
            },
        ],
        "total": 2,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/builder/agent/list"):
            return httpx.Response(200, json=list_payload)
        if request.url.path.endswith("/builder/agent/agent-123"):
            return httpx.Response(200, json=SAMPLE_AGENT)
        if request.url.path.endswith("/builder/mappings/repos"):
            return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", org_id="o1", project_id="p1", base_url="https://api.test")
        client._transport._async_client = http
        registry = await build_registry(client, BridgeTarget(all_deployed=True))
        assert len(registry.tools) == 1
