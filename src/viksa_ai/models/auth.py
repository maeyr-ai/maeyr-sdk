"""Auth service request/response models."""

from typing import Optional

from pydantic import BaseModel, Field


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
