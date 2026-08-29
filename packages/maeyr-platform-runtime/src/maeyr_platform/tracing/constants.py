"""Trace/span taxonomy and transport constants shared by platform services."""

from enum import Enum

PREFIX_TRACE = "TR"
PREFIX_SPAN = "SP"

REDIS_QUEUE_KEY = "platform:trace_spans:pending"
REDIS_PROCESSING_QUEUE_KEY = "platform:trace_spans:processing"

HEADER_TRACEPARENT = "traceparent"
HEADER_TRACESTATE = "tracestate"
HEADER_TRACE_ID = "X-Trace-ID"
HEADER_SPAN_ID = "X-Span-Id"
HEADER_PARENT_SPAN_ID = "X-Parent-Span-Id"
HEADER_ACTIVITY_ID = "X-Usage-Activity-Id"
HEADER_ENTITY_TYPE = "X-Usage-Entity-Type"
HEADER_ENTITY_ID = "X-Usage-Entity-Id"
HEADER_TENANT_ORG_ID = "X-Tenant-Org-Id"
HEADER_TENANT_PROJECT_ID = "X-Tenant-Project-Id"

DEFAULT_RETENTION_DAYS = 14
DEFAULT_ROLLUP_RETENTION_DAYS = 90


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    RUNNING = "running"


class SpanKind(str, Enum):
    INTERNAL = "internal"
    CLIENT = "client"
    SERVER = "server"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class SpanOperation(str, Enum):
    MESSAGE = "message"
    LLM_CALL = "llm_call"
    HTTP_CLIENT = "http_client"
    HTTP_SERVER = "http_server"
    AGENT_DISPATCH = "agent_dispatch"
    AGENT_THINK = "agent_think"
    AGENT_ACT = "agent_act"
    PULSE_INVOKE = "pulse_invoke"
    WORKER_EXECUTE = "worker_execute"
    SLACK_TURN = "slack_turn"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TRIGGER_FIRE = "trigger_fire"
    SCHEDULE_INVOKE = "schedule_invoke"
    WORKFLOW_EXECUTE = "workflow_execute"
    EVAL_RUN = "eval_run"
    AUTH_LOGIN = "auth_login"
    MARKETPLACE_INSTALL = "marketplace_install"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DECIDED = "approval_decided"
    EXECUTION_WAIT = "execution_wait"
    EXECUTION_RESUME = "execution_resume"


SPAN_MESSAGE_RECEIVE = "message.receive"
SPAN_LLM_CHAT = "llm.chat"
SPAN_LLM_JSON = "llm.json"
SPAN_AGENT_THINK = "agent.think"
SPAN_AGENT_ACT = "agent.act"
SPAN_HTTP_CLIENT = "http.client"
SPAN_HTTP_SERVER = "http.server"
SPAN_PULSE_INVOKE = "pulse.invoke"
SPAN_WORKER_EXECUTE = "worker.execute"
SPAN_SLACK_TURN = "slack.turn"
SPAN_TOOL_CALL = "tool.call"
SPAN_TOOL_RESULT = "tool.result"
SPAN_TRIGGER_FIRE = "trigger.fire"
SPAN_SCHEDULE_INVOKE = "schedule.invoke"
SPAN_WORKFLOW_EXECUTE = "workflow.execute"
SPAN_EVAL_RUN = "eval.run"
SPAN_AUTH_LOGIN = "auth.login"
SPAN_MARKETPLACE_INSTALL = "marketplace.install"
SPAN_APPROVAL_REQUIRED = "approval.required"
SPAN_APPROVAL_DECIDED = "approval.decided"
SPAN_EXECUTION_WAIT = "execution.wait"
SPAN_EXECUTION_RESUME = "execution.resume"
SPAN_AGENT_STEP = "agent.step"
SPAN_ERROR = "error"
