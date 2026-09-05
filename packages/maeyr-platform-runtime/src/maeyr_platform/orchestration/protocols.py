"""Canonical data types and protocols for orchestration.

These are intentionally lightweight (dataclasses + Protocols) so the harness has
zero coupling to any service. Host services supply two callables:

* ``llm_call(messages, tools) -> AssistantTurn`` — one native tool-calling LLM
  round (must pass ``tools``/``tool_choice``/``parallel_tool_calls`` through to
  the provider).
* ``run_tool(tool_call) -> AsyncGenerator[HarnessEvent]`` — execute one tool
  call against the service's backend, yielding optional passthrough events and
  exactly one terminal ``TOOL_RESULT``, ``APPROVAL_REQUIRED``, or
  ``USER_INPUT_REQUIRED`` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)


@dataclass
class ToolSpec:
    """Execution metadata for one Maeyr endpoint exposed as an LLM tool.

    Mirrors the shape produced by ``mcp-gateway-service`` so internal
    orchestration and the external MCP gateway advertise identical tools.
    """

    name: str  # tool name surfaced to the model (``{alias}_{endpoint_name}``)
    agent_id: str
    agent_alias: str
    agent_type: str  # "cloud" | "secure"
    endpoint: str  # full endpoint path (agent.module.function)
    endpoint_name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema for the function arguments
    chrona_queue: Any = None  # raw agent chrona_queue (str | dict | None)
    read_only: Optional[bool] = None
    destructive: Optional[bool] = None

    def to_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class ToolCall:
    """A single tool call requested by the model."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""  # original JSON string (for echoing back to the model)


@dataclass
class ToolResult:
    """The outcome of executing one tool call, fed back to the model."""

    call_id: str
    name: str
    content: str  # JSON-serialized payload the model will read
    is_error: bool = False
    raw: Any = None  # structured payload for host-side bookkeeping


@dataclass
class AssistantTurn:
    """One assistant response from a native tool-calling LLM round."""

    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    # Provider message dict to append verbatim to the running message list
    # (must include role="assistant" and any tool_calls in provider format).
    message: Dict[str, Any] = field(default_factory=dict)
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: Optional[str] = None
    fallback_used: bool = False
    finish_reason: Optional[str] = None


class HarnessEventType:
    """String constants for harness lifecycle events (host maps to its own UI)."""

    ITERATION_START = "iteration_start"
    MODEL_RESPONSE = "model_response"  # assistant turn produced (content/tool_calls)
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_RESULT = "tool_result"  # terminal per-tool event from run_tool
    APPROVAL_REQUIRED = "approval_required"  # terminal per-tool event; pauses run
    USER_INPUT_REQUIRED = "user_input_required"  # terminal per-tool event; pauses run
    PASSTHROUGH = "passthrough"  # opaque host event forwarded as-is
    FINAL = "final"  # model returned content with no tool calls
    MAX_ITERATIONS = "max_iterations"
    BUDGET_EVENT = "budget_event"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"


@dataclass
class HarnessEvent:
    """A single event emitted by (or routed through) the harness."""

    type: str
    # Generic payload bag. Common keys:
    #   tokens_used, content, tool_call, tool_result, iteration, max_iterations,
    #   stream_event (opaque host event for PASSTHROUGH), approval (host payload),
    #   input_request (host payload), messages (snapshot for pause/resume).
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passthrough(cls, stream_event: Any) -> "HarnessEvent":
        return cls(HarnessEventType.PASSTHROUGH, {"stream_event": stream_event})

    @classmethod
    def tool_result(cls, result: ToolResult) -> "HarnessEvent":
        return cls(HarnessEventType.TOOL_RESULT, {"tool_result": result})

    @classmethod
    def approval_required(cls, tool_call: ToolCall, approval: Any) -> "HarnessEvent":
        return cls(
            HarnessEventType.APPROVAL_REQUIRED,
            {"tool_call": tool_call, "approval": approval},
        )

    @classmethod
    def user_input_required(cls, tool_call: ToolCall, input_request: Any) -> "HarnessEvent":
        return cls(
            HarnessEventType.USER_INPUT_REQUIRED,
            {"tool_call": tool_call, "input_request": input_request},
        )


# --- Injected behavior signatures -------------------------------------------

LLMCall = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Awaitable[AssistantTurn]]
RunTool = Callable[[ToolCall], AsyncGenerator[HarnessEvent, None]]


@runtime_checkable
class ToolExecutor(Protocol):
    """Optional class form of ``run_tool`` for hosts that prefer objects."""

    def __call__(self, tool_call: ToolCall) -> AsyncGenerator[HarnessEvent, None]: ...
