from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import Request, Response

from viksa_platform.observability.performance import (
    create_request_performance_middleware,
)


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple[object, ...]]] = []
        self.errors: list[tuple[str, tuple[object, ...]]] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))

    def error(self, message: str, *args: object) -> None:
        self.errors.append((message, args))


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [],
            "server": ("example.test", 443),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


def _clock(*values: float) -> Callable[[], float]:
    readings: Iterator[float] = iter(values)
    return lambda: next(readings)


@pytest.mark.asyncio
async def test_success_adds_timing_header_and_logs_only_slow_requests() -> None:
    logger = _Logger()
    middleware = create_request_performance_middleware(
        logger=logger,
        clock=_clock(10.0, 11.25),
    )

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = await middleware(_request(), call_next)

    assert isinstance(response, Response)
    assert response.headers["X-Process-Time"] == "1.25"
    assert logger.warnings == [
        ("Slow request: %s %s took %.2fs", ("GET", "/health", 1.25))
    ]
    assert logger.errors == []


@pytest.mark.asyncio
async def test_failure_logs_only_the_exception_type_and_reraises() -> None:
    logger = _Logger()
    middleware = create_request_performance_middleware(
        logger=logger,
        clock=_clock(20.0, 20.5),
    )

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("secret-that-must-not-be-logged")

    with pytest.raises(RuntimeError, match="secret-that-must-not-be-logged"):
        await middleware(_request(), call_next)

    assert logger.errors == [
        (
            "Request failed method=%s path=%s duration_seconds=%.2f error_type=%s",
            ("GET", "/health", 0.5, "RuntimeError"),
        )
    ]
    assert "secret-that-must-not-be-logged" not in repr(logger.errors)


def test_slow_request_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        create_request_performance_middleware(logger=_Logger(), slow_request_seconds=0)
