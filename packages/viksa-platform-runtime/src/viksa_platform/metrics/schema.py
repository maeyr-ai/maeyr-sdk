"""Pydantic schemas for token-usage events and activities."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TokenUsageEvent(BaseModel):
    """Atomic token-usage record for one model call."""

    model_config = ConfigDict(populate_by_name=True)

    event_id: str | None = Field(default=None, alias="_id")
    account_id: str
    org_id: str
    project_id: str
    user_id: str | None = None
    user_email: str | None = None
    activity_id: str | None = None
    trace_id: str | None = None
    call_sequence: int | None = None
    parent_call_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    operation: str | None = None
    resource_type: str = "chat"
    resource_id: str | None = None
    sub_resource_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_used: int = 0
    cost_usd: float | None = None
    pricing_version: str | None = None
    model: str = "azure-openai"
    estimated: bool = False
    metadata: dict[str, Any] | None = None
    resource_refs: dict[str, Any] | None = None
    created_at: datetime | None = None
    date_bucket: str | None = None
    service: str | None = None


class TokenUsageBatchRequest(BaseModel):
    """Bounded internal batch-ingestion request."""

    events: list[TokenUsageEvent] = Field(default_factory=list, max_length=500)


class UsageActivitySummary(BaseModel):
    """Session or work-unit usage summary."""

    activity_id: str
    account_id: str
    org_id: str
    project_id: str
    user_id: str | None = None
    user_email: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    trace_id: str | None = None
    status: str = "in_progress"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_call_at: datetime | None = None
    totals: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None
    resource_refs: dict[str, Any] | None = None


__all__ = ["TokenUsageBatchRequest", "TokenUsageEvent", "UsageActivitySummary"]
