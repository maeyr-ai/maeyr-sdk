"""Production policy for JWT signing secrets."""

from __future__ import annotations

import os

_INSECURE_JWT_DEFAULTS = frozenset(
    {"", "MyHimalayanPinkSalt", "your-secret-key", "test-secret", "change-me"}
)


def allows_insecure_jwt_dev() -> bool:
    """Return true only for an explicitly non-production environment."""
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


def assert_production_jwt_secret(secret: str, *, service_name: str) -> None:
    """Reject a missing, default, or short production JWT secret."""
    if allows_insecure_jwt_dev():
        return
    normalized = (secret or "").strip()
    if normalized in _INSECURE_JWT_DEFAULTS or len(normalized) < 32:
        raise RuntimeError(
            f"{service_name}: JWT_SECRET_KEY must be set to a strong value (>=32 chars) "
            "in production. Use ALLOW_INSECURE_JWT=true for local dev only."
        )


__all__ = ["allows_insecure_jwt_dev", "assert_production_jwt_secret"]
