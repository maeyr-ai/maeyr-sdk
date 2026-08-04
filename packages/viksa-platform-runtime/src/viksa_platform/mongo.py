"""Pure Mongo projection and connection-usage policies."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any


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
    "mongo_collection",
    "optimize_projection",
    "require_mongo_client",
    "resilient_mongo_database",
    "warm_mongo_connection_pool",
]
