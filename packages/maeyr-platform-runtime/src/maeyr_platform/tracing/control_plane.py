"""Keep trace delivery and its authorization out of tenant trace metering."""

from collections.abc import Mapping
from typing import Any


def is_trace_control_request(service: object, route: object) -> bool:
    """Match service-owned namespaces, without excluding customer API paths."""
    if not isinstance(route, str):
        return False
    prefixes = {
        "auth-service": ("/internal/license", "/internal/usage"),
        "trace-service": ("/internal/metrics",),
    }.get(service if isinstance(service, str) else "", ())
    return any(route == prefix or route.startswith(prefix + "/") for prefix in prefixes)


def is_trace_control_span(document: Mapping[str, Any]) -> bool:
    """Recognize queued HTTP server spans from the same control-plane routes."""
    attributes = document.get("attributes")
    return (
        document.get("span_name") == "http.server"
        and isinstance(attributes, Mapping)
        and is_trace_control_request(document.get("service"), attributes.get("http.route"))
    )
