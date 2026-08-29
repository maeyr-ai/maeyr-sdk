"""Agent-to-agent (A2A) protocol models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

A2A_PROTOCOL_VERSION = 1


class A2AStatus(str, Enum):
    OK = "ok"
    INVALID_PAYLOAD = "invalid_payload"
    UNAUTHORIZED = "unauthorized"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"


class A2AEnvelope(BaseModel):
    """Wraps a single agent-to-agent call."""

    protocol_version: int = Field(default=A2A_PROTOCOL_VERSION)
    run_id: str = Field(..., description="Top-level run identifier")
    parent_step_id: Optional[str] = Field(default=None)
    caller_agent: Optional[str] = Field(default=None)
    callee_agent: str = Field(..., description="Target agent_alias")
    endpoint: str = Field(
        ...,
        description="Full endpoint path: agent_alias.module.function",
    )
    idempotency_key: Optional[str] = Field(default=None)
    deadline_at: Optional[datetime] = Field(default=None)
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class A2AResponse(BaseModel):
    """Typed response wrapper for A2A calls."""

    protocol_version: int = Field(default=A2A_PROTOCOL_VERSION)
    status: A2AStatus = Field(default=A2AStatus.OK)
    payload: Optional[Dict[str, Any]] = Field(default=None)
    error: Optional[Dict[str, Any]] = Field(default=None)
    tokens: Optional[int] = Field(default=None)
    latency_ms: Optional[int] = Field(default=None)
    cost_usd: Optional[float] = Field(default=None)
