"""Entity and operation taxonomy for token-usage metrics."""

from enum import Enum


class EntityType(str, Enum):
    CHAT = "chat"
    SLACK_CHAT = "slack_chat"
    TEAMS_CHAT = "teams_chat"
    AGENT_GENERATE = "agent_generate"
    AGENT_CHAT = "agent_chat"
    RESPONSE_STUDIO = "response_studio"
    SCHEDULE = "schedule"
    TRIGGER = "trigger"
    INTENT_DETECTION = "intent_detection"
    AGENT_FIX = "agent_fix"
    WORKFLOW_SUMMARY = "workflow_summary"
    INTERNAL = "internal"


class OperationType(str, Enum):
    INTENT_DETECTION = "intent_detection"
    STREAM_REPLY = "stream_reply"
    AGENTIC_THINK = "agentic_think"
    TITLE_GENERATE = "title_generate"
    GENERATE = "generate"
    FIX = "fix"
    DESIGN_WITH_AI = "design_with_ai"
    REGENERATE = "regenerate"
    EXECUTION = "execution"
    WEBHOOK_INVOKE = "webhook_invoke"
    DECODE_PROMPT = "decode_prompt"
    CHANNEL_REPLY = "channel_reply"
    SUMMARIZE = "summarize"
    TEST_RUN = "test_run"
    DEBUG_CHAT = "debug_chat"
    INTERNAL = "internal"


class ActivityStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


REDIS_QUEUE_KEY = "platform:token_usage:pending"
REDIS_QUEUE_KEY_LEGACY = "chat:token_usage:pending"
REDIS_PROCESSING_QUEUE_KEY = "platform:token_usage:processing"
REDIS_QUEUE_MAX_SIZE = 100_000

HEADER_ACTIVITY_ID = "X-Usage-Activity-Id"
HEADER_ENTITY_TYPE = "X-Usage-Entity-Type"
HEADER_ENTITY_ID = "X-Usage-Entity-Id"
HEADER_TRACE_ID = "X-Trace-ID"

PREFIX_TOKEN_USAGE = "TU"
PREFIX_USAGE_ACTIVITY = "UA"


def entity_type_from_resource_type(resource_type: str) -> str:
    """Map a legacy resource type to its standardized entity type."""
    mapping = {
        "chat": EntityType.CHAT.value,
        "intent_detection": EntityType.CHAT.value,
        "agent_generate": EntityType.AGENT_GENERATE.value,
        "agent_fix": EntityType.AGENT_GENERATE.value,
        "response_view_generate": EntityType.RESPONSE_STUDIO.value,
        "schedule_execution": EntityType.SCHEDULE.value,
        "trigger_execution": EntityType.TRIGGER.value,
        "slack_chat": EntityType.SLACK_CHAT.value,
        "teams_chat": EntityType.TEAMS_CHAT.value,
        "maeyrforce": EntityType.AGENT_CHAT.value,
        "workflow_ai": EntityType.CHAT.value,
        "workflow_summary": EntityType.WORKFLOW_SUMMARY.value,
        "conversation_title": EntityType.CHAT.value,
        "internal": EntityType.INTERNAL.value,
    }
    return mapping.get(resource_type, resource_type)


__all__ = [
    "ActivityStatus",
    "EntityType",
    "HEADER_ACTIVITY_ID",
    "HEADER_ENTITY_ID",
    "HEADER_ENTITY_TYPE",
    "HEADER_TRACE_ID",
    "OperationType",
    "PREFIX_TOKEN_USAGE",
    "PREFIX_USAGE_ACTIVITY",
    "REDIS_PROCESSING_QUEUE_KEY",
    "REDIS_QUEUE_KEY",
    "REDIS_QUEUE_KEY_LEGACY",
    "REDIS_QUEUE_MAX_SIZE",
    "entity_type_from_resource_type",
]
