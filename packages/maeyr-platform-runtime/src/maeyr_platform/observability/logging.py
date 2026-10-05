# ruff: noqa: E501
"""Canonical structured logging and trace-context integration for services."""

import contextvars
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from pythonjsonlogger.json import JsonEncoder, JsonFormatter

from maeyr_platform.security.tenant_safe_display import (
    redact_inline_secrets_in_text,
    redact_sensitive_structure,
)

from .log_policy import LoggingPolicy


class TraceContextView(Protocol):
    """Minimum trace-context surface consumed by structured logging."""

    trace_id: str
    span_id: str
    account_id: str | None
    org_id: str | None
    project_id: str | None


TraceContextProvider = Callable[[], TraceContextView | None]


def _platform_trace_context() -> TraceContextView | None:
    """Read the platform context lazily to avoid a tracing/logging import cycle."""
    try:
        from maeyr_platform.tracing.context import get_trace_context
    except ImportError:
        return None
    return get_trace_context()


_trace_context_provider: TraceContextProvider = _platform_trace_context


def configure_trace_context_provider(provider: TraceContextProvider) -> None:
    """Inject a service-specific trace-context reader when one is required."""
    global _trace_context_provider
    _trace_context_provider = provider


def _active_trace_context() -> TraceContextView | None:
    return _trace_context_provider()


# Context variables
trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)
account_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "account_id", default=None
)
org_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("org_id", default=None)
project_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "project_id", default=None
)


# Getters, setters, and clear helpers
def set_trace_id(trace_id: str | None = None) -> str:
    """Set or generate a trace_id."""
    if not trace_id:
        trace_id = str(uuid.uuid4())
    trace_id_var.set(trace_id)
    return trace_id


def get_trace_id() -> str | None:
    ctx = _active_trace_context()
    if ctx and ctx.trace_id:
        return ctx.trace_id
    return trace_id_var.get()


def clear_trace_id() -> None:
    trace_id_var.set(None)


def set_span_id(span_id: str | None = None) -> str:
    """Set or generate a span_id."""
    if not span_id:
        span_id = str(uuid.uuid4())
    span_id_var.set(span_id)
    return span_id


def get_span_id() -> str | None:
    ctx = _active_trace_context()
    if ctx and ctx.span_id:
        return ctx.span_id
    return span_id_var.get()


def clear_span_id() -> None:
    span_id_var.set(None)


def set_account_id(account_id: str | None) -> str | None:
    account_id_var.set(account_id)
    return account_id


def get_account_id() -> str | None:
    ctx = _active_trace_context()
    if ctx:
        return ctx.account_id
    return account_id_var.get()


def clear_account_id() -> None:
    account_id_var.set(None)


def set_org_id(org_id: str | None) -> str | None:
    org_id_var.set(org_id)
    return org_id


def get_org_id() -> str | None:
    ctx = _active_trace_context()
    if ctx:
        return ctx.org_id
    return org_id_var.get()


def clear_org_id() -> None:
    org_id_var.set(None)


def set_project_id(project_id: str | None) -> str | None:
    project_id_var.set(project_id)
    return project_id


def get_project_id() -> str | None:
    ctx = _active_trace_context()
    if ctx:
        return ctx.project_id
    return project_id_var.get()


def clear_project_id() -> None:
    project_id_var.set(None)


# Emoji Sanitizer Regex
EMOJI_PATTERN = re.compile(
    r"["
    r"\u2600-\u27BF"  # Misc symbols & Dingbats
    r"\uFE00-\uFE0F"  # Variation selectors
    r"\u200D"  # Zero-width joiner
    r"\U0001F000-\U0001F9FF"  # Emoticons, Pictographs, etc.
    r"\U0001FA00-\U0001FAFF"  # Extended symbols
    r"]+",
    flags=re.UNICODE,
)


def remove_emojis(text: str) -> str:
    if not isinstance(text, str):
        return text
    return EMOJI_PATTERN.sub("", text)


