"""Resolve active org/project from query params, request headers, or auth session."""

from __future__ import annotations

from logging import getLogger
from typing import Any, Dict, Mapping, Optional, Tuple

from fastapi import HTTPException, Request, status

from viksa_platform.auth.permission_checker import has_permission

logger = getLogger("[viksa_platform.auth.tenant_context]")


def merge_scope_filter(base: Dict[str, Any], scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine a base mongo filter with tenant scope ($and when scope is compound)."""
    if not scope:
        return base
    if "$and" in scope or "$or" in scope:
        return {"$and": [base, scope]}
    return {**base, **scope}


def _header(request: Request, name: str) -> Optional[str]:
    lower = name.lower()
    for key, val in request.headers.items():
        if key.lower() == lower:
            s = (val or "").strip()
            return s or None
    return None


def _query_param(request: Request, name: str) -> Optional[str]:
    val = request.query_params.get(name)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def resolve_tenant_ids(
    request: Request,
    current_user: Dict[str, Any],
    *,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a request tenant without allowing post-auth context switching.

    Auth validates the credential against the tenant headers before a protected
    handler runs.  Query/path values must therefore agree with that validated
    context.  Letting a query parameter override it would turn a permission in
    project A into a data-plane query against project B.
    """
    header_org = _header(request, "x-tenant-org-id")
    header_project = _header(request, "x-tenant-project-id")
    query_org = _query_param(request, "org_id")
    query_project = _query_param(request, "project_id")
    resolved = resolve_tenant_from_sources(
        current_user,
        org_id=org_id,
        project_id=project_id,
        query_org=query_org,
        query_project=query_project,
        header_org=header_org,
        header_project=header_project,
    )
    return require_validated_tenant(current_user, *resolved)


def require_validated_tenant(
    current_user: Mapping[str, Any],
    org_id: Optional[str],
    project_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Reject a tenant target that differs from Auth's validated context."""
    validated_org = str(current_user.get("org_id") or "").strip() or None
    validated_project = str(current_user.get("project_id") or "").strip() or None
    requested_org = str(org_id or "").strip() or None
    requested_project = str(project_id or "").strip() or None
    if validated_org and requested_org and requested_org != validated_org:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant organization is outside the authorized context",
        )
    if validated_project and requested_project and requested_project != validated_project:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant project is outside the authorized context",
        )
    return requested_org or validated_org, requested_project or validated_project


def permission_data_scope(
    current_user: Mapping[str, Any],
    module: str,
    action: str,
    *,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the widest Mongo data scope proved by the live Auth decision.

    The returned filter is safe to combine with a repository query.  Account
    grants may span the account database, organization grants remain in the
    authenticated organization, and context grants remain in the exact
    authenticated project.  Requested filters can narrow a grant but can
    never broaden it.
    """
    access = current_user.get("access")
    if not isinstance(access, dict):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission scope is unavailable",
        )
    requested_org = str(org_id or "").strip() or None
    requested_project = str(project_id or "").strip() or None
    validated_org = str(current_user.get("org_id") or "").strip() or None
    validated_project = str(current_user.get("project_id") or "").strip() or None

    if has_permission(access, module, action, grant_scope="account"):
        result: Dict[str, Any] = {}
        if requested_org:
            result["org_id"] = requested_org
        if requested_project:
            result["project_id"] = requested_project
        return result

    if has_permission(access, module, action, grant_scope="organization"):
        if not validated_org:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authorized organization context is required",
            )
        if requested_org and requested_org != validated_org:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization filter exceeds the authorized scope",
            )
        result = {"org_id": validated_org}
        if requested_project:
            result["project_id"] = requested_project
        return result

    if has_permission(access, module, action, grant_scope="context"):
        if not validated_org or not validated_project:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authorized project context is required",
            )
        if requested_org and requested_org != validated_org:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization filter exceeds the authorized scope",
            )
        if requested_project and requested_project != validated_project:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project filter exceeds the authorized scope",
            )
        return {"org_id": validated_org, "project_id": validated_project}

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission denied",
    )


def resolve_tenant_from_sources(
    current_user: Dict[str, Any],
    *,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    query_org: Optional[str] = None,
    query_project: Optional[str] = None,
    header_org: Optional[str] = None,
    header_project: Optional[str] = None,
    payload_org: Optional[str] = None,
    payload_project: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Priority: explicit arg → query → header → payload → auth session."""
    resolved_org = (
        org_id or query_org or header_org or payload_org or current_user.get("org_id") or ""
    ).strip() or None
    resolved_project = (
        project_id
        or query_project
        or header_project
        or payload_project
        or current_user.get("project_id")
        or ""
    ).strip() or None
    return resolved_org, resolved_project


def merge_tenant_into_user(
    user: Dict[str, Any],
    *,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    out = dict(user)
    if org_id:
        out["org_id"] = org_id
    if project_id:
        out["project_id"] = project_id
    return out


def apply_request_tenant(request: Request, user: Dict[str, Any]) -> Dict[str, Any]:
    """Merge URL/header tenant into the user dict used by chat handlers."""
    org_id, project_id = resolve_tenant_ids(request, user)
    return merge_tenant_into_user(user, org_id=org_id, project_id=project_id)


def apply_ws_tenant(
    scope: Mapping[str, Any],
    user: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge WebSocket handshake headers and optional JSON payload tenant into user."""
    headers: Dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        headers[k.lower()] = v
    data = payload or {}
    org_id, project_id = resolve_tenant_from_sources(
        user,
        header_org=headers.get("x-tenant-org-id"),
        header_project=headers.get("x-tenant-project-id"),
        payload_org=data.get("org_id"),
        payload_project=data.get("project_id"),
    )
    org_id, project_id = require_validated_tenant(user, org_id, project_id)
    return merge_tenant_into_user(user, org_id=org_id, project_id=project_id)


def trace_scope_match(
    request: Request,
    current_user: Dict[str, Any],
    *,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mongo match for trace list/stats scoped to org + project.

    Matches:
    - traces with exact org_id + project_id (new writes)
    - legacy traces with org_id set but missing/empty project_id (same org only)
    """
    resolved_org, resolved_project = resolve_tenant_ids(
        request, current_user, org_id=org_id, project_id=project_id
    )
    if not resolved_org:
        resolved_org = (current_user.get("org_id") or "").strip() or None
    if not resolved_project:
        resolved_project = (current_user.get("project_id") or "").strip() or None
    if not resolved_org or not resolved_project:
        logger.warning(
            "Trace query missing tenant scope org=%r project=%r user=%s",
            resolved_org,
            resolved_project,
            current_user.get("user_id"),
        )
        return {}

    legacy_project = {
        "$or": [
            {"project_id": {"$exists": False}},
            {"project_id": None},
            {"project_id": ""},
        ]
    }
    return {
        "$and": [
            {"org_id": resolved_org},
            {
                "$or": [
                    {"project_id": resolved_project},
                    legacy_project,
                ]
            },
        ]
    }


def ws_tenant_for_validate(
    scope: Mapping[str, Any],
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve org_id and project_id from WebSocket scope headers and JSON payload."""
    headers: Dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        headers[k.lower()] = v
    data = payload or {}

    org_id = (headers.get("x-tenant-org-id") or data.get("org_id") or "").strip() or None
    project_id = (
        headers.get("x-tenant-project-id") or data.get("project_id") or ""
    ).strip() or None
    return org_id, project_id
