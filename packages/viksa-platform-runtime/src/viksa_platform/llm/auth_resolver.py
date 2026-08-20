"""Authenticated Auth-service resolver for the shared LLM runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from typing import Any

import httpx

from viksa_platform.llm.errors import LLMConfigurationError
from viksa_platform.llm.models import (
    CredentialSource,
    LLMCapability,
    LLMProvider,
    LLMScope,
    LLMScopeType,
    ResolvedLLMConfiguration,
)
from viksa_platform.security.internal_request_signing import sign_internal_request

_RESOLVE_PATH = "/internal/llm-config/resolve"


class AuthLLMConfigurationResolver:
    """Resolve tenant configuration without persisting plaintext credentials."""

    def __init__(
        self,
        *,
        auth_service_url: str,
        auth_internal_key: str,
        caller_service: str,
        platform_configuration: Callable[[], ResolvedLLMConfiguration],
        timeout_seconds: float = 5.0,
    ) -> None:
        if not auth_service_url.strip() or not auth_internal_key.strip():
            raise ValueError("Auth LLM resolver requires service URL and internal key")
        if caller_service not in {"chat-service", "volt-engine-service"}:
            raise ValueError("Caller is not authorized to resolve LLM configuration")
        self._endpoint = f"{auth_service_url.rstrip('/')}{_RESOLVE_PATH}"
        self._key = auth_internal_key
        self._caller = caller_service
        self._platform_configuration = platform_configuration
        self._timeout = max(0.25, min(30.0, float(timeout_seconds)))
        self._session: httpx.AsyncClient | None = None
        self._session_lock = asyncio.Lock()

    async def __call__(
        self,
        scope: LLMScope,
        capability: LLMCapability = LLMCapability.CHAT,
    ) -> ResolvedLLMConfiguration:
        payload = {
            "account_id": scope.account_id,
            "org_id": scope.org_id,
            "project_id": scope.project_id,
            "capability": capability.value,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Account-Id": scope.account_id,
            **sign_internal_request(
                self._key,
                method="POST",
                path=_RESOLVE_PATH,
                body=body,
                service=self._caller,
                account_id=scope.account_id,
                org_id=scope.org_id or "",
                project_id=scope.project_id or "",
                nonce=secrets.token_urlsafe(24),
            ),
        }
        if scope.org_id:
            headers["X-Internal-Org-Id"] = scope.org_id
        if scope.project_id:
            headers["X-Internal-Project-Id"] = scope.project_id
        session = await self._get_session()
        try:
            response = await session.post(
                self._endpoint,
                content=body,
                headers=headers,
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise LLMConfigurationError(
                    self._status_message(response.status_code)
                )
            raw = response.json()
        except LLMConfigurationError:
            raise
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise LLMConfigurationError(
                "LLM configuration service is temporarily unavailable"
            ) from exc
        return self.parse_response(raw, self._platform_configuration)

    async def close(self) -> None:
        async with self._session_lock:
            session, self._session = self._session, None
        if session is not None:
            await session.aclose()

    async def _get_session(self) -> httpx.AsyncClient:
        session = self._session
        if session is not None and not session.is_closed:
            return session
        async with self._session_lock:
            session = self._session
            if session is None or session.is_closed:
                session = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=64,
                        max_keepalive_connections=64,
                    )
                )
                self._session = session
            return session

    @staticmethod
    def _status_message(status: int) -> str:
        if status in {401, 403}:
            return "LLM configuration authorization failed"
        if status == 422:
            return "Configured BYOLLM does not support this operation"
        if status == 404:
            return "LLM configuration was not found"
        return "LLM configuration service is temporarily unavailable"

    @staticmethod
    def parse_response(
        raw: Any,
        platform_configuration: Callable[[], ResolvedLLMConfiguration],
    ) -> ResolvedLLMConfiguration:
        if not isinstance(raw, dict):
            raise LLMConfigurationError("LLM configuration response is invalid")
        source = str(raw.get("credential_source") or "")
        if source == CredentialSource.PLATFORM.value:
            platform = platform_configuration()
            if (
                platform.credential_source is not CredentialSource.PLATFORM
                or platform.source_scope is not LLMScopeType.PLATFORM
            ):
                raise LLMConfigurationError("Platform LLM configuration is invalid")
            return platform
        if source != CredentialSource.CUSTOMER.value:
            raise LLMConfigurationError("LLM configuration response is invalid")
        try:
            return ResolvedLLMConfiguration(
                config_id=str(raw["config_id"]),
                revision=int(raw["revision"]),
                provider=LLMProvider(str(raw["provider"])),
                source_scope=LLMScopeType(str(raw["source_scope"])),
                credential_source=CredentialSource.CUSTOMER,
                models=dict(raw["models"]),
                connection=dict(raw.get("connection") or {}),
                credentials=dict(raw["credentials"]),
                credential_fingerprint=str(raw["credential_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMConfigurationError("LLM configuration response is invalid") from exc
