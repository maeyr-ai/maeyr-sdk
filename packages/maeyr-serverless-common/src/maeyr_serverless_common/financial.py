"""Strict trusted runtime financial handshake; never part of customer inputs."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Identifier, Scope


class StartReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    broker_id: Identifier
    boot_id: Identifier
    sandbox_id: Identifier
    started_at: str = Field(max_length=64)

    @field_validator("started_at")
    @classmethod
    def aware(cls, value: str) -> str:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("A trusted aware start timestamp is required")
        return value


class StartCommand(Scope):
    execution_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
    receipt: StartReceipt
