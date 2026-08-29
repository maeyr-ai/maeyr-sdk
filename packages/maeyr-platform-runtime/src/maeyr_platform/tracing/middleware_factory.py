"""Canonical ASGI trace middleware factories with server spans."""

from __future__ import annotations

import contextvars
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context import TraceContext, clear_trace_context, get_trace_context
from .ids import generate_trace_id, normalize_trace_id
from .inbound import bind_inbound_trace, headers_from_asgi_scope
from .server_span import schedule_http_server_span_from_context

TRACE_HEADER = "X-Trace-ID"


def _bind_request_headers(
    headers: Mapping[str, str],
    service: str,
) -> tuple[str, Optional[contextvars.Token[Optional[TraceContext]]]]:
    normalized = {str(k).lower(): v for k, v in headers.items()}
    trace_id = normalized.get(TRACE_HEADER.lower()) or generate_trace_id()
    trace_id = normalize_trace_id(trace_id)
    trace_tok = bind_inbound_trace(normalized, service=service, fallback_trace_id=trace_id)
    return trace_id, trace_tok


def create_asgi_trace_middleware(service: str) -> type[Any]:
    """Pure ASGI middleware — safe for SSE/WebSocket."""

    class TraceSpanMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] not in ("http", "websocket"):
                await self.app(scope, receive, send)
                return

            path = scope.get("path", "")
            if (
                path.startswith("/health")
                or path.startswith("/metrics")
                or path.startswith("/internal/metrics")
            ):
                await self.app(scope, receive, send)
                return

            header_map = headers_from_asgi_scope(scope)
            trace_id, trace_tok = _bind_request_headers(header_map, service)
            edge_ctx = get_trace_context()

            started_at = datetime.now(timezone.utc)
            start_mono = time.perf_counter()
            status_code = 500 if scope["type"] == "http" else 101

            async def send_with_trace(message: Message) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message.get("status", 500))
                    headers = list(message.get("headers", []))
                    headers.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_trace)
            finally:
                if scope["type"] == "http":
                    ctx = get_trace_context() or edge_ctx
                    if ctx is not None:
                        schedule_http_server_span_from_context(
                            ctx,
                            method=scope.get("method", "GET"),
                            route=scope.get("path", ""),
                            status_code=status_code,
                            duration_ms=int((time.perf_counter() - start_mono) * 1000),
                            started_at=started_at,
                        )
                if trace_tok is not None:
                    clear_trace_context(trace_tok)

    TraceSpanMiddleware.__name__ = f"TraceSpanMiddleware_{service.replace('-', '_')}"
    return TraceSpanMiddleware


def create_http_trace_middleware(service: str) -> type[Any]:
    """Starlette BaseHTTP middleware with http.server spans."""

    from starlette.middleware.base import BaseHTTPMiddleware

    class TraceSpanMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            path = request.url.path
            if (
                path.startswith("/health")
                or path.startswith("/metrics")
                or path.startswith("/internal/metrics")
            ):
                return await call_next(request)

            trace_id, trace_tok = _bind_request_headers(dict(request.headers), service)
            edge_ctx = get_trace_context()
            started_at = datetime.now(timezone.utc)
            start_mono = time.perf_counter()
            status_code = 500
            try:
                response = await call_next(request)
                status_code = int(response.status_code)
                response.headers[TRACE_HEADER] = trace_id
                return response
            finally:
                ctx = get_trace_context() or edge_ctx
                if ctx is not None:
                    schedule_http_server_span_from_context(
                        ctx,
                        method=request.method,
                        route=request.url.path,
                        status_code=status_code,
                        duration_ms=int((time.perf_counter() - start_mono) * 1000),
                        started_at=started_at,
                    )
                if trace_tok is not None:
                    clear_trace_context(trace_tok)

    TraceSpanMiddleware.__name__ = f"TraceSpanMiddleware_{service.replace('-', '_')}"
    return TraceSpanMiddleware
