"""Pure Redis endpoint and TLS configuration policies."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

_PRODUCTION_NAMES = {"prod", "production"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class RedisConfigurationError(RuntimeError):
    """Redis is configured in a way that cannot be used safely."""


def _value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name) or "").strip()


def _is_production(environ: Mapping[str, str]) -> bool:
    environment = (
        _value(environ, "APP_ENVIRONMENT") or _value(environ, "ENVIRON") or _value(environ, "ENV")
    )
    return environment.lower() in _PRODUCTION_NAMES


def _redis_endpoint(environ: Mapping[str, str]) -> str:
    return _value(environ, "REDIS_URL") or _value(environ, "REDIS_HOST")


def _configured_redis_ssl(environ: Mapping[str, str], endpoint: str) -> bool:
    raw_value = _value(environ, "REDIS_SSL")
    if not raw_value:
        return endpoint.lower().startswith("rediss://")
    normalized = raw_value.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RedisConfigurationError("REDIS_SSL must be a boolean value")


def redis_ssl_enabled(endpoint: str, raw_value: object | None) -> bool:
    """Resolve Redis TLS from an explicit boolean value or ``rediss://`` scheme."""
    if raw_value in (None, ""):
        return str(endpoint).strip().lower().startswith("rediss://")
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("REDIS_SSL must be a boolean value")


def redis_ssl_enabled_from_environment(endpoint: str) -> bool:
    """Compatibility adapter for callers that read ``REDIS_SSL`` implicitly."""
    return redis_ssl_enabled(endpoint, os.getenv("REDIS_SSL"))


def build_redis_connection_string(
    endpoint: str,
    *,
    port: int,
    database: int,
    ssl_enabled: bool,
) -> str:
    """Build a credential-free Redis URL while preserving Unix-socket endpoints."""
    raw_endpoint = str(endpoint).strip()
    if raw_endpoint.startswith("unix://"):
        if ssl_enabled:
            raise ValueError("REDIS_SSL cannot be used with a Unix socket")
        unix_endpoint = urlsplit(raw_endpoint)
        if (
            unix_endpoint.username is not None
            or unix_endpoint.password is not None
            or "password=" in unix_endpoint.query.lower()
        ):
            raise ValueError("Redis credentials must be supplied via REDIS_PASSWORD")
        return raw_endpoint

    parsed = urlsplit(raw_endpoint if "://" in raw_endpoint else f"//{raw_endpoint}")
    if parsed.scheme and parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("REDIS_URL must use redis:// or rediss://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Redis credentials must be supplied via REDIS_PASSWORD")
    if not parsed.hostname:
        raise ValueError("REDIS_URL must include a host")

    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    resolved_port = parsed.port or int(port)
    resolved_database = int(database)
    if parsed.path not in {"", "/"}:
        configured_database = parsed.path.removeprefix("/")
        if not configured_database.isdigit():
            raise ValueError("REDIS_URL database must be a non-negative integer")
        resolved_database = int(configured_database)
    scheme = "rediss" if ssl_enabled else "redis"
    return f"{scheme}://{host}:{resolved_port}/{resolved_database}"


def redis_tls_connection_kwargs(
    *,
    ssl_enabled: bool,
    ca_cert_path: str,
    environment: str,
) -> dict[str, object]:
    """Validate TLS policy and return Redis client keyword arguments."""
    if not ssl_enabled:
        return {}
    kwargs: dict[str, object] = {
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }
    normalized_path = ca_cert_path.strip()
    if not normalized_path:
        # Python's TLS stack uses the operating-system trust store when no
        # private CA bundle is supplied. This is the correct contract for
        # managed services whose certificates chain to public roots (AWS and
        # Azure), while private/self-signed deployments can still pin a CA.
        return kwargs
    ca_file = Path(normalized_path).expanduser()
    if not ca_file.is_file() or not os.access(ca_file, os.R_OK):
        raise RuntimeError("REDIS_CA_CERT_PATH must identify a readable file")
    kwargs["ssl_ca_certs"] = str(ca_file)
    return kwargs


def redis_settings_connection_string(settings: Any) -> str:
    """Build a URL from a class/object exposing the common Redis setting fields."""
    return build_redis_connection_string(
        str(settings.REDIS_HOST),
        port=int(settings.REDIS_PORT),
        database=int(settings.REDIS_DB),
        ssl_enabled=bool(settings.REDIS_SSL),
    )


