"""Pydantic schemas for OTel-compatible service trace records."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SpanRecord(BaseModel):
    """Atomic span emitted by one application operation."""

    span_id: str
    trace_id: str
    parent_span_id: Optional[str] = None
    activity_id: Optional[str] = None
    account_id: str
    org_id: str
    project_id: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    service: str = "unknown"
    span_kind: str = "internal"
    span_name: str = "internal"
    operation: Optional[str] = None
    status: str = "ok"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    resource_refs: Optional[Dict[str, Any]] = None
    attributes: Optional[Dict[str, Any]] = None
    labels: Optional[List[str]] = None
    expires_at: Optional[datetime] = None
    is_root: bool = False


class TraceRecord(BaseModel):
    """Root trace document aggregated from its emitted spans."""

    trace_id: str
    account_id: str
    org_id: str
    project_id: str
    activity_id: Optional[str] = None
    root_span_id: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    resource_refs: Optional[Dict[str, Any]] = None
    status: str = "ok"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    top_level_span: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    service_chain: List[str] = Field(default_factory=list)
    totals: Dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[datetime] = None


class SpanBatchRequest(BaseModel):
    """Bounded transport shape for internal batch span ingestion."""

    spans: List[Dict[str, Any]] = Field(default_factory=list)


__all__ = ["SpanBatchRequest", "SpanRecord", "TraceRecord"]
