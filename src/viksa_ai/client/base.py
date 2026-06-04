"""HTTP client base and ``ViksaClient`` factory."""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, Optional

import httpx

from viksa_ai._constants import (
    DEFAULT_BASE_URL,
    ENV_ACCESS_TOKEN,
    ENV_BASE_URL,
    ENV_ORG_ID,
    ENV_PROJECT_ID,
    ENV_REFRESH_TOKEN,
    SERVICE_PATHS,
)
from viksa_ai.client.auth import AuthClient
from viksa_ai.client.builder import BuilderClient
from viksa_ai.client.chat import ChatClient
from viksa_ai.client.errors import ViksaApiError
from viksa_ai.client.pulse import PulseClient
from viksa_ai.client.scheduler import SchedulerClient
from viksa_ai.client.workflow import WorkflowClient

__version__ = "0.1.0"


class _ServiceClient:
    def __init__(self, parent: ViksaClient, service_key: str) -> None:
        self._parent = parent
        self._prefix = SERVICE_PATHS[service_key]

    def _url(self, path: str) -> str:
        return f"{self._parent.base_url}{self._prefix}{path}"

    async def _arequest(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return await self._parent._arequest(method, self._prefix, path, json=json, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._parent._request(method, self._prefix, path, json=json, params=params)


class ViksaClient:
    """
    Viksa AI platform HTTP client.

    Example::

        async with ViksaClient(access_token=token, org_id=org, project_id=proj) as client:
            me = await client.auth.me()
            agents = await client.builder.agents.list()
    """

    def __init__(
        self,
        access_token: str,
        *,
        org_id: Optional[str] = None,
        project_id: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        refresh_token: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.org_id = org_id
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

        self.auth = AuthClient(self)
        self.builder = BuilderClient(self)
        self.chat = ChatClient(self)
        self.pulse = PulseClient(self)
        self.workflow = WorkflowClient(self)
        self.scheduler = SchedulerClient(self)

    @classmethod
    def from_env(cls) -> ViksaClient:
        token = os.environ.get(ENV_ACCESS_TOKEN)
        if not token:
            raise ValueError(f"{ENV_ACCESS_TOKEN} environment variable is required")
        return cls(
            access_token=token,
            org_id=os.environ.get(ENV_ORG_ID),
            project_id=os.environ.get(ENV_PROJECT_ID),
            base_url=os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL),
            refresh_token=os.environ.get(ENV_REFRESH_TOKEN),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.org_id:
            headers["X-Tenant-Org-Id"] = self.org_id
        if self.project_id:
            headers["X-Tenant-Project-Id"] = self.project_id
        return headers

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._headers(),
            )
        return self._async_client

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=self._timeout,
                headers=self._headers(),
            )
        return self._sync_client

    async def _arequest(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{prefix}{path}"
        client = self._get_async_client()
        response = await client.request(method, url, json=json, params=params)
        if response.status_code == 401 and self.refresh_token:
            await self.auth.refresh()
            response = await client.request(method, url, json=json, params=params)
        return self._parse_response(response, prefix.strip("/"))

    def _request(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{prefix}{path}"
        client = self._get_sync_client()
        response = client.request(method, url, json=json, params=params)
        if response.status_code == 401 and self.refresh_token:
            self.auth.refresh_sync()
            response = client.request(method, url, json=json, params=params)
        return self._parse_response(response, prefix.strip("/"))

    @staticmethod
    def _parse_response(response: httpx.Response, service: str) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise ViksaApiError(
                f"API error {response.status_code} from {service}",
                status_code=response.status_code,
                body=body,
                service=service,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def __aenter__(self) -> ViksaClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def __enter__(self) -> ViksaClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def iter_sse_lines(response: httpx.Response) -> Iterator[Dict[str, Any]]:
        """Parse ``data: {...}`` lines from an SSE response body."""
        import json

        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
