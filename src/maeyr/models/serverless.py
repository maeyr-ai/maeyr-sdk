"""Scoped execution-worker projections returned through the platform manager."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
]


class ServerlessPlacement(BaseModel):
    """Actual Chrona Serverless worker, never a management service address."""

    model_config = ConfigDict(extra="forbid", strict=True)
    worker_id: Identifier
    worker_name: str = Field(min_length=1, max_length=128)
    namespace: Literal["serverless"]
    task_queue: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    generation: int = Field(gt=0)

    @field_validator("worker_name")
    @classmethod
    def validate_worker_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid worker name")
        return value


class ServerlessAssignment(ServerlessPlacement):
    assigned_at: str = Field(min_length=20, max_length=40)
    state: Literal["assigned", "unavailable"]

    @field_validator("assigned_at")
    @classmethod
    def validate_assigned_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("Assignment timestamp must be UTC")
        return value


class ServerlessAssignmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: Identifier
    org_id: Identifier
    project_id: Identifier
    agent_id: Identifier
    assignment: ServerlessAssignment | None


class ServerlessExecutionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    occupied_micros: int = Field(ge=0)
    cpu_usage_micros: int | None = Field(default=None, ge=0)
    memory_peak_bytes: int | None = Field(default=None, ge=0)


class ServerlessExecutionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    placement: ServerlessPlacement | None = None
    usage: ServerlessExecutionUsage | None = None
    logs: str | None = None
