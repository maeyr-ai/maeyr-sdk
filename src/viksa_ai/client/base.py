"""HTTP client base and ``ViksaClient`` factory."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, Iterator, Literal, Optional

import httpx

from viksa_ai._constants import (
    DEFAULT_BASE_URL,
    ENV_ACCESS_TOKEN,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_EMAIL,
    ENV_ORG_ID,
    ENV_PASSWORD,
    ENV_PROJECT_ID,
    ENV_REFRESH_TOKEN,
)
from viksa_ai.client.auth import AuthClient
from viksa_ai.client.builder import BuilderClient
from viksa_ai.client.chat import ChatClient
from viksa_ai.client.config import ClientConfig, RetryConfig
from viksa_ai.client.errors import (
    ViksaApiError,
    raise_for_response,
    wrap_transport_error,
)
from viksa_ai.client.marketplace import MarketplaceClient
from viksa_ai.client.pulse import PulseClient
from viksa_ai.client.scheduler import SchedulerClient
from viksa_ai.client.transport import HttpTransport
from viksa_ai.client.webhook import WebhookClient
from viksa_ai.client.workflow import WorkflowClient

__version__ = "0.2.7"

AuthKind = Literal["access_token", "api_key"]


class ViksaClient:
    """
    Viksa AI platform HTTP client with typed errors, retries, and service sub-clients.

    Authentication modes:

    * **JWT** — pass ``access_token`` (from login or the console).
    * **API key** — use :meth:`from_api_key` or set ``VIKSA_API_KEY`` in :meth:`from_env`.
    * **Email/password** — use :meth:`from_login` / :meth:`from_login_sync` or
      ``VIKSA_EMAIL`` + ``VIKSA_PASSWORD`` in :meth:`from_env`.

    Set ``base_url`` (or ``VIKSA_BASE_URL``) for self-hosted or staging gateways.

    Example::

        async with ViksaClient(access_token=token, org_id=org, project_id=proj) as client:
            me = await client.auth.me()
            async for agent in client.builder.agents.iter_all():
                print(agent["agent_name"])
    """

    def __init__(
        self,
        access_token: str,
        *,
        org_id: Optional[str] = None,
        project_id: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        refresh_token: Optional[str] = None,
        auth_kind: AuthKind = "access_token",
        timeout: float = 60.0,
        config: Optional[ClientConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token if auth_kind == "access_token" else None
        self.auth_kind: AuthKind = auth_kind
        self.org_id = org_id
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self.config = config or ClientConfig(timeout=timeout)
        if retry is not None:
            self.config.retry = retry
        if auth_kind == "api_key":
            self.config.auto_refresh_on_401 = False

        self._transport = HttpTransport(
            base_url=self.base_url,
            headers=self._build_headers(),
            config=self.config,
        )

        self.auth = AuthClient(self)
        self.builder = BuilderClient(self)
        self.chat = ChatClient(self)
        self.pulse = PulseClient(self)
        self.workflow = WorkflowClient(self)
        self.scheduler = SchedulerClient(self)
        self.marketplace = MarketplaceClient(self)

    @property
    def api_key(self) -> Optional[str]:
        """Project API key when ``auth_kind`` is ``api_key``."""
        return self.access_token if self.auth_kind == "api_key" else None

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        org_id: Optional[str] = None,
        project_id: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        validate: bool = False,
        **kwargs: Any,
    ) -> ViksaClient:
        """
        Create a client authenticated with a project API key (sent as ``Bearer``).

        When ``validate`` is true, calls ``POST /auth/key/validate/api`` and fills
        ``org_id`` / ``project_id`` from the response when not provided.
        """
        client = cls(
            access_token=api_key,
            org_id=org_id,
            project_id=project_id,
            base_url=base_url,
            auth_kind="api_key",
            **kwargs,
        )
        if validate:
            result = client.auth.validate_api_key_sync(api_key)
            if not result.valid:
                raise ValueError(result.error or "Invalid API key")
            if not client.org_id and result.org_id:
                client.org_id = result.org_id
            if not client.project_id and result.project_id:
                client.project_id = result.project_id
            client._sync_headers()
        return client

    @classmethod
    async def from_login(
        cls,
        email: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> ViksaClient:
        """Log in with email/password and return an authenticated client."""
        client = cls(
            access_token="",
            base_url=base_url,
            auth_kind="access_token",
            **kwargs,
        )
        await client.auth.login(email, password)
        return client

    @classmethod
    def from_login_sync(
        cls,
        email: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> ViksaClient:
        """Synchronous variant of :meth:`from_login`."""
        client = cls(
            access_token="",
            base_url=base_url,
            auth_kind="access_token",
            **kwargs,
        )
        client.auth.login_sync(email, password)
        return client

    @classmethod
    def from_env(cls, **kwargs: Any) -> ViksaClient:
        """
        Build a client from environment variables.

        Precedence: ``VIKSA_API_KEY`` → ``VIKSA_ACCESS_TOKEN`` →
        ``VIKSA_EMAIL`` + ``VIKSA_PASSWORD`` (login).
        """
        base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
        org_id = os.environ.get(ENV_ORG_ID)
        project_id = os.environ.get(ENV_PROJECT_ID)
        api_key = os.environ.get(ENV_API_KEY)
        if api_key:
            return cls.from_api_key(
                api_key,
                org_id=org_id,
                project_id=project_id,
                base_url=base_url,
                **kwargs,
            )
        token = os.environ.get(ENV_ACCESS_TOKEN)
        if token:
            return cls(
                access_token=token,
                org_id=org_id,
                project_id=project_id,
                base_url=base_url,
                refresh_token=os.environ.get(ENV_REFRESH_TOKEN),
                **kwargs,
            )
        email = os.environ.get(ENV_EMAIL)
        password = os.environ.get(ENV_PASSWORD)
        if email and password:
            return cls.from_login_sync(
                email,
                password,
                base_url=base_url,
                org_id=org_id,
                project_id=project_id,
                **kwargs,
            )
        raise ValueError(
            "Set one of: VIKSA_API_KEY, VIKSA_ACCESS_TOKEN, or VIKSA_EMAIL and VIKSA_PASSWORD"
        )

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.org_id:
            headers["X-Tenant-Org-Id"] = self.org_id
        if self.project_id:
            headers["X-Tenant-Project-Id"] = self.project_id
        return headers

    def _sync_headers(self) -> None:
        self._transport.update_headers(self._build_headers())

    async def request(
        self,
        method: str,
        path_prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Any:
        """
        Low-level API call for routes not yet wrapped by a sub-client.

        ``path_prefix`` is the gateway prefix (e.g. ``/builder``,
        ``/marketplace/api/v1/marketplace``).
        """
        return await self._arequest(
            method,
            path_prefix,
            path,
            json=json,
            params=params,
            headers=headers,
            retry=retry,
        )

    def request_sync(self, method: str, path_prefix: str, path: str, **kwargs: Any) -> Any:
        return self._request(method, path_prefix, path, **kwargs)

    async def _arequest(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[RetryConfig] = None,
        _retry_auth: bool = True,
    ) -> Any:
        try:
            return await self._transport.arequest(
                method,
                prefix,
                path,
                json=json,
                params=params,
                headers=headers,
                retry=retry,
            )
        except ViksaApiError as exc:
            if (
                _retry_auth
                and self.auth_kind == "access_token"
                and self.config.auto_refresh_on_401
                and exc.status_code == 401
                and self.refresh_token
            ):
                await self.auth.refresh()
                return await self._arequest(
                    method,
                    prefix,
                    path,
                    json=json,
                    params=params,
                    headers=headers,
                    retry=retry,
                    _retry_auth=False,
                )
            raise

    def _request(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[RetryConfig] = None,
        _retry_auth: bool = True,
    ) -> Any:
        try:
            return self._transport.request(
                method,
                prefix,
                path,
                json=json,
                params=params,
                headers=headers,
                retry=retry,
            )
        except ViksaApiError as exc:
            if (
                _retry_auth
                and self.auth_kind == "access_token"
                and self.config.auto_refresh_on_401
                and exc.status_code == 401
                and self.refresh_token
            ):
                self.auth.refresh_sync()
                return self._request(
                    method,
                    prefix,
                    path,
                    json=json,
                    params=params,
                    headers=headers,
                    retry=retry,
                    _retry_auth=False,
                )
            raise

    async def aclose(self) -> None:
        await self._transport.aclose()

    def close(self) -> None:
        self._transport.close()

    async def __aenter__(self) -> ViksaClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def __enter__(self) -> ViksaClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def webhook(
        trigger_id: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        webhook_token: Optional[str] = None,
        timeout: float = 60.0,
    ) -> WebhookClient:
        """Create a client for public webhook trigger endpoints (no platform JWT)."""
        return WebhookClient(
            trigger_id,
            base_url=base_url,
            webhook_token=webhook_token,
            timeout=timeout,
        )

    @staticmethod
    def iter_sse_lines(response: httpx.Response) -> Iterator[Dict[str, Any]]:
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

    async def _astream(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        url = f"{self.base_url}{prefix}{path}"
        service = prefix.strip("/").split("/")[0] or prefix
        try:
            client = self._transport.get_async_client()
            async with client.stream(method, url, json=json_body, params=params) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_response(response, service=service, method=method, path=path)
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, method=method, url=url) from exc
