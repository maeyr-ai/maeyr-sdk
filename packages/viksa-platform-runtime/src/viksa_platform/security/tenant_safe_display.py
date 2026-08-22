"""Tenant-boundary redaction for displays, streams, and execution records."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, cast

_DEFAULT_MAX_STRING = 2000
_LARGE_FIELD_MAX = 500
# Agentic final summaries shown live in chat (match persisted message content budget).
_STREAM_SUMMARY_MAX = 12_000
# Only ``error`` — ``reason`` on task/approval events is user-facing, not a failure blob.
_PERSISTED_ERROR_KEYS = frozenset({"error"})
# Nested ``error`` fields are sanitized only under these container keys (not e.g. ``inputs``).
_NESTED_ERROR_PARENT_KEYS = frozenset(
    {"items", "execution_log", "results", "events", "steps", "tasks", "task_results"}
)
# User-visible content (not allowlisted as errors — truncate only).
_STREAM_CONTENT_KEYS = frozenset(
    {
        "chunk",
        "accumulated_content",
        "message",
        "content",
        "final_summary",
    }
)
_LARGE_FIELD_KEYS = frozenset(
    {
        "reasoning",
        "observation",
        "plan",
        "result",
        "inputs",
        "response",
        "summary",
        "combined_prompt",
        "test_payload",
        "webhook_payload",
        "approval_reason",
    }
)

TRACE_ERROR_RUN_FAILED = "Run failed"
TRACE_ERROR_REQUEST_FAILED = "Request failed"
TRACE_ERROR_AI_SERVICE = "AI service error"
TRACE_ERROR_INTERNAL_EXECUTION = "Internal execution error"
TRACE_ERROR_EXECUTION_TIMEOUT = "Execution timeout exceeded"
TRACE_TASK_FAILED = "Task failed"
TRACE_TASK_TIMED_OUT = "Task timed out"
TRACE_TASK_UNKNOWN = "Unknown error"
TRACE_AGENT_NOT_FOUND = "Agent not found"
TRACE_MAX_RETRIES = "Max retries exceeded for this task. Moving on."

CHAT_INTERNAL_RESUME_STATE_SEALED_KEY = "_resume_state_sealed"
RESUME_STATE_SEALED_KEY = "resume_state_sealed"
RESUME_STATE_PUBLIC_KEY = "resume_state_public"
_PRIVATE_RESUME_STATE_KEYS = frozenset(
    {
        # Reject plaintext resume blobs even though they are no longer a
        # supported persistence format.
        "_resume_state",
        CHAT_INTERNAL_RESUME_STATE_SEALED_KEY,
        "_resume_state_claim",
        "resume_state",
        RESUME_STATE_SEALED_KEY,
        "sealed_resume_state",
        "resumestate",
        "resumestatesealed",
        "_resumestate",
        "_resumestatesealed",
        "sealedresumestate",
    }
)

_TRACE_SAFE_PUBLIC_MESSAGES = frozenset(
    {
        TRACE_ERROR_RUN_FAILED,
        TRACE_ERROR_REQUEST_FAILED,
        TRACE_ERROR_AI_SERVICE,
        (
            "The LLM provider rate-limited this request. Wait a moment and try again, "
            "or check the key's usage limits."
        ),
        "The LLM provider rejected the API key. Update it in Settings → LLM providers.",
        (
            "The LLM provider rejected this request. Check the model and key in "
            "Settings → LLM providers."
        ),
        "The LLM provider could not complete this request. Try again in a moment.",
        "Failed to process request. Please try again.",
        "Server busy, please try again later",
        "Failed to start streaming",
        TRACE_ERROR_INTERNAL_EXECUTION,
        TRACE_ERROR_EXECUTION_TIMEOUT,
        TRACE_TASK_FAILED,
        TRACE_TASK_TIMED_OUT,
        TRACE_TASK_UNKNOWN,
        TRACE_AGENT_NOT_FOUND,
        TRACE_MAX_RETRIES,
        "Agent generation failed",
        "Failed to generate agent",
        "Failed to process AI request",
        "Failed to summarize workflow",
        "Failed to load conversation",
        "Failed to start execution stream",
        "Stream processing failed",
        "Reconnection failed",
        "No active execution found",
        "Trigger not found",
        "Trigger is disabled",
        "No agent/endpoint available for this trigger. Check allowed_agents configuration.",
        "No agent/endpoint available for this schedule. Check allowed_agents configuration.",
        "No agents available for this trigger. Check allowed_agents configuration.",
        "Execution cancelled (client disconnected or timeout)",
        "Execution interrupted",
        "Paused execution state is missing its harness transcript.",
        "Paused user input cannot be resumed in this execution context.",
        (
            "Unable to start resume because another attempt may be in progress. "
            "Please reconnect or retry shortly."
        ),
        "Schedule execution cancelled",
        "Replay error",
        "No agents available for replay",
        "Invalid request",
        "Failed to cancel execution",
        "Failed to check execution status",
        "Auth failed",
        "No token provided",
        "Permission denied: requires chat:execute",
        "Permission denied",
        "Connection error",
        "Plan limit reached. Please upgrade your plan.",
        "Total CPU exceeds plan limit. Please upgrade your plan.",
        "Total memory exceeds plan limit. Please upgrade your plan.",
        "Project resource limit reached. Please contact your administrator.",
        "Health check failed",
        "MongoDB health check failed",
        "Execution does not contain a replayable prompt",
    }
)

_TRACE_SAFE_ERROR_CODES = frozenset(
    {
        "llm_rate_limited",
        "llm_auth_failed",
        "llm_rejected",
        "llm_unavailable",
        "request_failed",
    }
)


def _truncate_http_text(text: str, max_message: int) -> str:
    if len(text) <= max_message:
        return text
    suffix = "… [truncated]"
    keep = max(0, max_message - len(suffix))
    return text[:keep] + suffix


def http_exception_detail_text(
    exc: BaseException,
    *,
    max_message: int = 500,
) -> str:
    """Extract human-readable text from FastAPI HTTPException.detail (any shape)."""
    try:
        from fastapi import HTTPException
    except ImportError:
        return _truncate_http_text(str(exc) or type(exc).__name__, max_message)

    if not isinstance(exc, HTTPException):
        return _truncate_http_text(str(exc) or type(exc).__name__, max_message)

    detail = exc.detail
    if isinstance(detail, str) and detail.strip():
        return _truncate_http_text(detail.strip(), max_message)
    if isinstance(detail, list):
        parts: List[str] = []
        for item in detail[:8]:
            if isinstance(item, dict):
                msg = item.get("msg") or item.get("message") or item.get("detail")
                if isinstance(msg, str) and msg.strip():
                    loc = item.get("loc")
                    if isinstance(loc, (list, tuple)) and loc:
                        parts.append(f"{'.'.join(str(x) for x in loc)}: {msg.strip()}")
                    else:
                        parts.append(msg.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return _truncate_http_text("; ".join(parts), max_message)
    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_http_text(value.strip(), max_message)
    return _truncate_http_text(f"HTTP {exc.status_code}", max_message)


def public_http_detail(
    exc: Optional[BaseException] = None,
    *,
    message: Optional[str] = None,
    fallback: str = TRACE_ERROR_REQUEST_FAILED,
) -> str:
    """
    Map exceptions to tenant-safe FastAPI/WebSocket ``detail`` strings.

    HTTPException details pass through only when allowlisted; all other
    exception text is replaced with ``fallback``.
    """
    if message:
        return tenant_safe_trace_message(message, fallback=fallback)
    if exc is None:
        return fallback
    try:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException):
            return tenant_safe_trace_message(
                http_exception_detail_text(exc),
                fallback=fallback,
            )
    except ImportError:
        pass
    return tenant_safe_trace_message(str(exc) or "", fallback=fallback)


_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "authorization",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "api_token",
        "access_token",
        "auth_token",
        "refresh_token",
        "id_token",
        "private_key",
        "access_key",
        "bearer",
        "jwt",
        "token",
    }
)
_TOKEN_METRIC_KEY_FRAGMENTS = (
    "token_count",
    "token_usage",
    "tokens_used",
    "total_tokens",
    "max_tokens",
    "num_tokens",
)


def _key_looks_sensitive(key: str) -> bool:
    """Heuristic key matcher — avoids metrics like ``token_count``."""
    k = str(key).lower().replace("-", "_")
    if any(metric in k for metric in _TOKEN_METRIC_KEY_FRAGMENTS):
        return False
    if k in _SENSITIVE_EXACT_KEYS:
        return True
    if any(
        k.endswith(suffix)
        for suffix in (
            "_password",
            "_secret",
            "_token",
            "_api_key",
            "_credential",
            "_authorization",
        )
    ):
        return True
    # Single-segment keys only (avoid redacting ``foo.token.bar`` field paths).
    segments = [s for s in re.split(r"[._]+", k) if s]
    if len(segments) == 1 and segments[0] in _SENSITIVE_EXACT_KEYS:
        return True
    return False


_INLINE_SECRET_RE = re.compile(
    r"(sk-[a-zA-Z0-9_\-]{8,}|"
    r"ghp_[a-zA-Z0-9_\-]{8,}|glpat-[a-zA-Z0-9_\-]{8,}|"
    r"xox[baprs]-[a-zA-Z0-9_\-]{8,}|"
    r"Bearer\s+[a-zA-Z0-9_\-\.]+|"
    r"eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+|"
    r"(?:password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{8,})",
    re.IGNORECASE,
)


def redact_inline_secrets_in_text(text: str) -> str:
    """Scrub common inline secret patterns from JSON/text blobs (dry-run previews)."""
    if not text:
        return text
    return _INLINE_SECRET_RE.sub("[redacted]", text)


def redact_sensitive_structure(value: Any) -> Any:
    """Replace values whose keys look sensitive (any depth)."""
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if _key_looks_sensitive(str(key)):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_sensitive_structure(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_structure(item) for item in value]
    return value


def redact_condition_detail_rows(details: Optional[List[Any]]) -> List[Any]:
    """Scrub ``actual_value`` fields shown in trigger dry-run condition tables."""
    if not details:
        return []
    out: List[Any] = []
    for row in details:
        if not isinstance(row, dict):
            out.append(row)
            continue
        item = dict(row)
        actual = item.get("actual_value")
        if actual is not None:
            text = actual if isinstance(actual, str) else json.dumps(actual, default=str)
            item["actual_value"] = _truncate_field(
                redact_inline_secrets_in_text(text),
                _DEFAULT_MAX_STRING,
            )
        out.append(sanitize_persisted_event_data(item))
    return out


def strip_resume_state_secrets(value: Any) -> Any:
    """Recursively remove raw and sealed resume state while retaining public projections."""
    if isinstance(value, dict):
        return {
            key: strip_resume_state_secrets(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_RESUME_STATE_KEYS
        }
    if isinstance(value, list):
        return [strip_resume_state_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_resume_state_secrets(item) for item in value)
    return value


def _public_pending_approval(approval: Any) -> Dict[str, Any]:
    if not isinstance(approval, dict):
        return {}
    allowed = (
        "task_id",
        "tool_call_id",
        "name",
        "agent_alias",
        "agent",
        "endpoint",
        "question",
        "context",
        "options",
        "inputs",
        "reason",
        "approval_reason",
        "approval_id",
        "requires_approval",
    )
    return cast(
        Dict[str, Any],
        sanitize_execution_persisted_blob(
            {key: approval[key] for key in allowed if key in approval}
        ),
    )


def project_resume_state_public(
    resume_state: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the bounded tenant-safe state needed to render a paused execution."""
    if not isinstance(resume_state, dict) or not resume_state:
        return {}

    out: Dict[str, Any] = {}
    pending_input = resume_state.get("pending_user_input")
    if isinstance(pending_input, dict):
        out["pending_user_input"] = sanitize_resume_input_request(pending_input)
        out["kind"] = "input"

    pending_endpoint = resume_state.get("pending_endpoint_approval")
    if isinstance(pending_endpoint, dict):
        out["pending_endpoint_approval"] = _public_pending_approval(pending_endpoint)
        out["kind"] = "approval"

    approvals = resume_state.get("harness_pending_approvals")
    if isinstance(approvals, list):
        public_approvals = [
            projected
            for projected in (_public_pending_approval(item) for item in approvals)
            if projected
        ]
        if public_approvals:
            out["harness_pending_approvals"] = public_approvals
            out["pending_count"] = len(public_approvals)
            out["kind"] = "approval"

    return cast(Dict[str, Any], strip_resume_state_secrets(out))


