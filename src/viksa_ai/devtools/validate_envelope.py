"""A2A envelope validation against endpoint input schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Union

from viksa_ai.models.a2a import A2AEnvelope, A2A_PROTOCOL_VERSION
from viksa_ai.models.agent import AgentEndpoint, AgentInput


def _index_inputs(inputs: List[AgentInput]) -> dict[str, AgentInput]:
    return {inp.name: inp for inp in inputs}


def _validate_value(name: str, value, schema: AgentInput) -> List[str]:
    issues: List[str] = []
    if value is None:
        return issues

    if not schema.type.validate_value(value):
        issues.append(
            f"input '{name}': expected {schema.type.value}, got {type(value).__name__}"
        )
        return issues

    if schema.allowed_values and value not in schema.allowed_values:
        issues.append(
            f"input '{name}': value '{value}' not in allowed_values {schema.allowed_values}"
        )

    if isinstance(value, (int, float)):
        if schema.min_value is not None and value < schema.min_value:
            issues.append(f"input '{name}': value {value} < min_value {schema.min_value}")
        if schema.max_value is not None and value > schema.max_value:
            issues.append(f"input '{name}': value {value} > max_value {schema.max_value}")

    if isinstance(value, str):
        if schema.min_length is not None and len(value) < schema.min_length:
            issues.append(
                f"input '{name}': length {len(value)} < min_length {schema.min_length}"
            )
        if schema.max_length is not None and len(value) > schema.max_length:
            issues.append(
                f"input '{name}': length {len(value)} > max_length {schema.max_length}"
            )

    return issues


def validate_a2a_envelope(
    envelope: Union[A2AEnvelope, dict],
    endpoint: Union[AgentEndpoint, dict],
    agent_inputs: List[Union[AgentInput, dict]],
    *,
    now: Optional[datetime] = None,
) -> List[str]:
    """
    Returns a list of validation issues. Empty list means the envelope passes.
    """
    if isinstance(envelope, dict):
        envelope = A2AEnvelope.model_validate(envelope)
    if isinstance(endpoint, dict):
        endpoint = AgentEndpoint.model_validate(endpoint)
    parsed_inputs = [
        AgentInput.model_validate(i) if isinstance(i, dict) else i for i in agent_inputs
    ]

    issues: List[str] = []
    now = now or datetime.now(timezone.utc)

    if envelope.protocol_version != A2A_PROTOCOL_VERSION:
        issues.append(
            f"unsupported protocol_version {envelope.protocol_version} "
            f"(this platform speaks {A2A_PROTOCOL_VERSION})"
        )

    if endpoint.endpoint and envelope.endpoint != endpoint.endpoint:
        issues.append(
            f"endpoint mismatch: envelope={envelope.endpoint!r} "
            f"but agent endpoint={endpoint.endpoint!r}"
        )

    if envelope.deadline_at is not None:
        deadline = envelope.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline < now:
            issues.append("deadline_at is already in the past")

    payload = envelope.payload or {}
    by_name = _index_inputs(parsed_inputs)

    for ref in endpoint.inputs or []:
        schema = by_name.get(ref.input_ref)
        if schema is None:
            issues.append(
                f"endpoint '{endpoint.endpoint or endpoint.name}' "
                f"references unknown input '{ref.input_ref}'"
            )
            continue
        present = ref.input_ref in payload
        value = payload.get(ref.input_ref)
        if ref.required and (not present or value is None):
            issues.append(f"required input '{ref.input_ref}' is missing")
            continue
        if present:
            issues.extend(_validate_value(ref.input_ref, value, schema))

    return issues
