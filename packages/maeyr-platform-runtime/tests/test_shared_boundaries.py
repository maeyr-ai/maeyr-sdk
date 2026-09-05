from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from maeyr_platform.compat.source_path import ensure_resolved_source_path
from maeyr_platform.directory.invoker_audit import InvokerAuditQueryStore
from maeyr_platform.health import build_mongo_readiness_endpoint
from maeyr_platform.security.encryption import RequiredPrimaryKeyMixin


class _Health:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy
        self.databases: list[str] = []

    async def check_connection(self, database_name: str) -> bool:
        self.databases.append(database_name)
        return self.healthy


@pytest.mark.asyncio
async def test_readiness_endpoint_uses_injected_health_and_fails_closed() -> None:
    healthy = _Health(True)
    assert await build_mongo_readiness_endpoint(healthy)() == {
        "status": "ready",
        "database": "mongodb",
    }
    assert healthy.databases == ["admin"]

    unavailable = await build_mongo_readiness_endpoint(_Health(False))()
    assert isinstance(unavailable, JSONResponse)
    assert unavailable.status_code == 503


def test_source_path_is_prepended_once(tmp_path: Path) -> None:
    original = list(sys.path)
    try:
        ensure_resolved_source_path(lambda: tmp_path)
        ensure_resolved_source_path(lambda: tmp_path)
        assert sys.path.count(str(tmp_path)) == 1
        assert sys.path[0] == str(tmp_path)
    finally:
        sys.path[:] = original


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.skip_value = -1
        self.limit_value = -1

    def sort(self, _field: str, _direction: int) -> _Cursor:
        return self

    def skip(self, value: int) -> _Cursor:
        self.skip_value = value
        return self

    def limit(self, value: int) -> _Cursor:
        self.limit_value = value
        return self

    def __aiter__(self) -> _Cursor:
        self._index = 0
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self._index]
        self._index += 1
        return row


class _Collection:
    def __init__(self) -> None:
        self.query: dict[str, Any] = {}
        self.cursor = _Cursor([{"_id": "audit-1"}])

    async def count_documents(self, query: dict[str, Any]) -> int:
        self.query = query
        return 1

    def find(self, query: dict[str, Any]) -> _Cursor:
        self.query = query
        return self.cursor


class _Mongo:
    def __init__(self) -> None:
        self.initialized = 0
        self.collection = _Collection()

    async def initialize(self) -> None:
        self.initialized += 1

    def get_collection(self, _name: str, _database: str) -> _Collection:
        return self.collection


@pytest.mark.asyncio
async def test_invoker_audit_query_is_scoped_normalized_and_bounded() -> None:
    mongo = _Mongo()
    store = InvokerAuditQueryStore(
        {"account_id": "acct", "project_id": "project"},
        mongo_client=mongo,
        database_for_account=lambda account: f"db-{account}",
        project_filter=lambda scope: {"project_id": scope["project_id"]},
    )
    rows, total = await store.list_entries(
        customer_user_id="  Alice ",
        endpoint=" /orders ",
        channel=" SLACK ",
        page=0,
        page_size=25,
    )
    assert rows == [{"_id": "audit-1"}]
    assert total == 1
    assert mongo.initialized == 1
    assert mongo.collection.query == {
        "project_id": "project",
        "customer_user_id": {"$regex": "Alice", "$options": "i"},
        "endpoint": {"$regex": "/orders", "$options": "i"},
        "channel": "slack",
    }
    assert mongo.collection.cursor.skip_value == 0
    assert mongo.collection.cursor.limit_value == 25


class _MissingPrimaryError(RuntimeError):
    pass


class _Keyring(RequiredPrimaryKeyMixin[str]):
    _primary_version = "v2"
    _keyring = {"v1": "old", "v2": "current"}
    _missing_primary_key_exception = _MissingPrimaryError
    _missing_primary_key_message = "primary unavailable"


def test_required_primary_key_mixin_returns_active_key_and_fails_typed() -> None:
    keyring = _Keyring()
    assert keyring._require_primary() == "current"
    keyring._primary_version = "v3"
    with pytest.raises(_MissingPrimaryError, match="primary unavailable"):
        keyring._require_primary()
