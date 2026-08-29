"""Public webhook client (no platform JWT)."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, cast

import httpx

from maeyr._constants import DEFAULT_BASE_URL, SERVICE_PATHS
from maeyr.client.errors import raise_for_response, wrap_transport_error
from maeyr.client.sse import JsonSseDecoder


class WebhookClient:
    """
    Invoke chat webhook triggers using HMAC signature or bearer webhook token.

    Does not use :class:`MaeyrClient` JWT authentication.
    """

    def __init__(
        self,
        trigger_id: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        webhook_token: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.trigger_id = trigger_id
        self.base_url = base_url.rstrip("/")
        self.webhook_token = webhook_token
        self._prefix = SERVICE_PATHS["chat"]
        self._timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.webhook_token:
            headers["Authorization"] = f"Bearer {self.webhook_token}"
        if extra:
            headers.update(extra)
        return headers

    async def invoke(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{self._prefix}/webhook/trigger/{self.trigger_id}"
        path = f"/webhook/trigger/{self.trigger_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, method="POST", url=url) from exc
        if response.status_code >= 400:
            raise_for_response(response, service="chat", method="POST", path=path)
        return cast(Dict[str, Any], response.json())

    async def stream(self, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        url = f"{self.base_url}{self._prefix}/webhook/trigger/{self.trigger_id}/stream"
        path = f"/webhook/trigger/{self.trigger_id}/stream"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise_for_response(response, service="chat", method="POST", path=path)
                    decoder = JsonSseDecoder()
                    async for line in response.aiter_lines():
                        event = decoder.feed(line)
                        if event is not None:
                            yield event
                        if decoder.finished:
                            break
                    event = decoder.finish()
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, method="POST", url=url) from exc

    async def health(self) -> Dict[str, Any]:
        url = f"{self.base_url}{self._prefix}/webhook/trigger/{self.trigger_id}/health"
        path = f"/webhook/trigger/{self.trigger_id}/health"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, method="GET", url=url) from exc
        if response.status_code >= 400:
            raise_for_response(response, service="chat", method="GET", path=path)
        return cast(Dict[str, Any], response.json())
