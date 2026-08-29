"""Pure projections over the Auth SSO access document."""

from __future__ import annotations

from typing import Any


def _organizations(access: object) -> tuple[dict[str, Any], ...]:
    """Return only structurally valid organization grants.

    Access documents cross a service boundary and must be treated as untrusted
    input.  Projection helpers intentionally skip malformed entries instead of
    raising or accepting truthy lookalikes.
    """
    if not isinstance(access, dict):
        return ()
    organizations = access.get("orgs")
    if not isinstance(organizations, list):
        return ()
    return tuple(item for item in organizations if isinstance(item, dict))


def extract_org_ids(access: dict[str, Any]) -> list[str]:
    """Return every organization identifier in an SSO access document."""
    return [
        org_id
        for organization in _organizations(access)
        if isinstance((org_id := organization.get("org_id")), str) and org_id
    ]


def extract_project_ids(access: dict[str, Any]) -> list[str]:
    """Return project identifiers across every accessible organization."""
    identifiers: list[str] = []
    for organization in _organizations(access):
        projects = organization.get("projects")
        if not isinstance(projects, list):
            continue
        for project in projects:
            if not isinstance(project, dict):
                continue
            project_id = project.get("project_id")
            if isinstance(project_id, str) and project_id:
                identifiers.append(project_id)
    return identifiers


def has_org_permission(
    access: dict[str, Any], org_id: str, module: str, action: str
) -> bool:
    """Check one exact organization-role permission.

    Wildcards and legacy aliases are deliberately not expanded here.  The Auth
    service emits canonical permission names; downstream services consume that
    contract exactly.
    """
    if not all(isinstance(value, str) and value for value in (org_id, module, action)):
        return False
    for organization in _organizations(access):
        if organization.get("org_id") != org_id:
            continue
        role = organization.get("org_role")
        if not isinstance(role, dict):
            continue
        permissions = role.get("permissions")
        if not isinstance(permissions, list):
            continue
        for permission in permissions:
            if not isinstance(permission, dict) or permission.get("module") != module:
                continue
            actions = permission.get("actions")
            if isinstance(actions, list) and action in actions:
                return True
    return False


def is_org_admin_for(access: dict[str, Any], org_id: str) -> bool:
    """Return whether the user can administer access for one organization."""
    return has_org_permission(access, org_id, "access_control", "manage")


def get_admin_org_ids(access: dict[str, Any]) -> list[str]:
    """Return organization identifiers for which the principal is an admin."""
    return [org_id for org_id in extract_org_ids(access) if is_org_admin_for(access, org_id)]


__all__ = [
    "extract_org_ids",
    "extract_project_ids",
    "get_admin_org_ids",
    "has_org_permission",
    "is_org_admin_for",
]