def redis_settings_tls_connection_kwargs(settings: Any) -> dict[str, object]:
    """Resolve TLS client options from the common ``REDISSettings`` surface."""
    return redis_tls_connection_kwargs(
        ssl_enabled=bool(settings.SSL),
        ca_cert_path=str(settings.CA_CERT_PATH),
        environment=os.getenv("APP_ENVIRONMENT", ""),
    )


def redis_connection_url(environ: Mapping[str, str] | None = None) -> str | None:
    """Build a validated credential-free Redis URL from the process environment."""
    env = os.environ if environ is None else environ
    endpoint = _redis_endpoint(env)
    if not endpoint:
        return None

    use_ssl = _configured_redis_ssl(env, endpoint)
    if endpoint.startswith("unix://"):
        if use_ssl:
            raise RedisConfigurationError("REDIS_SSL cannot be used with a Unix socket")
        parsed_unix = urlsplit(endpoint)
        if (
            parsed_unix.username is not None
            or parsed_unix.password is not None
            or "password=" in parsed_unix.query.lower()
        ):
            raise RedisConfigurationError("Redis credentials must be supplied via REDIS_PASSWORD")
        return endpoint

    parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
    if parsed.scheme and parsed.scheme not in {"redis", "rediss"}:
        raise RedisConfigurationError("REDIS_URL must use redis:// or rediss://")
    if parsed.username is not None or parsed.password is not None:
        raise RedisConfigurationError("Redis credentials must be supplied via REDIS_PASSWORD")
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys.intersection({"password", "username", "user"}):
        raise RedisConfigurationError("Redis credentials must be supplied via REDIS_PASSWORD")
    if not parsed.hostname:
        raise RedisConfigurationError("REDIS_URL must include a host")

    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = parsed.port or int(_value(env, "REDIS_PORT") or "6379")
    except ValueError as exc:
        raise RedisConfigurationError("REDIS_PORT must be an integer") from exc

    database = _value(env, "REDIS_DB") or "0"
    if parsed.path not in {"", "/"}:
        database = parsed.path.removeprefix("/")
    if not database.isdigit():
        raise RedisConfigurationError("Redis database must be a non-negative integer")

    scheme = "rediss" if use_ssl else "redis"
    return f"{scheme}://{host}:{port}/{int(database)}"


def redis_connection_kwargs(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return validated Redis client keyword arguments without embedding secrets in URLs."""
    env = os.environ if environ is None else environ
    endpoint = _redis_endpoint(env)
    if not endpoint:
        return {}

    kwargs: dict[str, Any] = {"decode_responses": True}
    username = env.get("REDIS_USERNAME")
    if username:
        kwargs["username"] = username
    password = env.get("REDIS_PASSWORD")
    if password:
        kwargs["password"] = password

    if not _configured_redis_ssl(env, endpoint):
        return kwargs

    kwargs.update({"ssl_cert_reqs": "required", "ssl_check_hostname": True})
    ca_cert_path = _value(env, "REDIS_CA_CERT_PATH")
    if not ca_cert_path:
        return kwargs

    ca_file = Path(ca_cert_path).expanduser()
    if not ca_file.is_file() or not os.access(ca_file, os.R_OK):
        raise RedisConfigurationError("REDIS_CA_CERT_PATH must identify a readable file")
    kwargs["ssl_ca_certs"] = str(ca_file)
    return kwargs


def validate_redis_configuration(environ: Mapping[str, str] | None = None) -> None:
    """Fail fast when Redis endpoint, credentials, or TLS policy is unsafe."""
    env = os.environ if environ is None else environ
    endpoint = _redis_endpoint(env)
    if not endpoint:
        raw_ssl = _value(env, "REDIS_SSL")
        if raw_ssl:
            enabled = _configured_redis_ssl(env, endpoint)
            if enabled and _is_production(env):
                raise RedisConfigurationError(
                    "REDIS_URL or REDIS_HOST is required when REDIS_SSL is enabled"
                )
        return
    redis_connection_url(env)
    redis_connection_kwargs(env)


def create_redis_client(
    redis_module: Any,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Construct a Redis client through an injected Redis module."""
    url = redis_connection_url(environ)
    if url is None:
        return None
    return redis_module.from_url(url, **redis_connection_kwargs(environ))


__all__ = [
    "RedisConfigurationError",
    "build_redis_connection_string",
    "create_redis_client",
    "redis_connection_kwargs",
    "redis_connection_url",
    "redis_ssl_enabled",
    "redis_ssl_enabled_from_environment",
    "redis_settings_connection_string",
    "redis_settings_tls_connection_kwargs",
    "redis_tls_connection_kwargs",
    "validate_redis_configuration",
]
