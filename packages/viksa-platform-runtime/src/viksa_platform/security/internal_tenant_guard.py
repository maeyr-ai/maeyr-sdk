"""Tenant-safety guards for internal FastAPI endpoints."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from viksa_platform.tracing.tenant import valid_tenant_id


def require_internal_tenant_fields(
    *,
    account_id: str | None,
    org_id: str | None,
    project_id: str | None,
) -> None:
    if not account_id or not valid_tenant_id(account_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid X-Internal-Account-Id",
        )
    if not org_id or not valid_tenant_id(org_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid X-Internal-Org-Id",
        )
    if not project_id or not valid_tenant_id(project_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid X-Internal-Project-Id",
        )


def validate_internal_tenant_body(
    request: Request,
    *,
    account_id: str | None,
    org_id: str | None,
    project_id: str | None,
) -> None:
    header_account = request.headers.get("X-Internal-Account-Id")
    header_org = request.headers.get("X-Internal-Org-Id")
    header_project = request.headers.get("X-Internal-Project-Id")
    if header_account and account_id and header_account.strip() != account_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context mismatch: Account ID",
        )
    if header_org and org_id and header_org.strip() != org_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context mismatch: Org ID",
        )
    if header_project and project_id and header_project.strip() != project_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context mismatch: Project ID",
        )


__all__ = ["require_internal_tenant_fields", "validate_internal_tenant_body"]
