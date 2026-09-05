"""Service-level operational metric payload helpers."""

from __future__ import annotations

from typing import Any


def service_metrics_payload(
    *,
    database_pool: dict[str, Any],
    service: str,
    version: str,
    uptime: float,
) -> dict[str, Any]:
    """Build the stable cross-service metrics response shape."""
    return {
        "database_pool": database_pool,
        "service": service,
        "version": version,
        "uptime": uptime,
    }


__all__ = ["service_metrics_payload"]
