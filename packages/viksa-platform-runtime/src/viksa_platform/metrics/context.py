"""Context-variable based token-usage attribution."""

from __future__ import annotations

import contextvars
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Any

from viksa_platform.metrics.constants import (
    PREFIX_USAGE_ACTIVITY,
    entity_type_from_resource_type,
)
from viksa_platform.metrics.resource_refs import build_resource_refs, merge_resource_refs


def _generate_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


@dataclass
class UsageContext:
    """Context for automatic token tracking within one request or activity."""

    account_id: str
    org_id: str
    project_id: str
    user_id: str | None = None
    user_email: str | None = None
    activity_id: str | None = None
    trace_id: str | None = None
    call_sequence: int = 0
    entity_type: str = "chat"
    entity_id: str | None = None
    operation: str | None = None
    resource_type: str = "chat"
    resource_id: str | None = None
    sub_resource_id: str | None = None
    metadata: dict[str, Any] | None = None
    resource_refs: dict[str, Any] | None = None
    service: str | None = None

    def with_operation(self, operation: str, **kwargs: Any) -> UsageContext:
        """Return a copy with an updated operation and optional overrides."""
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["operation"] = operation
        data.update(kwargs)
        return UsageContext(**data)

    def next_call(self) -> UsageContext:
        """Return a copy advanced to the next model-call sequence."""
        return UsageContext(
            account_id=self.account_id,
            org_id=self.org_id,
            project_id=self.project_id,
            user_id=self.user_id,
            user_email=self.user_email,
            activity_id=self.activity_id,
            trace_id=self.trace_id,
            call_sequence=self.call_sequence + 1,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            operation=self.operation,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            sub_resource_id=self.sub_resource_id,
            metadata=dict(self.metadata) if self.metadata else None,
            resource_refs=dict(self.resource_refs) if self.resource_refs else None,
            service=self.service,
        )

    def to_record_kwargs(self) -> dict[str, Any]:
        """Flatten this context for the historical recorder API."""
        entity_type = self.entity_type or entity_type_from_resource_type(self.resource_type)
        return {
            "account_id": self.account_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "activity_id": self.activity_id,
            "trace_id": self.trace_id,
            "call_sequence": self.call_sequence if self.call_sequence else None,
            "entity_type": entity_type,
            "entity_id": self.entity_id or self.resource_id,
            "operation": self.operation,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "sub_resource_id": self.sub_resource_id,
            "metadata": self.metadata,
            "resource_refs": self.resource_refs,
            "service": self.service,
        }


