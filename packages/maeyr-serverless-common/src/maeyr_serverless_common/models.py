"""Strict scoped commands; clients cannot supply routing or sandbox paths."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: Identifier
    org_id: Identifier
    project_id: Identifier

    @field_validator("account_id")
    @classmethod
    def account(cls, value: str) -> str:
        if not value.startswith("AC-") or len(value) > 63:
            raise ValueError("Invalid account identity")
        return value


class Submission(Scope):
    execution_id: Identifier
    user_id: Identifier
    agent_id: Identifier
    endpoint: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,511}$")
    inputs: dict[str, Any]
    deadline: str = Field(max_length=64)
    fence: int = Field(ge=1, le=9_007_199_254_740_991)
    root_execution_id: Identifier | None = None

    @field_validator("deadline")
    @classmethod
    def timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Execution deadline requires timezone")
        return parsed.astimezone(UTC).isoformat()

    @field_validator("inputs")
    @classmethod
    def payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode()) > 524_288:
            raise ValueError("Input limit exceeded")
        return value

    def reference(self) -> dict[str, Any]:
        return self.model_dump(
            include={"execution_id", "account_id", "org_id", "project_id", "deadline", "fence"}
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()


class BridgeScope(Scope):
    fence: int = Field(ge=1, le=9_007_199_254_740_991)


class CancelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fence: int = Field(ge=1, le=9_007_199_254_740_991)


class WorkerClaim(BridgeScope):
    worker_id: str = Field(pattern=r"^worker_[a-f0-9]{40}$")
    worker_token: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    assignment_generation: int = Field(ge=1, le=9007199254740991)
