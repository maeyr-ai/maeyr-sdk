"""Pure Mongo updates for durable Trace scheduling metadata.

Producers and Trace must use the same scheduling expression: incoming events
cannot erase a dependency cooldown or an active lease. No connections or index
creation happen here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_ACCOUNT = re.compile(r"^AC-[A-Za-z0-9][A-Za-z0-9_-]{0,124}$")
_KINDS = {"outbox", "rollup", "repair", "retention"}
TRACE_WORK_READY_INDEX = "trace_work_ready"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def enqueue_trace_work_update(
    account_id: str, kind: str, due_at: datetime, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Increment work generation without bypassing a persisted retry or lease.

    ``pending_due_at`` captures arrivals since the last claim. A consumer clears
    it when claiming and retains it on generation changes at completion.
    ``ready_at`` is the indexed eligibility timestamp, not an inventory scan.
    """
    if not _ACCOUNT.fullmatch(account_id) or kind not in _KINDS:
        raise ValueError("Trace work requires a canonical account and kind")
    now = now or datetime.now(timezone.utc)
    if due_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Trace scheduling timestamps must include a timezone")
    return [
        {
            "$set": {
                "account_id": {"$literal": account_id},
                "kind": {"$literal": kind},
                "created_at": {"$ifNull": ["$created_at", now]},
                "lease_until": {"$ifNull": ["$lease_until", _EPOCH]},
                "retry_at": {"$ifNull": ["$retry_at", _EPOCH]},
                "owner": {"$ifNull": ["$owner", None]},
                "failures": {"$ifNull": ["$failures", 0]},
                "generation": {"$add": [{"$ifNull": ["$generation", 0]}, 1]},
                "due_at": {"$min": [{"$ifNull": ["$due_at", due_at]}, due_at]},
                "pending_due_at": {
                    "$min": [
                        {"$ifNull": ["$pending_due_at", due_at]},
                        due_at,
                    ]
                },
                "updated_at": now,
            }
        },
        {"$set": {"ready_at": {"$max": ["$due_at", "$retry_at", "$lease_until"]}}},
    ]
