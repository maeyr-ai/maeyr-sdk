"""Auth service request/response models."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    account_id: str
    org_id: Optional[str] = None
    project_id: Optional[str] = None


class SwitchOrgRequest(BaseModel):
    org_id: str


class SwitchProjectRequest(BaseModel):
    project_id: str


class UserResponse(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    email: str
    account_id: str


class ApiKeyRequest(BaseModel):
    name: str = Field(..., description="Human-readable key name")
    description: Optional[str] = None


WorkerKeyScope = Literal["read", "write", "delete", "admin"]


def _default_worker_key_scopes() -> list[WorkerKeyScope]:
    return ["read", "write"]


class WorkerKeyRateLimit(BaseModel):
    """Optional per-key request budget accepted by the Auth service."""

    rpm: int = Field(default=600, gt=0)
    burst: int = Field(default=50, gt=0)


class WorkerKeyCreateRequest(BaseModel):
    """Project-scoped worker-key creation contract.

    ``key_type`` is intentionally fixed to ``worker`` so this request cannot be
    reused accidentally against a different credential route.
    """

    key_id: Optional[str] = None
    name: str = Field(..., min_length=1, description="Human-readable key name")
    key_type: Literal["worker"] = "worker"
    expires_in_days: Optional[int] = Field(default=None, gt=0)
    project_id: Optional[str] = None
    scopes: list[WorkerKeyScope] = Field(default_factory=_default_worker_key_scopes)
    rate_limit: Optional[WorkerKeyRateLimit] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("worker-key name must not be blank")
        return value


class KeyValidationRequest(BaseModel):
    api_key: str


class KeyValidationResponse(BaseModel):
    valid: bool
    account_id: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    scopes: list[str] = []
    error: Optional[str] = None
