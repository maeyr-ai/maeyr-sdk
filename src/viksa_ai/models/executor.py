"""Pulse executor models."""

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator


class AgentType(str, Enum):
    CLOUD = "cloud"
    SECURE = "secure"


class A2AEnvelopeIn(BaseModel):
    protocol_version: int = 1
    run_id: str
    parent_step_id: Optional[str] = None
    caller_agent: Optional[str] = None
    callee_agent: Optional[str] = None
    idempotency_key: Optional[str] = None
    deadline_at: Optional[str] = None


class EndpointExecutionRequest(BaseModel):
    agent_id: str
    agent_type: AgentType
    endpoint: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    task_queue: Optional[str] = None
    timeout: Optional[int] = None
    envelope: Optional[A2AEnvelopeIn] = None

    @model_validator(mode="after")
    def validate_secure_agent_queue(self) -> "EndpointExecutionRequest":
        if self.agent_type == AgentType.SECURE and not self.task_queue:
            raise ValueError("task_queue is mandatory for secure agents")
        return self


class EndpointExecutionResponse(BaseModel):
    status: str
    execution_id: str
    endpoint: str
    response: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class AgentInvokeRequest(BaseModel):
    agent_id: str
    agent_type: AgentType
    endpoint: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    task_queue: Optional[str] = None
    timeout: Optional[int] = None


class AgentInvokeResponse(BaseModel):
    status: str
    invocation_id: str
    endpoint: str
