"""Trace-context propagation through Temporal workflow payloads."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, cast

TRACE_PAYLOAD_KEY = "__platform_trace"


def _active_compatibility_context() -> object | None:
    """Read the service context lazily while supporting standalone SDK use."""
    try:
        module = import_module("common.platform_traces.context")
        getter = cast(Callable[[], object | None], getattr(module, "get_trace_context"))
        return getter()
    except (AttributeError, ModuleNotFoundError):
        from viksa_platform.tracing.context import get_trace_context

        return get_trace_context()


def enrich_workflow_inputs(inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Attach the active trace context to workflow inputs without blocking."""
    payload = dict(inputs or {})
    context = _active_compatibility_context()
    if context is None:
        return payload
    payload[TRACE_PAYLOAD_KEY] = {
        "trace_id": getattr(context, "trace_id", None),
        "parent_span_id": getattr(context, "span_id", None),
        "activity_id": getattr(context, "activity_id", None),
        "account_id": getattr(context, "account_id", None),
        "org_id": getattr(context, "org_id", None),
        "project_id": getattr(context, "project_id", None),
    }
    return payload


def split_trace_payload(payload: Any) -> tuple[Any, dict[str, Any] | None]:
    """Strip and return the internal trace envelope from an agent payload."""
    if not isinstance(payload, dict) or TRACE_PAYLOAD_KEY not in payload:
        return payload, None
    agent_payload = dict(payload)
    trace_metadata = agent_payload.pop(TRACE_PAYLOAD_KEY, None)
    return agent_payload, trace_metadata if isinstance(trace_metadata, dict) else None


__all__ = ["TRACE_PAYLOAD_KEY", "enrich_workflow_inputs", "split_trace_payload"]
