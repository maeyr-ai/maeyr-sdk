"""Single-document schema publication and explicit preparation drafts."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from maeyr_platform.directory.project_user_indexes import (
    SCHEMA_INDEX,
    ProfileIndex,
    ProjectUserIndexesUnavailable,
    ProjectUserIndexPreparationRequired,
    ProjectUserSchemaConflict,
)
from maeyr_platform.mongodb_indexes import mongodb_index_information

DRAFT_COLLECTION = "volt_project_user_schema_drafts"
DRAFT_ACCOUNT_INDEX = "volt_schema_drafts_account_status_idx"
SCHEMA_CONTRACT = ProfileIndex(SCHEMA_INDEX, [("org_id", 1), ("project_id", 1)], unique=True)


def draft_id(scope: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(scope[key] for key in ("account_id", "org_id", "project_id")).encode()
    ).hexdigest()


async def ensure_schema_index(
    collection: Any,
    *,
    allow_populated: bool = False,
    check_only: bool = False,
    before_mutation: Callable[[], Awaitable[None]] | None = None,
) -> None:
    indexes = await mongodb_index_information(collection)
    found = indexes.get(SCHEMA_INDEX)
    if found is not None:
        if not SCHEMA_CONTRACT.matches(found):
            raise ProjectUserIndexesUnavailable()
        return
    if check_only:
        raise ProjectUserIndexPreparationRequired()
    if (
        not allow_populated
        and await collection.find_one({}, {"_id": 1}, max_time_ms=1000) is not None
    ):
        raise ProjectUserIndexPreparationRequired()
    if before_mutation is not None:
        await before_mutation()
    await collection.create_index(
        SCHEMA_CONTRACT.keys,
        **SCHEMA_CONTRACT.options(),
        maxTimeMS=60_000 if allow_populated else 5000,
    )
    if not SCHEMA_CONTRACT.matches((await mongodb_index_information(collection)).get(SCHEMA_INDEX)):
        raise ProjectUserIndexesUnavailable()


async def publish_schema(
    collection: Any,
    scope: dict[str, str],
    document: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    """No lost updates: compare the exact observed Mongo document, not a clock."""
    if previous is not None:
        selector = {"_id": previous["_id"], "$expr": {"$eq": ["$$ROOT", {"$literal": previous}]}}
        result = await collection.update_one(selector, {"$set": document})
        if result.matched_count != 1:
            # A crash after publication is safely resumable without rewriting it.
            current = await collection.find_one(scope, max_time_ms=1000)
            if current != {**previous, **document}:
                raise ProjectUserSchemaConflict()
    else:
        try:
            await collection.insert_one({**document, "created_at": document["updated_at"]})
        except DuplicateKeyError as error:
            # Resume a confirmed identical publication after process failure or
            # a lost acknowledgment, without rewriting another user's update.
            current = await collection.find_one(scope, max_time_ms=1000)
            expected = {**document, "created_at": document["updated_at"]}
            if (
                current is None
                or {key: value for key, value in current.items() if key != "_id"} != expected
            ):
                raise ProjectUserSchemaConflict() from error


async def stage_schema(
    client: Any, scope: dict[str, str], document: dict[str, Any], previous: dict[str, Any] | None
) -> None:
    """Control-plane draft is never returned by get_schema or used by dispatch."""
    drafts = client["user-management"][DRAFT_COLLECTION]
    await drafts.create_index(
        [("account_id", 1), ("status", 1), ("_id", 1)], name=DRAFT_ACCOUNT_INDEX
    )
    identity = draft_id(scope)
    existing = await drafts.find_one({"_id": identity}, max_time_ms=1000)
    if existing is not None and existing.get("status") == "pending":
        # Do not change the source under an active release worker. The first
        # staged candidate remains reviewable until applied or explicitly replaced.
        if {
            key: value
            for key, value in (existing.get("document") or {}).items()
            if key != "updated_at"
        } != {key: value for key, value in document.items() if key != "updated_at"}:
            raise ProjectUserSchemaConflict()
        return
    selector: dict[str, Any] = {"_id": identity}
    if existing is not None:
        selector["$expr"] = {"$eq": ["$$ROOT", {"$literal": existing}]}
    try:
        result = await drafts.find_one_and_update(
            selector,
            {
                "$set": {
                    **scope,
                    "document": document,
                    "previous": previous,
                    "status": "pending",
                }
            },
            upsert=existing is None,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as error:
        raise ProjectUserSchemaConflict() from error
    if result is None:
        raise ProjectUserSchemaConflict()
