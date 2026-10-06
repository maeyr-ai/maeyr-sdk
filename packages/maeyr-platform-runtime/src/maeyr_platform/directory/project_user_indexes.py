"""Canonical profile index contracts and bounded, fail-closed preparation.

Customer schemas are authority only after their native Mongo constraints commit.
Old sparse constraints are retired by the explicit release command, never by a
request-side compatibility branch or an application-only uniqueness fallback.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pymongo.errors import PyMongoError

from maeyr_platform.mongodb_indexes import mongodb_index_information

FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
VERSION = 2
SCHEMA_COLLECTION = "project_user_schemas"
USERS_COLLECTION = "project_users"
IDENTITY_INDEX = "project_users_scope_identity_v2"
SCHEMA_INDEX = "project_user_schemas_unique"
SCOPE_KEYS = [("account_id", 1), ("org_id", 1), ("project_id", 1)]
METADATA_TIMEOUT_MS = 5000


class ProjectUserIndexPreparationRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            409,
            {
                "code": "project_user_indexes_preparation_required",
                "message": "Directory constraints need preparation before this change "
                "can be applied. "
                "Your current schema remains active. Ask your administrator to run "
                "project-user-index-preparation and retry.",
            },
        )


class ProjectUserIndexesUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            503,
            {
                "code": "project_user_indexes_unavailable",
                "message": "Directory constraints could not be verified. Try again shortly.",
            },
        )


class ProjectUserSchemaConflict(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            409,
            {
                "code": "project_user_schema_conflict",
                "message": "The directory schema changed while this update was being prepared. "
                "Review the latest schema and try again.",
            },
        )


def profile_field_name(value: Any) -> str:
    if not isinstance(value, str) or FIELD_NAME.fullmatch(value) is None:
        raise ValueError(
            "Profile field names must use letters, numbers, and underscores (1–64 characters)."
        )
    return value


def collect_unique_profile_fields(schema: dict[str, Any] | None) -> set[str]:
    """The same constraint set used by validation, native indexes, and lookup."""
    unique: set[str] = set()
    if not schema:
        return unique
    for key in ("fields", "connector_bindings", "match_rules", "agent_connectors"):
        items = schema.get(key)
        if items is not None and (
            not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
        ):
            raise ValueError("Profile schema collections must contain objects")
    for item in schema.get("fields") or []:
        for flag in ("unique", "required"):
            if flag in item and type(item[flag]) is not bool:
                raise ValueError("Profile field flags must be booleans")
        if item.get("type", "string") not in {"string", "email", "phone", "number", "boolean"}:
            raise ValueError("Profile field type is invalid")
        if isinstance(item, dict) and item.get("unique"):
            unique.add(profile_field_name(item.get("name")))
    for item in schema.get("connector_bindings") or []:
        if isinstance(item, dict) and item.get("profile_field"):
            unique.add(profile_field_name(item["profile_field"]))
    for item in schema.get("match_rules") or []:
        if isinstance(item, dict) and str(item.get("channel") or "").strip() not in {"", "*"}:
            unique.add(profile_field_name(item.get("field")))
    primary = schema.get("primary_key")
    if primary and primary != "customer_user_id":
        unique.add(profile_field_name(primary))
    if len(unique) > 8:
        raise ValueError("Maximum of 8 unique profile fields allowed to prevent index exhaustion.")
    return unique


def lookup_profile_fields(schema: dict[str, Any] | None) -> set[str]:
    fields = {"email", "phone_e164"} | collect_unique_profile_fields(schema)
    for item in (schema or {}).get("fields") or []:
        if not isinstance(item, dict):
            raise ValueError("Profile field definitions must be objects")
        profile_field_name(item.get("name"))
    for item in (schema or {}).get("agent_connectors") or []:
        if isinstance(item, dict) and item.get("primary_key_field"):
            fields.add(profile_field_name(item["primary_key_field"]))
    for item in (schema or {}).get("match_rules") or []:
        if isinstance(item, dict) and item.get("field"):
            fields.add(profile_field_name(item["field"]))
    return fields


def unique_partial_filter(field: str) -> dict[str, Any]:
    # Optional absent/null/empty columns do not collide. Numbers and booleans
    # are supported schema scalars; native uniqueness handles concurrent writers.
    path = f"profile.{profile_field_name(field)}"
    return {"$or": [{path: {"$type": "string", "$gt": ""}}, {path: {"$type": ["number", "bool"]}}]}


@dataclass(frozen=True)
class ProfileIndex:
    name: str
    keys: list[tuple[str, int]]
    unique: bool = False
    partial: dict[str, Any] | None = None

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"name": self.name}
        if self.unique:
            options["unique"] = True
        if self.partial is not None:
            options["partialFilterExpression"] = self.partial
        return options

    def matches(self, observed: dict[str, Any] | None) -> bool:
        return bool(
            observed is not None
            and observed.get("key") == self.keys
            and observed.get("unique", False) is self.unique
            and observed.get("partialFilterExpression") == self.partial
            and observed.get("sparse", False) is False
            and observed.get("hidden", False) is False
            and "buildUUID" not in observed
            and "expireAfterSeconds" not in observed
            and observed.get("collation", {"locale": "simple"}) == {"locale": "simple"}
        )


def profile_index_manifest(schemas: list[dict[str, Any]]) -> list[ProfileIndex]:
    unique: set[str] = set()
    lookup = {"email", "phone_e164"}
    for schema in schemas:
        unique |= collect_unique_profile_fields(schema)
        lookup |= lookup_profile_fields(schema)
    return (
        [ProfileIndex(IDENTITY_INDEX, SCOPE_KEYS + [("customer_user_id", 1)], unique=True)]
        + [
            ProfileIndex(f"profile_lookup_{field}_v2", SCOPE_KEYS + [(f"profile.{field}", 1)])
            for field in sorted(lookup)
        ]
        + [
            ProfileIndex(
                f"unique_profile_{field}_v2",
                SCOPE_KEYS + [(f"profile.{field}", 1)],
                unique=True,
                partial=unique_partial_filter(field),
            )
            for field in sorted(unique)
        ]
    )


def _exact_old_index(name: str, spec: dict[str, Any]) -> bool:
    """An explicit allowlist of formerly published specifications, not names alone."""
    if "buildUUID" in spec or "expireAfterSeconds" in spec or spec.get("hidden", False):
        return False
    if spec.get("partialFilterExpression") is not None or spec.get("collation") is not None:
        return False
    if name.startswith("unique_profile_"):
        field = name.removeprefix("unique_profile_")
        return (
            FIELD_NAME.fullmatch(field) is not None
            and spec.get("unique") is True
            and spec.get("sparse") is True
            and spec.get("key") == SCOPE_KEYS + [(f"profile.{field}", 1)]
        )
    if name.startswith("conn_profile_"):
        field = name.removeprefix("conn_profile_")
        return (
            FIELD_NAME.fullmatch(field) is not None
            and spec.get("unique", False) is False
            and spec.get("sparse") is True
            and spec.get("key") == SCOPE_KEYS + [(f"profile.{field}", 1)]
        )
    definitions = {
        "account_id_1_org_id_1_project_id_1_customer_user_id_1": (
            SCOPE_KEYS + [("customer_user_id", 1)],
            True,
            False,
        ),
        "project_users_unique": (
            [("org_id", 1), ("project_id", 1), ("customer_user_id", 1)],
            True,
            False,
        ),
        "project_users_email_idx": (
            [("org_id", 1), ("project_id", 1), ("profile.email", 1)],
            False,
            True,
        ),
        "project_users_phone_idx": (
            [("org_id", 1), ("project_id", 1), ("profile.phone_e164", 1)],
            False,
            True,
        ),
    }
    expected = definitions.get(name)
    return bool(
        expected is not None
        and spec.get("key") == expected[0]
        and spec.get("unique", False) is expected[1]
        and spec.get("sparse", False) is expected[2]
    )


def _canonical_unique_name(name: str, spec: dict[str, Any]) -> bool:
    keys = spec.get("key")
    if not isinstance(keys, list) or len(keys) != 4 or keys[:3] != SCOPE_KEYS:
        return False
    path = keys[-1][0]
    if not isinstance(path, str) or not path.startswith("profile."):
        return False
    field = path.removeprefix("profile.")
    return FIELD_NAME.fullmatch(field) is not None and name == f"unique_profile_{field}_v2"


def _obsolete_profile_indexes(indexes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: spec
        for name, spec in indexes.items()
        if (
            (name.startswith("unique_profile_") and not _canonical_unique_name(name, spec))
            or name.startswith("conn_profile_")
            or name
            in {
                "account_id_1_org_id_1_project_id_1_customer_user_id_1",
                "project_users_unique",
                "project_users_email_idx",
                "project_users_phone_idx",
            }
        )
    }


def assess_profile_index_contract(
    observed: dict[str, dict[str, Any]],
    schemas: list[dict[str, Any]],
) -> tuple[list[ProfileIndex], dict[str, dict[str, Any]]]:
    """Finite missing work; unknown/malformed/unfinished authority is refused."""
    manifest = profile_index_manifest(schemas)
    obsolete = _obsolete_profile_indexes(observed)
    if any(not _exact_old_index(name, spec) for name, spec in obsolete.items()):
        raise ProjectUserIndexPreparationRequired()
    missing: list[ProfileIndex] = []
    for index in manifest:
        found = observed.get(index.name)
        if found is None:
            missing.append(index)
        elif not index.matches(found):
            if "buildUUID" in found:
                raise ProjectUserIndexesUnavailable()
            raise ProjectUserIndexPreparationRequired()
    for name in obsolete:
        if name.startswith("unique_profile_"):
            field = name.removeprefix("unique_profile_")
            replacement = ProfileIndex(
                f"unique_profile_{field}_v2",
                SCOPE_KEYS + [(f"profile.{field}", 1)],
                unique=True,
                partial=unique_partial_filter(field),
            )
            if replacement.name not in {index.name for index in missing}:
                found = observed.get(replacement.name)
                if found is None:
                    missing.append(replacement)
                elif not replacement.matches(found):
                    raise ProjectUserIndexPreparationRequired()
    if max(1, len(observed)) + len(missing) > 64:
        raise ProjectUserIndexPreparationRequired()
    return missing, obsolete


async def prepare_project_user_indexes(
    collection: Any,
    schemas: list[dict[str, Any]],
    *,
    allow_populated: bool = False,
    retire_obsolete: bool = False,
    check_only: bool = False,
    before_mutation: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Commit the complete manifest before acknowledging authority or publishing schemas."""
    try:
        manifest = profile_index_manifest(schemas)
        observed = await mongodb_index_information(collection, max_time_ms=METADATA_TIMEOUT_MS)
        missing, obsolete = assess_profile_index_contract(observed, schemas)
        if check_only and (missing or obsolete):
            raise ProjectUserIndexPreparationRequired()
        if obsolete and not retire_obsolete:
            raise ProjectUserIndexPreparationRequired()
        if missing:
            if (
                not allow_populated
                and await collection.find_one(
                    {},
                    {"_id": 1},
                    max_time_ms=1000,
                )
                is not None
            ):
                raise ProjectUserIndexPreparationRequired()
            # Native limit is 64 including _id. There is no weaker uniqueness
            # mode near capacity. Migration requires room for old AND new indexes.
            if max(1, len(observed)) + len(missing) > 64:
                raise ProjectUserIndexPreparationRequired()
            for index in missing:
                if before_mutation is not None:
                    await before_mutation()
                await collection.create_index(
                    index.keys, **index.options(), maxTimeMS=60_000 if allow_populated else 5000
                )
            observed = await mongodb_index_information(collection, max_time_ms=METADATA_TIMEOUT_MS)
            if not all(index.matches(observed.get(index.name)) for index in manifest):
                raise ProjectUserIndexesUnavailable()
        if retire_obsolete:
            # Always build the replacements first. Even an interrupted migration
            # retains either the old constraint or the committed replacement.
            for name, spec in obsolete.items():
                if name.startswith("unique_profile_"):
                    field = name.removeprefix("unique_profile_")
                    replacement = ProfileIndex(
                        f"unique_profile_{field}_v2",
                        SCOPE_KEYS + [(f"profile.{field}", 1)],
                        unique=True,
                        partial=unique_partial_filter(field),
                    )
                    # Removed schema fields keep their old policy unless a future
                    # explicit policy removal authorizes loosening that constraint.
                    if not replacement.matches(observed.get(replacement.name)):
                        if before_mutation is not None:
                            await before_mutation()
                        await collection.create_index(
                            replacement.keys, **replacement.options(), maxTimeMS=60_000
                        )
                        observed = await mongodb_index_information(
                            collection, max_time_ms=METADATA_TIMEOUT_MS
                        )
                        if not replacement.matches(observed.get(replacement.name)):
                            raise ProjectUserIndexesUnavailable()
                if before_mutation is not None:
                    await before_mutation()
                await collection.drop_index(name, maxTimeMS=5000)
            final = await mongodb_index_information(collection, max_time_ms=METADATA_TIMEOUT_MS)
            if not all(
                index.matches(final.get(index.name)) for index in manifest
            ) or _obsolete_profile_indexes(final):
                raise ProjectUserIndexesUnavailable()
    except (ProjectUserIndexPreparationRequired, ProjectUserIndexesUnavailable):
        raise
    except (PyMongoError, ValueError, TimeoutError) as error:
        raise ProjectUserIndexesUnavailable() from error
