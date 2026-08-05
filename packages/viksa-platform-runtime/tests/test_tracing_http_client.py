from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from viksa_platform.tracing.http_client import (
    HTTPResponseTooLargeError,
    traced_httpx_request,
)


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_traced_httpx_request_rejects_oversized_chunked_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkedStream([b"1234", b"56789"]),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPResponseTooLargeError):
            await traced_httpx_request(
                client,
                "GET",
                "https://service.test/data",
                max_response_bytes=8,
            )


@pytest.mark.asyncio
async def test_traced_httpx_request_returns_reusable_bounded_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await traced_httpx_request(
            client,
            "GET",
            "https://service.test/data",
            max_response_bytes=1024,
        )

    assert response.json() == {"ok": True}
