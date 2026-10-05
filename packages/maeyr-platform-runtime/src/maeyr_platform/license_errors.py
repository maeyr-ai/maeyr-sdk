"""Finite tenant-safe license errors shared by HTTP and stream boundaries."""

from __future__ import annotations

from collections.abc import Mapping

_PUBLIC_LICENSE_DENIALS: dict[tuple[int, str], dict[str, object]] = {
    (403, "trial_expired"): {
        "code": "trial_expired",
        "message": "The Free trial has expired.",
        "retryable": False,
    },
    (403, "license_disabled"): {
        "code": "license_disabled",
        "message": "This account's license is disabled. Contact your administrator.",
        "retryable": False,
    },
    (403, "managed_ai_not_included"): {
        "code": "managed_ai_not_included",
        "message": "Maeyr AI is not included in this plan. Configure your own LLM key.",
        "retryable": False,
    },
    (403, "spending_not_authorized"): {
        "code": "spending_not_authorized",
        "message": "This AI request exceeds the authorized spending limit. Check billing limits.",
        "retryable": False,
    },
    (429, "quota_exceeded"): {
        "code": "quota_exceeded",
        "message": "The plan or workspace allowance has been reached. Check usage and limits.",
        "retryable": False,
    },
    (429, "concurrency_exceeded"): {
        "code": "concurrency_exceeded",
        "message": "All concurrent execution slots are in use. Wait for a run to finish.",
        "retryable": True,
    },
    (429, "rate_exceeded"): {
        "code": "rate_exceeded",
        "message": "The platform request rate limit was reached. Wait and try again.",
        "retryable": True,
    },
    (429, "model_call_limit_exceeded"): {
        "code": "model_call_limit_exceeded",
        "message": "This AI turn reached its model-call limit. Review the turn and its limits.",
        "retryable": False,
    },
    (429, "queue_exceeded"): {
        "code": "queue_exceeded",
        "message": "The execution queue is full. Wait for queued runs to finish.",
        "retryable": True,
    },
    (409, "operation_conflict"): {
        "code": "operation_conflict",
        "message": "This request conflicts with the current operation. Refresh its status.",
        "retryable": False,
    },
}

_LICENSE_CONFLICT_MESSAGES = {
    "cleanup_busy": "Resource cleanup is already running. Wait for it to finish.",
    "execution_duration_exceeded": (
        "This execution reached its duration limit. Review its limits before starting another run."
    ),
    "inventory_changed": (
        "The resource inventory changed during this request. Refresh and try again."
    ),
    "lease_owner_conflict": "Another worker owns this execution. Refresh the execution status.",
    "operation_already_started": (
        "This operation has already started. Check its existing execution."
    ),
    "operation_completed": "This operation has already completed. Open its existing result.",
    "operation_expired": "This operation expired. Start a new request.",
    "operation_released": "This operation was released. Start a new request.",
    "policy_changed": "The account policy changed during this request. Refresh and try again.",
    "queue_not_found": "This queued operation is no longer available. Refresh its status.",
    "reservation_exceeded": (
        "This operation exceeded its reserved allowance. Review its usage and limits."
    ),
    "reservation_not_found": (
        "This operation's allowance reservation is no longer available. Start a new request."
    ),
    "root_identity_required": (
        "This request is missing its parent execution identity. "
        "Start it again from the original workflow."
    ),
    "root_not_started": "The parent execution has not started. Check its status before continuing.",
    "stale_lease_generation": (
        "This worker's execution lease is outdated. Refresh the execution status."
    ),
    "trial_cleanup_in_progress": "Trial resource cleanup is in progress. Wait for it to finish.",
    "trial_not_expired": "The trial is still active, so expiry cleanup cannot run yet.",
    "turn_identity_required": (
        "This AI request is missing its conversation-turn identity. Start a new turn."
    ),
}
_PUBLIC_LICENSE_DENIALS.update(
    {
        (409, code): {"code": code, "message": message, "retryable": False}
        for code, message in _LICENSE_CONFLICT_MESSAGES.items()
    }
)


PUBLIC_LICENSE_DENIAL_MESSAGES = frozenset(
    str(detail["message"]) for detail in _PUBLIC_LICENSE_DENIALS.values()
)
PUBLIC_LICENSE_DENIAL_CODES = frozenset(code for _, code in _PUBLIC_LICENSE_DENIALS)


def public_license_denial_detail(status_code: int, detail: object) -> dict[str, object] | None:
    """Return static copy for a known status/code pair; never copy caller text."""
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("code")
    if not isinstance(code, str):
        return None
    known = _PUBLIC_LICENSE_DENIALS.get((status_code, code))
    return dict(known) if known is not None else None
