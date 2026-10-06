"""Account database write admission, shared by every platform Mongo adapter.

The authority is ``user-management.accounts``: stored limits and observations,
never plan-name fallbacks. One indexed atomic reservation protects concurrent
writers; successful and ambiguous writes retain their growth charge until a
primary dbStats observation reconciles it. No document scan or Auth RPC occurs
in admission. Reservations are conservative estimates, not a claim that Mongo
can predict physical index allocation before a mutation.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
from inspect import isawaitable, iscoroutinefunction
from itertools import islice
from typing import Any

from fastapi import HTTPException

MAX_STORAGE_BYTES = 9_007_199_254_740_991
ACCOUNT_DATABASE = re.compile(r"AC-[A-Za-z0-9][A-Za-z0-9_-]{0,59}\Z")
CONTROL_DATABASES = frozenset(
    {
        "admin",
        "config",
        "local",
        "user-management",
        "infrastructure",
        "shared",
        "scheduler",
        "pulse",
        "marketplace",
        "maeyr_trace_control",
    }
)
MAX_WRITE_BATCH = 1000
MIN_GROWTH_RESERVATION = 4096
INDEX_GROWTH_FACTOR = 8
_READ_COMMANDS = frozenset(
    {
        "ping",
        "hello",
        "ismaster",
        "dbstats",
        "collstats",
        "count",
        "distinct",
        "find",
        "listcollections",
        "listindexes",
        "getmore",
        "killcursors",
    }
)
_COLLECTION_READS = frozenset(
    {
        "find",
        "find_one",
        "count_documents",
        "estimated_document_count",
        "distinct",
        "index_information",
        "list_indexes",
        "options",
        "watch",
        "find_raw_batches",
    }
)
_CLIENT_OPERATIONS = frozenset(
    {
        "close",
        "start_session",
        "list_database_names",
        "list_databases",
        "server_info",
    }
)
_COPY_PROPERTIES = frozenset(
    {
        "codec_options",
        "read_preference",
        "write_concern",
        "read_concern",
    }
)
_CLIENT_COPY_PROPERTIES = _COPY_PROPERTIES | {
    "address",
    "nodes",
    "is_primary",
    "is_mongos",
    "options",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MongoStorageCapacityError(HTTPException):
    """Finite public errors; never leak driver diagnostics or customer data."""

    detail: Any

    def __init__(self, code: str) -> None:
        status_code, message, retryable = {
            "mongodb_storage_limit_exceeded": (
                429,
                "The account database storage limit has been reached. "
                "Review storage and your plan.",
                False,
            ),
            "mongodb_storage_reservation_exceeds_headroom": (
                429,
                "This operation requires more database storage headroom for estimated growth. "
                "Review storage and the operation.",
                False,
            ),
            "mongodb_storage_observation_unavailable": (
                503,
                "Account database storage usage is being verified. Try again shortly.",
                True,
            ),
            "mongodb_storage_grant_unavailable": (
                409,
                "The account database storage allowance is not configured. "
                "Contact your administrator.",
                False,
            ),
            "mongodb_storage_accounting_unavailable": (
                503,
                "Database storage admission is unavailable. Try again shortly.",
                True,
            ),
            "mongodb_storage_operation_unsupported": (
                409,
                "This database operation requires a bounded, storage-aware maintenance operation.",
                False,
            ),
        }[code]
        super().__init__(
            status_code=status_code,
            detail={
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        )


def _integer(value: object, *, minimum: int = 0) -> int:
    # PyMongo decodes BSON long as Int64, an int subclass. This is native
    # storage typing, not string/float/legacy data coercion.
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_STORAGE_BYTES
    ):
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    return int(value)


def storage_admission_state(account: Mapping[str, Any] | None, now: datetime) -> tuple[int, int]:
    """Validate persisted authority. Missing, stale and inconsistent data deny."""
    limits = (account or {}).get("limits")
    limit = limits.get("max_mongodb_storage_bytes") if isinstance(limits, Mapping) else None
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_STORAGE_BYTES:
        raise MongoStorageCapacityError("mongodb_storage_grant_unavailable")
    observation = (account or {}).get("mongodb_storage")
    if not isinstance(observation, Mapping):
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    observed_at = observation.get("observed_at")
    age = observation.get("max_age_seconds")
    if (
        not isinstance(observed_at, datetime)
        or not isinstance(age, int)
        or isinstance(age, bool)
        or not 5 <= age <= 300
    ):
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    observed_at = (
        observed_at.replace(tzinfo=None)
        if observed_at.tzinfo is None
        else (observed_at.astimezone(timezone.utc).replace(tzinfo=None))
    )
    now = (
        now.replace(tzinfo=None)
        if now.tzinfo is None
        else (now.astimezone(timezone.utc).replace(tzinfo=None))
    )
    if observed_at > now or observed_at <= now - timedelta(seconds=age):
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    data = _integer(observation.get("data_size_bytes"))
    indexes = _integer(observation.get("index_size_bytes"))
    total = _integer(observation.get("total_bytes"))
    committed = _integer(observation.get("committed_growth_total"))
    baseline = _integer(observation.get("baseline_committed_growth"))
    pending = _integer(observation.get("pending_growth_bytes"))
    _integer(observation.get("active_operations"))
    if data + indexes != total or baseline > committed:
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    admitted = total + committed - baseline + pending
    if admitted > MAX_STORAGE_BYTES:
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
    return limit, admitted


def _control(client: Any) -> Any:
    from pymongo import ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern

    raw = client._delegate if isinstance(client, GuardedMongoClient) else client
    return raw["user-management"]["accounts"].with_options(
        read_preference=ReadPreference.PRIMARY,
        read_concern=ReadConcern("majority"),
        write_concern=WriteConcern("majority", wtimeout=5000),
    )


def _driver_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap sessions only at the trusted driver boundary, never to callers."""
    result = dict(options)
    session = result.get("session")
    if isinstance(session, GuardedMongoSession):
        result["session"] = session._delegate
    return result


