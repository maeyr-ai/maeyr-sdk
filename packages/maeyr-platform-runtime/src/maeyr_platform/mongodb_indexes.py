"""Bounded primary index metadata, including native unfinished build identity."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from bson.int64 import Int64
from pymongo.errors import OperationFailure
from pymongo.read_preferences import ReadPreference

MAX_NATIVE_INDEXES = 64


async def mongodb_index_information(
    collection: Any, *, max_time_ms: int = 5000
) -> dict[str, dict[str, Any]]:
    """Return driver-shaped definitions, retaining ``buildUUID`` for active builds.

    Ordinary ``index_information`` cannot prove usability: MongoDB advertises
    an index's specification before its build commits. The native UUID option
    distinguishes those entries without scanning documents or querying currentOp.
    MongoDB 7 supports this option; Stable API strict mode must permit it.
    """
    if type(max_time_ms) is not int or not 1 <= max_time_ms <= 30_000:
        raise ValueError("MongoDB index metadata timeout is invalid")
    database = collection.database.with_options(read_preference=ReadPreference.PRIMARY)
    name = collection.name
    namespace = f"{database.name}.{name}"
    result: dict[str, dict[str, Any]] = {}
    cursor_id = 0

    async def collect() -> dict[str, dict[str, Any]]:
        nonlocal cursor_id
        try:
            response = await database.command(
                {
                    "listIndexes": name,
                    "includeBuildUUIDs": True,
                    "cursor": {"batchSize": MAX_NATIVE_INDEXES},
                    "maxTimeMS": max_time_ms,
                }
            )
        except OperationFailure as error:
            if error.code == 26:
                return {}  # Native NamespaceNotFound, never an unknown metadata fallback.
            raise
        try:
            # Two wire batches bound round trips and memory. A response too large
            # to fit those bounds is denied rather than partially declared ready.
            for batch_number in range(2):
                cursor = response.get("cursor") if isinstance(response, Mapping) else None
                if not isinstance(cursor, Mapping) or cursor.get("ns") != namespace:
                    raise ValueError("MongoDB index metadata cursor is invalid")
                value = cursor.get("id")
                if type(value) not in {int, Int64}:
                    raise ValueError("MongoDB index metadata cursor identity is invalid")
                cursor_id = int(cast(int, value))
                if cursor_id < 0:
                    raise ValueError("MongoDB index metadata cursor identity is invalid")
                batch = cursor.get("firstBatch" if batch_number == 0 else "nextBatch")
                if not isinstance(batch, list) or len(batch) + len(result) > MAX_NATIVE_INDEXES:
                    raise ValueError("MongoDB index metadata exceeds its bounded collection limit")
                for entry in batch:
                    if not isinstance(entry, Mapping):
                        raise ValueError("MongoDB index metadata entry is invalid")
                    if "indexBuildInfo" in entry:
                        raise ValueError("MongoDB index metadata used an unexpected build format")
                    building = "buildUUID" in entry
                    specification = entry.get("spec") if building else entry
                    if not isinstance(specification, Mapping):
                        raise ValueError("MongoDB index metadata specification is invalid")
                    index_name, keys = specification.get("name"), specification.get("key")
                    if (
                        not isinstance(index_name, str)
                        or not index_name
                        or index_name in result
                        or not isinstance(keys, Mapping)
                        or not keys
                    ):
                        raise ValueError("MongoDB index metadata identity is invalid")
                    metadata = {
                        key: item
                        for key, item in specification.items()
                        if key not in {"name", "ns"}
                    }
                    metadata["key"] = list(keys.items())
                    if building:
                        metadata["buildUUID"] = entry["buildUUID"]
                    result[index_name] = metadata
                if not cursor_id:
                    return result
                if batch_number == 0:
                    response = await database.command(
                        {
                            "getMore": cursor_id,
                            "collection": name,
                            "batchSize": MAX_NATIVE_INDEXES,
                        }
                    )
            raise ValueError("MongoDB index metadata cursor exceeded its bounded batch limit")
        finally:
            if cursor_id:
                # Cancellation or malformed metadata must not strand an open
                # server cursor. Cleanup is best effort and cannot mask refusal.
                try:
                    await asyncio.wait_for(
                        database.command({"killCursors": name, "cursors": [cursor_id]}),
                        timeout=0.25,
                    )
                except Exception:
                    pass

    try:
        return await asyncio.wait_for(collect(), timeout=max_time_ms / 1000)
    except asyncio.TimeoutError as error:
        raise TimeoutError("MongoDB index metadata timed out") from error
