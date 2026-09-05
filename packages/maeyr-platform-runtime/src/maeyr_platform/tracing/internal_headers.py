"""Internal tenant headers for authenticated remote trace delivery."""

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


def internal_tenant_headers_from_span(
    span: Mapping[str, Any],
) -> dict[str, str] | None:
    account_id = str(span.get("account_id") or "").strip()
    org_id = str(span.get("org_id") or "").strip()
    project_id = str(span.get("project_id") or "").strip()
    if not (account_id and org_id and project_id):
        return None
    return internal_tenant_headers(
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
    )


__all__ = ["internal_tenant_headers", "internal_tenant_headers_from_span"]
