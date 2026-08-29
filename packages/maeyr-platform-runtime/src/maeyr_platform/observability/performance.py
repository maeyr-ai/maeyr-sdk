"""Shared request timing middleware for platform HTTP entrypoints."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint


class PerformanceLogger(Protocol):
    """The structured logging surface used by request timing."""

    def warning(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...


PerformanceMiddleware = Callable[
    [Request, RequestResponseEndpoint],
    Awaitable[Response],
]


def create_request_performance_middleware(
    *,
    logger: PerformanceLogger,
    slow_request_seconds: float = 1.0,
    clock: Callable[[], float] = time.time,
) -> PerformanceMiddleware:
    """Create the common timing/header/logging middleware without global state."""

    if slow_request_seconds <= 0:
        raise ValueError("slow_request_seconds must be positive")

    async def performance_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        started_at = clock()
        try:
            response = await call_next(request)
            elapsed = clock() - started_at
            response.headers["X-Process-Time"] = str(elapsed)
            if elapsed > slow_request_seconds:
                logger.warning(
                    "Slow request: %s %s took %.2fs",
                    request.method,
                    request.url.path,
                    elapsed,
                )
            return response
        except Exception as exc:
            elapsed = clock() - started_at
            logger.error(
                "Request failed method=%s path=%s duration_seconds=%.2f error_type=%s",
                request.method,
                request.url.path,
                elapsed,
                type(exc).__name__,
            )
            raise

    return performance_middleware


__all__ = [
    "PerformanceLogger",
    "PerformanceMiddleware",
    "create_request_performance_middleware",
]
