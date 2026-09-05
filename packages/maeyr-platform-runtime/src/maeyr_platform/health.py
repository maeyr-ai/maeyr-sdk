"""Typed HTTP health endpoints shared by service composition roots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import status
from fastapi.responses import JSONResponse


class AsyncConnectionHealth(Protocol):
    """Minimal dependency surface required by a readiness endpoint."""

    async def check_connection(self, database_name: str) -> bool: ...


ReadinessEndpoint = Callable[[], Awaitable[dict[str, str] | JSONResponse]]


def build_mongo_readiness_endpoint(
    health: AsyncConnectionHealth,
    *,
    database_name: str = "admin",
    backend_name: str = "mongodb",
) -> ReadinessEndpoint:
    """Build a fail-closed readiness handler around an injected health probe."""

    async def readiness_check() -> dict[str, str] | JSONResponse:
        if await health.check_connection(database_name):
            return {"status": "ready", "database": backend_name}
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not ready", "database": backend_name},
        )

    return readiness_check


__all__ = ["AsyncConnectionHealth", "ReadinessEndpoint", "build_mongo_readiness_endpoint"]
