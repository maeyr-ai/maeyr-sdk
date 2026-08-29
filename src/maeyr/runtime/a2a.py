from typing import Any, Dict, Optional, TypedDict

A2A_PAYLOAD_KEY = "__maeyr_a2a__"

_A2A_PAYLOAD_KEY = A2A_PAYLOAD_KEY
_A2A_ENVELOPE: Dict[str, Any] = {}


class A2AContext(TypedDict, total=False):
    """Common keys in the per-call A2A envelope (``context()``)."""

    protocol_version: int
    run_id: str
    parent_step_id: str
    caller_agent: str
    callee_agent: str
    endpoint: str
    idempotency_key: str
    deadline_at: str
    metadata: Dict[str, Any]


def attach_envelope(
    inputs: Dict[str, Any],
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach an A2A envelope to workflow inputs under the reserved key."""
    out = dict(inputs)
    out[A2A_PAYLOAD_KEY] = envelope
    return out


def _set_envelope(envelope: Optional[Dict[str, Any]]) -> None:
    """Internal: importer-side hook to install the per-call envelope."""
    global _A2A_ENVELOPE
    _A2A_ENVELOPE = dict(envelope or {})


def _strip_envelope(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Internal: importer-side hook to pop the envelope key from inputs."""
    if not isinstance(inputs, dict):
        return inputs
    envelope = inputs.pop(_A2A_PAYLOAD_KEY, None)
    if envelope is not None:
        _set_envelope(envelope)
    return inputs


def context() -> Dict[str, Any]:
    """Returns the A2A envelope for the current call (or {} if absent).

    Example:
        ctx = maeyr.context()
        run_id = ctx.get("run_id")
        parent = ctx.get("parent_step_id")
    """
    return dict(_A2A_ENVELOPE)