def refs_for_entity(
    entity_type: str,
    entity_id: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Map an entity type and identifier to canonical resource references."""
    base = build_resource_refs(user_id=user_id, user_email=user_email, **kwargs)
    if not entity_id:
        return base
    mapping = {
        "chat": ("conversation_id", entity_id),
        "trigger": ("trigger_id", entity_id),
        "trigger_execution": ("trigger_id", entity_id),
        "schedule": ("schedule_id", entity_id),
        "schedule_execution": ("schedule_id", entity_id),
        "agent_generate": ("agent_id", entity_id),
        "agent_fix": ("agent_id", entity_id),
        "response_studio": ("agent_id", entity_id),
        "response_view_generate": ("agent_id", entity_id),
        "slack_chat": ("slack_thread_ts", entity_id),
    }
    key, value = mapping.get(entity_type, ("conversation_id", entity_id))
    base[key] = value
    return base


TokenContext = UsageContext

_usage_context: contextvars.ContextVar[UsageContext | None] = contextvars.ContextVar(
    "_usage_context", default=None
)
_call_counter: contextvars.ContextVar[int] = contextvars.ContextVar("_call_counter", default=0)


def get_usage_context() -> UsageContext | None:
    return _usage_context.get()


def get_token_context() -> UsageContext | None:
    """Backward-compatible name for get_usage_context."""
    return get_usage_context()


def set_usage_context(ctx: UsageContext) -> contextvars.Token[UsageContext | None]:
    return _usage_context.set(ctx)


def set_token_context(ctx: UsageContext) -> contextvars.Token[UsageContext | None]:
    """Backward-compatible name for set_usage_context."""
    return set_usage_context(ctx)


def clear_usage_context(reset_token: contextvars.Token[UsageContext | None]) -> None:
    _usage_context.reset(reset_token)


def clear_token_context(reset_token: contextvars.Token[UsageContext | None]) -> None:
    """Backward-compatible name for clear_usage_context."""
    clear_usage_context(reset_token)


def start_activity(
    account_id: str,
    org_id: str,
    project_id: str,
    entity_type: str,
    entity_id: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    trace_id: str | None = None,
    resource_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    resource_refs: dict[str, Any] | None = None,
    service: str | None = None,
    activity_id: str | None = None,
) -> tuple[str, contextvars.Token[UsageContext | None]]:
    """Start and bind a usage activity, returning its ID and reset token."""
    resolved_activity_id = activity_id or _generate_id(PREFIX_USAGE_ACTIVITY)
    resolved_resource_type = resource_type or entity_type
    auto_refs = refs_for_entity(
        entity_type,
        entity_id,
        user_id=user_id,
        user_email=user_email,
    )
    merged_refs = merge_resource_refs(auto_refs, resource_refs)
    context = UsageContext(
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
        user_id=user_id,
        user_email=user_email,
        activity_id=resolved_activity_id,
        trace_id=trace_id,
        call_sequence=0,
        entity_type=entity_type,
        entity_id=entity_id,
        resource_type=resolved_resource_type,
        resource_id=entity_id,
        metadata=metadata,
        resource_refs=merged_refs or None,
        service=service,
    )
    reset_token = set_usage_context(context)
    _call_counter.set(0)
    return resolved_activity_id, reset_token


@contextmanager
def usage_context_scope(ctx: UsageContext) -> Iterator[UsageContext]:
    """Temporarily bind a usage context."""
    token = set_usage_context(ctx)
    try:
        yield ctx
    finally:
        clear_usage_context(token)


@contextmanager
def token_metadata_patch(**metadata_fields: Any) -> Iterator[None]:
    """Merge metadata and resource-reference fields into the current context."""
    context = get_usage_context()
    if not context:
        yield
        return
    merged_metadata = dict(context.metadata) if context.metadata else {}
    merged_metadata.update(metadata_fields)
    reference_keys = {
        "agent_id",
        "agent_ids",
        "trigger_id",
        "schedule_id",
        "conversation_id",
        "execution_id",
        "workforce_id",
        "endpoint_id",
        "message_id",
        "slack_user_id",
        "slack_user_email",
        "slack_channel_id",
        "slack_thread_ts",
    }
    reference_fields = {
        key: merged_metadata.pop(key)
        for key in tuple(merged_metadata)
        if key in reference_keys and merged_metadata.get(key) not in (None, "", [])
    }
    merged_refs = merge_resource_refs(
        context.resource_refs,
        build_resource_refs(**reference_fields),
    )
    child = UsageContext(
        account_id=context.account_id,
        org_id=context.org_id,
        project_id=context.project_id,
        user_id=context.user_id,
        user_email=context.user_email,
        activity_id=context.activity_id,
        trace_id=context.trace_id,
        call_sequence=context.call_sequence,
        entity_type=context.entity_type,
        entity_id=context.entity_id,
        operation=context.operation,
        resource_type=context.resource_type,
        resource_id=context.resource_id,
        sub_resource_id=context.sub_resource_id,
        metadata=merged_metadata or None,
        resource_refs=merged_refs or None,
        service=context.service,
    )
    token = set_usage_context(child)
    try:
        yield
    finally:
        clear_usage_context(token)


__all__ = [
    "TokenContext",
    "UsageContext",
    "clear_token_context",
    "clear_usage_context",
    "get_token_context",
    "get_usage_context",
    "refs_for_entity",
    "set_token_context",
    "set_usage_context",
    "start_activity",
    "token_metadata_patch",
    "usage_context_scope",
]
