from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from maeyr_platform.tracing import mongo_outbox as module


def expression(value: Any, row: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return row.get(value[1:])
    if isinstance(value, dict):
        op, args = next(iter(value.items()))
        if op == "$literal":
            return args
        values = [expression(item, row) for item in args]
        if op == "$ifNull":
            return values[1] if values[0] is None else values[0]
        if op == "$min":
            return min(values)
        if op == "$max":
            return max(values)
        if op == "$add":
            return sum(values)
        raise AssertionError(op)
    return value


class Collection:
    def __init__(self, client: Client, name: str) -> None:
        self.client, self.name = client, name
        self.index_calls = 0

    async def create_index(self, *args: Any, **kwargs: Any) -> str:
        self.index_calls += 1
        if self.client.index_error:
            raise RuntimeError("index unavailable")
        return str(kwargs["name"])

    async def update_one(self, selector: Any, update: Any, **kwargs: Any) -> Any:
        assert kwargs["session"] is self.client.session
        if self.name.endswith(module.TRACE_MAINTENANCE_COLLECTION) and self.client.wakeup_error:
            raise RuntimeError("wakeup unavailable")
        rows = self.client.pending.setdefault(self.name, {})
        ident = selector["_id"]
        if isinstance(update, list):
            row = rows.setdefault(ident, {"_id": ident})
            for stage in update:
                before = deepcopy(row)
                row.update({key: expression(value, before) for key, value in stage["$set"].items()})
            return SimpleNamespace(acknowledged=not self.client.unacknowledged)
        if ident not in rows:
            rows[ident] = {"_id": ident, **deepcopy(update.get("$setOnInsert", {}))}
        row = rows[ident]
        row.update(deepcopy(update.get("$set", {})))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        for key, value in update.get("$min", {}).items():
            row[key] = min(row.get(key, value), value)
        return SimpleNamespace(acknowledged=not self.client.unacknowledged)


class Database:
    def __init__(self, client: Client, name: str) -> None:
        self.client, self.name = client, name

    def __getitem__(self, name: str) -> Collection:
        key = self.name + "." + name
        return self.client.collections.setdefault(key, Collection(self.client, key))


class Session:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def with_transaction(self, callback: Any, **kwargs: Any) -> Any:
        assert kwargs["write_concern"].document["w"] == "majority"
        assert kwargs["read_concern"].document["level"] == "snapshot"
        self.client.pending = deepcopy(self.client.rows)
        result = await callback(self)
        if self.client.commit_error:
            raise RuntimeError("commit unavailable")
        self.client.rows = self.client.pending
        return result


class Client:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.pending: dict[str, Any] = {}
        self.collections: dict[str, Collection] = {}
        self.session = Session(self)
        self.wakeup_error = self.commit_error = self.unacknowledged = self.index_error = False

    def __getitem__(self, name: str) -> Database:
        return Database(self, name)

    async def start_session(self) -> Session:
        return self.session


def event(account: str = "AC-A") -> dict[str, Any]:
    return {
        "account_id": account,
        "trace_id": "trace-1",
        "span_id": "span-1",
        "status": "completed",
    }


@pytest.fixture(autouse=True)
def clear_index_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACE_MAINTENANCE_DATABASE", raising=False)
    module._indexed_outboxes.clear()


@pytest.mark.asyncio
async def test_fallback_event_and_wakeup_commit_together_and_retry_is_idempotent() -> None:
    client = Client()
    assert await module.persist_mongo_trace_event(client, event())
    assert await module.persist_mongo_trace_event(client, event())
    assert len(client.rows["AC-A.trace_producer_outbox"]) == 1
    task = client.rows["maeyr_trace_control.trace_maintenance_tasks"]["outbox:AC-A"]
    assert task["generation"] == 2
    assert task["account_id"] == "AC-A"
    assert task["kind"] == "outbox"
    assert client.collections["AC-A.trace_producer_outbox"].index_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["wakeup_error", "commit_error", "unacknowledged", "index_error"]
)
async def test_failed_fallback_never_acknowledges_or_commits_partial_work(failure: str) -> None:
    client = Client()
    setattr(client, failure, True)
    with pytest.raises(RuntimeError):
        await module.persist_mongo_trace_event(client, event())
    assert not client.rows
    setattr(client, failure, False)
    assert await module.persist_mongo_trace_event(client, event())
    assert len(client.rows["AC-A.trace_producer_outbox"]) == 1


