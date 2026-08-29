# ruff: noqa: E501
"""Build canonical user-visible thought displays for tool-calling turns.

The legacy embedded-JSON loop had the model write these fields explicitly.
Native ``tool_calls`` usually leave ``content`` empty, so we synthesize a
concise, tenant-safe narrative from the harness thread, registry, and the
model's optional prose.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .discovery import FIND_TOOLS_NAME, LOAD_TOOLS_NAME
from .interaction import ASK_USER_NAME
from .protocols import ToolCall

_MAX_OBS_CHARS = 1200
_MAX_REASON_CHARS = 900
_MAX_PLAN_STEPS = 8
_MAX_ARG_PREVIEW = 100


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _brief_value(value: Any, *, limit: int = 60) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, default=str)
    else:
        raw = str(value)
    raw = re.sub(r"\s+", " ", raw).strip()
    return _truncate(raw, limit)


def _format_arguments(arguments: Mapping[str, Any]) -> str:
    if not arguments:
        return "(no inputs)"
    parts = [f"{k}={_brief_value(v)}" for k, v in arguments.items()]
    joined = ", ".join(parts)
    return _truncate(joined, _MAX_ARG_PREVIEW)


def _latest_user_query(messages: Sequence[Mapping[str, Any]], fallback: str) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return (fallback or "").strip()


def _summarize_tool_message(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        return "Tool returned an empty result."
    text = content.strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _truncate(text, 280)

    if not isinstance(data, dict):
        return _truncate(text, 280)

    if data.get("error"):
        return f"Tool error: {_brief_value(data.get('error'), limit=200)}"

    if data.get("matches") and isinstance(data["matches"], list):
        names = [
            str(m.get("tool_name") or m.get("name") or "")
            for m in data["matches"][:5]
            if isinstance(m, dict)
        ]
        names = [n for n in names if n]
        if names:
            return f"Catalog search returned: {', '.join(names)}."
        return "Catalog search returned no matches."

    if data.get("activated") and isinstance(data["activated"], list):
        activated = [str(x) for x in data["activated"][:5] if x]
        if activated:
            return f"Activated tools: {', '.join(activated)}."

    if data.get("question"):
        return f"Asked user: {_brief_value(data.get('question'), limit=200)}"

    preview_keys = []
    for key in ("status", "health", "cluster", "message", "summary", "result", "output"):
        if key in data and data[key] not in (None, "", [], {}):
            preview_keys.append(key)
    if not preview_keys:
        preview_keys = list(data.keys())[:5]

    parts = [f"{k}={_brief_value(data[k])}" for k in preview_keys[:5]]
    return "Tool result: " + ", ".join(parts)


def _collect_tool_observations(
    messages: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
) -> List[str]:
    summaries: List[str] = []
    for msg in reversed(messages or []):
        if msg.get("role") != "tool":
            continue
        summaries.append(_summarize_tool_message(msg.get("content")))
        if len(summaries) >= limit:
            break
    summaries.reverse()
    return summaries


def _registry_label(call: ToolCall, registry: Mapping[str, Any]) -> str:
    spec = registry.get(call.name)
    if spec is None:
        return call.name.replace("_", " ")
    agent = getattr(spec, "agent_alias", None) or (
        spec.get("agent_alias") if isinstance(spec, dict) else None
    )
    endpoint_name = getattr(spec, "endpoint_name", None) or (
        spec.get("endpoint_name") if isinstance(spec, dict) else None
    )
    if agent and endpoint_name:
        return f"{agent}.{endpoint_name}"
    return call.name.replace("_", " ")


def _plan_step_for_call(call: ToolCall, registry: Mapping[str, Any]) -> str:
    args = call.arguments or {}
    if call.name == FIND_TOOLS_NAME:
        query = str(args.get("query") or "").strip() or "additional capability"
        return f"Search the tool catalog for “{query}”."
    if call.name == LOAD_TOOLS_NAME:
        names = list(args.get("tool_names") or [])
        if names:
            shown = ", ".join(str(n) for n in names[:4])
            extra = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
            return f"Activate tool(s): {shown}{extra}."
        return "Activate additional tools from the catalog."
    if call.name == ASK_USER_NAME:
        question = str(args.get("question") or "").strip() or "Need more information from the user."
        return f"Ask the user: {question}"
    label = _registry_label(call, registry)
    return f"Run {label} with {_format_arguments(args)}."


def _reasoning_for_calls(
    *,
    user_query: str,
    tool_calls: Sequence[ToolCall],
    registry: Mapping[str, Any],
    assistant_content: Optional[str],
) -> str:
    content = (assistant_content or "").strip()
    if content:
        return _truncate(content, _MAX_REASON_CHARS)

    if not tool_calls:
        return "I have enough information to provide the final answer."

    agent_calls = [
        c for c in tool_calls if c.name not in (FIND_TOOLS_NAME, LOAD_TOOLS_NAME, ASK_USER_NAME)
    ]
    meta_calls = [c for c in tool_calls if c not in agent_calls]

    goal = _truncate(user_query, 220) if user_query else "the user's goal"

    if len(agent_calls) == 1 and not meta_calls:
        call = agent_calls[0]
        spec = registry.get(call.name)
        description = ""
        if spec is not None:
            description = (
                getattr(spec, "description", None)
                or (spec.get("description") if isinstance(spec, dict) else None)
                or ""
            ).strip()
        label = _registry_label(call, registry)
        args_text = _format_arguments(call.arguments or {})
        if description:
            return _truncate(
                f"To accomplish “{goal}”, use {label}. {description} Proceeding with {args_text}.",
                _MAX_REASON_CHARS,
            )
        return _truncate(
            f"To accomplish “{goal}”, call {label} with {args_text}.",
            _MAX_REASON_CHARS,
        )

    if agent_calls and len(agent_calls) > 1 and not meta_calls:
        labels = [_registry_label(c, registry) for c in agent_calls[:4]]
        joined = ", ".join(labels)
        extra = f" and {len(agent_calls) - 4} more" if len(agent_calls) > 4 else ""
        return _truncate(
            f"“{goal}” needs {len(agent_calls)} independent lookups in parallel: {joined}{extra}.",
            _MAX_REASON_CHARS,
        )

    parts: List[str] = []
    if meta_calls:
        for call in meta_calls:
            if call.name == FIND_TOOLS_NAME:
                q = str((call.arguments or {}).get("query") or "").strip()
                parts.append(
                    f"search the catalog for “{q or 'more capability'}”"
                    if q
                    else "search the catalog for additional tools"
                )
            elif call.name == LOAD_TOOLS_NAME:
                parts.append("load the selected tools for the next step")
            elif call.name == ASK_USER_NAME:
                q = str((call.arguments or {}).get("question") or "").strip()
                parts.append(f"ask the user: {q}" if q else "ask the user for clarification")
    if agent_calls:
        parts.append(
            f"run {len(agent_calls)} agent action(s)"
            + (" in parallel" if len(agent_calls) > 1 else "")
        )
    action = "; then ".join(parts) if parts else "continue the next orchestration step"
    return _truncate(f"For “{goal}”, I will {action}.", _MAX_REASON_CHARS)


def build_thought_complete_display(
    *,
    messages: Sequence[Mapping[str, Any]],
    user_query: str,
    tool_calls: Sequence[ToolCall],
    assistant_content: Optional[str],
    registry: Mapping[str, Any],
    active_tool_count: int,
    iteration: int,
) -> Dict[str, Any]:
    """Return observation, reasoning, plan, and action_type for THOUGHT_COMPLETE."""
    query = _latest_user_query(messages, user_query)
    prior = _collect_tool_observations(messages)

    if prior:
        observation = "From the previous step(s):\n" + "\n".join(f"- {line}" for line in prior)
    elif iteration <= 1:
        scope = (
            f"{active_tool_count} tool(s) are in scope for this run."
            if active_tool_count
            else "No agent tools are loaded yet."
        )
        observation = f'The user requested: "{_truncate(query, 300)}". {scope}' if query else scope
    else:
        observation = (
            f'Continuing work on: "{_truncate(query, 300)}".'
            if query
            else "Continuing the orchestration loop with the latest context."
        )
    observation = _truncate(observation, _MAX_OBS_CHARS)

    if tool_calls:
        if any(c.name == ASK_USER_NAME for c in tool_calls):
            action_type = "ask_user"
        else:
            action_type = "execute_task"
        plan = [_plan_step_for_call(c, registry) for c in tool_calls][:_MAX_PLAN_STEPS]
        reasoning = _reasoning_for_calls(
            user_query=query,
            tool_calls=tool_calls,
            registry=registry,
            assistant_content=assistant_content,
        )
    else:
        action_type = "complete"
        if prior:
            plan = ["Review the gathered results.", "Provide a clear final answer to the user."]
        else:
            plan = ["Respond directly without calling additional tools."]
        reasoning = _reasoning_for_calls(
            user_query=query,
            tool_calls=[],
            registry=registry,
            assistant_content=assistant_content,
        )

    return {
        "observation": observation,
        "reasoning": reasoning,
        "plan": plan,
        "action_type": action_type,
    }
