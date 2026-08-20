"""Fail-closed helpers for HTTP deployment configuration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

LOCAL_BROWSER_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://localhost:3001",
)


def allowed_origins_from_env(
    environment: str,
    *,
    environ: Mapping[str, str] | None = None,
    local_defaults: Sequence[str] = LOCAL_BROWSER_ORIGINS,
) -> list[str]:
    """Parse exact credentialed-CORS origins and fail closed in production."""

    source = os.environ if environ is None else environ
    raw = str(source.get("ALLOWED_ORIGINS", "")).strip()
    production = environment.strip().lower() in {"prod", "production"}
    if not raw:
        if production:
            raise RuntimeError("ALLOWED_ORIGINS is required in production")
        return list(local_defaults)
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ALLOWED_ORIGINS must be a JSON array") from exc
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        raise RuntimeError("ALLOWED_ORIGINS must be a non-empty JSON string array")

    result: list[str] = []
    for value in values:
        origin = value.strip().rstrip("/")
        parsed = urlsplit(origin)
        try:
            parsed.port
        except ValueError as exc:
            raise RuntimeError("ALLOWED_ORIGINS contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("ALLOWED_ORIGINS contains an invalid HTTP(S) origin")
        if production and parsed.scheme != "https":
            raise RuntimeError("ALLOWED_ORIGINS must use HTTPS in production")
        result.append(origin)
    if len(result) != len(set(result)):
        raise RuntimeError("ALLOWED_ORIGINS must not contain duplicates")
    return result


def deployment_service_url(
    name: str,
    *,
    environment: str,
    local_port: int,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve one credential-free service origin with production-safe defaults."""

    source = os.environ if environ is None else environ
    value = str(source.get(name, "")).strip()
    if not value:
        if environment.strip().lower() in {"prod", "production"}:
            raise RuntimeError(f"{name} is required in production")
        value = f"http://localhost:{local_port}"
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} contains an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be an absolute credential-free service origin")
    return normalized


__all__ = [
    "LOCAL_BROWSER_ORIGINS",
    "allowed_origins_from_env",
    "deployment_service_url",
]
