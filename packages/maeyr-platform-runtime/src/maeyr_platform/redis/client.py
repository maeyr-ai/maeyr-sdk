"""Dependency-injected Redis client lifecycle primitives."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast


async def connect_redis_client(client_state: Any) -> None:
    """Connect and publish a client only after its readiness probe succeeds."""
    if client_state._redis is not None:
        return

    client = None
    try:
        settings = client_state._settings
        connection_params = {
            "encoding": "utf-8",
            "decode_responses": True,
            "max_connections": settings.MAX_CONNECTIONS,
            "retry_on_timeout": settings.RETRY_ON_TIMEOUT,
        }
        if settings.PASSWORD:
            connection_params["password"] = settings.PASSWORD
        connection_params.update(settings.tls_connection_kwargs())
        from_url = cast(Callable[..., Any], client_state._redis_module.from_url)
        client = from_url(settings.CONNECTION_STRING, **connection_params)
        await client.ping()
        client_state._redis = client
        client_state._logger.info("Successfully connected to Redis")
    except Exception as exc:
        if client is not None:
            close = getattr(client, "aclose", client.close)
            await close()
        client_state._logger.error(
            "Failed to connect to Redis (%s)",
            type(exc).__name__,
        )
        raise


__all__ = ["connect_redis_client"]
