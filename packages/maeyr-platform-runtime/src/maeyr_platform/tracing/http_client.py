"""Canonical non-blocking HTTP client spans with trace propagation."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, MutableMapping, Optional, Union
from urllib.parse import urlparse

import httpx

from .constants import SPAN_HTTP_CLIENT, SpanKind, SpanOperation
from .context import get_trace_context
from .ids import generate_span_id, normalize_span_id
from .propagation import merge_trace_headers
from .recorder import record_span
from .semconv import ATTR_HTTP_METHOD, ATTR_HTTP_ROUTE, ATTR_HTTP_STATUS

HeaderMapping = Union[Mapping[str, str], MutableMapping[str, str], Dict[str, str]]


class HTTPResponseTooLargeError(RuntimeError):
    """A decoded HTTP response exceeded its configured in-memory byte budget."""


def _merge_headers(headers: Optional[HeaderMapping]) -> Dict[str, str]:
    base: Dict[str, str] = {}
    if headers:
        base.update({str(k): str(v) for k, v in headers.items()})
    return merge_trace_headers(base)


def schedule_http_client_span(
    *,
    method: str,
    url: str,
    status_code: int,
    duration_ms: int,
    started_at: datetime,
    service: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    ctx = get_trace_context()
    if not ctx:
        return
    span_id = normalize_span_id(generate_span_id())
    parsed = urlparse(url)
    route = parsed.path or url
    status = "ok" if status_code < 400 and not error_message else "error"
    attrs: Dict[str, Any] = {
        ATTR_HTTP_METHOD: method.upper(),
        ATTR_HTTP_ROUTE: route,
        ATTR_HTTP_STATUS: status_code,
        "url.full": url,
        "server.address": parsed.hostname or "",
    }
    if error_message:
        attrs["error.message"] = error_message[:500]

    async def _emit() -> None:
        await record_span(
            span_id=span_id,
            trace_id=ctx.trace_id,
            parent_span_id=ctx.span_id,
            span_name=SPAN_HTTP_CLIENT,
            operation=SpanOperation.HTTP_CLIENT.value,
            span_kind=SpanKind.CLIENT.value,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            duration_ms=max(duration_ms, 0),
            attributes=attrs,
            service=service or ctx.service,
            account_id=ctx.account_id,
            org_id=ctx.org_id,
            project_id=ctx.project_id,
            user_id=ctx.user_id,
            user_email=ctx.user_email,
            activity_id=ctx.activity_id,
            entity_type=ctx.entity_type,
            entity_id=ctx.entity_id,
            resource_refs=ctx.resource_refs,
        )

    asyncio.create_task(_emit(), name="http_client_span")


async def _bounded_httpx_request(
    client: Any,
    method: str,
    url: str,
    *,
    max_response_bytes: int,
    **kwargs: Any,
) -> httpx.Response:
    if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool):
        raise ValueError("max_response_bytes must be a positive integer")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")

    async with client.stream(method, url, **kwargs) as response:
        raw_content_length = response.headers.get("content-length")
        if raw_content_length:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                content_length = None
            if content_length is not None and content_length > max_response_bytes:
                raise HTTPResponseTooLargeError("HTTP response exceeds the configured size limit")

        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > max_response_bytes:
                raise HTTPResponseTooLargeError("HTTP response exceeds the configured size limit")
            content.extend(chunk)

        decoded_headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            status_code=response.status_code,
            headers=decoded_headers,
            content=bytes(content),
            request=response.request,
            extensions=dict(response.extensions),
            history=list(response.history),
        )


async def traced_httpx_request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
    """Wrap httpx.AsyncClient.request with propagation + http.client span."""
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()
    headers = _merge_headers(kwargs.pop("headers", None))
    kwargs["headers"] = headers
    service = kwargs.pop("service", None)
    max_response_bytes = kwargs.pop("max_response_bytes", None)
    status_code = 0
    err: Optional[str] = None
    try:
        if max_response_bytes is None:
            response = await client.request(method, url, **kwargs)
        else:
            response = await _bounded_httpx_request(
                client,
                method,
                url,
                max_response_bytes=max_response_bytes,
                **kwargs,
            )
        status_code = int(response.status_code)
        return response
    except Exception as exc:
        err = str(exc)
        status_code = 0
        raise
    finally:
        schedule_http_client_span(
            method=method,
            url=url,
            status_code=status_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
            started_at=started_at,
            service=service if isinstance(service, str) else None,
            error_message=err,
        )


async def traced_aiohttp_post_json(
    session: Any,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> tuple[int, Any]:
    """POST via aiohttp with propagation + http.client span. Returns (status, json_body)."""
    import async_timeout

    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()
    merged = _merge_headers(headers)
    status_code = 0
    err: Optional[str] = None
    try:
        async with async_timeout.timeout(timeout or 30):
            async with session.post(url, json=json_data, headers=merged, **kwargs) as response:
                status_code = int(response.status)
                body = await response.json()
                return status_code, body
    except Exception as exc:
        err = str(exc)
        raise
    finally:
        schedule_http_client_span(
            method="POST",
            url=url,
            status_code=status_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
            started_at=started_at,
            error_message=err,
        )
