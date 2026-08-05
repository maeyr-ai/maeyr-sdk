from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import quote

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
    WorkerKeyCreateRequest,
    WorkerKeyRateLimit,
    WorkerKeyScope,
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

    async def create_worker_key(
        self,
        name: str | WorkerKeyCreateRequest,
        description: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
        scopes: Optional[Sequence[WorkerKeyScope]] = None,
        expires_in_days: Optional[int] = None,
        key_id: Optional[str] = None,
        rate_limit: Optional[WorkerKeyRateLimit] = None,
    ) -> Any:
        """Create a project-scoped worker key using the current Auth contract.

        Passing a string preserves the pre-0.2.8 call shape. ``description`` is
        still accepted for source compatibility but is not sent because the
        Auth worker-key request does not define that field. New callers may pass
        a :class:`WorkerKeyCreateRequest` as the first argument.
        """
        if isinstance(name, WorkerKeyCreateRequest):
            if any(
                value is not None
                for value in (
                    description,
                    project_id,
                    scopes,
                    expires_in_days,
                    key_id,
                    rate_limit,
                )
            ):
                raise ValueError(
                    "worker-key request model cannot be combined with individual fields"
                )
            request = name
        else:
            request_fields: Dict[str, Any] = {
                "name": name,
                "key_type": "worker",
            }
            if project_id is not None:
                request_fields["project_id"] = project_id
            if scopes is not None:
                request_fields["scopes"] = list(scopes)
            if expires_in_days is not None:
                request_fields["expires_in_days"] = expires_in_days
            if key_id is not None:
                request_fields["key_id"] = key_id
            if rate_limit is not None:
                request_fields["rate_limit"] = rate_limit
            request = WorkerKeyCreateRequest.model_validate(request_fields)
        return await self._client._arequest(
            "POST",
            self._prefix,
            "/key/worker",
            json=request.model_dump(exclude_none=True),
        )

    async def list_worker_keys(
        self,
        *,
        project_id: Optional[str] = None,
        include_revoked: bool = False,
        skip: int = 0,
        limit: int = 50,
        search: Optional[str] = None,
    ) -> Any:
        """List worker keys using Auth's project and pagination filters."""
        if skip < 0:
            raise ValueError("skip must be non-negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        params: Dict[str, Any] = {
            "include_revoked": include_revoked,
            "skip": skip,
            "limit": limit,
        }
        if project_id is not None:
            params["project_id"] = project_id
        if search is not None:
            params["search"] = search
        return await self._client._arequest(
            "GET",
            self._prefix,
            "/key/worker",
            params=params,
        )

    async def revoke_worker_key(
        self,
        key_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> Any:
        """Revoke a worker key without deleting its audit record."""
        path = self._worker_key_path(key_id, suffix="/revoke")
        params = {"project_id": project_id} if project_id is not None else None
        return await self._client._arequest(
            "POST",
            self._prefix,
            path,
            params=params,
        )

    async def delete_worker_key(
        self,
        key_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> Any:
        """Permanently delete a worker key in the selected project."""
        path = self._worker_key_path(key_id)
        params = {"project_id": project_id} if project_id is not None else None
        return await self._client._arequest(
            "DELETE",
            self._prefix,
            path,
            params=params,
        )

    @staticmethod
    def _worker_key_path(key_id: str, *, suffix: str = "") -> str:
        key_id = key_id.strip()
        if not key_id:
            raise ValueError("key_id is required")
        return f"/key/worker/{quote(key_id, safe='')}{suffix}"

    def _apply_tokens(self, tokens: TokenResponse) -> None:
        self._client.access_token = tokens.access_token
        self._client.refresh_token = tokens.refresh_token
        if tokens.org_id:
            self._client.org_id = tokens.org_id
        if tokens.project_id:
            self._client.project_id = tokens.project_id
        self._client._sync_headers()
