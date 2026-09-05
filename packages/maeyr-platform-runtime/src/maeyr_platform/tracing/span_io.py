"""Canonical bounded span I/O for tool and trigger spans."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Dict, Optional, TypeAlias

from .semconv import (
    ATTR_EXECUTION_ID,
    ATTR_TOOL_EXECUTION_MODE,
    ATTR_TOOL_INPUT,
    ATTR_TOOL_NAME,
    ATTR_TOOL_OUTPUT,
    ATTR_TOOL_TASK_QUEUE,
    ATTR_TRIGGER_ID,
    ATTR_TRIGGER_PAYLOAD_HASH,
)

DEFAULT_TRACE_IO_MAX_CHARS = 2048
MAX_TRACE_IO_COLLECTION_ITEMS = 50
MAX_TRACE_IO_TOTAL_ITEMS = 500
MAX_TRACE_IO_DEPTH = 8

_REDACTED = "<redacted>"
_TRUNCATED = "<truncated>"
_CIRCULAR = "<circular>"
_MAX_NESTED_STRING_CHARS = 4096

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "api_token",
        "authorization",
        "bearer",
        "client_secret",
        "connection_string",
        "cookie",
        "credential",
        "credentials",
        "database_url",
        "dsn",
        "id_token",
        "jwt",
        "mongo_uri",
        "password",
        "passwd",
        "private_key",
        "pwd",
        "redis_url",
        "refresh_token",
        "secret",
        "secrets",
        "set_cookie",
        "token",
    }
)
_TOKEN_METRIC_FRAGMENTS = (
    "max_tokens",
    "num_tokens",
    "token_count",
    "token_usage",
    "tokens_used",
    "total_tokens",
)
_SENSITIVE_SUFFIXES = (
    "_access_key",
    "_access_token",
    "_api_key",
    "_api_token",
    "_authorization",
    "_client_secret",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
)
_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<prefix>
        (?P<key_quote>["']?)
        (?:
            access[_-]?key|access[_-]?token|api[_-]?key|api[_-]?token|
            auth(?:orization)?|bearer|client[_-]?secret|cookie|credentials?|
            id[_-]?token|jwt|passwd|password|private[_-]?key|pwd|
            refresh[_-]?token|secret|set[_-]?cookie|token
        )
        (?P=key_quote)\s*[:=]\s*
    )
    (?:
        (?P<value_quote>["'])(?P<quoted_value>[^"'\r\n]*)(?P=value_quote)
        |
        (?P<bare_value>[^\s,;&]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_AUTHORIZATION_RE = re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*)(?:(?:basic|bearer)\s+)?[^\s,;]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URI_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@",
    re.IGNORECASE,
)
_PROVIDER_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9_-]{8,}|glpat-[a-z0-9_-]{8,}|xox[baprs]-[a-z0-9_-]{8,})\b"
)


def _plain_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    suffix = "… [truncated]"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


def _key_looks_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    if any(fragment in normalized for fragment in _TOKEN_METRIC_FRAGMENTS):
        return False
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _replace_assignment(match: re.Match[str]) -> str:
    quote = match.group("value_quote") or ""
    return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"


def _redact_inline_secrets(text: str) -> str:
    redacted = _AUTHORIZATION_RE.sub(r"\1<redacted>", text)
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _URI_CREDENTIAL_RE.sub(r"\g<scheme><redacted>@", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub(_REDACTED, redacted)
    return _ASSIGNMENT_RE.sub(_replace_assignment, redacted)


class _TraceValueSanitizer:
    """Copy an arbitrary value into a small, acyclic, secret-safe JSON value."""

    def __init__(self, max_chars: int) -> None:
        self._leaf_chars = max(0, min(max_chars, _MAX_NESTED_STRING_CHARS))
        self._remaining_items = MAX_TRACE_IO_TOTAL_ITEMS
        self._seen: set[int] = set()

    def _take_item(self) -> bool:
        if self._remaining_items <= 0:
            return False
        self._remaining_items -= 1
        return True

    def _safe_text(self, value: object) -> str:
        try:
            text = str(value)
        except Exception:
            return "<unserializable>"
        return _plain_truncate(_redact_inline_secrets(text), self._leaf_chars)

    def sanitize(self, value: Any, *, depth: int = 0) -> JsonValue:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else self._safe_text(value)
        if isinstance(value, str):
            return self._safe_text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<binary:{len(value)} bytes>"
        if depth >= MAX_TRACE_IO_DEPTH:
            return _TRUNCATED

        if isinstance(value, Mapping):
            identity = id(value)
            if identity in self._seen:
                return _CIRCULAR
            self._seen.add(identity)
            try:
                result: dict[str, JsonValue] = {}
                for index, (raw_key, item) in enumerate(value.items()):
                    if index >= MAX_TRACE_IO_COLLECTION_ITEMS or not self._take_item():
                        result[_TRUNCATED] = _TRUNCATED
                        break
                    key = _plain_truncate(
                        _redact_inline_secrets(self._safe_text(raw_key)),
                        256,
                    )
                    result[key] = (
                        _REDACTED
                        if _key_looks_sensitive(key)
                        else self.sanitize(item, depth=depth + 1)
                    )
                return result
            finally:
                self._seen.remove(identity)

        if isinstance(value, (list, tuple, set, frozenset)):
            identity = id(value)
            if identity in self._seen:
                return _CIRCULAR
            self._seen.add(identity)
            try:
                result_list: list[JsonValue] = []
                for index, item in enumerate(value):
                    if index >= MAX_TRACE_IO_COLLECTION_ITEMS or not self._take_item():
                        result_list.append(_TRUNCATED)
                        break
                    result_list.append(self.sanitize(item, depth=depth + 1))
                return result_list
            finally:
                self._seen.remove(identity)

        return self._safe_text(value)


def serialize_trace_value(value: Any, *, max_chars: int = DEFAULT_TRACE_IO_MAX_CHARS) -> str:
    """Serialize bounded trace data after recursively removing credential material."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _plain_truncate(_redact_inline_secrets(value.strip()), max_chars)
    safe_value = _TraceValueSanitizer(max_chars).sanitize(value)
    text = json.dumps(safe_value, ensure_ascii=False)
    return _plain_truncate(text, max_chars)


