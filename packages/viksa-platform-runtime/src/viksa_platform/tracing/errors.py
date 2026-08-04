"""OTel-aligned error attributes for synchronous, non-blocking span recording."""

from __future__ import annotations

import os
import re
import traceback
from typing import Any, Protocol, cast

from viksa_platform.tracing.semconv import (
    ATTR_CODE_FILEPATH,
    ATTR_CODE_FUNCTION,
    ATTR_CODE_LINENO,
    ATTR_ERROR_MESSAGE,
    ATTR_ERROR_STACK,
    ATTR_ERROR_TYPE,
)

_FAILED_STATUSES = frozenset({"error", "timeout"})


class _DisplayMessagePolicy(Protocol):
    TRACE_ERROR_REQUEST_FAILED: str

    def http_exception_detail_text(self, exc: BaseException, *, max_message: int = 500) -> str: ...

    def tenant_safe_trace_message(self, message: str | None, *, fallback: str) -> str: ...


class _SafeFallbackDisplayPolicy:
    TRACE_ERROR_REQUEST_FAILED = "Request failed"

    @staticmethod
    def http_exception_detail_text(exc: BaseException, *, max_message: int = 500) -> str:
        text = str(exc) or type(exc).__name__
        return _truncate(text, max_message)

    @staticmethod
    def tenant_safe_trace_message(message: str | None, *, fallback: str) -> str:
        text = str(message or "").strip()
        return text if text == fallback else fallback


_FALLBACK_DISPLAY_POLICY = _SafeFallbackDisplayPolicy()


def _display_message_policy() -> _DisplayMessagePolicy:
    """Return the shared tenant-display security policy."""
    from viksa_platform.security import tenant_safe_display

    return tenant_safe_display


def _include_stack_from_env() -> bool:
    """Include tenant-visible stacks only under explicit development policy."""
    environment_value = os.getenv("PLATFORM_TRACES_INCLUDE_STACK")
    if environment_value is not None:
        return environment_value.lower() in ("1", "true", "yes")
    if os.getenv("APP_DEBUG", "false").lower() in ("1", "true", "yes"):
        return True
    return os.getenv("APP_ENVIRONMENT", "").lower() in ("development", "dev", "local")


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = "… [truncated]"
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def _message_from_exception(exc: BaseException, *, max_message: int = 500) -> str:
    policy = _display_message_policy()
    return _truncate(
        policy.http_exception_detail_text(exc, max_message=max_message),
        max_message,
    )


def _redact_stack_for_tenant(stack: str) -> str:
    """Strip absolute paths from stack text before tenant-visible export."""

    def _basename_path(match: re.Match[str]) -> str:
        prefix, path = match.group(1), match.group(2)
        base = path.replace("\\", "/").split("/")[-1] or path
        return f'{prefix}{base}"'

    return re.sub(r'(File ")([^"]+)(")', _basename_path, stack)


def error_attributes_from_exception(
    exc: BaseException,
    *,
    max_message: int = 500,
    max_stack: int = 2048,
    include_stack: bool | None = None,
) -> dict[str, str]:
    """Build error and optional code attributes from an exception."""
    resolved_include_stack = _include_stack_from_env() if include_stack is None else include_stack
    policy = _display_message_policy()
    raw_message = _message_from_exception(exc, max_message=max_message)
    message = policy.tenant_safe_trace_message(
        raw_message,
        fallback=policy.TRACE_ERROR_REQUEST_FAILED,
    )
    attributes = {
        ATTR_ERROR_TYPE: type(exc).__name__,
        ATTR_ERROR_MESSAGE: _truncate(message, max_message),
        "error.message_internal": _truncate(raw_message, max_message),
    }
    if resolved_include_stack:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
        if stack:
            attributes[ATTR_ERROR_STACK] = _truncate(_redact_stack_for_tenant(stack), max_stack)
    trace = exc.__traceback__
    if trace is not None:
        while trace.tb_next is not None:
            trace = trace.tb_next
        frame = trace.tb_frame
        filename = frame.f_code.co_filename
        if filename:
            attributes[ATTR_CODE_FILEPATH] = os.path.basename(filename)
        function = frame.f_code.co_name
        if function:
            attributes[ATTR_CODE_FUNCTION] = function
        attributes[ATTR_CODE_LINENO] = str(trace.tb_lineno)
    return attributes


def merge_error_attributes(
    base_attrs: dict[str, Any] | None,
    error_attrs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge error attributes without overwriting non-empty base values."""
    output = dict(base_attrs or {})
    if not error_attrs:
        return output
    for key, value in error_attrs.items():
        if value is None or value == "":
            continue
        if key not in output or output[key] in (None, ""):
            output[key] = value
    return output


def error_attributes_for_status(
    status: str,
    *,
    exc: BaseException | None = None,
    message: str | None = None,
    error_type: str | None = None,
    max_message: int = 500,
    include_stack: bool | None = None,
) -> dict[str, str]:
    """Build error attributes from a failed status and optional exception/message."""
    if status not in _FAILED_STATUSES:
        return {}
    if exc is not None:
        return error_attributes_from_exception(
            exc,
            max_message=max_message,
            include_stack=include_stack,
        )
    attributes: dict[str, str] = {}
    if error_type:
        attributes[ATTR_ERROR_TYPE] = error_type
    if message:
        policy = _display_message_policy()
        safe_message = policy.tenant_safe_trace_message(
            message,
            fallback=policy.TRACE_ERROR_REQUEST_FAILED,
        )
        attributes[ATTR_ERROR_MESSAGE] = _truncate(safe_message, max_message)
        attributes["error.message_internal"] = _truncate(message, max_message)
    elif error_type:
        attributes[ATTR_ERROR_MESSAGE] = error_type
    return attributes


def attach_error_to_span_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Merge supported error inputs into a span recorder keyword mapping."""
    status = str(kwargs.get("status") or "ok").lower()
    if status not in _FAILED_STATUSES:
        kwargs.pop("exc", None)
        kwargs.pop("error_attributes", None)
        return kwargs

    exception = kwargs.pop("exc", None)
    explicit = kwargs.pop("error_attributes", None)
    base = kwargs.get("attributes")
    if not isinstance(base, dict):
        base = dict(base) if base else {}

    if explicit:
        merged = merge_error_attributes(base, cast(dict[str, Any], explicit))
        if exception is not None and isinstance(exception, BaseException):
            exception_attributes = error_attributes_from_exception(exception)
            has_message = bool(
                str(merged.get(ATTR_ERROR_MESSAGE) or merged.get("error.message") or "").strip()
            )
            if not has_message:
                merged = merge_error_attributes(merged, exception_attributes)
    elif exception is not None and isinstance(exception, BaseException):
        merged = merge_error_attributes(base, error_attributes_from_exception(exception))
    else:
        merged = dict(base)
        if ATTR_ERROR_MESSAGE not in merged and ATTR_ERROR_TYPE not in merged:
            fallback = error_attributes_for_status(
                status,
                message="Unknown error",
                error_type="Error",
            )
            merged = merge_error_attributes(merged, fallback)

    if merged:
        kwargs["attributes"] = merged
    return kwargs


__all__ = [
    "attach_error_to_span_kwargs",
    "error_attributes_for_status",
    "error_attributes_from_exception",
    "merge_error_attributes",
]
