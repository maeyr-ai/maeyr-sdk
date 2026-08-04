"""Canonical Auth-service internal request encoding and signing."""

from __future__ import annotations

import json
from typing import Any, Protocol

from viksa_platform.security.internal_request_signing import (
    requires_internal_signature,
    sign_internal_request,
)


class AuthInternalSettings(Protocol):
    AUTH_INTERNAL_KEY: str


class ServiceIdentitySettings(Protocol):
    NAME: str


class _DefaultAuthSettings:
    AUTH_INTERNAL_KEY = ""


class _DefaultServiceIdentitySettings:
    NAME = "unknown-service"


auth_settings: AuthInternalSettings = _DefaultAuthSettings()
app_settings: ServiceIdentitySettings = _DefaultServiceIdentitySettings()


def configure_auth_internal_request(
    *,
    auth: AuthInternalSettings,
    service: ServiceIdentitySettings,
) -> None:
    """Bind request signing to one service's live settings objects."""
    global auth_settings, app_settings
    auth_settings = auth
    app_settings = service


def encode_internal_json(payload: Any) -> bytes:
    """Encode a deterministic request body suitable for exact-body HMAC signing."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def build_auth_internal_headers(
    *,
    auth_settings: AuthInternalSettings,
    app_settings: ServiceIdentitySettings,
    method: str,
    path: str,
    body: bytes = b"",
    account_id: str = "",
    org_id: str = "",
    project_id: str = "",
) -> dict[str, str]:
    """Sign an Auth request using live service settings and tenant context."""
    key = auth_settings.AUTH_INTERNAL_KEY
    headers = {"Content-Type": "application/json"}
    for name, value in (
        ("X-Internal-Account-Id", account_id),
        ("X-Internal-Org-Id", org_id),
        ("X-Internal-Project-Id", project_id),
    ):
        if value:
            headers[name] = str(value)
    headers.update(
        sign_internal_request(
            key,
            method=method,
            path=path,
            body=body,
            service=app_settings.NAME,
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
        )
    )
    if not requires_internal_signature():
        headers["X-Internal-Auth-Key"] = key
    return headers


def auth_internal_headers(
    *,
    method: str,
    path: str,
    body: bytes = b"",
    account_id: str = "",
    org_id: str = "",
    project_id: str = "",
) -> dict[str, str]:
    """Sign with the currently configured service settings."""
    return build_auth_internal_headers(
        auth_settings=auth_settings,
        app_settings=app_settings,
        method=method,
        path=path,
        body=body,
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
    )


__all__ = [
    "app_settings",
    "auth_internal_headers",
    "auth_settings",
    "build_auth_internal_headers",
    "configure_auth_internal_request",
    "encode_internal_json",
    "requires_internal_signature",
]
