"""Typed IAM access-policy contract shared by Directory Sync and Volt."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PolicyPrincipals(BaseModel):
    groups: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    query: dict[str, Any] = Field(default_factory=dict)


class PolicyConditions(BaseModel):
    channels: list[str] = Field(default_factory=list)
    ip_ranges: list[str] = Field(default_factory=list)


class VoltAccessPolicy(BaseModel):
    account_id: str
    org_id: str
    project_id: str
    id: str = Field(default="", description="Composite policy identifier")
    name: str = Field(min_length=1)
    description: str = ""
    effect: Literal["allow", "deny"] = "allow"
    principals: PolicyPrincipals = Field(default_factory=PolicyPrincipals)
    resources: list[str] = Field(default_factory=list)
    conditions: PolicyConditions = Field(default_factory=PolicyConditions)


__all__ = ["PolicyConditions", "PolicyPrincipals", "VoltAccessPolicy"]
