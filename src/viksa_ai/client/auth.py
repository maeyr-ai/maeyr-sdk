from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from viksa_ai._constants import SERVICE_PATHS
from viksa_ai.models.auth import (
    ApiKeyRequest,
    KeyValidationRequest,
    KeyValidationResponse,
    LoginRequest,
    RefreshRequest,
    SwitchOrgRequest,
    SwitchProjectRequest,
    TokenResponse,
    UserResponse,
)

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class _OrgClient:
    def __init__(self, auth: AuthClient) -> None:
        self._auth = auth
        self._prefix = SERVICE_PATHS["org"]

    async def list(self) -> Any:
        return await self._auth._client._arequest("GET", self._prefix, "/list")

    async def create(self, body: Dict[str, Any]) -> Any:
        return await self._auth._client._arequest("POST", self._prefix, "/create", json=body)

    async def get(self, org_id: str) -> Any:
        return await self._auth._client._arequest("GET", self._prefix, f"/{org_id}")

    async def update(self, org_id: str, body: Dict[str, Any]) -> Any:
        return await self._auth._client._arequest("PUT", self._prefix, f"/{org_id}", json=body)

    async def delete(self, org_id: str) -> Any:
        return await self._auth._client._arequest("DELETE", self._prefix, f"/{org_id}")


class _ProjectClient:
    def __init__(self, auth: AuthClient) -> None:
        self._auth = auth
        self._prefix = SERVICE_PATHS["project"]

    async def create(self, body: Dict[str, Any]) -> Any:
        return await self._auth._client._arequest("POST", self._prefix, "/create", json=body)

    async def list(self, org_id: str) -> Any:
        return await self._auth._client._arequest("GET", self._prefix, f"/list/{org_id}")

    async def get(self, project_id: str) -> Any:
        return await self._auth._client._arequest("GET", self._prefix, f"/{project_id}")

    async def update(self, project_id: str, body: Dict[str, Any]) -> Any:
        return await self._auth._client._arequest("PUT", self._prefix, f"/{project_id}", json=body)

    async def delete(self, project_id: str) -> Any:
        return await self._auth._client._arequest("DELETE", self._prefix, f"/{project_id}")


class AuthClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self._prefix = SERVICE_PATHS["auth"]
        self.orgs = _OrgClient(self)
        self.projects = _ProjectClient(self)

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

    async def logout(self) -> Any:
        return await self._client._arequest("POST", self._prefix, "/logout")

    async def logout_all(self) -> Any:
        return await self._client._arequest("POST", self._prefix, "/logout-all")

    async def me(self) -> UserResponse:
        data = await self._client._arequest("GET", self._prefix, "/me")
        return UserResponse.model_validate(data)

    def me_sync(self) -> UserResponse:
        data = self._client._request("GET", self._prefix, "/me")
        return UserResponse.model_validate(data)

    async def usage(self) -> Any:
        return await self._client._arequest("GET", self._prefix, "/usage")

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

    async def list_sessions(self) -> Any:
        return await self._client._arequest("GET", self._prefix, "/sessions")

    async def revoke_session(self, session_id: str) -> Any:
        return await self._client._arequest("DELETE", self._prefix, f"/sessions/{session_id}")

    async def revoke_all_sessions(self) -> Any:
        return await self._client._arequest("DELETE", self._prefix, "/sessions")

    async def validate_api_key(self, api_key: str) -> KeyValidationResponse:
        """Validate a project API key (no JWT required)."""
        data = await self._client._arequest(
            "POST",
            self._prefix,
            "/key/validate/api",
            json=KeyValidationRequest(api_key=api_key).model_dump(),
        )
        return KeyValidationResponse.model_validate(data)

    def validate_api_key_sync(self, api_key: str) -> KeyValidationResponse:
        data = self._client._request(
            "POST",
            self._prefix,
            "/key/validate/api",
            json=KeyValidationRequest(api_key=api_key).model_dump(),
        )
        return KeyValidationResponse.model_validate(data)

    async def create_api_key(self, name: str, description: Optional[str] = None) -> Any:
        return await self._client._arequest(
            "POST",
            self._prefix,
            "/key/api",
            json=ApiKeyRequest(name=name, description=description).model_dump(),
        )

    async def list_api_keys(self, *, skip: int = 0, limit: int = 50) -> Any:
        return await self._client._arequest(
            "GET",
            self._prefix,
            "/key/api",
            params={"skip": skip, "limit": limit},
        )

    async def revoke_api_key(self, key_id: str) -> Any:
        return await self._client._arequest("POST", self._prefix, f"/key/api/{key_id}/revoke")

    async def delete_api_key(self, key_id: str) -> Any:
        return await self._client._arequest("DELETE", self._prefix, f"/key/api/{key_id}")

    async def create_worker_key(self, name: str, description: Optional[str] = None) -> Any:
        return await self._client._arequest(
            "POST",
            self._prefix,
            "/key/worker",
            json=ApiKeyRequest(name=name, description=description).model_dump(),
        )

    async def list_worker_keys(self, *, skip: int = 0, limit: int = 50) -> Any:
        return await self._client._arequest(
            "GET",
            self._prefix,
            "/key/worker",
            params={"skip": skip, "limit": limit},
        )

    def _apply_tokens(self, tokens: TokenResponse) -> None:
        self._client.access_token = tokens.access_token
        self._client.refresh_token = tokens.refresh_token
        if tokens.org_id:
            self._client.org_id = tokens.org_id
        if tokens.project_id:
            self._client.project_id = tokens.project_id
        self._client._sync_headers()
