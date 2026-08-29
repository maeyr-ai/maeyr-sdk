"""Structured errors for the Maeyr platform HTTP client."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import httpx
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Single validation or API error detail."""

    message: str
    code: Optional[str] = None
    field: Optional[str] = None
    loc: Optional[List[Union[str, int]]] = None


class MaeyrError(Exception):
    """Base exception for all Maeyr SDK errors."""


class MaeyrTransportError(MaeyrError):
    """Network-level failure (timeout, connection, TLS)."""

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
        request_method: Optional[str] = None,
        request_url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.request_method = request_method
        self.request_url = request_url


class MaeyrStreamError(MaeyrError):
    """A successful streaming response violated the SDK event contract."""


class MaeyrApiError(MaeyrError):
    """HTTP API error with parsed platform response metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        service: str,
        method: str,
        path: str,
        body: Any = None,
        details: Optional[List[ErrorDetail]] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.service = service
        self.method = method
        self.path = path
        self.body = body
        self.details = details or []
        self.request_id = request_id
        self.retry_after = retry_after

    @property
    def detail_message(self) -> str:
        if self.details:
            return self.details[0].message
        if isinstance(self.body, dict):
            detail = self.body.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(detail, dict) and "message" in detail:
                return str(detail["message"])
        return str(self)


class MaeyrAuthenticationError(MaeyrApiError):
    """401 Unauthorized."""


class MaeyrPermissionError(MaeyrApiError):
    """403 Forbidden."""


class MaeyrNotFoundError(MaeyrApiError):
    """404 Not Found."""


class MaeyrConflictError(MaeyrApiError):
    """409 Conflict."""


class MaeyrValidationError(MaeyrApiError):
    """422 Unprocessable Entity."""


class MaeyrRateLimitError(MaeyrApiError):
    """429 Too Many Requests."""


class MaeyrServerError(MaeyrApiError):
    """5xx server error."""


_STATUS_TO_EXCEPTION: Dict[int, type[MaeyrApiError]] = {
    401: MaeyrAuthenticationError,
    403: MaeyrPermissionError,
    404: MaeyrNotFoundError,
    409: MaeyrConflictError,
    422: MaeyrValidationError,
    429: MaeyrRateLimitError,
}


def _parse_retry_after(headers: httpx.Headers) -> Optional[float]:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_request_id(headers: httpx.Headers) -> Optional[str]:
    for key in (
        "X-Request-Id",
        "X-Request-ID",
        "x-request-id",
        "X-Correlation-Id",
        "X-Correlation-ID",
        "traceparent",
    ):
        value = headers.get(key)
        if value:
            return str(value)
    return None


def parse_error_details(body: Any) -> List[ErrorDetail]:
    """Parse FastAPI-style ``detail`` payloads into ``ErrorDetail`` list."""
    if body is None:
        return []
    if isinstance(body, str):
        return [ErrorDetail(message=body)]
    if not isinstance(body, dict):
        return [ErrorDetail(message=str(body))]

    detail = body.get("detail", body.get("message"))
    if detail is None:
        msg = body.get("error") or body.get("msg")
        return [ErrorDetail(message=str(msg))] if msg else []

    if isinstance(detail, str):
        return [ErrorDetail(message=detail)]

    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("msg") or str(detail)
        return [
            ErrorDetail(
                message=str(message),
                code=detail.get("code"),
                field=detail.get("field"),
            )
        ]

    if isinstance(detail, list):
        out: List[ErrorDetail] = []
        for item in detail:
            if isinstance(item, str):
                out.append(ErrorDetail(message=item))
            elif isinstance(item, dict):
                loc = item.get("loc")
                msg = item.get("msg") or item.get("message") or str(item)
                field = None
                if isinstance(loc, list) and loc:
                    field = ".".join(str(x) for x in loc if x != "body")
                out.append(
                    ErrorDetail(
                        message=str(msg),
                        code=item.get("type") or item.get("code"),
                        field=field,
                        loc=loc if isinstance(loc, list) else None,
                    )
                )
        return out

    return [ErrorDetail(message=str(detail))]


def raise_for_response(
    response: httpx.Response,
    *,
    service: str,
    method: str,
    path: str,
) -> None:
    """Raise a typed :class:`MaeyrApiError` subclass for error HTTP statuses."""
    status = response.status_code
    if status < 400:
        return

    try:
        body = response.json()
    except Exception:
        body = response.text

    details = parse_error_details(body)
    request_id = _extract_request_id(response.headers)
    retry_after = _parse_retry_after(response.headers) if status == 429 else None

    summary = details[0].message if details else f"HTTP {status}"
    message = f"{service} {method} {path} failed ({status}): {summary}"
    if request_id:
        message = f"{message} [request_id={request_id}]"

    exc_type: type[MaeyrApiError]
    if status >= 500:
        exc_type = MaeyrServerError
    else:
        exc_type = _STATUS_TO_EXCEPTION.get(status, MaeyrApiError)

    raise exc_type(
        message,
        status_code=status,
        service=service,
        method=method,
        path=path,
        body=body,
        details=details,
        request_id=request_id,
        retry_after=retry_after,
    )


def wrap_transport_error(
    exc: BaseException,
    *,
    method: str,
    url: str,
) -> MaeyrTransportError:
    return MaeyrTransportError(
        f"Transport error for {method} {url}: {exc}",
        cause=exc,
        request_method=method,
        request_url=url,
    )
