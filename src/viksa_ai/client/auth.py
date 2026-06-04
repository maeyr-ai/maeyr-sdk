from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from viksa_ai.models.auth import (
    ApiKeyRequest,
    LoginRequest,
    RefreshRequest,
    SwitchOrgRequest,
    SwitchProjectRequest,
    TokenResponse,
    UserResponse,
)

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class AuthClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self._prefix = "/auth"

    async def login(self, email: str, password: str) -> TokenResponse:
        data = await self._client._arequest(
            "POST",
            self._prefix,
            "/individual/login",
            json=LoginRequest(email=email, password=password).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    def login_sync(self, email: str, password: str) -> TokenResponse:
        data = self._client._request(
            "POST",
            self._prefix,
            "/individual/login",
            json=LoginRequest(email=email, password=password).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    async def refresh(self) -> TokenResponse:
        if not self._client.refresh_token:
            raise ValueError("refresh_token is required to refresh")
        data = await self._client._arequest(
            "POST",
            self._prefix,
            "/refresh",
            json=RefreshRequest(refresh_token=self._client.refresh_token).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    def refresh_sync(self) -> TokenResponse:
        if not self._client.refresh_token:
            raise ValueError("refresh_token is required to refresh")
        data = self._client._request(
            "POST",
            self._prefix,
            "/refresh",
            json=RefreshRequest(refresh_token=self._client.refresh_token).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    async def me(self) -> UserResponse:
        data = await self._client._arequest("GET", self._prefix, "/me")
        return UserResponse.model_validate(data)

    def me_sync(self) -> UserResponse:
        data = self._client._request("GET", self._prefix, "/me")
        return UserResponse.model_validate(data)

    async def switch_org(self, org_id: str) -> TokenResponse:
        data = await self._client._arequest(
            "POST",
            self._prefix,
            "/switch-org",
            json=SwitchOrgRequest(org_id=org_id).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    async def switch_project(self, project_id: str) -> TokenResponse:
        data = await self._client._arequest(
            "POST",
            self._prefix,
            "/switch-project",
            json=SwitchProjectRequest(project_id=project_id).model_dump(),
        )
        tokens = TokenResponse.model_validate(data)
        self._apply_tokens(tokens)
        return tokens

    async def create_api_key(self, name: str, description: Optional[str] = None) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST",
            self._prefix,
            "/key/api",
            json=ApiKeyRequest(name=name, description=description).model_dump(),
        )

    async def list_api_keys(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET",
            self._prefix,
            "/key/api",
            params={"skip": skip, "limit": limit},
        )

    def _apply_tokens(self, tokens: TokenResponse) -> None:
        self._client.access_token = tokens.access_token
        self._client.refresh_token = tokens.refresh_token
        if tokens.org_id:
            self._client.org_id = tokens.org_id
        if tokens.project_id:
            self._client.project_id = tokens.project_id
        if self._client._async_client is not None:
            self._client._async_client.headers.update(self._client._headers())
        if self._client._sync_client is not None:
            self._client._sync_client.headers.update(self._client._headers())