def hash_trace_payload(value: Any) -> str:
    """Stable short hash for trigger/webhook payloads."""
    try:
        raw = json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tool_execution_mode(agent_type: Optional[str]) -> str:
    mode = str(agent_type or "cloud").strip().lower()
    return "secure" if mode == "secure" else "cloud"


def build_tool_span_attributes(
    *,
    name: str,
    inputs: Any = None,
    output: Any = None,
    task_queue: Optional[str] = None,
    agent_type: Optional[str] = None,
    task_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    max_chars: int = DEFAULT_TRACE_IO_MAX_CHARS,
) -> Dict[str, Any]:
    """Build opt-in tool I/O attributes (truncated). Does not capture LLM prompts."""
    tool_name = str(name or endpoint or "").strip()
    attrs: Dict[str, Any] = {}
    if tool_name:
        attrs[ATTR_TOOL_NAME] = tool_name
    if inputs is not None:
        summary = serialize_trace_value(inputs, max_chars=max_chars)
        if summary:
            attrs[ATTR_TOOL_INPUT] = summary
            attrs["input"] = summary
            attrs["inputs"] = summary
    if output is not None:
        summary = serialize_trace_value(output, max_chars=max_chars)
        if summary:
            attrs[ATTR_TOOL_OUTPUT] = summary
            attrs["output"] = summary
    if task_queue:
        attrs[ATTR_TOOL_TASK_QUEUE] = str(task_queue)
        attrs["task_queue"] = str(task_queue)
    mode = tool_execution_mode(agent_type)
    attrs[ATTR_TOOL_EXECUTION_MODE] = mode
    if task_id:
        attrs["task_id"] = str(task_id)
    if endpoint and endpoint != tool_name:
        attrs["endpoint"] = str(endpoint)
    return attrs


def build_trigger_fire_attributes(
    *,
    trigger_id: str,
    payload: Any = None,
    execution_id: Optional[str] = None,
    max_chars: int = DEFAULT_TRACE_IO_MAX_CHARS,
) -> Dict[str, Any]:
    """Key trigger.fire fields without storing full webhook bodies."""
    attrs: Dict[str, Any] = {ATTR_TRIGGER_ID: str(trigger_id)}
    if execution_id:
        attrs[ATTR_EXECUTION_ID] = str(execution_id)
    if payload is not None:
        attrs[ATTR_TRIGGER_PAYLOAD_HASH] = hash_trace_payload(payload)
        preview = serialize_trace_value(payload, max_chars=max_chars)
        if preview:
            attrs["trigger.payload_preview"] = preview
    return attrs


def build_pulse_invoke_attributes(
    *,
    endpoint: str,
    inputs: Any,
    task_queue: str,
    agent_type: str,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
    output: Any = None,
    max_chars: int = DEFAULT_TRACE_IO_MAX_CHARS,
) -> Dict[str, Any]:
    """pulse.invoke spans carry tool semantics for trace debugging."""
    attrs = build_tool_span_attributes(
        name=endpoint,
        inputs=inputs,
        output=output,
        task_queue=task_queue,
        agent_type=agent_type,
        task_id=task_id,
        endpoint=endpoint,
        max_chars=max_chars,
    )
    if agent_id:
        attrs["agent_id"] = str(agent_id)
    attrs["agent_type"] = tool_execution_mode(agent_type)
    if endpoint:
        attrs["endpoint"] = str(endpoint)
    return attrs
