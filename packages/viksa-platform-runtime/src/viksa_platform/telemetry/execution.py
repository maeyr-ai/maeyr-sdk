"""Shared execution-rollup contracts for trace telemetry.

This is a code module, not a service. Trace-service stores traces; UI and
APIs import these grouping/status helpers the same way they import tracing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

EXECUTION_ROLLUP_GROUP_BY = ("status", "user", "day")
EXECUTION_SUCCESS_STATUSES = ("ok", "completed")
EXECUTION_FAILURE_STATUSES = ("error", "timeout")

_GROUP_ALIASES = {
    "person": "user",
    "date": "day",
}

_STATUS_LABELS = {
    "ok": "Succeeded",
    "completed": "Succeeded",
    "error": "Failed",
    "timeout": "Timed out",
    "running": "Running",
    "waiting": "Waiting",
    "waiting_approval": "Waiting for approval",
}


def normalize_execution_group_by(value: str | None) -> str:
    """Return a canonical execution rollup dimension."""
    raw = str(value or "status").strip().lower()
    group_by = _GROUP_ALIASES.get(raw, raw)
    if group_by not in EXECUTION_ROLLUP_GROUP_BY:
        raise ValueError("unsupported execution rollup group")
    return group_by


def execution_success_cond() -> dict[str, Any]:
    """Mongo expression: trace finished successfully."""
    return {"$in": [{"$ifNull": ["$status", ""]}, list(EXECUTION_SUCCESS_STATUSES)]}


def execution_failure_cond() -> dict[str, Any]:
    """Mongo expression: trace finished with error or timeout."""
    return {"$in": [{"$ifNull": ["$status", ""]}, list(EXECUTION_FAILURE_STATUSES)]}


def execution_rollup_group_id(group_by: str) -> Any:
    """Mongo ``$group._id`` expression for an execution dimension."""
    canonical = normalize_execution_group_by(group_by)
    if canonical == "status":
        return {"$ifNull": ["$status", "unknown"]}
    if canonical == "user":
        return {
            "$ifNull": [
                "$user_email",
                {"$ifNull": ["$user_id", "unknown"]},
            ]
        }
    return {
        "$dateToString": {
            "format": "%Y-%m-%d",
            "date": "$started_at",
            "timezone": "UTC",
        }
    }


def execution_status_label(status: str | None) -> str:
    raw = str(status or "unknown").strip() or "unknown"
    return _STATUS_LABELS.get(raw, raw.replace("_", " ").capitalize())


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def shape_execution_rollup_row(raw: Mapping[str, Any], group_by: str) -> dict[str, Any]:
    """Normalize one Mongo group row into the public execution rollup shape."""
    canonical = normalize_execution_group_by(group_by)
    group_id = raw.get("_id")
    if group_id is None or group_id == "":
        group_id = "unknown"
    label = str(group_id)
    if canonical == "status":
        label = execution_status_label(str(group_id))
    return {
        "group_id": str(group_id),
        "group_label": label,
        "run_count": _as_int(raw.get("run_count")),
        "succeeded": _as_int(raw.get("succeeded")),
        "failed": _as_int(raw.get("failed")),
        "avg_duration_ms": _as_int(raw.get("avg_duration_ms")),
        "tokens": _as_int(raw.get("tokens")),
    }


def shape_execution_rollup(
    *,
    group_by: str,
    totals: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the public execution-rollup payload from a ``$facet`` result."""
    canonical = normalize_execution_group_by(group_by)
    source = totals or {}
    return {
        "group_by": canonical,
        "run_count": _as_int(source.get("run_count")),
        "succeeded": _as_int(source.get("succeeded")),
        "failed": _as_int(source.get("failed")),
        "avg_duration_ms": _as_int(source.get("avg_duration_ms")),
        "p95_duration_ms": _as_int(source.get("max_duration_ms") or source.get("p95_duration_ms")),
        "tokens": _as_int(source.get("tokens")),
        "rows": [shape_execution_rollup_row(row, canonical) for row in rows],
    }


__all__ = [
    "EXECUTION_FAILURE_STATUSES",
    "EXECUTION_ROLLUP_GROUP_BY",
    "EXECUTION_SUCCESS_STATUSES",
    "execution_failure_cond",
    "execution_rollup_group_id",
    "execution_status_label",
    "execution_success_cond",
    "normalize_execution_group_by",
    "shape_execution_rollup",
    "shape_execution_rollup_row",
]
