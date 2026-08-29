"""Canonical application metrics HTTP endpoint factory."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException


class MetricsSettings(Protocol):
    METRICS_ENABLED: bool
    NAME: str
    VERSION: str


class MongoHealthView(Protocol):
    def get_pool_status(self) -> Awaitable[dict[str, Any]]: ...


MetricsHandler = Callable[[], Awaitable[dict[str, Any]]]


def build_metrics_endpoint(
    settings: MetricsSettings,
    mongo_health: MongoHealthView,
) -> tuple[APIRouter, MetricsHandler]:
    """Build a service-bound router while keeping endpoint policy shared."""
    router = APIRouter(prefix="/metrics", tags=["Metrics"])

    async def metrics() -> dict[str, Any]:
        if not settings.METRICS_ENABLED:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        return {
            "database_pool": await mongo_health.get_pool_status(),
            "service": settings.NAME,
            "version": settings.VERSION,
            "uptime": time.time(),
        }

    router.get("/")(metrics)
    return router, metrics


__all__ = ["MetricsHandler", "MetricsSettings", "MongoHealthView", "build_metrics_endpoint"]
