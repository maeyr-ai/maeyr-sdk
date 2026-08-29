"""Production policy for service-to-service internal keys."""

from __future__ import annotations

import os

_WEAK_KEY_VALUES = frozenset(
    {
        "changeme",
        "default",
        "development",
        "example",
        "internalkey",
        "password",
        "placeholder",
        "sample",
        "secret",
        "test",
        "testkey",
        "testsecret",
    }
)


def _is_placeholder_key(value: str) -> bool:
    lowered = value.strip().lower()
    compact = "".join(character for character in lowered if character.isalnum())
    return (
        not lowered
        or "placeholder" in lowered
        or compact.startswith(("replaceme", "changeme"))
        or compact in _WEAK_KEY_VALUES
        or len(set(lowered)) < 4
    )


def _allows_insecure_dev() -> bool:
    environments = [
        value.strip().lower()
        for value in (
            os.getenv("APP_ENVIRONMENT"),
            os.getenv("ENVIRON"),
            os.getenv("ENV"),
        )
        if value and value.strip()
    ]
    if any(value in {"prod", "production"} for value in environments):
        return False
    if os.getenv("ALLOW_INSECURE_JWT", "").lower() in {"1", "true", "yes"}:
        return True
    return not environments or all(
        value in {"development", "dev", "local", "test"} for value in environments
    )


def assert_production_internal_key(
    key: str,
    *,
    env_name: str,
    service_name: str,
    minimum_bytes: int = 16,
) -> None:
    """Reject a missing, placeholder, or undersized production mesh key."""
    if _allows_insecure_dev():
        return
    normalized = (key or "").strip()
    if _is_placeholder_key(normalized):
        raise RuntimeError(
            f"{service_name}: {env_name} must be set to a non-placeholder value in production. "
            "Use ALLOW_INSECURE_JWT=true for local dev only."
        )
    minimum = max(1, int(minimum_bytes))
    if len(normalized.encode("utf-8")) < minimum:
        raise RuntimeError(
            f"{service_name}: {env_name} must contain at least {minimum} bytes in production."
        )


__all__ = ["assert_production_internal_key"]