def _driver_args(args: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(item._delegate if isinstance(item, GuardedMongoSession) else item for item in args)


async def _await_result(result: Any) -> Any:
    return await result if isawaitable(result) else result


def _require_acknowledged_write(handle: Any, options: Mapping[str, Any]) -> None:
    # Unacknowledged writes can land after dbStats reconciles their charge.
    if getattr(getattr(handle, "write_concern", None), "acknowledged", None) is not True:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    concern = options.get("write_concern")
    if concern is not None and getattr(concern, "acknowledged", None) is not True:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    raw_concern = options.get("writeConcern")
    if raw_concern is not None:
        from pymongo.write_concern import WriteConcern

        try:
            if (
                not isinstance(raw_concern, Mapping)
                or not WriteConcern(**dict(raw_concern)).acknowledged
            ):
                raise ValueError("unacknowledged write")
        except Exception as error:
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported") from error


async def assert_mongodb_storage_available(client: Any, database_name: str) -> None:
    """Read-only maintenance preflight; never creates a grant or observation."""
    if database_name in CONTROL_DATABASES:
        return
    if not ACCOUNT_DATABASE.fullmatch(database_name):
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    try:
        row = await _control(client).find_one(
            {"_id": database_name},
            {
                "limits.max_mongodb_storage_bytes": 1,
                "mongodb_storage": 1,
            },
        )
    except Exception as error:
        raise MongoStorageCapacityError("mongodb_storage_accounting_unavailable") from error
    limit, admitted = storage_admission_state(row, utcnow())
    if admitted >= limit:
        raise MongoStorageCapacityError("mongodb_storage_limit_exceeded")


def _bson_size(document: Mapping[str, Any]) -> int:
    # Mongo remains an optional SDK dependency; resolve only on a Mongo write.
    from bson import BSON

    return len(BSON.encode(dict(document)))


def _bounded_items(values: Any, maximum: int) -> list[Any]:
    if isinstance(values, Sequence) and len(values) > maximum:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    items = list(islice(iter(values), maximum + 1))
    if not 1 <= len(items) <= maximum:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    return items


def _same_index_definition(found: Mapping[str, Any], definition: Mapping[str, Any]) -> bool:
    requested_keys = list(definition["key"].items())
    text_fields = {field for field, direction in requested_keys if direction == "text"}
    if text_fields:
        # MongoDB reports text indexes with internal _fts/_ftsx keys and a
        # weights map, not the original field key list supplied to IndexModel.
        # Compare native semantic definitions, never a fabricated data fallback.
        normalized_keys: list[tuple[str, Any]] = []
        for field, direction in requested_keys:
            if direction != "text":
                normalized_keys.append((field, direction))
            elif ("_fts", "text") not in normalized_keys:
                normalized_keys.extend([("_fts", "text"), ("_ftsx", 1)])
        weights = {field: 1 for field in text_fields}
        weights.update(definition.get("weights", {}))
        if found.get("weights") != weights:
            return False
        if any(
            found.get(key, default) != definition.get(key, default)
            for key, default in (("default_language", "english"), ("language_override", "language"))
        ):
            return False
        requested_keys = normalized_keys
    if list(found.get("key", [])) != requested_keys:
        return False
    if any(
        found.get(option, False) != definition.get(option, False)
        for option in ("unique", "sparse", "hidden")
    ):
        return False
    if any(
        found.get(option) != definition.get(option)
        for option in (
            "partialFilterExpression",
            "collation",
            "expireAfterSeconds",
            "wildcardProjection",
        )
    ):
        return False
    return all(
        found.get(option) == definition[option]
        for option in (
            "textIndexVersion",
            "2dsphereIndexVersion",
            "bits",
            "min",
            "max",
            "bucketSize",
        )
        if option in definition
    )


def estimate_write_growth(document: Mapping[str, Any], operations: int = 1) -> int:
    """Bound serialized input and reserve index headroom; never zero-charge a write."""
    if type(operations) is not int or not 1 <= operations <= MAX_WRITE_BATCH:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    try:
        size = _bson_size(document)
    except Exception as error:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported") from error
    if size > 16 * 1024 * 1024:
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    return max(MIN_GROWTH_RESERVATION, size * INDEX_GROWTH_FACTOR) * operations


def _read_pipeline(pipeline: object) -> bool:
    if isinstance(pipeline, Mapping):
        return not any(
            key in {"$out", "$merge"} or not _read_pipeline(value)
            for key, value in pipeline.items()
        )
    if isinstance(pipeline, (list, tuple)):
        return all(_read_pipeline(value) for value in pipeline)
    return True


def _bounded_condition(value: object) -> bool:
    """Read-only comparisons used by atomic checkpoint updates; no expansion."""
    if isinstance(value, Mapping):
        return all(
            (
                not str(key).startswith("$")
                or key
                in {
                    "$literal",
                    "$eq",
                    "$ne",
                    "$gt",
                    "$gte",
                    "$lt",
                    "$lte",
                    "$and",
                    "$or",
                    "$not",
                    "$ifNull",
                    "$cond",
                    "$switch",
                }
            )
            and (key == "$literal" or _bounded_condition(item))
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_bounded_condition(item) for item in value)
    return not isinstance(value, str) or not value.startswith("$$")


def _bounded_assignment(value: object, field: str) -> bool:
    if isinstance(value, str):
        return not value.startswith("$") or value in {f"${field}", "$$REMOVE"}
    if isinstance(value, (list, tuple)):
        # Returning a repeated old field inside an array/object could amplify
        # existing data without corresponding serialized input. Require $literal.
        return _constant_assignment(value)
    if not isinstance(value, Mapping):
        return True
    if not any(str(key).startswith("$") for key in value):
        return _constant_assignment(value)
    if len(value) != 1:
        return False
    operator, argument = next(iter(value.items()))
    if operator == "$literal":
        return True
    if operator == "$ifNull":
        return (
            isinstance(argument, (list, tuple))
            and len(argument) >= 2
            and all(_bounded_assignment(item, field) for item in argument)
        )
    if operator == "$cond":
        parts = (
            [argument["if"], argument["then"], argument["else"]]
            if isinstance(argument, Mapping) and set(argument) == {"if", "then", "else"}
            else argument
        )
        return (
            isinstance(parts, (list, tuple))
            and len(parts) == 3
            and (
                _bounded_condition(parts[0])
                and _bounded_assignment(parts[1], field)
                and _bounded_assignment(parts[2], field)
            )
        )
    if operator == "$switch" and isinstance(argument, Mapping):
        branches = argument.get("branches")
        return (
            isinstance(branches, (list, tuple))
            and bool(branches)
            and all(
                isinstance(branch, Mapping)
                and set(branch) == {"case", "then"}
                and _bounded_condition(branch["case"])
                and _bounded_assignment(branch["then"], field)
                for branch in branches
            )
            and "default" in argument
            and _bounded_assignment(argument["default"], field)
        )
    # No server-side Javascript, concatenation, copying another document field,
    # array expansion or executable data-dependent growth in a write pipeline.
    return False


def _constant_assignment(value: object) -> bool:
    if isinstance(value, str):
        return not value.startswith("$")
    if isinstance(value, Mapping):
        return all(
            not str(key).startswith("$") and _constant_assignment(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_constant_assignment(item) for item in value)
    return True


def _update_document(update: object) -> Mapping[str, Any]:
    if isinstance(update, Mapping):
        supported = {
            "$set",
            "$setOnInsert",
            "$unset",
            "$inc",
            "$mul",
            "$min",
            "$max",
            "$currentDate",
            "$push",
            "$addToSet",
            "$pop",
            "$pull",
            "$pullAll",
            "$bit",
        }
        if not update or not all(
            operator in supported
            and isinstance(fields, Mapping)
            and all(
                isinstance(field, str)
                and "$[" not in field
                and not any(part.isdecimal() for part in field.split(".")[1:])
                for field in fields
            )
            for operator, fields in update.items()
        ):
            # Positional-all/filtered updates can apply one small literal to
            # arbitrarily many existing elements. Cardinality must be proved
            # by a separate bounded maintenance operation, never guessed here.
            # Structural $rename also needs explicit index-growth preparation.
            # Numeric dotted array indexes can pad arbitrarily many null slots.
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return update
    if isinstance(update, (list, tuple)) and 1 <= len(update) <= 100:
        for stage in update:
            if not isinstance(stage, Mapping) or len(stage) != 1 or "$set" not in stage:
                raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
            fields = stage["$set"]
            if not isinstance(fields, Mapping) or not all(
                isinstance(field, str)
                and bool(field)
                and "." not in field
                and not field.startswith("$")
                and _bounded_assignment(value, field)
                for field, value in fields.items()
            ):
                raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return {"pipeline": update}
    raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")


class StorageWriteGuard:
    def __init__(self, client: Any, account_id: str) -> None:
        self._client = client
        self._accounts = _control(client)
        self.account_id = account_id

    async def reserve(
        self, growth: int, *, session: Any = None, refresh_stale: bool = True
    ) -> None:
        _integer(growth)
        now = utcnow()
        args = _driver_options({"session": session}) if session is not None else {}
        state = "mongodb_storage."
        used = {
            "$add": [
                "$mongodb_storage.total_bytes",
                {
                    "$subtract": [
                        "$mongodb_storage.committed_growth_total",
                        "$mongodb_storage.baseline_committed_growth",
                    ]
                },
                "$mongodb_storage.pending_growth_bytes",
            ]
        }
        selector: dict[str, Any] = {
            "_id": self.account_id,
            "limits.max_mongodb_storage_bytes": {
                "$type": ["int", "long"],
                "$gte": 0,
                "$lte": MAX_STORAGE_BYTES,
            },
            state + "max_age_seconds": {"$type": ["int", "long"], "$gte": 5, "$lte": 300},
            state + "observed_at": {"$type": "date", "$lte": now},
            "$expr": {
                "$and": [
                    {"$eq": [{"$type": "$limits"}, "object"]},
                    {"$eq": [{"$type": "$mongodb_storage"}, "object"]},
                    {
                        "$in": [
                            {"$type": "$limits.max_mongodb_storage_bytes"},
                            ["int", "long"],
                        ]
                    },
                    {
                        "$in": [
                            {"$type": "$mongodb_storage.max_age_seconds"},
                            ["int", "long"],
                        ]
                    },
                    {"$eq": [{"$type": "$mongodb_storage.observed_at"}, "date"]},
                    {
                        "$gt": [
                            "$mongodb_storage.observed_at",
                            {
                                "$subtract": [
                                    now,
                                    {"$multiply": ["$mongodb_storage.max_age_seconds", 1000]},
                                ]
                            },
                        ]
                    },
                    {
                        "$eq": [
                            "$mongodb_storage.total_bytes",
                            {
                                "$add": [
                                    "$mongodb_storage.data_size_bytes",
                                    "$mongodb_storage.index_size_bytes",
                                ]
                            },
                        ]
                    },
                    {"$gte": ["$mongodb_storage.baseline_committed_growth", 0]},
                    {
                        "$gte": [
                            "$mongodb_storage.committed_growth_total",
                            "$mongodb_storage.baseline_committed_growth",
                        ]
                    },
                    {"$gte": ["$mongodb_storage.pending_growth_bytes", 0]},
                    {"$gte": ["$mongodb_storage.active_operations", 0]},
                    {"$lt": [used, "$limits.max_mongodb_storage_bytes"]},
                    {"$lte": [{"$add": [used, growth]}, "$limits.max_mongodb_storage_bytes"]},
                    {
                        "$lte": [
                            {"$add": ["$mongodb_storage.committed_growth_total", growth]},
                            MAX_STORAGE_BYTES,
                        ]
                    },
                    {"$lt": ["$mongodb_storage.active_operations", MAX_STORAGE_BYTES]},
                ]
            },
        }
        for field in (
            "data_size_bytes",
            "index_size_bytes",
            "total_bytes",
            "committed_growth_total",
            "baseline_committed_growth",
            "pending_growth_bytes",
            "active_operations",
        ):
            # Query $type predicates match array elements. Admission authority
            # must check the field itself in the same atomic reservation so a
            # malformed array grant cannot win BSON type-order comparisons.
            selector["$expr"]["$and"].append(
                {"$in": [{"$type": "$" + state + field}, ["int", "long"]]}
            )
            selector[state + field] = {
                "$type": ["int", "long"],
                "$gte": 0,
                "$lte": MAX_STORAGE_BYTES,
            }
        try:
            row = await self._accounts.find_one_and_update(
                selector,
                {
                    "$inc": {
                        state + "pending_growth_bytes": growth,
                        state + "active_operations": 1,
                    }
                },
                projection={"_id": 1},
                **args,
            )
        except Exception as error:
            raise MongoStorageCapacityError("mongodb_storage_accounting_unavailable") from error
        if row is not None:
            return
        try:
            await self._accounts.update_one(
                {"_id": self.account_id},
                {
                    "$min": {
                        state + "next_observation_at": now,
                    }
                },
                **args,
            )
            row = await self._accounts.find_one(
                {"_id": self.account_id},
                {
                    "limits.max_mongodb_storage_bytes": 1,
                    "mongodb_storage": 1,
                },
                **args,
            )
        except Exception as error:
            raise MongoStorageCapacityError("mongodb_storage_accounting_unavailable") from error
        try:
            limit, used_bytes = storage_admission_state(row, now)
        except MongoStorageCapacityError as error:
            if (
                error.detail["code"] == "mongodb_storage_observation_unavailable"
                and refresh_stale
                and session is None
            ):
                # Dormant accounts need a fresh observation on their first
                # write. Bounded cold path only; active writes remain atomic
                # reservations without dbStats or an Auth service request.
                from maeyr_platform.mongodb_storage_observer import refresh_account_storage

                outcome = await refresh_account_storage(self._client, self.account_id)
                if outcome == "observed":
                    return await self.reserve(growth, refresh_stale=False)
            raise
        if used_bytes >= limit:
            raise MongoStorageCapacityError("mongodb_storage_limit_exceeded")
        if used_bytes + growth > limit:
            raise MongoStorageCapacityError("mongodb_storage_reservation_exceeds_headroom")
        # Invalid Mongo numeric types or a concurrent policy/snapshot edit must
        # never degrade to a permissive fallback.
        raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")

    async def settle(self, growth: int, *, session: Any = None) -> None:
        args = _driver_options({"session": session}) if session is not None else {}
        state = "mongodb_storage."
        try:
            result = await self._accounts.update_one(
                {
                    "_id": self.account_id,
                    state + "pending_growth_bytes": {"$gte": growth},
                    state + "active_operations": {"$gte": 1},
                },
                {
                    "$inc": {
                        state + "pending_growth_bytes": -growth,
                        state + "active_operations": -1,
                        state + "committed_growth_total": growth,
                    },
                    "$min": {state + "next_observation_at": utcnow()},
                },
                **args,
            )
            if result.matched_count != 1:
                raise MongoStorageCapacityError("mongodb_storage_accounting_unavailable")
        except MongoStorageCapacityError:
            raise
        except Exception as error:
            raise MongoStorageCapacityError("mongodb_storage_accounting_unavailable") from error

    async def execute(
        self, call: Any, args: tuple[Any, ...], kwargs: dict[str, Any], growth: int
    ) -> Any:
        session = kwargs.get("session")
        await self.reserve(growth, session=session)
        try:
            result = await call(*_driver_args(args), **_driver_options(kwargs))
        except BaseException:
            # A network timeout/cancellation can follow a committed write. Keep
            # the charge, never assume failure means nothing reached MongoDB.
            try:
                await self.settle(growth, session=session)
            except BaseException:
                pass  # Unsettled pending charge remains fail-closed.
            raise
        await self.settle(growth, session=session)
        return result


class GuardedMongoSession:
    """Motor session API whose public client remains under write admission."""

    def __init__(self, client: GuardedMongoClient, delegate: Any) -> None:
        self.client = client
        self._delegate = delegate

    async def __aenter__(self) -> GuardedMongoSession:
        await _await_result(self._delegate.__aenter__())
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await _await_result(self._delegate.__aexit__(*args))

    def start_transaction(self, *args: Any, **kwargs: Any) -> GuardedMongoTransaction:
        return GuardedMongoTransaction(self._delegate.start_transaction(*args, **kwargs))

    async def with_transaction(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        # The driver's retry/commit algorithm stays authoritative, but every
        # callback receives the guarded session rather than the raw parent.
        if not iscoroutinefunction(self._delegate.with_transaction):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")

        async def guarded_callback(_session: Any) -> Any:
            return await callback(self)

        return await self._delegate.with_transaction(guarded_callback, *args, **kwargs)

    async def commit_transaction(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_result(self._delegate.commit_transaction(*args, **kwargs))

    async def abort_transaction(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_result(self._delegate.abort_transaction(*args, **kwargs))

    async def end_session(self, *args: Any, **kwargs: Any) -> Any:
        return await _await_result(self._delegate.end_session(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        if name in {"advance_cluster_time", "advance_operation_time"}:
            return lambda *args, **kwargs: getattr(self._delegate, name)(*args, **kwargs)
        if name in {
            "cluster_time",
            "has_ended",
            "in_transaction",
            "options",
            "operation_time",
            "session_id",
        }:
            return getattr(self._delegate, name)
        raise AttributeError(name)


class GuardedMongoTransaction:
    """Transaction context never returns the driver's raw context object."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def __aenter__(self) -> GuardedMongoTransaction:
        await _await_result(self._delegate.__aenter__())
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await _await_result(self._delegate.__aexit__(*args))


class GuardedMongoCursor:
    """Read cursor with explicit APIs and guarded collection/session parents."""

    def __init__(self, collection: Any, delegate: Any) -> None:
        self.collection = collection
        self._delegate = delegate

    @property
    def session(self) -> GuardedMongoSession | None:
        session = self._delegate.session
        if session is None:
            return None
        client = (
            self.collection
            if isinstance(self.collection, GuardedMongoClient)
            else self.collection.database.client
        )
        return GuardedMongoSession(client, session)

    def __aiter__(self) -> GuardedMongoCursor:
        return self

    async def __anext__(self) -> Any:
        return await self._delegate.__anext__()

    async def __aenter__(self) -> GuardedMongoCursor:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self._delegate.__aexit__(*args)

    async def next(self) -> Any:
        return await self._delegate.next()

    async def try_next(self) -> Any:
        return await self._delegate.try_next()

    async def close(self) -> Any:
        return await self._delegate.close()

    async def to_list(self, *args: Any, **kwargs: Any) -> Any:
        return await self._delegate.to_list(*args, **kwargs)

    def __copy__(self) -> GuardedMongoCursor:
        return GuardedMongoCursor(self.collection, copy(self._delegate))

    def __deepcopy__(self, memo: dict[int, Any]) -> GuardedMongoCursor:
        return GuardedMongoCursor(self.collection, deepcopy(self._delegate, memo))

    def __getattr__(self, name: str) -> Any:
        if name in {
            "sort",
            "limit",
            "skip",
            "hint",
            "where",
            "collation",
            "rewind",
            "batch_size",
            "add_option",
            "remove_option",
            "max_scan",
            "max_await_time_ms",
            "max_time_ms",
            "min",
            "max",
            "comment",
            "allow_disk_use",
            "clone",
        }:

            def chain(*args: Any, **kwargs: Any) -> GuardedMongoCursor:
                cursor = getattr(self._delegate, name)(*args, **kwargs)
                return (
                    self
                    if cursor is self._delegate
                    else GuardedMongoCursor(self.collection, cursor)
                )

            return chain
        if name in {"next_object", "each", "distinct", "explain"}:
            return lambda *args, **kwargs: getattr(self._delegate, name)(*args, **kwargs)
        if name in {
            "alive",
            "address",
            "cursor_id",
            "closed",
            "started",
            "retrieved",
            "fetch_next",
        }:
            return getattr(self._delegate, name)
        raise AttributeError(name)


class GuardedMongoChangeStream:
    """Change stream exposes documents and lifecycle APIs, never raw handles."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __aiter__(self) -> GuardedMongoChangeStream:
        return self

    async def __anext__(self) -> Any:
        return await self._delegate.__anext__()

    async def __aenter__(self) -> GuardedMongoChangeStream:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self._delegate.__aexit__(*args)

    async def next(self) -> Any:
        return await self._delegate.next()

    async def try_next(self) -> Any:
        return await self._delegate.try_next()

    async def close(self) -> Any:
        return await self._delegate.close()

    def __getattr__(self, name: str) -> Any:
        if name in {"alive", "resume_token"}:
            return getattr(self._delegate, name)
        raise AttributeError(name)


def _guarded_watch(
    delegate: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> GuardedMongoChangeStream:
    pipeline = kwargs.get("pipeline", args[0] if args else None)
    if not _read_pipeline(pipeline):
        raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
    return GuardedMongoChangeStream(delegate.watch(*_driver_args(args), **_driver_options(kwargs)))


class GuardedMongoClient:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getitem__(self, name: str) -> Any:
        return self.get_database(name)

    def get_database(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return GuardedMongoDatabase(self, self._delegate.get_database(name, *args, **kwargs))

    def get_default_database(self, *args: Any, **kwargs: Any) -> Any:
        return GuardedMongoDatabase(self, self._delegate.get_default_database(*args, **kwargs))

    async def start_session(self, *args: Any, **kwargs: Any) -> GuardedMongoSession:
        return GuardedMongoSession(self, await self._delegate.start_session(*args, **kwargs))

    async def list_databases(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        cursor = await self._delegate.list_databases(*_driver_args(args), **_driver_options(kwargs))
        return GuardedMongoCursor(self, cursor)

    def watch(self, *args: Any, **kwargs: Any) -> GuardedMongoChangeStream:
        return _guarded_watch(self._delegate, args, kwargs)

    async def drop_database(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        name = name.name if isinstance(name, GuardedMongoDatabase) else name
        db = self.get_database(name)
        return await db._write(self._delegate.drop_database, (name, *args), kwargs, 0)

    def __getattr__(self, name: str) -> Any:
        if name in _CLIENT_OPERATIONS:
            return lambda *args, **kwargs: getattr(self._delegate, name)(
                *_driver_args(args), **_driver_options(kwargs)
            )
        if name in _CLIENT_COPY_PROPERTIES:
            return getattr(self._delegate, name)
        if name == "admin":
            return self.get_database("admin")
        if not name.startswith("_"):
            return self.get_database(name)
        raise AttributeError(name)


class GuardedMongoDatabase:
    def __init__(self, client: GuardedMongoClient, delegate: Any) -> None:
        self.client = client
        self._delegate = delegate
        self.name: str = delegate.name

    def __getitem__(self, name: str) -> Any:
        return self.get_collection(name)

    def get_collection(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return GuardedMongoCollection(self, self._delegate.get_collection(name, *args, **kwargs))

    def with_options(self, *args: Any, **kwargs: Any) -> Any:
        return GuardedMongoDatabase(self.client, self._delegate.with_options(*args, **kwargs))

    async def _write(
        self, call: Any, args: tuple[Any, ...], kwargs: dict[str, Any], growth: int
    ) -> Any:
        if self.name in CONTROL_DATABASES:
            return await call(*_driver_args(args), **_driver_options(kwargs))
        if not ACCOUNT_DATABASE.fullmatch(self.name):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        _require_acknowledged_write(self._delegate, kwargs)
        return await StorageWriteGuard(self.client, self.name).execute(call, args, kwargs, growth)

    async def command(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        # PyMongo merges keyword command fields AFTER the input mapping. Check
        # that effective document, so a kwargs pipeline cannot conceal $out.
        effective = (
            dict(command)
            if isinstance(command, Mapping)
            else {
                command: args[0] if args else 1,
            }
        )
        effective.update(
            {
                key: value
                for key, value in kwargs.items()
                if key
                not in {
                    "session",
                    "codec_options",
                    "check",
                    "allowable_errors",
                    "read_preference",
                }
            }
        )
        verb = next(iter(effective), "")
        if any(
            key in effective
            for key in {
                "insert",
                "update",
                "delete",
                "findAndModify",
                "bulkWrite",
                "applyOps",
                "renameCollection",
                "mapReduce",
                "mapreduce",
            }
        ):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        if str(verb).lower() == "aggregate":
            pipeline = effective.get("pipeline", [])
            if not _read_pipeline(pipeline):
                raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        elif str(verb).lower() not in _READ_COMMANDS and not (
            self.name in CONTROL_DATABASES
            and str(verb).lower()
            in {
                "serverstatus",
                "connectionstatus",
                "buildinfo",
                "getparameter",
                "createindexes",
                "dropindexes",
                "create",
                "drop",
                "collmod",
            }
        ):
            # Raw write commands, applyOps, $out/$merge and mapReduce would
            # otherwise bypass the collection boundary.
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return await self._delegate.command(command, *_driver_args(args), **_driver_options(kwargs))

    async def list_collections(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        cursor = await self._delegate.list_collections(
            *_driver_args(args), **_driver_options(kwargs)
        )
        return GuardedMongoCursor(self.get_collection("$cmd"), cursor)

    def aggregate(self, pipeline: Sequence[Any], *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        if not _read_pipeline(pipeline):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return GuardedMongoCursor(
            self.get_collection("$cmd.aggregate"),
            self._delegate.aggregate(pipeline, *_driver_args(args), **_driver_options(kwargs)),
        )

    def watch(self, *args: Any, **kwargs: Any) -> GuardedMongoChangeStream:
        return _guarded_watch(self._delegate, args, kwargs)

    async def create_collection(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.name not in CONTROL_DATABASES and len(args) > 2:
            _require_acknowledged_write(self._delegate, {"write_concern": args[2], **kwargs})
        collection = await self._write(
            self._delegate.create_collection, (name, *args), kwargs, MIN_GROWTH_RESERVATION
        )
        return GuardedMongoCollection(self, collection)

    async def drop_collection(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        name = name.name if isinstance(name, GuardedMongoCollection) else name
        return await self._write(self._delegate.drop_collection, (name, *args), kwargs, 0)

    def __getattr__(self, name: str) -> Any:
        if name == "list_collection_names":
            return lambda *args, **kwargs: self._delegate.list_collection_names(
                *_driver_args(args), **_driver_options(kwargs)
            )
        if name in _COPY_PROPERTIES or name in {
            "list_collection_names",
            "list_collections",
            "watch",
        }:
            return getattr(self._delegate, name)
        if not name.startswith("_"):
            return self.get_collection(name)
        raise AttributeError(name)


class GuardedMongoCollection:
    def __init__(self, database: GuardedMongoDatabase, delegate: Any) -> None:
        self.database = database
        self._delegate = delegate
        self.name: str = delegate.name
        self.full_name: str = delegate.full_name

    def __getitem__(self, name: str) -> Any:
        return self.database.get_collection(f"{self.name}.{name}")

    def with_options(self, *args: Any, **kwargs: Any) -> Any:
        return GuardedMongoCollection(self.database, self._delegate.with_options(*args, **kwargs))

    def aggregate(self, pipeline: Sequence[Any], *args: Any, **kwargs: Any) -> Any:
        if not _read_pipeline(pipeline):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return GuardedMongoCursor(
            self, self._delegate.aggregate(pipeline, *_driver_args(args), **_driver_options(kwargs))
        )

    def aggregate_raw_batches(self, pipeline: Sequence[Any], *args: Any, **kwargs: Any) -> Any:
        if not _read_pipeline(pipeline):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return GuardedMongoCursor(
            self,
            self._delegate.aggregate_raw_batches(
                pipeline, *_driver_args(args), **_driver_options(kwargs)
            ),
        )

    def find(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        return GuardedMongoCursor(
            self, self._delegate.find(*_driver_args(args), **_driver_options(kwargs))
        )

    def find_raw_batches(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        return GuardedMongoCursor(
            self, self._delegate.find_raw_batches(*_driver_args(args), **_driver_options(kwargs))
        )

    def list_indexes(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        cursor = self._delegate.list_indexes(*_driver_args(args), **_driver_options(kwargs))
        return GuardedMongoCursor(self, cursor)

    def list_search_indexes(self, *args: Any, **kwargs: Any) -> GuardedMongoCursor:
        cursor = self._delegate.list_search_indexes(*_driver_args(args), **_driver_options(kwargs))
        return GuardedMongoCursor(self, cursor)

    def watch(self, *args: Any, **kwargs: Any) -> GuardedMongoChangeStream:
        return _guarded_watch(self._delegate, args, kwargs)

    async def _mutation(
        self,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        document: Mapping[str, Any] | None = None,
        count: int = 1,
    ) -> Any:
        if self.database.name in CONTROL_DATABASES:
            return await getattr(self._delegate, method)(
                *_driver_args(args), **_driver_options(kwargs)
            )
        _require_acknowledged_write(self._delegate, kwargs)
        growth = estimate_write_growth(document, count) if document is not None else 0
        return await self.database._write(getattr(self._delegate, method), args, kwargs, growth)

    async def insert_one(self, document: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("insert_one", (document, *args), kwargs, document)

    async def insert_many(self, documents: Any, *args: Any, **kwargs: Any) -> Any:
        if self.database.name in CONTROL_DATABASES:
            return await self._delegate.insert_many(
                documents, *_driver_args(args), **_driver_options(kwargs)
            )
        _require_acknowledged_write(self._delegate, kwargs)
        documents = _bounded_items(documents, MAX_WRITE_BATCH)
        growth = sum(estimate_write_growth(doc) for doc in documents)
        return await self.database._write(
            self._delegate.insert_many, (documents, *args), kwargs, growth
        )

    async def update_one(self, filter: Any, update: Any, *args: Any, **kwargs: Any) -> Any:
        document = update if self.database.name in CONTROL_DATABASES else _update_document(update)
        if self.database.name not in CONTROL_DATABASES:
            document = {"filter": filter, "update": document}
        return await self._mutation("update_one", (filter, update, *args), kwargs, document)

    async def replace_one(self, filter: Any, replacement: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation(
            "replace_one",
            (filter, replacement, *args),
            kwargs,
            {"filter": filter, "replacement": replacement},
        )

    async def find_one_and_update(self, filter: Any, update: Any, *args: Any, **kwargs: Any) -> Any:
        document = update if self.database.name in CONTROL_DATABASES else _update_document(update)
        if self.database.name not in CONTROL_DATABASES:
            document = {"filter": filter, "update": document}
        return await self._mutation(
            "find_one_and_update", (filter, update, *args), kwargs, document
        )

    async def find_one_and_replace(
        self, filter: Any, replacement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        return await self._mutation(
            "find_one_and_replace",
            (filter, replacement, *args),
            kwargs,
            {"filter": filter, "replacement": replacement},
        )

    async def delete_one(self, filter: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("delete_one", (filter, *args), kwargs)

    async def delete_many(self, filter: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("delete_many", (filter, *args), kwargs)

    async def find_one_and_delete(self, filter: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("find_one_and_delete", (filter, *args), kwargs)

    async def update_many(self, filter: Any, update: Any, *args: Any, **kwargs: Any) -> Any:
        if self.database.name in CONTROL_DATABASES:
            return await self._delegate.update_many(
                filter, update, *_driver_args(args), **_driver_options(kwargs)
            )
        _require_acknowledged_write(self._delegate, kwargs)
        # Bound matched IDs, never estimate a multi-update as one document. The
        # final predicate is narrowed to that proved set so concurrent inserts
        # cannot expand the write behind the reservation.
        document = _update_document(update)
        if kwargs.get("upsert") or args:
            document = {"filter": filter, "update": document}
        query_options: dict[str, Any] = {
            key: kwargs[key] for key in ("session", "collation", "hint") if key in kwargs
        }
        rows = (
            await self._delegate.find(filter, {"_id": 1}, **_driver_options(query_options))
            .limit(MAX_WRITE_BATCH + 1)
            .to_list(length=MAX_WRITE_BATCH + 1)
        )
        if len(rows) > MAX_WRITE_BATCH:
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        if not rows:
            if kwargs.get("upsert"):
                return await self._mutation(
                    "update_many", (filter, update, *args), kwargs, document
                )
            # Still gate zero-match modifications at the limit.
            narrowed = {"$and": [filter, {"_id": {"$in": []}}]}
            return await self._mutation("update_many", (narrowed, update, *args), kwargs, document)
        if kwargs.get("upsert"):
            # If all matched rows disappear, the narrowed predicate is not a
            # safe upsert identity. Require explicit bounded UpdateOne instead.
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        narrowed = {"$and": [filter, {"_id": {"$in": [row["_id"] for row in rows]}}]}
        return await self._mutation(
            "update_many", (narrowed, update, *args), kwargs, document, len(rows)
        )

    async def bulk_write(self, requests: Any, *args: Any, **kwargs: Any) -> Any:
        from pymongo import DeleteOne, InsertOne, ReplaceOne, UpdateOne

        if self.database.name in CONTROL_DATABASES:
            return await self._delegate.bulk_write(
                requests, *_driver_args(args), **_driver_options(kwargs)
            )
        _require_acknowledged_write(self._delegate, kwargs)
        requests = _bounded_items(requests, MAX_WRITE_BATCH)
        growth = 0
        for request in requests:
            if isinstance(request, UpdateOne):
                growth += estimate_write_growth(
                    {"filter": request._filter, "update": _update_document(request._doc)}
                )
            elif isinstance(request, ReplaceOne):
                growth += estimate_write_growth(
                    {"filter": request._filter, "replacement": request._doc}
                )
            elif isinstance(request, InsertOne):
                growth += estimate_write_growth(request._doc)
            elif not isinstance(request, DeleteOne):
                raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return await self.database._write(
            self._delegate.bulk_write, (requests, *args), kwargs, growth
        )

    async def create_indexes(self, indexes: Any, *args: Any, **kwargs: Any) -> Any:
        indexes = _bounded_items(indexes, 64)
        # Index allocations cannot be predicted exactly. Reserve a deliberately
        # conservative budget from metadata, build away from request traffic,
        # then request immediate authoritative reconciliation.
        if self.database.name in CONTROL_DATABASES:
            return await self._delegate.create_indexes(
                indexes, *_driver_args(args), **_driver_options(kwargs)
            )
        _require_acknowledged_write(self._delegate, kwargs)
        from maeyr_platform.mongodb_indexes import mongodb_index_information

        try:
            existing = await mongodb_index_information(self._delegate)
        except (ValueError, TimeoutError) as error:
            raise MongoStorageCapacityError("mongodb_storage_observation_unavailable") from error
        new_indexes = []
        for model in indexes:
            definition = model.document
            if not 1 <= len(definition.get("key", {})) <= 32:
                raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
            found = existing.get(definition["name"])
            if found is not None:
                if "buildUUID" in found:
                    raise MongoStorageCapacityError("mongodb_storage_observation_unavailable")
                if not _same_index_definition(found, definition):
                    raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
            else:
                new_indexes.append(model)
        if not new_indexes:
            return [model.document["name"] for model in indexes]
        if max(1, len(existing)) + len(new_indexes) > 64:
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        from pymongo.errors import OperationFailure

        try:
            stats = await self.database.command({"collStats": self.name, "scale": 1})
        except OperationFailure as error:
            if error.code != 26:
                raise
            stats = {"size": 0}  # Verified NamespaceNotFound, not an unknown measurement.
        from maeyr_platform.mongodb_storage_observer import normalize_mongodb_byte_measurement

        try:
            size = normalize_mongodb_byte_measurement(stats.get("size"))
        except ValueError as error:
            raise MongoStorageCapacityError("mongodb_storage_observation_unavailable") from error
        growth = max(262_144, size * INDEX_GROWTH_FACTOR) * len(new_indexes)
        if growth > MAX_STORAGE_BYTES:
            raise MongoStorageCapacityError("mongodb_storage_reservation_exceeds_headroom")
        await self.database._write(
            self._delegate.create_indexes, (new_indexes, *args), kwargs, growth
        )
        return [model.document["name"] for model in indexes]

    async def create_index(self, keys: Any, *args: Any, **kwargs: Any) -> Any:
        from pymongo import IndexModel

        if args:
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        command_args = {
            key: kwargs.pop(key) for key in ("session", "comment", "maxTimeMS") if key in kwargs
        }
        names = await self.create_indexes([IndexModel(keys, **kwargs)], **command_args)
        return names[0]

    async def drop(self, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("drop", args, kwargs)

    async def drop_index(self, index: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("drop_index", (index, *args), kwargs)

    async def drop_search_index(
        self, name: str, *args: Any, timeout_seconds: float = 10.0, **kwargs: Any
    ) -> Any:
        """Admit finite Atlas Search DDL through the ordinary account write guard."""
        if (
            not isinstance(name, str)
            or not name
            or type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= 60
        ):
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return await asyncio.wait_for(
            self._mutation("drop_search_index", (name, *args), kwargs),
            timeout=timeout_seconds,
        )

    async def drop_indexes(self, *args: Any, **kwargs: Any) -> Any:
        return await self._mutation("drop_indexes", args, kwargs)

    async def rename(self, name: str, *args: Any, **kwargs: Any) -> Any:
        # PyMongo merges kwargs into the command after setting source/target.
        # Admit only a rename within this account; namespace overrides could
        # move existing data into another account without its reservation.
        if "to" in kwargs or "renameCollection" in kwargs:
            raise MongoStorageCapacityError("mongodb_storage_operation_unsupported")
        return await self._mutation("rename", (name, *args), kwargs, {"name": name})

    def __getattr__(self, name: str) -> Any:
        if name in _COLLECTION_READS:
            return lambda *args, **kwargs: getattr(self._delegate, name)(
                *_driver_args(args), **_driver_options(kwargs)
            )
        if name in _COPY_PROPERTIES:
            return getattr(self._delegate, name)
        # A new driver API must be classified explicitly, never raw-delegated.
        raise AttributeError(name)


def guard_platform_mongo_client(client: Any) -> GuardedMongoClient:
    """Wrap only Maeyr's owned DB connection, never a customer's Volt connector."""
    return client if isinstance(client, GuardedMongoClient) else GuardedMongoClient(client)
