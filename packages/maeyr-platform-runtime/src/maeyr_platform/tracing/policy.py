"""Positive tenant activity policy; platform HTTP diagnostics use a separate sink.

Classification is derived from semantic events, never a producer supplied
category. Trace-meter bookkeeping is not billable customer activity: tracing
that bookkeeping would recursively create more metering calls.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from .control_plane import is_trace_control_span

TraceCategory = Literal["ai", "billing"]

AI_PREFIXES = (
    "agent.",
    "llm.",
    "gen_ai.",
    "tool.",
    "worker.",
    "workflow.",
    "execution.",
    "eval.",
    "approval.",
    "message.",
    "pulse.",
    "trigger.",
    "schedule.",
    "chat.",
    "slack.",
    "telegram.",
    "teams.",
    "whatsapp.",
    "discord.",
    "gchat.",
    "email.",
    "messenger.",
    "web_widget.",
    "scheduler.",
    "connector.",
    "instagram.",
    "sms.",
    "line.",
    "viber.",
    "google_chat.",
    "googlechat.",
    "webchat.",
)
AI_OPERATIONS = frozenset(
    {
        "llm_call",
        "agent_dispatch",
        "agent_think",
        "agent_act",
        "pulse_invoke",
        "worker_execute",
        "slack_turn",
        "tool_call",
        "tool_result",
        "trigger_fire",
        "schedule_invoke",
        "workflow_execute",
        "eval_run",
        "approval_required",
        "approval_decided",
        "execution_wait",
        "execution_resume",
        "message",
        "connector_receive",
        "connector_authorization",
        "llm_provider_request",
    }
)
EXECUTION_REFS = ("execution_id", "workflow_id", "tool_call_id", "llm_request_id")
AI_ENTITIES = frozenset(
    {
        "agent",
        "execution",
        "workflow",
        "eval",
        "chat",
        "web_chat",
        "ai_chat",
        "mcp",
        "mcp_call",
        "slack",
        "slack_chat",
        "telegram",
        "telegram_chat",
        "teams",
        "teams_chat",
        "whatsapp",
        "whatsapp_chat",
        "discord",
        "discord_chat",
        "gchat",
        "gchat_chat",
        "email",
        "email_chat",
        "trigger",
        "schedule",
    }
)
MCP_ACTIVITY = frozenset({"mcp.tools.call", "mcp.resources.read", "mcp.prompts.get"})
DISCOVERY_SPANS = frozenset(
    {
        "mcp.tools.list",
        "mcp.resources.list",
        "mcp.resources.templates.list",
        "mcp.prompts.list",
        "mcp.initialize",
        "mcp.ping",
        "mcp.discovery",
        "auth.login",
        "marketplace.install",
    }
)
_HTTP = frozenset({"http.server", "http.client"})
_CONTROL = ("/internal/usage", "/internal/license", "/internal/metrics")
_HTTP_EXECUTIONS = {
    "pulse-service": frozenset({"/executor/execute", "/internal/execute"}),
    "headless-service": frozenset({"/headless/execute"}),
    "chat-service": frozenset(
        {
            "/chat/message",
            "/chat/indent_finder",
            "/chat/indent_finder/stream",
            "/chat/improve-role-description",
            "/internal/chat/ask",
            "/internal/chat/run-single",
            "/chat/generate/agent",
            "/chat/fix/agent",
            "/chat/fix/agent/stream",
            "/chat/generate/response-view",
        }
    ),
}
_PLATFORM_HTTP_PEERS = (
    "auth-service",
    "builder-service",
    "chat-service",
    "devspace-service",
    "directory-sync-service",
    "headless-service",
    "marketplace-service",
    "mcp-gateway-service",
    "pulse-service",
    "scheduler-service",
    "trace-service",
    "volt-engine-service",
    "worker-service",
    "serverless-service",
)
_PLATFORM_PEER_PATTERN = "^(?:" + "|".join(_PLATFORM_HTTP_PEERS) + r")(?:\.|$)"


def _execution_http_peer(host: str, route: str, method: str) -> bool:
    return method == "POST" and route in _HTTP_EXECUTIONS.get(host.split(".", 1)[0], ())


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _under(route: str, prefix: str) -> bool:
    return route == prefix or route.startswith(prefix + "/")


def _execution_context(document: Mapping[str, Any]) -> bool:
    refs = document.get("resource_refs")
    if isinstance(refs, Mapping) and any(_present(refs.get(key)) for key in EXECUTION_REFS):
        return True
    return _text(document.get("entity_type")) in AI_ENTITIES


def tenant_trace_category(document: Mapping[str, Any]) -> TraceCategory | None:
    """Retain meaningful billing/AI, including failure and lifecycle updates."""
    name = _text(document.get("span_name"))
    operation = _text(document.get("operation"))
    attrs = document.get("attributes")
    attrs = attrs if isinstance(attrs, Mapping) else {}
    route = _text(attrs.get("http.route"))
    if is_trace_control_span(document) or name in DISCOVERY_SPANS:
        return None
    # Applies to both the server and its caller. Never admit trace-metering
    # telemetry even when an execution context was propagated to the call.
    if attrs.get("billing.resource") == "trace_ingestion_bytes":
        return None
    if name in _HTTP:
        if any(_under(route, prefix) for prefix in _CONTROL):
            return None
        method = _text(attrs.get("http.request.method")) or _text(attrs.get("http.method"))
        if name == "http.server":
            if document.get("_tenant_verified") is not True:
                return None
            if method == "POST" and route in _HTTP_EXECUTIONS.get(
                _text(document.get("service")), ()
            ):
                return "ai"
            return None
        # Tool/provider network dependencies are retained with their execution.
        # Service administration/discovery does not qualify from correlation alone.
        if document.get("_tenant_verified") is False:
            return None
        host = _text(attrs.get("server.address")).lower()
        if re.match(_PLATFORM_PEER_PATTERN, host) and not _execution_http_peer(host, route, method):
            return None
        if _execution_context(document) and _present(document.get("parent_span_id")):
            return "ai"
        return None
    if name.startswith("billing.") or operation == "billing":
        return "billing"
    if name in MCP_ACTIVITY or name.startswith(AI_PREFIXES) or operation in AI_OPERATIONS:
        return "ai"
    # Custom instrumented functions and errors remain visible inside a genuine
    # execution; an account ID or arbitrary category alone is insufficient.
    if _execution_context(document):
        return "ai"
    return None


def ai_trace_expression() -> dict[str, Any]:
    """Aggregation counterpart of the same positive root policy."""
    names = "|".join(prefix.replace(".", r"\.") for prefix in AI_PREFIXES)
    return {
        "$or": [
            {"$eq": ["$trace_category", "ai"]},
            {
                "$regexMatch": {
                    "input": {"$ifNull": ["$top_level_span", ""]},
                    "regex": "^(" + names + ")",
                }
            },
            {"$in": [{"$ifNull": ["$top_level_span", ""]}, sorted(MCP_ACTIVITY)]},
        ]
    }


def billing_trace_expression() -> dict[str, Any]:
    return {
        "$and": [
            {"$not": [ai_trace_expression()]},
            {
                "$or": [
                    {"$eq": ["$trace_category", "billing"]},
                    {
                        "$regexMatch": {
                            "input": {"$ifNull": ["$top_level_span", ""]},
                            "regex": r"^billing\.",
                        }
                    },
                ]
            },
        ]
    }


def ai_trace_match() -> dict[str, Any]:
    """Positive root summary predicate; HTTP-only history is not a run."""
    names = "|".join(prefix.replace(".", r"\.") for prefix in AI_PREFIXES)
    return {
        "$or": [
            {"trace_category": "ai"},
            {"top_level_span": {"$regex": "^(" + names + ")"}},
            {"top_level_span": {"$in": sorted(MCP_ACTIVITY)}},
        ]
    }


def billing_trace_match() -> dict[str, Any]:
    return {
        "$and": [
            {"$nor": [ai_trace_match()]},
            {
                "$or": [
                    {"trace_category": "billing"},
                    {"top_level_span": {"$regex": r"^billing\."}},
                ]
            },
        ]
    }


def tenant_trace_match() -> dict[str, Any]:
    return {"$or": [ai_trace_match(), billing_trace_match()]}


def _string_expression(field: Any) -> dict[str, Any]:
    return {"$cond": [{"$eq": [{"$type": field}, "string"]}, field, ""]}


def _attribute_expression(key: str) -> dict[str, Any]:
    return {
        "$getField": {
            "field": key,
            "input": {
                "$cond": [
                    {"$eq": [{"$type": "$attributes"}, "object"]},
                    "$attributes",
                    {},
                ]
            },
        }
    }


def _present_expression(field: Any) -> dict[str, Any]:
    return {"$ne": [{"$trim": {"input": _string_expression(field)}}, ""]}


def _span_expressions() -> tuple[dict[str, Any], dict[str, Any]]:
    """Native Mongo equivalents of tenant_trace_category, with the same precedence."""
    name = _string_expression("$span_name")
    operation = _string_expression("$operation")
    route = _string_expression(_attribute_expression("http.route"))
    http = {"$in": [name, sorted(_HTTP)]}
    eligible = {
        "$and": [
            {"$not": [{"$in": [name, sorted(DISCOVERY_SPANS)]}]},
            {
                "$ne": [
                    {"$ifNull": [_attribute_expression("billing.resource"), ""]},
                    "trace_ingestion_bytes",
                ]
            },
            {
                "$not": [
                    {
                        "$and": [
                            http,
                            {
                                "$regexMatch": {
                                    "input": route,
                                    "regex": r"^/internal/(usage|license|metrics)(/|$)",
                                }
                            },
                        ]
                    }
                ]
            },
        ]
    }
    billing = {
        "$or": [
            {"$regexMatch": {"input": name, "regex": r"^billing\."}},
            {"$eq": [operation, "billing"]},
        ]
    }
    context = {
        "$or": [
            {"$in": [_string_expression("$entity_type"), sorted(AI_ENTITIES)]},
            *[_present_expression("$resource_refs." + key) for key in EXECUTION_REFS],
        ]
    }
    primary_method = _string_expression(_attribute_expression("http.request.method"))
    methods = {
        "$cond": [
            {"$ne": [primary_method, ""]},
            primary_method,
            _string_expression(_attribute_expression("http.method")),
        ]
    }
    server = {
        "$and": [
            {"$eq": [name, "http.server"]},
            {"$eq": ["$_tenant_verified", True]},
            {"$eq": [methods, "POST"]},
            {
                "$or": [
                    {"$and": [{"$eq": ["$service", service]}, {"$in": [route, sorted(routes)]}]}
                    for service, routes in _HTTP_EXECUTIONS.items()
                ]
            },
        ]
    }
    client = {
        "$and": [
            {"$eq": [name, "http.client"]},
            {"$ne": ["$_tenant_verified", False]},
            context,
            _present_expression("$parent_span_id"),
            {
                "$or": [
                    {
                        "$not": [
                            {
                                "$regexMatch": {
                                    "input": {
                                        "$toLower": _string_expression(
                                            _attribute_expression("server.address")
                                        )
                                    },
                                    "regex": _PLATFORM_PEER_PATTERN,
                                }
                            }
                        ]
                    },
                    {
                        "$and": [
                            {"$eq": [methods, "POST"]},
                            {
                                "$or": [
                                    {
                                        "$and": [
                                            {
                                                "$regexMatch": {
                                                    "input": {
                                                        "$toLower": _string_expression(
                                                            _attribute_expression("server.address")
                                                        )
                                                    },
                                                    "regex": "^" + service + r"(?:\.|$)",
                                                }
                                            },
                                            {"$in": [route, sorted(routes)]},
                                        ]
                                    }
                                    for service, routes in _HTTP_EXECUTIONS.items()
                                ]
                            },
                        ]
                    },
                ]
            },
        ]
    }
    names = "|".join(prefix.replace(".", r"\.") for prefix in AI_PREFIXES)
    semantic = {
        "$and": [
            {"$not": [http]},
            {"$not": [billing]},
            {
                "$or": [
                    {"$in": [name, sorted(MCP_ACTIVITY)]},
                    {"$regexMatch": {"input": name, "regex": "^(" + names + ")"}},
                    {"$in": [operation, sorted(AI_OPERATIONS)]},
                    context,
                ]
            },
        ]
    }
    return (
        {"$and": [eligible, {"$or": [server, client, semantic]}]},
        {"$and": [eligible, {"$not": [http]}, billing]},
    )


def ai_span_expression() -> dict[str, Any]:
    return _span_expressions()[0]


def billing_span_expression() -> dict[str, Any]:
    return _span_expressions()[1]


def tenant_span_match() -> dict[str, Any]:
    ai, billing = _span_expressions()
    return {"$expr": {"$or": [ai, billing]}}
