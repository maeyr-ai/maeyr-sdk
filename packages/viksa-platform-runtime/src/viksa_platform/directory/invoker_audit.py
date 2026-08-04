"""Shared persistence behavior for tenant-scoped invoker audit queries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

DatabaseForAccount = Callable[[str], str]
ProjectFilter = Callable[[Mapping[str, str]], Mapping[str, Any]]


class InvokerAuditQueryStore:
    """Mongo-backed audit query adapter with bounded, tenant-scoped pagination."""

    def __init__(
        self,
        scope: Mapping[str, str],
        *,
        mongo_client: Any,
        database_for_account: DatabaseForAccount,
        project_filter: ProjectFilter,
        collection_name: str = "invoker_subject_audit",
    ) -> None:
        self._scope = dict(scope)
        self._mongo_client = mongo_client
        self._project_filter = project_filter
        self._collection_name = collection_name
        self._db = database_for_account(self._scope["account_id"])

    def _coll(self) -> Any:
        return self._mongo_client.get_collection(self._collection_name, self._db)

    async def record(
        self,
        *,
        run_id: str | None,
        channel: str,
        external_user_id: str,
        customer_user_id: str | None,
        agent_alias: str,
        endpoint: str,
        trust: str,
        injected_fields: dict[str, str],
        dropped_llm_fields: list[str],
    ) -> None:
        """Retain the historical no-op recorder until durable writes are enabled."""

    async def list_entries(
        self,
        *,
        customer_user_id: str = "",
        endpoint: str = "",
        channel: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        await self._mongo_client.initialize()
        query: dict[str, Any] = dict(self._project_filter(self._scope))
        customer = customer_user_id.strip()
        if customer:
            query["customer_user_id"] = {"$regex": customer, "$options": "i"}
        normalized_endpoint = endpoint.strip()
        if normalized_endpoint:
            query["endpoint"] = {"$regex": normalized_endpoint, "$options": "i"}
        normalized_channel = channel.strip().lower()
        if normalized_channel:
            query["channel"] = normalized_channel
        collection = self._coll()
        total = await collection.count_documents(query)
        cursor = (
            collection.find(query)
            .sort("created_at", -1)
            .skip(max(0, (page - 1) * page_size))
            .limit(page_size)
        )
        rows = [document async for document in cursor]
        return rows, total


__all__ = [
    "DatabaseForAccount",
    "InvokerAuditQueryStore",
    "ProjectFilter",
]