def clean_emojis_recursive(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: clean_emojis_recursive(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_emojis_recursive(x) for x in data]
    elif isinstance(data, str):
        return remove_emojis(data)
    return data


_LOG_URL = re.compile(r"(?:https?|mongodb(?:\+srv)?|redis)://[^\s<>\"']+", re.IGNORECASE)


def _safe_log_value(value: Any) -> Any:
    """Keep scoped diagnostics useful without exposing URL credentials/secrets."""
    if isinstance(value, dict):
        return {
            key: _safe_log_value(item) for key, item in redact_sensitive_structure(value).items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    if isinstance(value, str):

        def safe_url(match: re.Match[str]) -> str:
            try:
                parts = urlsplit(match.group(0))
                authority = parts.netloc.rsplit("@", 1)[-1]
                return urlunsplit(
                    (parts.scheme, authority, parts.path, "[redacted]" if parts.query else "", "")
                )
            except ValueError:
                return "[redacted-url]"

        return redact_inline_secrets_in_text(_LOG_URL.sub(safe_url, value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # The JSON encoder can stringify request/exception objects or expand typed
    # values after process_log_record. Normalize them first so that conversion
    # cannot bypass URL and structured-secret redaction.
    try:
        normalized = json.loads(json.dumps(value, cls=JsonEncoder))
    except Exception:
        return f"[unsupported {type(value).__name__}]"
    return _safe_log_value(normalized)


class ContextLogFilter(logging.Filter):
    """Bind tenant context and apply the deployment policy at the output boundary.

    A logger must accept the lowest configured level before this filter runs;
    setting the root to WARNING would irreversibly discard scoped DEBUG logs.
    Explicit record context supports jobs outside an active request/trace.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _active_trace_context()
        for field, getter in (
            ("trace_id", get_trace_id),
            ("span_id", get_span_id),
            ("account_id", get_account_id),
            ("org_id", get_org_id),
            ("project_id", get_project_id),
        ):
            # An active trace is authoritative, including absent tenant fields.
            # Never fill a new tenant's missing field from an older fallback.
            value = getter()
            setattr(record, field, value if ctx is not None else getattr(record, field, value))
        verified = ctx is None or getattr(ctx, "tenant_verified", True)
        return _logging_policy.allows(
            record.levelno,
            account_id=getattr(record, "account_id", None) if verified else None,
            org_id=getattr(record, "org_id", None) if verified else None,
            project_id=getattr(record, "project_id", None) if verified else None,
        )


# Maintain backward compatibility alias
ContextTraceSpanFilter = ContextLogFilter


class StandardJsonFormatter(JsonFormatter):
    """Custom JSON formatter implementing global standard logging fields and sanitizing emojis."""

    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        # 1. Recursively sanitize emojis from all fields in log_data
        log_data = clean_emojis_recursive(_safe_log_value(log_data))

        # 2. Ensure account_id, org_id, and project_id keys are always present
        log_data.setdefault("account_id", None)
        log_data.setdefault("org_id", None)
        log_data.setdefault("project_id", None)

        # 3. Ensure trace_id and span_id are always present
        log_data.setdefault("trace_id", None)
        log_data.setdefault("span_id", None)

        return log_data


# Create the standard JSON formatter
log_format = StandardJsonFormatter(
    fmt="%(levelname)s %(name)s %(module)s %(lineno)d %(trace_id)s %(span_id)s %(account_id)s %(org_id)s %(project_id)s %(message)s",
    rename_fields={"levelname": "severity", "name": "logger"},
    json_ensure_ascii=False,
    timestamp="timestamp",  # Configures python-json-logger to auto-generate ISO 8601 UTC timestamp
)

# Configure the root logger console handler
root_logger = logging.getLogger()
if root_logger.hasHandlers():
    root_logger.handlers.clear()

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_format)
console_handler.addFilter(ContextLogFilter())
root_logger.addHandler(console_handler)
_logging_policy = LoggingPolicy.from_env()


def configure_logging(policy: LoggingPolicy | None = None) -> LoggingPolicy:
    """Configure every service/library output from one validated policy.

    Call without arguments at process startup, or pass a validated policy when
    explicitly reloading configuration. Tenant switches never mutate loggers.
    """
    global _logging_policy
    candidate = LoggingPolicy.from_env() if policy is None else policy
    if not isinstance(candidate, LoggingPolicy):
        raise ValueError("Logging configuration requires a validated LoggingPolicy")
    _logging_policy = candidate
    minimum = _logging_policy.minimum_level
    root_logger.setLevel(minimum)
    # Existing library loggers can otherwise discard a record before handler
    # filters see it. New library loggers inherit the root threshold normally.
    for entry in logging.Logger.manager.loggerDict.values():
        if isinstance(entry, logging.Logger):
            entry.setLevel(logging.NOTSET)
            for handler in entry.handlers:
                handler.setLevel(minimum)
                if not any(isinstance(item, ContextLogFilter) for item in handler.filters):
                    handler.addFilter(ContextLogFilter())
    for handler in root_logger.handlers:
        handler.setLevel(minimum)
        if not any(isinstance(item, ContextLogFilter) for item in handler.filters):
            handler.addFilter(ContextLogFilter())
    return _logging_policy


configure_logging(_logging_policy)


def get_logger(
    name: str,
    log_level: int | None = None,
    log_file: str | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(
        logging.NOTSET if log_level is None else min(log_level, _logging_policy.minimum_level)
    )

    # Avoid adding duplicate console handlers; child loggers automatically propagate to the root logger.
    # We only add a file handler if a file path is specified.
    if log_file:
        existing_file_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, RotatingFileHandler) and h.baseFilename == log_file
        ]
        if not existing_file_handlers:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            file_handler.setFormatter(log_format)
            file_handler.setLevel(_logging_policy.minimum_level)
            file_handler.addFilter(ContextLogFilter())
            logger.addHandler(file_handler)

    return logger


__all__ = [
    "ContextLogFilter",
    "ContextTraceSpanFilter",
    "LoggingPolicy",
    "StandardJsonFormatter",
    "TraceContextProvider",
    "TraceContextView",
    "account_id_var",
    "clean_emojis_recursive",
    "clear_account_id",
    "clear_org_id",
    "clear_project_id",
    "clear_span_id",
    "clear_trace_id",
    "configure_trace_context_provider",
    "configure_logging",
    "console_handler",
    "get_account_id",
    "get_logger",
    "get_org_id",
    "get_project_id",
    "get_span_id",
    "get_trace_id",
    "log_format",
    "org_id_var",
    "project_id_var",
    "remove_emojis",
    "root_logger",
    "set_account_id",
    "set_org_id",
    "set_project_id",
    "set_span_id",
    "set_trace_id",
    "span_id_var",
    "trace_id_var",
]
