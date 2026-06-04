import pytest

from viksa_ai.devtools import AgentValidationError, validate_agent_manifest, validate_a2a_envelope
from viksa_ai.models.a2a import A2AEnvelope
from viksa_ai.models.agent import AgentEndpoint, AgentInput, EndpointInputRef, InputType

VALID_MANIFEST = {
    "agent_name": "Test Agent",
    "agent_description": "desc",
    "files": [
        {
            "name": "main.py",
            "mime_type": "python",
            "content": '''
from typing import Any, Dict
from viksa_ai.runtime import mcp_endpoint

@mcp_endpoint("echo")
async def echo(payload: Dict[str, Any]):
    return {"out": payload.get("msg")}
''',
        }
    ],
    "inputs": [{"name": "msg", "type": "string"}],
    "outputs": [{"name": "out", "type": "string"}],
    "agent_endpoints": [
        {
            "name": "echo",
            "module": "main",
            "description": "echo",
            "inputs": [{"input_ref": "msg", "required": True}],
            "outputs": ["out"],
        }
    ],
}


def test_validate_agent_manifest_ok():
    validate_agent_manifest(VALID_MANIFEST)


def test_validate_agent_manifest_missing_endpoint():
    bad = {**VALID_MANIFEST, "agent_endpoints": []}
    with pytest.raises(AgentValidationError):
        validate_agent_manifest(bad)


def test_validate_a2a_envelope_ok():
    issues = validate_a2a_envelope(
        A2AEnvelope(
            run_id="r1",
            callee_agent="test",
            endpoint="test.main.echo",
            payload={"msg": "hi"},
        ),
        AgentEndpoint(
            name="echo",
            module="main",
            endpoint="test.main.echo",
            description="d",
            inputs=[EndpointInputRef(input_ref="msg", required=True)],
        ),
        [AgentInput(name="msg", type=InputType.STRING)],
    )
    assert issues == []


def test_validate_a2a_missing_required():
    issues = validate_a2a_envelope(
        A2AEnvelope(
            run_id="r1",
            callee_agent="test",
            endpoint="test.main.echo",
            payload={},
        ),
        AgentEndpoint(
            name="echo",
            module="main",
            endpoint="test.main.echo",
            description="d",
            inputs=[EndpointInputRef(input_ref="msg", required=True)],
        ),
        [AgentInput(name="msg", type=InputType.STRING)],
    )
    assert any("required" in i for i in issues)
