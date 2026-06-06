from viksa_ai.mcp_bridge.registry import build_registry_from_docs


def test_build_registry_from_docs_minimal():
    agent_docs = [
        {
            "_id": "AI-1",
            "agent_alias": "demo",
            "agent_name": "Demo",
            "agent_type": "cloud",
            "inputs": [],
            "outputs": [],
            "agent_endpoints": [
                {
                    "name": "ping",
                    "module": "main",
                    "status": "enabled",
                    "description": "Ping",
                    "inputs": [],
                    "outputs": [],
                }
            ],
        }
    ]
    registry = build_registry_from_docs(agent_docs, [], org_id="OI-1", project_id="PI-1")
    assert "demo_ping" in registry.tools
    assert len(registry.agents) == 1
