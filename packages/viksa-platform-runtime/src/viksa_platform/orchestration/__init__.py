"""Chat's canonical provider-agnostic orchestration engine.

A provider-agnostic native tool-calling harness shared by
chat-service and volt-engine-service. The model is given real OpenAI ``tools``
derived from Viksa agent endpoints, emits (often parallel) ``tool_calls``, and
the harness executes them concurrently against whatever execution backend the
host service injects (Pulse for chat, task_dispatcher for channels), feeding the
results back as ``tool`` messages until the model returns a final answer.

This package is PURE: it must not import any service-specific modules. Host
services inject behavior via the small callables/protocols in ``protocols.py``.

Source of truth lives in ``devops/shared/orchestration`` and is rsync'd into
each service's ``common/orchestration`` by ``devops/scripts/sync-orchestration.sh``.
"""

from __future__ import annotations

from .budget import (
    BudgetConfig,
    BudgetDecision,
    BudgetPolicy,
    BudgetReason,
    BudgetState,
    TrimResult,
    estimate_messages_tokens,
    estimate_request_tokens,
    estimate_text_tokens,
    estimate_tools_tokens,
    tool_pairs_are_valid,
    trim_messages_for_budget,
    truncate_tool_content,
)
from .discovery import (
    DISCOVERY_PROMPT,
    DISCOVERY_TOOL_NAMES,
    FIND_TOOLS_NAME,
    LOAD_TOOLS_NAME,
    agent_docs_for_endpoints,
    build_catalog_entries,
    discovery_tool_schemas,
    search_catalog,
)
from .harness import OrchestrationHarness
from .interaction import (
    ASK_USER_NAME,
    ASK_USER_PROMPT,
    ask_user_tool_schema,
)
from .protocols import (
    AssistantTurn,
    HarnessEvent,
    HarnessEventType,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from .system_prompt import ORCHESTRATOR_SYSTEM_PROMPT, build_system_prompt
from .thought_display import build_thought_complete_display
from .tool_schema import (
    MAX_OPENAI_TOOLS,
    build_openai_tools,
    cap_tools,
    make_tool_name,
    sanitize_tool_segment,
)

__all__ = [
    "AssistantTurn",
    "HarnessEvent",
    "HarnessEventType",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "OrchestrationHarness",
    "BudgetConfig",
    "BudgetDecision",
    "BudgetPolicy",
    "BudgetReason",
    "BudgetState",
    "TrimResult",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_text_tokens",
    "estimate_tools_tokens",
    "tool_pairs_are_valid",
    "trim_messages_for_budget",
    "truncate_tool_content",
    "ORCHESTRATOR_SYSTEM_PROMPT",
    "build_system_prompt",
    "ASK_USER_NAME",
    "ASK_USER_PROMPT",
    "ask_user_tool_schema",
    "build_openai_tools",
    "cap_tools",
    "MAX_OPENAI_TOOLS",
    "make_tool_name",
    "sanitize_tool_segment",
    # dynamic discovery
    "DISCOVERY_PROMPT",
    "DISCOVERY_TOOL_NAMES",
    "FIND_TOOLS_NAME",
    "LOAD_TOOLS_NAME",
    "discovery_tool_schemas",
    "build_catalog_entries",
    "search_catalog",
    "agent_docs_for_endpoints",
    "build_thought_complete_display",
]
