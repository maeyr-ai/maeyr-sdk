"""Resolve active org/project from query params, request headers, or auth session."""

from __future__ import annotations

from logging import getLogger
from typing import Any, Dict, Mapping, Optional, Tuple

from fastapi import Request

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
    """Priority: explicit arg → query param → tenant header → auth session."""
    header_org = _header(request, "x-tenant-org-id")
    header_project = _header(request, "x-tenant-project-id")
    query_org = _query_param(request, "org_id")
    query_project = _query_param(request, "project_id")
    return resolve_tenant_from_sources(
        current_user,
        org_id=org_id,
        project_id=project_id,
        query_org=query_org,
        query_project=query_project,
        header_org=header_org,
        header_project=header_project,
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
