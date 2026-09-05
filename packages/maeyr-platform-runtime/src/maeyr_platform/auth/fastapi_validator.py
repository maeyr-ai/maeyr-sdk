import asyncio
import json
import os
import secrets
from dataclasses import dataclass
from logging import Logger, getLogger
from typing import Any, Dict, Optional

import aiohttp
from fastapi import Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from maeyr_platform.security.internal_request_signing import sign_internal_request


@dataclass
class AuthSettings:
    AUTH_SERVICE_URL: str = os.getenv("AUTH_SERVICE_URL", "")
    AUTH_INTERNAL_KEY: str = os.getenv("AUTH_INTERNAL_KEY", "")
    AUTH_API_TIMEOUT: float = float(os.getenv("AUTH_API_TIMEOUT", "10"))
    AUTH_API_RETRIES: int = int(os.getenv("AUTH_API_RETRIES", "3"))
    AUTH_API_RETRY_BACKOFF: float = float(os.getenv("AUTH_API_RETRY_BACKOFF", "0.1"))
    AUTH_API_POOL_SIZE: int = int(os.getenv("AUTH_API_POOL_SIZE", "100"))
    AUTH_API_POOL_SIZE_PER_HOST: int = int(os.getenv("AUTH_API_POOL_SIZE_PER_HOST", "50"))


auth_settings = AuthSettings()
security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)

logger = getLogger("[maeyr_platform.auth.fastapi_validator]")
_CALLER_SERVICE = os.getenv("SERVICE_NAME", "chat-service")

_AUTH_POOL_LIMIT = max(1, int(os.environ.get("AUTH_VALIDATOR_POOL_LIMIT", "100")))
_AUTH_POOL_LIMIT_PER_HOST = max(
    1,
    int(os.environ.get("AUTH_VALIDATOR_POOL_LIMIT_PER_HOST", "50")),
)

_auth_session: Optional[aiohttp.ClientSession] = None
_auth_session_lock: asyncio.Lock | None = None


class AuthServiceError(Exception):
    """Raised when the auth service returns an unexpected response."""


def configure_auth_validator(
    *,
    settings: Any,
    service_name: str,
    service_logger: Logger,
) -> None:
    """Bind the shared validator to one service's live configuration objects."""
    global auth_settings, logger, _CALLER_SERVICE, _AUTH_POOL_LIMIT, _AUTH_POOL_LIMIT_PER_HOST
    auth_settings = settings
    logger = service_logger
    _CALLER_SERVICE = service_name
    _AUTH_POOL_LIMIT = max(
        1,
        int(getattr(settings, "AUTH_API_POOL_SIZE", _AUTH_POOL_LIMIT)),
    )
    _AUTH_POOL_LIMIT_PER_HOST = max(
        1,
        int(getattr(settings, "AUTH_API_POOL_SIZE_PER_HOST", _AUTH_POOL_LIMIT_PER_HOST)),
    )


def _extract_bearer_credential(
    credentials: Optional[HTTPAuthorizationCredentials],
    x_api_key: Optional[str],
) -> str:
    if credentials and credentials.credentials:
        return credentials.credentials.strip()
    if x_api_key:
        return x_api_key.strip()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _call_auth_validate(
    session: aiohttp.ClientSession,
    endpoint: str,
    headers: Dict[str, str],
    credential_or_body: str | bytes | None = None,
    timeout: float = 10.0,
    *,
    body: bytes | None = None,
) -> Dict[str, Any]:
    request_body = body
    if request_body is None:
        request_body = (
            credential_or_body
            if isinstance(credential_or_body, bytes)
            else _credential_body(str(credential_or_body or ""))
        )
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with session.post(
        endpoint,
        data=request_body,
        headers=headers,
        timeout=request_timeout,
    ) as response:
        status_code = response.status
        if status_code != 200:
            logger.warning("Auth service returned non-200 status: %s", status_code)
            if 500 <= status_code < 600:
                raise aiohttp.ClientError(f"Server error {status_code}")
            if status_code == 401:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired credentials",
                )
            raise AuthServiceError(f"Unexpected status {status_code}")

        try:
            data: Dict[str, Any] = await response.json()
        except json.JSONDecodeError as json_err:
            logger.error(
                "Auth service returned invalid JSON error_type=%s",
                type(json_err).__name__,
            )
            raise AuthServiceError("Invalid JSON response") from json_err

        if not isinstance(data, dict):
            logger.error("Auth service returned a non-object response")
            raise AuthServiceError("Invalid authentication response")
        if data.get("valid") is not True:
            logger.error("Credential validation failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired credentials",
            )
        return data


