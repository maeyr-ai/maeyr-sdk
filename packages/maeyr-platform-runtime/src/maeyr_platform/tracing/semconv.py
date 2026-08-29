"""OpenTelemetry-aligned semantic conventions shared across platform traces."""

from __future__ import annotations

from maeyr_platform.tracing.constants import (
    SPAN_AGENT_ACT,
    SPAN_AGENT_STEP,
    SPAN_AGENT_THINK,
    SPAN_APPROVAL_DECIDED,
    SPAN_APPROVAL_REQUIRED,
    SPAN_ERROR,
    SPAN_EVAL_RUN,
    SPAN_EXECUTION_RESUME,
    SPAN_EXECUTION_WAIT,
    SPAN_HTTP_CLIENT,
    SPAN_HTTP_SERVER,
    SPAN_LLM_CHAT,
    SPAN_LLM_JSON,
    SPAN_MARKETPLACE_INSTALL,
    SPAN_MESSAGE_RECEIVE,
    SPAN_PULSE_INVOKE,
    SPAN_SCHEDULE_INVOKE,
    SPAN_SLACK_TURN,
    SPAN_TOOL_CALL,
    SPAN_TOOL_RESULT,
    SPAN_TRIGGER_FIRE,
    SPAN_WORKER_EXECUTE,
    SPAN_WORKFLOW_EXECUTE,
    SpanOperation,
)

ATTR_SERVICE_NAME = "service.name"
ATTR_ERROR_TYPE = "error.type"
ATTR_ERROR_MESSAGE = "error.message"
ATTR_ERROR_STACK = "error.stack"
ATTR_CODE_FILEPATH = "code.filepath"
ATTR_CODE_FUNCTION = "code.function"
ATTR_CODE_LINENO = "code.lineno"
ATTR_HTTP_METHOD = "http.request.method"
ATTR_HTTP_ROUTE = "http.route"
ATTR_HTTP_STATUS = "http.response.status_code"
ATTR_GEN_AI_MODEL = "gen_ai.request.model"
ATTR_GEN_AI_OPERATION = "gen_ai.operation.name"
ATTR_GEN_AI_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
ATTR_GEN_AI_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"

ATTR_TOOL_NAME = "tool.name"
ATTR_TOOL_INPUT = "tool.input.summary"
ATTR_TOOL_OUTPUT = "tool.output.summary"
ATTR_TOOL_TASK_QUEUE = "tool.task_queue"
ATTR_TOOL_EXECUTION_MODE = "tool.execution_mode"
ATTR_TRIGGER_ID = "trigger.id"
ATTR_TRIGGER_PAYLOAD_HASH = "trigger.payload_hash"
ATTR_EXECUTION_ID = "execution.id"

SPAN_NAME_TO_OPERATION: dict[str, str] = {
    SPAN_MESSAGE_RECEIVE: SpanOperation.MESSAGE.value,
    SPAN_LLM_CHAT: SpanOperation.LLM_CALL.value,
    SPAN_LLM_JSON: SpanOperation.LLM_CALL.value,
    SPAN_AGENT_THINK: SpanOperation.AGENT_THINK.value,
    SPAN_AGENT_ACT: SpanOperation.AGENT_ACT.value,
    SPAN_HTTP_CLIENT: SpanOperation.HTTP_CLIENT.value,
    SPAN_HTTP_SERVER: SpanOperation.HTTP_SERVER.value,
    SPAN_PULSE_INVOKE: SpanOperation.PULSE_INVOKE.value,
    SPAN_WORKER_EXECUTE: SpanOperation.WORKER_EXECUTE.value,
    SPAN_SLACK_TURN: SpanOperation.SLACK_TURN.value,
    SPAN_TOOL_CALL: SpanOperation.TOOL_CALL.value,
    SPAN_TOOL_RESULT: SpanOperation.TOOL_RESULT.value,
    SPAN_TRIGGER_FIRE: SpanOperation.TRIGGER_FIRE.value,
    SPAN_SCHEDULE_INVOKE: SpanOperation.SCHEDULE_INVOKE.value,
    SPAN_WORKFLOW_EXECUTE: SpanOperation.WORKFLOW_EXECUTE.value,
    SPAN_EVAL_RUN: SpanOperation.EVAL_RUN.value,
    SPAN_MARKETPLACE_INSTALL: SpanOperation.MARKETPLACE_INSTALL.value,
    SPAN_APPROVAL_REQUIRED: SpanOperation.APPROVAL_REQUIRED.value,
    SPAN_APPROVAL_DECIDED: SpanOperation.APPROVAL_DECIDED.value,
    SPAN_EXECUTION_WAIT: SpanOperation.EXECUTION_WAIT.value,
    SPAN_EXECUTION_RESUME: SpanOperation.EXECUTION_RESUME.value,
    SPAN_AGENT_STEP: SpanOperation.AGENT_ACT.value,
    SPAN_ERROR: SpanOperation.MESSAGE.value,
}


def operation_for_span_name(span_name: str, explicit: str | None = None) -> str | None:
    """Resolve the stored operation from a canonical span name."""
    if explicit:
        return explicit
    return SPAN_NAME_TO_OPERATION.get(span_name)


def enrich_span_attributes(
    attributes: dict[str, object] | None,
    *,
    service: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> dict[str, object]:
    """Merge standard semantic attributes without overwriting caller values."""
    output = dict(attributes or {})
    if service and ATTR_SERVICE_NAME not in output:
        output[ATTR_SERVICE_NAME] = service
    if model and ATTR_GEN_AI_MODEL not in output:
        output[ATTR_GEN_AI_MODEL] = model
    if prompt_tokens is not None and ATTR_GEN_AI_PROMPT_TOKENS not in output:
        output[ATTR_GEN_AI_PROMPT_TOKENS] = prompt_tokens
    if completion_tokens is not None and ATTR_GEN_AI_COMPLETION_TOKENS not in output:
        output[ATTR_GEN_AI_COMPLETION_TOKENS] = completion_tokens
    return output
