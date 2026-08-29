"""Derive stable Bento-style labels from trace span metadata."""

from __future__ import annotations

from typing import Any


def derive_labels(
    *,
    status: str,
    operation: str | None = None,
    attributes: dict[str, Any] | None = None,
    span_name: str | None = None,
) -> list[str]:
    attrs = attributes or {}
    labels: list[str] = []

    if status == "error":
        error_type = str(attrs.get("error.type") or attrs.get("error_type") or "").lower()
        if "llm" in error_type or operation == "llm_call" or (span_name or "").startswith("llm."):
            labels.append("llm_error")
        elif "http" in error_type or operation == "http_client":
            labels.append("api_error")
        elif "timeout" in error_type or status == "timeout":
            labels.append("timeout")
        else:
            labels.append("error")

    http_status = attrs.get("http.status_code")
    if http_status and int(http_status) >= 400:
        labels.append("api_error")

    if operation == "pulse_invoke" and status == "error":
        labels.append("pulse_error")
    if operation == "worker_execute" and status == "error":
        labels.append("worker_error")

    mode = str(attrs.get("tool.execution_mode") or attrs.get("agent_type") or "").lower()
    if mode == "cloud":
        labels.append("cloud")
    elif mode == "secure":
        labels.append("secure")

    queue = attrs.get("tool.task_queue") or attrs.get("task_queue")
    if queue:
        labels.append(f"queue:{queue}")

    return list(dict.fromkeys(labels))


__all__ = ["derive_labels"]
