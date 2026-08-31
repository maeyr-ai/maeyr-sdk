"""Pure Mongo projection and connection-usage policies."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MONGO_SCHEMES = frozenset({"mongodb", "mongodb+srv"})


def _environment_value(environment: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environment.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _environment_boolean(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool = False,
) -> bool:
    value = environment.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _validate_mongo_uri(uri: str) -> None:
    if any(character.isspace() for character in uri):
        raise ValueError("MONGODB_URI must not contain whitespace")
    parsed = urlsplit(uri)
    if parsed.scheme.lower() not in _MONGO_SCHEMES or not parsed.netloc or parsed.fragment:
        raise ValueError("MONGODB_URI must be a complete mongodb:// or mongodb+srv:// URI")


def _append_uri_options(uri: str, options: list[tuple[str, str]]) -> str:
    if not options:
        return uri
    parsed = urlsplit(uri)
    existing = {name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    additions = [
        f"{quote_plus(name)}={quote_plus(value)}"
        for name, value in options
        if name.lower() not in existing
    ]
    if not additions:
        return uri
    if "?" not in uri:
        separator = "?"
    elif uri.endswith(("?", "&")):
        separator = ""
    else:
        separator = "&"
    return f"{uri}{separator}{'&'.join(additions)}"


def _mongo_tls_options(environment: Mapping[str, str]) -> list[tuple[str, str]]:
    ca_file = _environment_value(environment, "MONGO_TLS_CA_FILE")
    certificate_file = _environment_value(environment, "MONGO_TLS_CERT_FILE")
    key_file = _environment_value(environment, "MONGO_TLS_KEY_FILE")
    if key_file and key_file != certificate_file:
        raise ValueError(
            "MONGO_TLS_CERT_FILE must reference a combined certificate/key PEM; "
            "a separate MONGO_TLS_KEY_FILE is not supported by PyMongo"
        )
    allow_invalid = _environment_boolean(environment, "MONGO_TLS_ALLOW_INVALID_CERTIFICATES")
    production = (environment.get("APP_ENVIRONMENT") or "").strip().lower() in {
        "prod",
        "production",
    }
    if allow_invalid and production:
        raise ValueError("MONGO_TLS_ALLOW_INVALID_CERTIFICATES is forbidden in production")
    tls_enabled = _environment_boolean(environment, "MONGO_TLS_ENABLED")
    tls_enabled = tls_enabled or bool(ca_file or certificate_file or allow_invalid)
    if not tls_enabled:
        return []
    options = [("tls", "true")]
    if ca_file:
        options.append(("tlsCAFile", ca_file))
    if certificate_file:
        options.append(("tlsCertificateKeyFile", certificate_file))
    if allow_invalid:
        options.append(("tlsAllowInvalidCertificates", "true"))
    return options


def mongo_connection_uri(environment: Mapping[str, str] | None = None) -> str:
    """Resolve the provider-neutral Mongo connection contract.

    ``MONGODB_URI`` is authoritative and remains intact, including its database,
    SRV record, replica-set, authentication, and Atlas options.  The existing
    decomposed ``MONGO_*`` variables remain a compatibility input.  Optional
    mounted CA and combined client certificate/key PEM files are added as URI
    options so every Motor/PyMongo call site receives the same TLS behavior.
    """

    values: Mapping[str, str] = os.environ if environment is None else environment
    explicit_uri = _environment_value(values, "MONGODB_URI", "MONGO_URI")
    tls_options = _mongo_tls_options(values)
    if explicit_uri:
        _validate_mongo_uri(explicit_uri)
        return _append_uri_options(explicit_uri, tls_options)

    host = _environment_value(values, "MONGO_HOST")
    if not host:
        raise ValueError("MongoDB is not configured; set MONGODB_URI or MONGO_HOST")
    if (
        "://" in host
        or any(character.isspace() for character in host)
        or any(character in host for character in "/?#")
    ):
        raise ValueError("MONGO_HOST must contain host names and ports only")

    username = _environment_value(values, "MONGO_USERNAME", "MONGODB_USER")
    password = _environment_value(values, "MONGO_PASSWORD", "MONGODB_PASS")
    if bool(username) != bool(password):
        raise ValueError(
            "MONGO_USERNAME and MONGO_PASSWORD must either both be set or both be absent"
        )
    credentials = ""
    if username and password:
        credentials = f"{quote_plus(username)}:{quote_plus(password)}@"

    srv = _environment_boolean(values, "MONGO_SRV")
    scheme = "mongodb+srv" if srv else "mongodb"
    options: list[tuple[str, str]] = [
        ("retryWrites", "true"),
        ("w", "majority"),
    ]
    optional_parameters = (
        ("replicaSet", "MONGO_REPLICA_SET"),
        ("authSource", "MONGO_AUTH_SOURCE"),
        ("authMechanism", "MONGO_AUTH_MECHANISM"),
        ("appName", "MONGO_APP_NAME"),
        ("readConcernLevel", "MONGO_READ_CONCERN_LEVEL"),
    )
    for parameter, variable in optional_parameters:
        value = _environment_value(values, variable)
        if value:
            options.append((parameter, value))
    if _environment_boolean(values, "MONGO_WRITE_CONCERN_J"):
        options.append(("journal", "true"))
    options.extend(tls_options)
    uri = f"{scheme}://{credentials}{host}/"
    resolved = _append_uri_options(uri, options)
    _validate_mongo_uri(resolved)
    return resolved


def redact_mongo_uri(uri: str) -> str:
    """Return a diagnostic-safe Mongo URI without credentials or secret options."""

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return "<invalid-mongodb-uri>"
    if parsed.scheme.lower() not in _MONGO_SCHEMES or not parsed.netloc:
        return "<invalid-mongodb-uri>"
    authority = parsed.netloc
    if "@" in authority:
        authority = f"<credentials-redacted>@{authority.rsplit('@', 1)[-1]}"
    redacted_options: list[str] = []
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = name.lower()
        if normalized == "authmechanismproperties" or any(
            token in normalized for token in ("password", "secret", "token")
        ):
            value = "<redacted>"
        redacted_options.append(f"{quote_plus(name)}={quote_plus(value)}")
    suffix = f"?{'&'.join(redacted_options)}" if redacted_options else ""
    return f"{parsed.scheme}://{authority}{parsed.path}{suffix}"


def require_mongo_client(client: Any | None) -> Any:
    """Return an initialized client or fail with one consistent lifecycle error."""
    if client is None:
        raise RuntimeError("MongoDB client not initialized. Call initialize() first.")
    return client


def cached_mongo_database(
    client: Any | None,
    databases: MutableMapping[str, Any],
    database_name: str,
) -> Any:
    """Validate and cache one database handle without constructing a new client."""
    resolved_client = require_mongo_client(client)
    if not database_name:
        raise ValueError("database_name is required and cannot be empty")
    cached = databases.get(database_name)
    if cached is None:
        cached = resolved_client[database_name]
        databases[database_name] = cached
    return cached


def mongo_collection(database: Any, collection_name: str) -> Any:
    """Resolve a collection from an already-owned database handle."""
    return database[collection_name]


async def warm_mongo_connection_pool(client: Any | None, connections: int) -> int:
    """Ping a bounded number of pool connections and report successful warmups."""
    if client is None:
        return 0
    warm_count = min(max(int(connections), 0), 10)
    outcomes = await asyncio.gather(
        *(client.admin.command("ping") for _ in range(warm_count)),
        return_exceptions=True,
    )
    return sum(not isinstance(outcome, BaseException) for outcome in outcomes)


@asynccontextmanager
async def resilient_mongo_database(
    database_name: str,
    *,
    mongo_client: Any,
    recoverable_errors: tuple[type[BaseException], ...],
    logger: Any,
) -> AsyncIterator[Any]:
    """Yield a database with bounded reconnects and exponential backoff."""
    if not database_name:
        raise ValueError("database_name is required and cannot be empty")
    retry_delay = 0.1
    for attempt in range(3):
        try:
            await mongo_client.initialize()
            yield mongo_client.get_database(database_name)
            return
        except recoverable_errors as exc:
            await mongo_client.reset()
            if attempt < 2:
                logger.warning(
                    "MongoDB connection attempt %s failed error_type=%s; retrying in %.1fs",
                    attempt + 1,
                    type(exc).__name__,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
                continue
            logger.error("MongoDB connection failed after 3 attempts")
            raise
        except Exception as exc:
            if "after close" in str(exc).lower():
                await mongo_client.reset()
            logger.error("Unexpected MongoDB error", exc_info=True)
            raise


def optimize_projection(
    include_fields: list[str] | None = None,
    exclude_fields: list[str] | None = None,
) -> dict[str, int] | None:
    """Build a bounded projection that omits ``_id`` unless requested."""
    if include_fields:
        projection = {field: 1 for field in include_fields}
        if "_id" not in include_fields:
            projection["_id"] = 0
        return projection
    if exclude_fields:
        return {field: 0 for field in exclude_fields}
    return None


def connection_usage_log(stats: dict[str, Any]) -> tuple[int, str] | None:
    """Return logging level and message for a meaningful Mongo pool-usage band."""
    current = int(stats.get("current_connections", 0))
    available = int(stats.get("available_connections", 0))
    total_capacity = current + available
    if total_capacity <= 0:
        return None
    usage_percent = (current / total_capacity) * 100
    if usage_percent > 90:
        return 40, (
            f"CRITICAL: MongoDB connection usage: {current}/{total_capacity} ({usage_percent:.1f}%)"
        )
    if usage_percent > 80:
        return 30, (
            f"HIGH: MongoDB connection usage: {current}/{total_capacity} ({usage_percent:.1f}%)"
        )
    if usage_percent > 60:
        return 20, (f"MongoDB connection usage: {current}/{total_capacity} ({usage_percent:.1f}%)")
    return None


async def log_mongo_connection_status(
    get_stats: Callable[[], Awaitable[dict[str, Any]]],
    emit: Callable[[int, str], Any],
) -> None:
    """Fetch pool statistics and emit the applicable shared usage band."""
    log_entry = connection_usage_log(await get_stats())
    if log_entry is not None:
        emit(*log_entry)


__all__ = [
    "cached_mongo_database",
    "connection_usage_log",
    "log_mongo_connection_status",
    "mongo_connection_uri",
    "mongo_collection",
    "optimize_projection",
    "redact_mongo_uri",
    "require_mongo_client",
    "resilient_mongo_database",
    "warm_mongo_connection_pool",
]