def _credential_body(credential: str) -> bytes:
    return json.dumps(
        {"access_token": credential},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


async def _get_auth_session() -> aiohttp.ClientSession:
    """Return the process-wide bounded Auth connection pool."""
    global _auth_session, _auth_session_lock
    if _auth_session is not None and not _auth_session.closed:
        return _auth_session
    if _auth_session_lock is None:
        _auth_session_lock = asyncio.Lock()
    async with _auth_session_lock:
        if _auth_session is None or _auth_session.closed:
            request_timeout = float(auth_settings.AUTH_API_TIMEOUT)
            connector = aiohttp.TCPConnector(
                limit=max(
                    1,
                    int(getattr(auth_settings, "AUTH_API_POOL_SIZE", _AUTH_POOL_LIMIT)),
                ),
                limit_per_host=max(
                    1,
                    int(
                        getattr(
                            auth_settings,
                            "AUTH_API_POOL_SIZE_PER_HOST",
                            _AUTH_POOL_LIMIT_PER_HOST,
                        )
                    ),
                ),
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            _auth_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=request_timeout,
                    connect=min(request_timeout, 5.0),
                    sock_connect=min(request_timeout, 5.0),
                    sock_read=request_timeout,
                ),
            )
    return _auth_session


async def close_auth_validator() -> None:
    """Close the shared Auth connection pool during application shutdown."""
    global _auth_session, _auth_session_lock
    session, _auth_session = _auth_session, None
    _auth_session_lock = None
    if session is not None and not session.closed:
        await session.close()


async def _discard_auth_session(session: aiohttp.ClientSession) -> None:
    """Remove one failed pooled session without closing a newer replacement."""
    global _auth_session, _auth_session_lock
    if _auth_session_lock is None:
        _auth_session_lock = asyncio.Lock()
    async with _auth_session_lock:
        if _auth_session is session:
            _auth_session = None
    if not session.closed:
        await session.close()


async def _validate_credential_with_retries(
    credential: str,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    endpoint = f"{auth_settings.AUTH_SERVICE_URL.rstrip('/')}/internal/validate-token"
    path = "/internal/validate-token"
    body = _credential_body(credential)
    account_id = ""
    org_id = (org_id or "").strip()
    project_id = (project_id or "").strip()
    retries = auth_settings.AUTH_API_RETRIES
    initial_delay = auth_settings.AUTH_API_RETRY_BACKOFF * 0.5
    delay = initial_delay

    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        session = await _get_auth_session()
        try:
            headers = {
                "Content-Type": "application/json",
                **sign_internal_request(
                    auth_settings.AUTH_INTERNAL_KEY,
                    method="POST",
                    path=path,
                    body=body,
                    service=_CALLER_SERVICE,
                    account_id=account_id,
                    org_id=org_id,
                    project_id=project_id,
                    nonce=secrets.token_urlsafe(24),
                ),
            }
            if org_id:
                headers["X-Tenant-Org-Id"] = org_id
                headers["X-Internal-Org-Id"] = org_id
            if project_id:
                headers["X-Tenant-Project-Id"] = project_id
                headers["X-Internal-Project-Id"] = project_id
            data = await _call_auth_validate(
                session=session,
                endpoint=endpoint,
                headers=headers,
                body=body,
                timeout=auth_settings.AUTH_API_TIMEOUT,
            )
            return data
        except AuthServiceError as exc:
            logger.error("Auth service contract failure error_type=%s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable",
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError)):
                await _discard_auth_session(session)

        if attempt < retries:
            logger.warning(
                "Auth request transient failure; retrying attempt=%s/%s error_type=%s delay=%.1fs",
                attempt,
                retries,
                type(last_error).__name__ if last_error is not None else "unknown",
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

    logger.error(
        "Auth request failed after retries attempts=%s error_type=%s",
        retries,
        type(last_error).__name__ if last_error is not None else "unknown",
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service unavailable",
    )


async def _validate_token_with_retries(
    token: str,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Backward-compatible alias for JWT or API key credentials."""
    return await _validate_credential_with_retries(token, org_id=org_id, project_id=project_id)


async def get_logged_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
) -> Dict[str, Any]:
    org_id = request.headers.get("x-tenant-org-id") or request.headers.get("X-Tenant-Org-Id")
    project_id = request.headers.get("x-tenant-project-id") or request.headers.get(
        "X-Tenant-Project-Id"
    )
    credential = _extract_bearer_credential(credentials, x_api_key)
    return await _validate_credential_with_retries(
        credential,
        org_id=org_id,
        project_id=project_id,
    )


async def get_optional_logged_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
) -> Optional[Dict[str, Any]]:
    """Return user dict if valid Bearer/API key present, else None."""
    if not credentials and not x_api_key:
        return None
    try:
        org_id = request.headers.get("x-tenant-org-id") or request.headers.get("X-Tenant-Org-Id")
        project_id = request.headers.get("x-tenant-project-id") or request.headers.get(
            "X-Tenant-Project-Id"
        )
        credential = _extract_bearer_credential(credentials, x_api_key)
        return await _validate_credential_with_retries(
            credential,
            org_id=org_id,
            project_id=project_id,
        )
    except HTTPException:
        return None


async def get_websocket_user(
    token: str = Query(..., description="JWT or API key for WebSocket authentication"),
) -> Dict[str, Any]:
    return await _validate_credential_with_retries(token)