@pytest.mark.asyncio
async def test_new_event_preserves_claim_owner_and_earliest_due_time() -> None:
    client = Client()
    old_due = datetime.now(timezone.utc) - timedelta(hours=1)
    lease_end = datetime.now(timezone.utc) + timedelta(minutes=1)
    client.rows["maeyr_trace_control.trace_maintenance_tasks"] = {
        "outbox:AC-A": {
            "account_id": "AC-A",
            "kind": "outbox",
            "due_at": old_due,
            "generation": 10,
            "owner": "worker-1",
            "lease_until": lease_end,
            "failures": 3,
        },
    }
    assert await module.persist_mongo_trace_event(client, event())
    row = client.rows["maeyr_trace_control.trace_maintenance_tasks"]["outbox:AC-A"]
    assert row["generation"] == 11
    assert row["due_at"] == old_due
    assert row["owner"] == "worker-1"
    assert row["lease_until"] == lease_end
    assert row["failures"] == 3


@pytest.mark.asyncio
async def test_custom_control_database_and_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACE_MAINTENANCE_DATABASE", "trace_custom")
    client = Client()
    assert await module.persist_mongo_trace_event(client, event("AC-A"))
    assert await module.persist_mongo_trace_event(client, event("AC-B"))
    assert set(client.rows["trace_custom.trace_maintenance_tasks"]) == {
        "outbox:AC-A",
        "outbox:AC-B",
    }
    assert "AC-A.trace_producer_outbox" in client.rows
    assert "AC-B.trace_producer_outbox" in client.rows


@pytest.mark.asyncio
async def test_empty_account_never_opens_session_or_writes() -> None:
    client = Client()
    assert not await module.persist_mongo_trace_event(client, event(""))
    assert not client.collections


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "",
        "trace/control",
        "trace.db",
        "trace db",
        "x" * 64,
        "AC-A",
        "admin",
        "local",
        "config",
    ],
)
async def test_invalid_control_database_never_writes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("TRACE_MAINTENANCE_DATABASE", name)
    client = Client()
    with pytest.raises(ValueError, match="database name"):
        await module.persist_mongo_trace_event(client, event())
    assert not client.collections


@pytest.mark.asyncio
async def test_new_events_cannot_reset_failed_tenant_cooldown() -> None:
    client = Client()
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    client.rows["maeyr_trace_control.trace_maintenance_tasks"] = {
        "outbox:AC-A": {
            "account_id": "AC-A",
            "kind": "outbox",
            "generation": 3,
            "retry_at": retry_at,
            "failures": 5,
            "due_at": datetime.now(timezone.utc) - timedelta(hours=1),
        },
    }
    for _ in range(20):
        assert await module.persist_mongo_trace_event(client, event())
    row = client.rows["maeyr_trace_control.trace_maintenance_tasks"]["outbox:AC-A"]
    assert row["retry_at"] == retry_at
    assert row["ready_at"] == retry_at
    assert row["generation"] == 23
    assert row["failures"] == 5


@pytest.mark.asyncio
async def test_index_memo_is_bounded_and_separate_between_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_INDEX_CACHE_SIZE", 3)
    client = Client()
    for ident in range(10):
        assert await module.persist_mongo_trace_event(client, event(f"AC-{ident}"))
    assert len(module._indexed_outboxes) == 3
    assert await module.persist_mongo_trace_event(Client(), event("AC-9"))
    assert len(module._indexed_outboxes) == 3
