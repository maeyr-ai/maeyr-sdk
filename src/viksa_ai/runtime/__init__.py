"""Agent runtime SDK (``ViksaAI.py`` semantics)."""

from viksa_ai.runtime.a2a import (
    _A2A_PAYLOAD_KEY,
    A2A_PAYLOAD_KEY,
    A2AContext,
    _set_envelope,
    _strip_envelope,
    attach_envelope,
    context,
)
from viksa_ai.runtime.auth import ViksaAuth, ViksaAuthError
from viksa_ai.runtime.endpoints import mcp_endpoint
from viksa_ai.runtime.inject import to_module_source

__all__ = [
    "A2AContext",
    "A2A_PAYLOAD_KEY",
    "_A2A_PAYLOAD_KEY",
    "_set_envelope",
    "_strip_envelope",
    "attach_envelope",
    "context",
    "mcp_endpoint",
    "to_module_source",
    "ViksaAuth",
    "ViksaAuthError",
]