def sanitize_resume_event_payload(value: Any) -> Any:
    """Prepare nested event/approval payloads without raw or sealed resume state."""
    stripped = strip_resume_state_secrets(value)
    return sanitize_execution_persisted_blob(stripped)


def sanitize_execution_persisted_blob(value: Any) -> Any:
    """Key-redact then truncate values stored on execution documents."""
    if isinstance(value, dict):
        return sanitize_persisted_event_data(
            redact_sensitive_structure(value),
            initial_depth=1,
        )
    if isinstance(value, list):
        return [sanitize_execution_persisted_blob(item) for item in value]
    if isinstance(value, str):
        return _truncate_field(
            redact_inline_secrets_in_text(value),
            _DEFAULT_MAX_STRING,
        )
    return value


def sanitize_execution_document_for_tenant(
    execution: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Redact/truncate an execution document returned to tenants (read path)."""
    if not execution:
        return {}
    out = dict(execution)
    out.pop(RESUME_STATE_SEALED_KEY, None)
    out.pop(CHAT_INTERNAL_RESUME_STATE_SEALED_KEY, None)
    out.pop("_resume_state", None)
    for key in ("webhook_payload", "final_results", "task_outputs"):
        if key in out and out[key] is not None:
            out[key] = sanitize_execution_persisted_blob(out[key])
    if out.get(RESUME_STATE_PUBLIC_KEY):
        out[RESUME_STATE_PUBLIC_KEY] = sanitize_resume_event_payload(out[RESUME_STATE_PUBLIC_KEY])
    if out.get("resume_state"):
        # Migration-only API fallback: expose the same minimal projection that
        # new records store, never the full legacy continuation object.
        out["resume_state"] = project_resume_state_public(out["resume_state"])
    tasks = out.get("tasks_executed")
    if isinstance(tasks, list):
        sanitized_tasks: List[Any] = []
        for row in tasks:
            if not isinstance(row, dict):
                sanitized_tasks.append(row)
                continue
            item = dict(row)
            if "inputs" in item:
                item["inputs"] = sanitize_execution_persisted_blob(item.get("inputs"))
            sanitized_tasks.append(item)
        out["tasks_executed"] = sanitized_tasks
    iterations = out.get("iterations")
    if isinstance(iterations, list):
        sanitized_iters: List[Any] = []
        for row in iterations:
            if not isinstance(row, dict):
                sanitized_iters.append(row)
                continue
            item = dict(row)
            for field in ("observation", "reasoning", "plan"):
                val = item.get(field)
                if isinstance(val, str):
                    item[field] = _truncate_field(
                        redact_inline_secrets_in_text(val),
                        _LARGE_FIELD_MAX,
                    )
            sanitized_iters.append(item)
        out["iterations"] = sanitized_iters
    for text_key in ("prompt", "final_summary"):
        val = out.get(text_key)
        if isinstance(val, str):
            out[text_key] = _truncate_field(
                redact_inline_secrets_in_text(val),
                _LARGE_FIELD_MAX,
            )
    return out


def _strip_server_only_execution_log_row(row: Any) -> Any:
    """Drop raw/sealed resume state from execution_log rows (chat UI/API)."""
    if not isinstance(row, dict):
        return row
    return strip_resume_state_secrets(row)


def _sanitize_execution_log_row_for_tenant(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    return sanitize_resume_event_payload(row)


def chat_stream_event_data_for_client(
    event_kind: Any,
    data: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Raw stream payload for chat SSE/WebSocket (no trace truncation/redaction).

    Raw/sealed resume state is removed recursively for every event type.
    """
    if not data:
        return data
    return cast(Optional[Dict[str, Any]], strip_resume_state_secrets(data))


def chat_message_metadata_for_client(
    metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Chat UI/API metadata: strip server-only fields only."""
    if not metadata:
        return {}
    out = cast(Dict[str, Any], strip_resume_state_secrets(dict(metadata)))
    log = out.get("execution_log")
    if isinstance(log, list):
        out["execution_log"] = [_strip_server_only_execution_log_row(row) for row in log]
    return out


def sanitize_message_metadata_for_llm(
    metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Metadata safe to embed in LLM prompts (strip server resume blob, redact logs).
    """
    if not metadata:
        return {}
    return sanitize_chat_message_metadata_for_tenant(metadata)


def sanitize_chat_message_metadata_for_tenant(
    metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Redact/truncate metadata for LLM prompts (not chat UI)."""
    if not metadata:
        return {}
    out = cast(Dict[str, Any], strip_resume_state_secrets(dict(metadata)))
    log = out.get("execution_log")
    if isinstance(log, list):
        out["execution_log"] = [_sanitize_execution_log_row_for_tenant(row) for row in log]
    return out


def sanitize_chat_message_for_tenant(
    message: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Prepare a stored message for chat history/search (no trace-style sanitization)."""
    if not message:
        return {}
    out = dict(message)
    meta = out.get("metadata")
    if isinstance(meta, dict):
        out["metadata"] = chat_message_metadata_for_client(meta)
    return out


def sanitize_trigger_dry_run_preview(
    preview: Optional[Dict[str, Any]],
    *,
    payload_already_redacted: bool = False,
) -> Dict[str, Any]:
    """
    Redact/truncate trigger dry-run preview for HTTP and stream responses.

    Sensitive keyed fields are replaced before truncation so short secrets do not leak.
    """
    if not preview:
        return {}
    out = dict(preview)
    for key in ("combined_prompt", "prompt"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = _truncate_field(redact_inline_secrets_in_text(val), _LARGE_FIELD_MAX)
    for body_key in ("body_passed_to_prompt", "test_payload", "webhook_payload"):
        body = out.get(body_key)
        if isinstance(body, dict):
            if payload_already_redacted:
                out[body_key] = sanitize_persisted_event_data(body)
            else:
                out[body_key] = sanitize_persisted_event_data(redact_sensitive_structure(body))
    if "condition_details" in out:
        out["condition_details"] = redact_condition_detail_rows(out.get("condition_details"))
    return out


def sanitize_task_result_for_stream(result: Any) -> Any:
    """Tenant-safe task result payload for TASK_COMPLETE stream events."""
    if isinstance(result, dict):
        if str(result.get("status") or "").lower() == "error":
            raw_err = result.get("error")
            text = (
                raw_err if isinstance(raw_err, str) else str(raw_err) if raw_err is not None else ""
            )
            return {
                "status": "error",
                "error": tenant_safe_trace_message(text, fallback=TRACE_TASK_UNKNOWN),
            }
        safe: Dict[str, Any] = {}
        for key, value in result.items():
            if key == "error" and isinstance(value, str):
                safe[key] = tenant_safe_trace_message(value, fallback=TRACE_TASK_UNKNOWN)
            else:
                safe[key] = value
        return sanitize_persisted_event_data(safe)
    if isinstance(result, str):
        return _truncate_field(result, _LARGE_FIELD_MAX)
    if result is None:
        return None
    try:
        return _truncate_field(json.dumps(result, default=str), _LARGE_FIELD_MAX)
    except Exception:
        return _truncate_field(str(result), _LARGE_FIELD_MAX)


def tenant_safe_trace_message(
    message: Optional[str],
    *,
    fallback: str = TRACE_ERROR_RUN_FAILED,
) -> str:
    """Only explicitly allowlisted strings pass through; all else uses fallback."""
    if not message or not str(message).strip():
        return fallback
    text = str(message).strip()
    if text in _TRACE_SAFE_PUBLIC_MESSAGES:
        return text
    return fallback


def _truncate_field(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    suffix = "… [truncated]"
    keep = max(0, max_len - len(suffix))
    return text[:keep] + suffix


_STRIP_ON_FAILURE_KEYS = frozenset(
    {
        "detail",
        "exception",
        "stack",
        "traceback",
        "internal",
        "debug",
        "cause",
        "errors",
        "failure",
        "msg",
        "message",
    }
)


def _sanitize_top_level_failure_fields(
    data: Optional[Dict[str, Any]],
    *,
    fallback: str,
) -> Dict[str, Any]:
    """Allowlist top-level ``error`` / ``reason``, drop leak-prone extras, then truncate."""
    payload = dict(data or {})
    for key in ("error", "reason"):
        if key in payload:
            raw = payload.get(key)
            text = raw if isinstance(raw, str) else str(raw) if raw is not None else ""
            payload[key] = tenant_safe_trace_message(text, fallback=fallback)
    for key in _STRIP_ON_FAILURE_KEYS:
        payload.pop(key, None)
    code = payload.get("error_code")
    if "error_code" in payload and (
        not isinstance(code, str) or code not in _TRACE_SAFE_ERROR_CODES
    ):
        payload.pop("error_code", None)
    return sanitize_persisted_event_data(payload, error_fallback=fallback)


def sanitize_stream_task_error_data(
    data: Optional[Dict[str, Any]],
    *,
    fallback: str = TRACE_TASK_FAILED,
) -> Dict[str, Any]:
    """Return a copy of TASK_ERROR event data with tenant-safe failure fields."""
    return _sanitize_top_level_failure_fields(data, fallback=fallback)


def sanitize_stream_error_data(
    data: Optional[Dict[str, Any]],
    *,
    fallback: str = TRACE_ERROR_RUN_FAILED,
) -> Dict[str, Any]:
    """Return a copy of ERROR stream data with tenant-safe failure fields."""
    return _sanitize_top_level_failure_fields(data, fallback=fallback)


def sanitize_persisted_event_data(
    data: Optional[Dict[str, Any]],
    *,
    error_fallback: str = TRACE_ERROR_RUN_FAILED,
    max_string_len: int = _DEFAULT_MAX_STRING,
    large_field_max: int = _LARGE_FIELD_MAX,
    initial_depth: int = 0,
) -> Dict[str, Any]:
    """Sanitize event payloads before execution_log / run_events persistence."""

    def _walk(
        key: str,
        value: Any,
        *,
        depth: int = initial_depth,
        parent_key: Optional[str] = None,
    ) -> Any:
        if key in _PERSISTED_ERROR_KEYS:
            if depth == 0 or parent_key in _NESTED_ERROR_PARENT_KEYS:
                text = value if isinstance(value, str) else str(value) if value is not None else ""
                return tenant_safe_trace_message(text, fallback=error_fallback)
        if key in _STREAM_CONTENT_KEYS:
            if isinstance(value, str):
                return _truncate_field(value, max_string_len)
            if isinstance(value, (dict, list)):
                return _truncate_field(json.dumps(value, default=str), max_string_len)
            return value
        if key in _LARGE_FIELD_KEYS:
            if isinstance(value, str):
                return _truncate_field(redact_inline_secrets_in_text(value), large_field_max)
            if isinstance(value, dict):
                return {
                    str(k): _walk(
                        str(k),
                        v,
                        depth=depth + 1,
                        parent_key=key,
                    )
                    for k, v in value.items()
                }
            if isinstance(value, list):
                out_list: List[Any] = []
                for item in value:
                    if isinstance(item, dict):
                        out_list.append(
                            {
                                str(k): _walk(
                                    str(k),
                                    v,
                                    depth=depth + 1,
                                    parent_key=key,
                                )
                                for k, v in item.items()
                            }
                        )
                    elif isinstance(item, str):
                        out_list.append(
                            _truncate_field(
                                redact_inline_secrets_in_text(item),
                                large_field_max,
                            )
                        )
                    else:
                        out_list.append(item)
                return out_list
            return value
        if isinstance(value, str):
            return _truncate_field(value, max_string_len)
        if isinstance(value, dict):
            return {
                str(k): _walk(str(k), v, depth=depth + 1, parent_key=key) for k, v in value.items()
            }
        if isinstance(value, list):
            out: List[Any] = []
            for item in value:
                if isinstance(item, dict):
                    out.append(
                        {
                            str(k): _walk(str(k), v, depth=depth + 1, parent_key=key)
                            for k, v in item.items()
                        }
                    )
                elif isinstance(item, str):
                    out.append(_truncate_field(item, max_string_len))
                else:
                    out.append(item)
            return out
        return value

    if not data:
        return {}
    return {str(k): _walk(str(k), v, depth=initial_depth, parent_key=None) for k, v in data.items()}


def sanitize_stream_execution_summary_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Tenant-safe EXECUTION_SUMMARY (pause/complete) for live SSE/WebSocket."""
    payload = dict(data or {})
    raw_resume_state = payload.pop("resume_state", None)
    if isinstance(raw_resume_state, dict):
        public = project_resume_state_public(raw_resume_state)
        if public:
            payload[RESUME_STATE_PUBLIC_KEY] = public
    payload = strip_resume_state_secrets(payload)
    results = payload.get("results")
    if results is not None:
        payload["results"] = sanitize_execution_persisted_blob(results)
    summary = payload.get("summary")
    if isinstance(summary, str):
        payload["summary"] = _truncate_field(
            redact_inline_secrets_in_text(summary),
            _STREAM_SUMMARY_MAX,
        )
    return sanitize_persisted_event_data(payload)


def _scrub_stream_string_fields(
    payload: Dict[str, Any],
    keys: tuple[str, ...],
    *,
    max_len: int = _LARGE_FIELD_MAX,
) -> Dict[str, Any]:
    out = dict(payload)
    for key in keys:
        val = out.get(key)
        if isinstance(val, str):
            out[key] = _truncate_field(redact_inline_secrets_in_text(val), max_len)
    return out


def sanitize_stream_thought_complete_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Tenant-safe THOUGHT_COMPLETE (observation / reasoning / plan)."""
    payload = _scrub_stream_string_fields(
        dict(data or {}),
        ("observation", "reasoning", "plan"),
    )
    return sanitize_persisted_event_data(payload)


def sanitize_stream_asking_input_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Tenant-safe ASKING_INPUT question/context."""
    payload = _scrub_stream_string_fields(
        dict(data or {}),
        ("question", "context"),
        max_len=_DEFAULT_MAX_STRING,
    )
    return sanitize_persisted_event_data(payload)


def sanitize_stream_task_starting_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Tenant-safe TASK_STARTING (inputs shown in UI before run)."""
    payload = dict(data or {})
    if "inputs" in payload:
        payload["inputs"] = sanitize_execution_persisted_blob(payload.get("inputs"))
    payload = _scrub_stream_string_fields(payload, ("reason",))
    return sanitize_persisted_event_data(payload)


def sanitize_tenant_display_text(
    text: Any,
    *,
    max_len: int = _LARGE_FIELD_MAX,
) -> str:
    """Truncate and scrub inline secrets from tenant-visible prose (summaries, questions)."""
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    if not raw.strip():
        return ""
    return _truncate_field(redact_inline_secrets_in_text(raw), max_len)


def sanitize_resume_input_request(
    request: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Tenant-safe ``input_request`` on execution resume HTTP responses."""
    if not request:
        return None
    out = dict(request)
    for key in ("question", "context"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = sanitize_tenant_display_text(val, max_len=_DEFAULT_MAX_STRING)
    options = out.get("options")
    if isinstance(options, list):
        out["options"] = [
            sanitize_tenant_display_text(item, max_len=500) if isinstance(item, str) else item
            for item in options
        ]
    return out


def sanitize_resume_execution_api_result(
    result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Sanitize ``resume_execution`` return dict before HTTP serialization."""
    if not result:
        return {}
    out = dict(result)
    message = out.get("message")
    if message is not None:
        out["message"] = sanitize_tenant_display_text(message)
    input_request = out.get("input_request")
    if input_request is not None:
        out["input_request"] = sanitize_resume_input_request(input_request)
    return out


def sanitize_approval_document_for_tenant(
    doc: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Redact approval request documents returned from HITL APIs."""
    if not doc:
        return {}
    out = cast(Dict[str, Any], strip_resume_state_secrets(doc))
    if "inputs" in out:
        out["inputs"] = sanitize_execution_persisted_blob(out.get("inputs"))
    modified = out.get("modified_inputs")
    if modified is not None:
        out["modified_inputs"] = sanitize_execution_persisted_blob(modified)
    if isinstance(out.get("reason"), str):
        out["reason"] = _truncate_field(
            redact_inline_secrets_in_text(out["reason"]),
            _LARGE_FIELD_MAX,
        )
    if isinstance(out.get("decision_note"), str):
        out["decision_note"] = _truncate_field(
            redact_inline_secrets_in_text(out["decision_note"]),
            _DEFAULT_MAX_STRING,
        )
    return out


def sanitize_stream_task_complete_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Tenant-safe TASK_COMPLETE stream payload."""
    payload = dict(data or {})
    if "result" in payload:
        payload["result"] = sanitize_task_result_for_stream(payload.get("result"))
    if "inputs" in payload:
        payload["inputs"] = sanitize_execution_persisted_blob(payload.get("inputs"))
    return sanitize_persisted_event_data(payload)


def sanitize_run_event_document_for_tenant(
    doc: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Redact/truncate a run_events row returned from the observability API."""
    if not doc:
        return {}
    out = cast(Dict[str, Any], strip_resume_state_secrets(doc))
    payload = out.get("payload")
    if payload is not None:
        out["payload"] = sanitize_resume_event_payload(payload)
    err = out.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            err = dict(err)
            err["message"] = tenant_safe_trace_message(msg, fallback=TRACE_ERROR_RUN_FAILED)
            out["error"] = err
    return out


_STREAM_GENERIC_UI_KINDS = frozenset(
    {
        "init",
        "intent_detecting",
        "intent_detected",
        "agent_discovery_start",
        "agent_discovery_complete",
        "endpoint_discovery_start",
        "endpoint_discovery_complete",
        "building_workflow",
        "workflow_ready",
        "schedule_created",
        "auth_method_resolved",
        "iteration_start",
        "iteration_end",
        "task_progress",
        "input_received",
        "approval_received",
        "asking_approval",
        "agent_complete",
        "done",
        "cancelled",
    }
)


def sanitize_stream_generic_event_data(
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Key-redact plus inline scrub for informational stream events."""
    blob = sanitize_execution_persisted_blob(data or {})
    if not isinstance(blob, dict):
        return sanitize_persisted_event_data(data)
    return sanitize_persisted_event_data(
        _scrub_stream_string_fields(
            blob,
            (
                "message",
                "detail",
                "summary",
                "description",
                "question",
                "context",
                "content",
                "chunk",
                "agent_alias",
                "endpoint",
                "auth_method",
                "profile_name",
                "workflow_name",
            ),
            max_len=_DEFAULT_MAX_STRING,
        )
    )


def sanitize_stream_event_payload(
    event_kind: Any,
    data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Tenant-safe stream payload by event kind (string or StreamEventType).

    For trace export, triggers, and persisted redaction — not chat SSE/WebSocket
    (use ``chat_stream_event_data_for_client``).
    """
    kind = str(getattr(event_kind, "value", event_kind) or "").lower()
    if kind == "task_error":
        return sanitize_stream_task_error_data(data)
    if kind == "error":
        return sanitize_stream_error_data(data)
    if kind == "execution_summary":
        return sanitize_stream_execution_summary_data(data)
    if kind == "task_complete":
        return sanitize_stream_task_complete_data(data)
    if kind == "endpoint_approval_required":
        payload = dict(data or {})
        if "inputs" in payload:
            payload["inputs"] = sanitize_execution_persisted_blob(payload.get("inputs"))
        return sanitize_persisted_event_data(payload)
    if kind == "thought_complete":
        return sanitize_stream_thought_complete_data(data)
    if kind == "asking_input":
        return sanitize_stream_asking_input_data(data)
    if kind == "task_starting":
        return sanitize_stream_task_starting_data(data)
    if kind == "task_skipped":
        return sanitize_persisted_event_data(
            _scrub_stream_string_fields(dict(data or {}), ("reason",))
        )
    if kind == "inspect_response":
        payload = dict(data or {})
        if payload.get("payload") is not None:
            payload["payload"] = sanitize_execution_persisted_blob(payload.get("payload"))
        return sanitize_persisted_event_data(payload)
    if kind in ("message_chunk", "message_complete"):
        payload = dict(data or {})
        for key in ("chunk", "content", "message", "summary", "final_summary"):
            val = payload.get(key)
            if isinstance(val, str):
                payload[key] = _truncate_field(
                    redact_inline_secrets_in_text(val),
                    _DEFAULT_MAX_STRING,
                )
        return sanitize_persisted_event_data(payload)
    if kind == "thinking":
        payload = _scrub_stream_string_fields(dict(data or {}), ("message",))
        return sanitize_persisted_event_data(payload)
    if kind in _STREAM_GENERIC_UI_KINDS:
        return sanitize_stream_generic_event_data(data)
    return sanitize_persisted_event_data(data)
