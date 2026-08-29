"""Canonical internal tenant-header construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def internal_tenant_headers(
    *,
    account_id: str,
    org_id: str,
    project_id: str,
) -> dict[str, str]:
    return {
        "X-Internal-Account-Id": account_id.strip(),
        "X-Internal-Org-Id": org_id.strip(),
        "X-Internal-Project-Id": project_id.strip(),
    }


def internal_tenant_headers_from_mapping(
    data: Mapping[str, Any],
    *,
    account_key: str = "account_id",
    org_key: str = "org_id",
    project_key: str = "project_id",
) -> dict[str, str] | None:
    account_id = str(data.get(account_key) or "").strip()
    org_id = str(data.get(org_key) or "").strip()
    project_id = str(data.get(project_key) or "").strip()
    if not (account_id and org_id and project_id):
        return None
    return internal_tenant_headers(
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
    )


def internal_tenant_headers_from_span(
    span: Mapping[str, Any],
) -> dict[str, str] | None:
    return internal_tenant_headers_from_mapping(span)


__all__ = [
    "internal_tenant_headers",
    "internal_tenant_headers_from_mapping",
    "internal_tenant_headers_from_span",
]
