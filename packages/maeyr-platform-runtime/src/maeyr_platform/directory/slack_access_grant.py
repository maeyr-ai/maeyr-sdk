"""Time-limited Slack access-grant policy shared by platform services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def coerce_expires_at(raw: Any) -> datetime | None:
    """Parse a Mongo datetime or ISO timestamp into an aware UTC datetime."""

    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _ensure_utc(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def grant_is_active(
    grant: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Return whether a grant is enabled and has not expired."""

    if not bool(grant.get("enabled", True)):
        return False
    expires_at = coerce_expires_at(grant.get("expires_at"))
    if expires_at is None:
        return True
    reference = now or utc_now()
    return expires_at > reference


def grant_is_expired(
    grant: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Return whether a grant has an expiry at or before the reference time."""

    expires_at = coerce_expires_at(grant.get("expires_at"))
    if expires_at is None:
        return False
    reference = now or utc_now()
    return expires_at <= reference


__all__ = ["coerce_expires_at", "grant_is_active", "grant_is_expired", "utc_now"]
