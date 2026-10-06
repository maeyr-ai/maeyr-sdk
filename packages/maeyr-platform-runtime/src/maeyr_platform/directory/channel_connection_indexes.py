"""Explicit application-owned channel connection constraint preparation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pymongo.errors import PyMongoError

from maeyr_platform.directory.project_user_indexes import (
    ProfileIndex,
    ProjectUserIndexesUnavailable,
    ProjectUserIndexPreparationRequired,
)
from maeyr_platform.mongodb_indexes import mongodb_index_information

CONNECTION_INDEX = ProfileIndex(
    "channel_connections_project_channel_connector_type_unique",
    [("org_id", 1), ("project_id", 1), ("channel", 1), ("connector_type", 1)],
    unique=True,
)
OLD_CONNECTION_INDEX = "channel_connections_project_channel_unique"
UNUSED_VERIFY_TOKEN_INDEX = "channel_connections_verify_token_idx"


def connection_index_work(observed: dict[str, dict[str, Any]]) -> tuple[bool, list[str]]:
    """Only exact prior application specifications can be retired."""
    found = observed.get(CONNECTION_INDEX.name)
    if found is not None and not CONNECTION_INDEX.matches(found):
        raise ProjectUserIndexesUnavailable()
    obsolete: list[str] = []
    previous = observed.get(OLD_CONNECTION_INDEX)
    if previous is not None:
        expected = ProfileIndex(
            OLD_CONNECTION_INDEX, [("org_id", 1), ("project_id", 1), ("channel", 1)], unique=True
        )
        if not expected.matches(previous):
            raise ProjectUserIndexPreparationRequired()
        obsolete.append(OLD_CONNECTION_INDEX)
    unused = observed.get(UNUSED_VERIFY_TOKEN_INDEX)
    if unused is not None:
        if (
            unused.get("key") != [("webhook_verify_token", 1)]
            or unused.get("unique", False) is not False
            or unused.get("sparse") is not True
            or unused.get("hidden", False) is not False
            or "buildUUID" in unused
            or "expireAfterSeconds" in unused
            or "partialFilterExpression" in unused
            or "collation" in unused
        ):
            raise ProjectUserIndexPreparationRequired()
        obsolete.append(UNUSED_VERIFY_TOKEN_INDEX)
    return found is None, obsolete


async def prepare_channel_connection_indexes(
    collection: Any,
    *,
    allow_populated: bool = False,
    retire_obsolete: bool = False,
    check_only: bool = False,
    before_mutation: Callable[[], Awaitable[None]] | None = None,
) -> None:
    try:
        await _prepare_channel_connection_indexes(
            collection,
            allow_populated=allow_populated,
            retire_obsolete=retire_obsolete,
            check_only=check_only,
            before_mutation=before_mutation,
        )
    except (ProjectUserIndexPreparationRequired, ProjectUserIndexesUnavailable):
        raise
    except (PyMongoError, ValueError, TimeoutError) as error:
        raise ProjectUserIndexesUnavailable() from error


async def _prepare_channel_connection_indexes(
    collection: Any,
    *,
    allow_populated: bool,
    retire_obsolete: bool,
    check_only: bool,
    before_mutation: Callable[[], Awaitable[None]] | None,
) -> None:
    observed = await mongodb_index_information(collection)
    missing, obsolete = connection_index_work(observed)
    if check_only and (missing or obsolete):
        raise ProjectUserIndexPreparationRequired()
    if OLD_CONNECTION_INDEX in obsolete and not retire_obsolete:
        raise ProjectUserIndexPreparationRequired()
    if missing:
        if (
            not allow_populated
            and await collection.find_one({}, {"_id": 1}, max_time_ms=1000) is not None
        ):
            raise ProjectUserIndexPreparationRequired()
        if before_mutation is not None:
            await before_mutation()
        await collection.create_index(
            CONNECTION_INDEX.keys,
            **CONNECTION_INDEX.options(),
            maxTimeMS=60_000 if allow_populated else 5000,
        )
    observed = await mongodb_index_information(collection)
    if not CONNECTION_INDEX.matches(observed.get(CONNECTION_INDEX.name)):
        raise ProjectUserIndexesUnavailable()
    if retire_obsolete:
        # New uniqueness commits first. Refuse malformed connector identities
        # rather than guessing a type or dropping the previous authority.
        if OLD_CONNECTION_INDEX in obsolete:
            invalid = await collection.find_one(
                {
                    "$or": [
                        {field: {"$not": {"$type": "string"}}}
                        for field in ("org_id", "project_id", "channel", "connector_type")
                    ]
                    + [
                        {field: ""}
                        for field in ("org_id", "project_id", "channel", "connector_type")
                    ]
                },
                {"_id": 1},
                max_time_ms=5000,
            )
            if invalid is not None:
                raise ProjectUserIndexPreparationRequired()
        for name in obsolete:
            if before_mutation is not None:
                await before_mutation()
            await collection.drop_index(name, maxTimeMS=5000)
        final = await mongodb_index_information(collection)
        if (
            not CONNECTION_INDEX.matches(final.get(CONNECTION_INDEX.name))
            or connection_index_work(final)[1]
        ):
            raise ProjectUserIndexesUnavailable()
