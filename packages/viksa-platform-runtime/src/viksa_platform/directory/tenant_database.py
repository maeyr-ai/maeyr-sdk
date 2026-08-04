"""Per-account Mongo tenant-scope policy shared by Directory Sync and Volt."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

LEGACY_GLOBAL_VOLT_DB = "cosmoagent"
LEGACY_COSMO_DB = LEGACY_GLOBAL_VOLT_DB

_ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_RESERVED_DATABASE_NAMES = frozenset({"admin", "local", "config"})


def validate_account_id(account_id: str) -> str:
    """Validate and normalize an account identifier used as a Mongo database."""

    raw = str(account_id or "").strip()
    if not raw or not _ACCOUNT_ID_RE.match(raw):
        raise ValueError(f"invalid account_id for Mongo database: {account_id!r}")
    if raw in _RESERVED_DATABASE_NAMES:
        raise ValueError(f"reserved Mongo database name: {raw}")
    return raw


def database_for_account(account_id: str) -> str:
    """Return the per-account Mongo database name."""

    return validate_account_id(account_id)


def extract_tenant_scope(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Extract a complete account, organization, and project scope."""

    if not raw or not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key in ("account_id", "org_id", "project_id"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return {}
        cleaned[key] = value.strip()
    cleaned["account_id"] = validate_account_id(cleaned["account_id"])
    return cleaned


def project_filter(scope: Mapping[str, str]) -> dict[str, str]:
    """Return the default project-isolation filter within an account database."""

    return {
        "org_id": scope["org_id"],
        "project_id": scope["project_id"],
    }


def document_scope(scope: Mapping[str, str]) -> dict[str, str]:
    """Return tenant fields suitable for an insert or upsert document."""

    return dict(scope)


__all__ = [
    "LEGACY_COSMO_DB",
    "LEGACY_GLOBAL_VOLT_DB",
    "database_for_account",
    "document_scope",
    "extract_tenant_scope",
    "project_filter",
    "validate_account_id",
]
