"""Tenant/account validation primitives for trace persistence."""

from __future__ import annotations

from typing import Any


def valid_tenant_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return bool(normalized) and normalized.lower() != "unknown"


def valid_span_tenant_scope(doc: dict[str, Any]) -> bool:
    """Require account, organization, and project before forwarding a span."""
    return (
        valid_tenant_id(doc.get("account_id"))
        and valid_tenant_id(doc.get("org_id"))
        and valid_tenant_id(doc.get("project_id"))
    )


def span_ref(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": doc.get("trace_id"),
        "span_name": doc.get("span_name"),
        "service": doc.get("service"),
    }


__all__ = ["span_ref", "valid_span_tenant_scope", "valid_tenant_id"]
