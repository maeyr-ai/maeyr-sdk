"""Canonical tenant identifiers for authenticated service boundaries."""

from __future__ import annotations

import re

_CANONICAL_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def canonical_tenant_id(value: object, *, field: str = "tenant_id") -> str:
    """Return one canonical tenant ID or reject ambiguous/unsafe input."""
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    normalized = value.strip()
    if normalized.lower() == "unknown" or not _CANONICAL_TENANT_ID_RE.fullmatch(normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def is_canonical_tenant_id(value: object) -> bool:
    """Return whether a value satisfies the authenticated-boundary policy."""
    try:
        canonical_tenant_id(value)
    except ValueError:
        return False
    return True


__all__ = ["canonical_tenant_id", "is_canonical_tenant_id"]
