"""Shared bounded primary database observations and lease-fenced publication."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from bson.int64 import Int64
from pymongo import ReadPreference, ReturnDocument
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from maeyr_platform.mongo_storage import ACCOUNT_DATABASE, MAX_STORAGE_BYTES
from maeyr_platform.mongodb_indexes import mongodb_index_information

MONGODB_STORAGE_LIMIT_FIELD = "max_mongodb_storage_bytes"


def valid_storage_grant(value: object) -> bool:
    return (
        isinstance(value, int) and type(value) in {int, Int64} and 0 <= value <= MAX_STORAGE_BYTES
    )


def valid_storage_counters(state: object) -> bool:
    fields = (
        "committed_growth_total",
        "baseline_committed_growth",
        "pending_growth_bytes",
        "active_operations",
    )
    return (
        isinstance(state, dict)
        and all(valid_storage_grant(state.get(key)) for key in fields)
        and state["baseline_committed_growth"] <= state["committed_growth_total"]
        and valid_storage_grant(state.get("max_age_seconds"))
        and 5 <= state["max_age_seconds"] <= 300
    )


def normalize_mongodb_byte_measurement(value: object) -> int:
    """Normalize exact scale=1 provider metadata; never persisted grants."""
    if isinstance(value, float) and type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("Database storage metrics are unavailable or invalid")
    elif not (isinstance(value, int) and type(value) in {int, Int64}):
        raise ValueError("Database storage metrics are unavailable or invalid")
    if not 0 <= value <= MAX_STORAGE_BYTES:
        raise ValueError("Database storage metrics are unavailable or invalid")
    return int(value)


def measured_storage_fields(stats: dict[str, Any], *, observed_at: datetime) -> dict[str, Any]:
    """Mongo dbStats scale=1 emits doubles; normalize only exact provider bytes.

    Persisted license grants and growth counters never use this normalization.
    """
    values = [
        normalize_mongodb_byte_measurement(stats.get(field)) for field in ("dataSize", "indexSize")
    ]
    if sum(values) > MAX_STORAGE_BYTES:
        raise ValueError("Database storage metrics are unavailable or invalid")
    stamp = (
        observed_at.astimezone(timezone.utc).replace(tzinfo=None)
        if observed_at.tzinfo
        else observed_at
    )
    return {
        "data_size_bytes": values[0],
        "index_size_bytes": values[1],
        "total_bytes": sum(values),
        "observed_at": stamp,
    }


@dataclass(frozen=True, slots=True)
class StorageObservationSettings:
    MAX_AGE_SECONDS: int = 60
    TICK_SECONDS: int = 5
    BATCH_SIZE: int = 100
    CONCURRENCY: int = 4
    LEASE_SECONDS: int = 30
    QUERY_TIMEOUT_MS: int = 5000

    def __post_init__(self) -> None:
        bounds = {
            "MAX_AGE_SECONDS": (5, 300),
            "TICK_SECONDS": (1, 30),
            "BATCH_SIZE": (1, 1000),
            "CONCURRENCY": (1, 16),
            "LEASE_SECONDS": (15, 120),
            "QUERY_TIMEOUT_MS": (1000, 5000),
        }
        for field, (minimum, maximum) in bounds.items():
            value = getattr(self, field)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Storage observation configuration is invalid")


DUE_INDEX = "account_mongodb_storage_due_v1"
DUE_INDEX_KEYS = [("mongodb_storage.next_observation_at", 1), ("_id", 1)]
DUE_INDEX_FILTER = {"mongodb_storage.next_observation_at": {"$type": "date"}}
ACTIVE_DUE_INDEX = "account_mongodb_storage_active_due_v1"
ACTIVE_DUE_FILTER = {
    "$or": [
        {"mongodb_storage.active_operations": {"$gt": 0}},
        {"mongodb_storage.pending_growth_bytes": {"$gt": 0}},
    ]
}
DORMANT_AT = datetime(9999, 1, 1)
_ACCOUNT_ID = ACCOUNT_DATABASE
ACCOUNT_DATABASE_MONGO_PATTERN = "^" + ACCOUNT_DATABASE.pattern.removesuffix(r"\Z") + r"\z"


def _accounting_authority_expression() -> dict[str, Any]:
    """Only canonical BSON grants and counters may be leased or published.

    Query field type predicates also match array elements. Expression types
    check the field itself, so an array cannot authorize a partial ledger write.
    No missing grant or state is initialized by the runtime observer.
    """
    bounded_fields = [
        f"limits.{MONGODB_STORAGE_LIMIT_FIELD}",
        "mongodb_storage.committed_growth_total",
        "mongodb_storage.baseline_committed_growth",
        "mongodb_storage.pending_growth_bytes",
        "mongodb_storage.active_operations",
    ]
    checks: list[dict[str, Any]] = [
        {"$eq": [{"$type": "$limits"}, "object"]},
        {"$eq": [{"$type": "$mongodb_storage"}, "object"]},
    ]
    for field in bounded_fields:
        checks.extend(
            [
                {"$in": [{"$type": f"${field}"}, ["int", "long"]]},
                {"$gte": [f"${field}", 0]},
                {"$lte": [f"${field}", MAX_STORAGE_BYTES]},
            ]
        )
    checks.extend(
        [
            {"$in": [{"$type": "$mongodb_storage.max_age_seconds"}, ["int", "long"]]},
            {"$gte": ["$mongodb_storage.max_age_seconds", 5]},
            {"$lte": ["$mongodb_storage.max_age_seconds", 300]},
            {
                "$lte": [
                    "$mongodb_storage.baseline_committed_growth",
                    "$mongodb_storage.committed_growth_total",
                ]
            },
        ]
    )
    return {"$and": checks}


class MongoDBStorageObserver:
    def __init__(
        self,
        accounts: Any,
        client: Any,
        *,
        settings: StorageObservationSettings | None = None,
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        self.accounts = accounts.with_options(
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern("majority", wtimeout=5000),
        )
        self.client = client
        self.settings = settings or StorageObservationSettings()
        self.clock = clock

    async def ensure_indexes(self) -> None:
        await self.accounts.create_index(
            DUE_INDEX_KEYS, name=DUE_INDEX, partialFilterExpression=DUE_INDEX_FILTER
        )
        await self.accounts.create_index(
            DUE_INDEX_KEYS, name=ACTIVE_DUE_INDEX, partialFilterExpression=ACTIVE_DUE_FILTER
        )

    async def require_indexes(self) -> None:
        indexes = await mongodb_index_information(
            self.accounts, max_time_ms=self.settings.QUERY_TIMEOUT_MS
        )
        for name, expected_filter in (
            (DUE_INDEX, DUE_INDEX_FILTER),
            (ACTIVE_DUE_INDEX, ACTIVE_DUE_FILTER),
        ):
            spec = indexes.get(name, {})
            if (
                list(spec.get("key", [])) != DUE_INDEX_KEYS
                or spec.get("partialFilterExpression") != expected_filter
                or any(spec.get(key) for key in ("sparse", "hidden", "unique", "collation"))
                or any(key in spec for key in ("buildUUID", "indexBuildInfo", "expireAfterSeconds"))
            ):
                raise RuntimeError("MongoDB storage observation index requires explicit setup")

    async def due_page(self) -> list[dict[str, Any]]:
        due: dict[str, Any] = {
            "_id": {"$regex": ACCOUNT_DATABASE_MONGO_PATTERN},
            "mongodb_storage.next_observation_at": {"$type": "date", "$lte": self.clock()},
            "$expr": _accounting_authority_expression(),
        }
        # Malformed rows require explicit release repair. Repeatedly returning
        # them without an eligible lease would starve valid due accounts behind
        # the same bounded first page. This is a read filter, never data repair.
        projection = {"_id": 1, "mongodb_storage.next_observation_at": 1}
        active = list(
            await self.accounts.find({**due, **ACTIVE_DUE_FILTER}, projection)
            .hint(ACTIVE_DUE_INDEX)
            .sort(DUE_INDEX_KEYS)
            .limit(self.settings.BATCH_SIZE)
            .max_time_ms(self.settings.QUERY_TIMEOUT_MS)
            .to_list(length=self.settings.BATCH_SIZE)
        )
        remaining = self.settings.BATCH_SIZE - len(active)
        if not remaining:
            return active
        # At most one page of IDs; initial unknown accounts cannot starve
        # active leases behind a large release-migration backlog.
        if active:
            due["_id"]["$nin"] = [row["_id"] for row in active]
        others = list(
            await self.accounts.find(due, projection)
            .hint(DUE_INDEX)
            .sort(DUE_INDEX_KEYS)
            .limit(remaining)
            .max_time_ms(self.settings.QUERY_TIMEOUT_MS)
            .to_list(length=remaining)
        )
        return active + others

    async def observe_account(self, account_id: str, *, force: bool = False) -> str:
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            return "invalid_account"
        now = self.clock()
        owner = uuid4().hex
        selector: dict[str, Any] = {"_id": account_id}
        if not force:
            selector["mongodb_storage.next_observation_at"] = {"$type": "date", "$lte": now}
        try:
            claim = await self.accounts.find_one_and_update(
                {
                    **selector,
                    "$expr": _accounting_authority_expression(),
                    "$or": [
                        {"mongodb_storage.observation_lease_until": {"$exists": False}},
                        {"mongodb_storage.observation_lease_until": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "mongodb_storage.observation_lease_owner": owner,
                        "mongodb_storage.observation_lease_until": now
                        + timedelta(seconds=self.settings.LEASE_SECONDS),
                    }
                },
                projection={"_id": 1, "limits": 1, "mongodb_storage": 1},
                return_document=ReturnDocument.AFTER,
                maxTimeMS=self.settings.QUERY_TIMEOUT_MS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unavailable"
        if claim is None:
            if force:
                # Distinguish a competing lease from missing accounting for
                # operator refresh/preparation. This exact read never seeds
                # fields or contacts an account database.
                try:
                    current = await asyncio.wait_for(
                        self.accounts.find_one(
                            {"_id": account_id},
                            {"limits": 1, "mongodb_storage": 1},
                            max_time_ms=self.settings.QUERY_TIMEOUT_MS,
                        ),
                        timeout=8,
                    )
                    limits = current.get("limits") if isinstance(current, dict) else None
                    limit = (
                        limits.get(MONGODB_STORAGE_LIMIT_FIELD)
                        if isinstance(limits, dict) else None
                    )
                    if not valid_storage_grant(limit) or not valid_storage_counters(
                        current.get("mongodb_storage") if isinstance(current, dict) else None
                    ):
                        return "unavailable"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return "unavailable"
            return "not_due"
        fence: dict[str, Any] = {
            "_id": account_id,
            "$expr": _accounting_authority_expression(),
            "mongodb_storage.observation_lease_owner": owner,
            "mongodb_storage.observation_lease_until": {"$gt": self.clock()},
        }
        try:
            state = claim.get("mongodb_storage")
            limits = claim.get("limits")
            limit = limits.get(MONGODB_STORAGE_LIMIT_FIELD) if isinstance(limits, dict) else None
            if not valid_storage_grant(limit) or not valid_storage_counters(state):
                raise ValueError("Storage accounting requires explicit data migration")
            # Capture before dbStats. Pending operations and commits occurring
            # after this capture remain conservatively charged after publishing.
            baseline = state["committed_growth_total"]
            database = self.client[account_id].with_options(read_preference=ReadPreference.PRIMARY)
            stats = await asyncio.wait_for(
                database.command(
                    {"dbStats": 1, "scale": 1, "maxTimeMS": self.settings.QUERY_TIMEOUT_MS}
                ),
                timeout=8,
            )
            observed = self.clock()
            fields = measured_storage_fields(stats, observed_at=observed)
            active = {
                "$or": [
                    {"$gt": ["$mongodb_storage.active_operations", 0]},
                    {"$gt": ["$mongodb_storage.pending_growth_bytes", 0]},
                    {"$gt": ["$mongodb_storage.committed_growth_total", baseline]},
                ]
            }
            updates = {f"mongodb_storage.{key}": value for key, value in fields.items()}
            updates.update(
                {
                    "mongodb_storage.baseline_committed_growth": baseline,
                    "mongodb_storage.next_observation_at": {
                        "$cond": [
                            active,
                            observed + timedelta(seconds=max(1, state["max_age_seconds"] // 2)),
                            DORMANT_AT,
                        ]
                    },
                }
            )
            fence["mongodb_storage.observation_lease_until"] = {"$gt": self.clock()}
            fence["$and"] = [
                {
                    "$or": [
                        {"mongodb_storage.observed_at": None},
                        {"mongodb_storage.observed_at": {"$lte": observed}},
                    ]
                },
                {"mongodb_storage.baseline_committed_growth": {"$lte": baseline}},
                {"mongodb_storage.committed_growth_total": {"$gte": baseline}},
            ]
            result = await self.accounts.update_one(
                fence,
                [
                    {"$set": updates},
                    {
                        "$unset": [
                            "mongodb_storage.observation_lease_owner",
                            "mongodb_storage.observation_lease_until",
                            "mongodb_storage.observation_error",
                        ]
                    },
                ],
            )
            return "observed" if result.modified_count else "lease_lost"
        except asyncio.CancelledError:
            raise  # The durable lease expires; no pending reservation is forgiven.
        except Exception:
            fence["mongodb_storage.observation_lease_until"] = {"$gt": self.clock()}
            try:
                await self.accounts.update_one(
                    fence,
                    {
                        "$set": {
                            "mongodb_storage.observation_error": "observation_unavailable",
                            "mongodb_storage.next_observation_at": self.clock()
                            + timedelta(seconds=30),
                        },
                        "$unset": {
                            "mongodb_storage.observation_lease_owner": "",
                            "mongodb_storage.observation_lease_until": "",
                        },
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # The durable lease expires; never clear pending growth.
            return "unavailable"

    async def run_once(self) -> dict[str, int]:
        await self.require_indexes()
        rows = await self.due_page()
        semaphore = asyncio.Semaphore(self.settings.CONCURRENCY)

        async def run(row: dict[str, Any]) -> str:
            async with semaphore:
                return await self.observe_account(row["_id"])

        results = await asyncio.gather(*(run(row) for row in rows))
        return {
            "due": len(rows),
            **{
                key: results.count(key)
                for key in ("observed", "not_due", "unavailable", "lease_lost", "invalid_account")
            },
        }


async def refresh_account_storage(
    client: Any, account_id: str, *, settings: StorageObservationSettings | None = None
) -> str:
    """Targeted migration refresh; callers recheck canonical admission afterward.

    A competing lease returns not_due. No grant is inferred, account document
    created, ledger refunded, scan performed, or Auth service contacted.
    """
    accounts = client["user-management"]["accounts"]
    observer = MongoDBStorageObserver(accounts, client, settings=settings)
    return await observer.observe_account(account_id, force=True)
