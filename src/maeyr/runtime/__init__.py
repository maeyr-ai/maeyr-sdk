"""Agent runtime SDK (``Maeyr.py`` semantics)."""

from maeyr.runtime.a2a import (
    _A2A_PAYLOAD_KEY,
    A2A_PAYLOAD_KEY,
    A2AContext,
    _set_envelope,
    _strip_envelope,
    attach_envelope,
    context,
)
from maeyr.runtime.auth import MaeyrAuth, MaeyrAuthError
from maeyr.runtime.endpoints import mcp_endpoint
from maeyr.runtime.inject import to_module_source

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
    "MaeyrAuth",
    "MaeyrAuthError",
]
