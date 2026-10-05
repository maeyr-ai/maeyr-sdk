"""Atomic producer fallback and its indexed Trace delivery wakeup.

The Mongo client is owned by each producer. This optional adapter deliberately
does not create a connection or scan databases. Both writes use one transaction,
so acknowledging the fallback always leaves discoverable delivery work.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from maeyr_platform.tracing.transport import durable_outbox_event_id
from maeyr_platform.tracing.work_registry import enqueue_trace_work_update

TRACE_PRODUCER_OUTBOX_COLLECTION = "trace_producer_outbox"
TRACE_MAINTENANCE_COLLECTION = "trace_maintenance_tasks"
# Only producer fallback writes reach this memo. Keep client references while
# cached so a recycled object id cannot incorrectly reuse another cluster's DDL.
_indexed_outboxes: OrderedDict[tuple[int, str], Any] = OrderedDict()
_INDEX_CACHE_SIZE = 2048


async def _ensure_delivery_index(client: Any, outbox: Any, account_id: str) -> None:
    key = (id(client), account_id)
    if key in _indexed_outboxes:
        _indexed_outboxes.move_to_end(key)
        return
    # DDL cannot run inside the event transaction. Concurrent first writes may
    # issue the same idempotent command; failed creation is never memoized.
    await outbox.create_index(
        [("stored_at", 1), ("_id", 1)],
        name="trace_producer_outbox_delivery_due",
    )
    _indexed_outboxes[key] = client
    _indexed_outboxes.move_to_end(key)
    while len(_indexed_outboxes) > _INDEX_CACHE_SIZE:
        _indexed_outboxes.popitem(last=False)


def maintenance_database_name() -> str:
    name = os.environ.get("TRACE_MAINTENANCE_DATABASE", "maeyr_trace_control")
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", name)
        or name.startswith("AC-")
        or name in {"admin", "local", "config"}
    ):
        raise ValueError("Invalid Trace maintenance database name")
    return name


async def persist_mongo_trace_event(client: Any, doc: Mapping[str, Any]) -> bool:
    """Store an idempotent event and wakeup; return only after commit.

    Requires Mongo transactions (replica set/sharded cluster). Failure propagates
    to the recorder, which retains the event instead of falsely acknowledging it.
    """
    account_id = str(doc.get("account_id") or "").strip()
    if not account_id:
        return False
    # Mongo support is optional to the shared runtime. Producers that configure
    # this adapter already provide Motor/PyMongo with their client.
    from pymongo import ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern

    control_name = maintenance_database_name()
    if account_id == control_name:
        raise ValueError("Trace account database cannot be its control database")
    event_id = durable_outbox_event_id(dict(doc))
    now = datetime.now(timezone.utc)
    wakeup_update = enqueue_trace_work_update(account_id, "outbox", now, now=now)
    outbox = client[account_id][TRACE_PRODUCER_OUTBOX_COLLECTION]
    tasks = client[control_name][TRACE_MAINTENANCE_COLLECTION]
    await _ensure_delivery_index(client, outbox, account_id)

    async def persist(session: Any) -> bool:
        event = await outbox.update_one(
            {"_id": event_id},
            {"$setOnInsert": {"span": dict(doc), "account_id": account_id, "stored_at": now}},
            upsert=True,
            session=session,
        )
        wakeup = await tasks.update_one(
            {"_id": "outbox:" + account_id},
            wakeup_update,
            upsert=True,
            session=session,
        )
        if not event.acknowledged or not wakeup.acknowledged:
            raise RuntimeError("Trace outbox transaction was not acknowledged")
        return True

    async with await client.start_session() as session:
        result = await session.with_transaction(
            persist,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )
    return bool(result)
