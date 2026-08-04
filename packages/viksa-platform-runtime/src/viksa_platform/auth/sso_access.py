"""Pure projections over the Auth SSO access document."""

from __future__ import annotations

from typing import Any


def extract_org_ids(access: dict[str, Any]) -> list[str]:
    """Return every organization identifier in an SSO access document."""
    return [org["org_id"] for org in access.get("orgs") or []]


def extract_project_ids(access: dict[str, Any]) -> list[str]:
    """Return project identifiers across every accessible organization."""
    identifiers: list[str] = []
    for organization in access.get("orgs") or []:
        for project in organization.get("projects") or []:
            identifiers.append(project["project_id"])
    return identifiers


def is_org_admin_for(access: dict[str, Any], org_id: str) -> bool:
    """Return whether the access document grants organization admin rights."""
    for organization in access.get("orgs") or []:
        if organization.get("org_id") != org_id:
            continue
        role = organization.get("org_role") or {}
        for permission in role.get("permissions") or []:
            if permission.get("module") != "organization":
                continue
            actions = permission.get("actions") or []
            if "admin" in actions or "all" in actions:
                return True
    return False


def get_admin_org_ids(access: dict[str, Any]) -> list[str]:
    """Return organization identifiers for which the principal is an admin."""
    return [
        organization["org_id"]
        for organization in access.get("orgs") or []
        if is_org_admin_for(access, organization["org_id"])
    ]


__all__ = [
    "extract_org_ids",
    "extract_project_ids",
    "get_admin_org_ids",
    "is_org_admin_for",
]
