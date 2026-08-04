"""Injected customer/channel-grant application operations for Directory and Volt."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

Document = dict[str, Any]
Scope = dict[str, str]


@dataclass(frozen=True, slots=True)
class CustomerChannelGrantDependencies:
    """Service-owned adapters required by the shared grant application policy."""

    user_store: Callable[[Scope], Any]
    channel_access_store: Callable[[Scope, str], Any]
    normalize_identity: Callable[[str, str], str]
    resolve_connector_binding: Callable[[Document | None, str], Document | None]
    is_master_user: Callable[[Document | None], bool]
    schema_field_type: Callable[[Document | None, str], str]
    default_connector_bindings: Callable[[], Sequence[Document]]
    channel_identity_fields: Mapping[str, str]
    wildcard_identity: str
    invalidate: Callable[..., Awaitable[None]]
    delete_customer_record: Callable[..., Awaitable[Document]]
    list_customer_channel_access: Callable[..., Awaitable[list[Document]]]
    set_customer_channel_access_enabled: Callable[..., Awaitable[Document]]


class CustomerChannelGrantOperations:
    """Shared application behavior with persistence and cache effects injected."""

    def __init__(self, dependencies: CustomerChannelGrantDependencies) -> None:
        self._dependencies = dependencies

    def connector_bindings_from_schema(self, schema: Document | None) -> list[Document]:
        bindings: list[Document] = []
        if schema:
            bindings = [
                binding
                for binding in (schema.get("connector_bindings") or [])
                if isinstance(binding, dict)
            ]
            if not bindings:
                for rule in schema.get("match_rules") or []:
                    if isinstance(rule, dict) and rule.get("channel") != "*":
                        bindings.append(
                            {
                                "channel": rule.get("channel"),
                                "profile_field": rule.get("field"),
                                "required": True,
                            }
                        )
        if not bindings:
            bindings = [dict(item) for item in self._dependencies.default_connector_bindings()]
        seen: set[str] = set()
        result: list[Document] = []
        for raw in bindings:
            channel = str(raw.get("channel") or "").strip().lower()
            profile_field = str(raw.get("profile_field") or "").strip()
            if not channel or not profile_field or channel in seen:
                continue
            seen.add(channel)
            result.append({"channel": channel, "profile_field": profile_field})
        return result

    def profile_connector_value(
        self,
        profile: Document,
        *,
        channel: str,
        profile_field: str,
    ) -> tuple[str, str]:
        raw = str(profile.get(profile_field) or "").strip()
        if not raw:
            return "", ""
        return raw, self._dependencies.normalize_identity(channel, raw)

    async def snapshot_direct_grant(
        self,
        scope: Scope,
        channel: str,
        identity_value: str,
    ) -> Document | None:
        access_store = self._dependencies.channel_access_store(scope, channel)
        grant = await access_store.find_direct_grant(identity_value)
        if not grant:
            return None
        return {
            "agents": list(grant.get("agents") or []),
            "enabled": bool(grant.get("enabled", True)),
            "expires_at": grant.get("expires_at"),
        }

    async def link_customer_connector_identity(
        self,
        scope: Scope,
        *,
        channel: str,
        customer_user_id: str,
        external_user_id: str,
        linked_by: str = "auto",
    ) -> Document:
        channel_key = (channel or "").strip().lower()
        customer_id = (customer_user_id or "").strip()
        external_id = (external_user_id or "").strip()
        if not customer_id:
            raise ValueError("customer_user_id is required")
        if not external_id:
            raise ValueError("external_user_id is required")

        user_store = self._dependencies.user_store(scope)
        user = await user_store.get_user(customer_id)
        if not user:
            raise ValueError(f"customer_user_id '{customer_id}' not found")
        if not self._dependencies.is_master_user(user):
            raise ValueError(
                f"Identity auto-link applies to master customers only (got '{customer_id}')"
            )

        existing_link = await user_store.get_identity_link(channel_key, external_id)
        if existing_link:
            linked_customer_id = str(existing_link.get("customer_user_id") or "").strip()
            if linked_customer_id and linked_customer_id != customer_id:
                raise ValueError(
                    f"Connector identity '{external_id}' on {channel_key} is already "
                    f"linked to customer '{linked_customer_id}'"
                )

        identity = await user_store.upsert_identity_link(
            channel=channel_key,
            external_user_id=external_id,
            customer_user_id=customer_id,
            linked_by=linked_by,
        )
        await self._dependencies.invalidate(
            scope,
            channel=channel_key,
            identity_values=[external_id],
        )
        return {
            "customer_user_id": customer_id,
            "channel": channel_key,
            "external_user_id": external_id,
            "identity": identity,
        }

    async def sync_customer_connector(
        self,
        scope: Scope,
        *,
        channel: str,
        customer_user_id: str,
        connector_value: str,
        profile_updates: Document | None = None,
        linked_by: str = "admin",
    ) -> Document:
        channel_key = (channel or "").strip().lower()
        customer_id = (customer_user_id or "").strip()
        if not customer_id:
            raise ValueError("customer_user_id is required")

        user_store = self._dependencies.user_store(scope)
        schema = await user_store.get_schema()
        binding = self._dependencies.resolve_connector_binding(schema, channel_key)
        if not binding or not binding.get("profile_field"):
            raise ValueError(f"No connector primary key binding configured for {channel_key}")

        profile_field = str(binding["profile_field"])
        user = await user_store.get_user(customer_id)
        if not user:
            raise ValueError(f"customer_user_id '{customer_id}' not found")
        if not self._dependencies.is_master_user(user):
            raise ValueError(
                f"Connector profile sync applies to master customers only (got '{customer_id}')"
            )

        profile = dict(user.get("profile") or {})
        if profile_updates:
            profile.update(profile_updates)
        raw_value = (connector_value or "").strip()
        if not raw_value:
            raise ValueError(f"{profile_field} is required for {channel_key}")

        normalized_identity = self._dependencies.normalize_identity(
            channel_key,
            raw_value,
        )
        profile[profile_field] = normalized_identity

        field_type = self._dependencies.schema_field_type(schema, profile_field)
        candidates = await user_store.find_users_by_field(
            profile_field,
            normalized_identity,
            enabled_only=False,
            field_type=field_type,
        )
        for document in candidates:
            other_id = str(document.get("customer_user_id") or "").strip()
            if other_id and other_id != customer_id:
                raise ValueError(
                    f"{profile_field} '{normalized_identity}' is already assigned "
                    f"to customer '{other_id}'"
                )

        existing_link = await user_store.get_identity_link(
            channel_key,
            normalized_identity,
        )
        if existing_link:
            linked_customer_id = str(existing_link.get("customer_user_id") or "").strip()
            if linked_customer_id and linked_customer_id != customer_id:
                raise ValueError(
                    f"Connector identity '{normalized_identity}' on {channel_key} "
                    f"is already linked to customer '{linked_customer_id}'"
                )

        try:
            await user_store.upsert_user(
                customer_user_id=customer_id,
                profile=profile,
                enabled=user.get("enabled", True),
            )
            identity = await user_store.upsert_identity_link(
                channel=channel_key,
                external_user_id=normalized_identity,
                customer_user_id=customer_id,
                linked_by=linked_by,
            )
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise ValueError(
                    f"Connector identity '{normalized_identity}' on {channel_key} is "
                    "already linked to another customer (database constraint violation)"
                ) from exc
            raise

        updated_user = await user_store.get_user(customer_id)
        if updated_user:
            updated_user.pop("_id", None)

        await self._dependencies.invalidate(
            scope,
            channel=channel_key,
            identity_values=[normalized_identity],
        )
        return {
            "customer_user_id": customer_id,
            "channel": channel_key,
            "profile_field": profile_field,
            "connector_value": normalized_identity,
            "identity": identity,
            "user": updated_user,
        }

    async def clear_profile_connector_field(
        self,
        user_store: Any,
        *,
        channel: str,
        customer_user_id: str,
        norm_identity: str,
    ) -> bool:
        user = await user_store.get_user(customer_user_id)
        if not user:
            return False
        schema = await user_store.get_schema()
        binding = self._dependencies.resolve_connector_binding(schema, channel) if schema else None
        if not binding or not binding.get("profile_field"):
            return False
        profile_field = str(binding["profile_field"])
        profile = dict(user.get("profile") or {})
        current = str(profile.get(profile_field) or "").strip()
        if not current:
            return False
        if self._dependencies.normalize_identity(channel, current) != norm_identity:
            return False
        profile.pop(profile_field, None)
        await user_store.upsert_user(
            customer_user_id=customer_user_id,
            profile=profile,
            enabled=user.get("enabled", True),
            remote=True,
        )
        return True

    async def collect_identities_to_revoke(
        self,
        user_store: Any,
        *,
        channel: str,
        identity_value: str | None = None,
        customer_user_id: str | None = None,
    ) -> list[str]:
        channel_key = (channel or "").strip().lower()
        collected: list[str] = []

        if identity_value:
            normalized = self._dependencies.normalize_identity(
                channel_key,
                identity_value,
            )
            if normalized == self._dependencies.wildcard_identity:
                return [self._dependencies.wildcard_identity]
            collected.append(normalized)

        if customer_user_id:
            customer_id = customer_user_id.strip()
            user = await user_store.get_user(customer_id)
            if not user:
                raise ValueError(f"customer_user_id '{customer_id}' not found")
            for identity in await user_store.list_identities_for_user(customer_id):
                if str(identity.get("channel") or "").strip().lower() != channel_key:
                    continue
                external_id = str(identity.get("external_user_id") or "").strip()
                if external_id and external_id not in collected:
                    collected.append(external_id)
            schema = await user_store.get_schema()
            binding = (
                self._dependencies.resolve_connector_binding(schema, channel_key)
                if schema
                else None
            )
            if binding and binding.get("profile_field"):
                profile_field = str(binding["profile_field"])
                profile = dict(user.get("profile") or {})
                raw = str(profile.get(profile_field) or "").strip()
                if raw:
                    normalized_raw = self._dependencies.normalize_identity(
                        channel_key,
                        raw,
                    )
                    if normalized_raw not in collected:
                        collected.append(normalized_raw)
        return collected

    async def bulk_delete_customer_records(
        self,
        scope: Scope,
        *,
        customer_user_ids: list[str] | None = None,
        search: str = "",
        enabled_only: bool = False,
        data_source_kind: str = "",
        data_source_provider: str = "",
        data_source_connection_id: str = "",
    ) -> Document:
        deleted = 0
        failed: list[Document] = []

        if customer_user_ids is not None:
            customer_ids: list[str] = []
            seen: set[str] = set()
            for raw in customer_user_ids:
                customer_id = str(raw or "").strip()
                if not customer_id or customer_id in seen:
                    continue
                seen.add(customer_id)
                customer_ids.append(customer_id)
            if not customer_ids:
                raise ValueError("customer_user_ids must contain at least one id")
            for customer_id in customer_ids:
                try:
                    result = await self._dependencies.delete_customer_record(
                        scope,
                        customer_id,
                        invalidate_cache=False,
                    )
                    if result.get("deleted"):
                        deleted += 1
                    else:
                        failed.append(
                            {
                                "customer_user_id": customer_id,
                                "error": "Not deleted",
                            }
                        )
                except Exception as exc:
                    failed.append({"customer_user_id": customer_id, "error": str(exc)})
            if deleted > 0:
                await self._dependencies.invalidate(scope)
            return {
                "deleted": deleted,
                "failed": failed,
                "total": len(customer_ids),
            }

        user_store = self._dependencies.user_store(scope)
        rows, _ = await user_store.list_users(
            search=search,
            page=1,
            page_size=10000,
            enabled_only=enabled_only,
            data_source_kind=data_source_kind,
            data_source_provider=data_source_provider,
            data_source_connection_id=data_source_connection_id,
        )
        for row in rows:
            customer_id = str(row.get("customer_user_id") or "").strip()
            if not customer_id:
                continue
            try:
                result = await self._dependencies.delete_customer_record(
                    scope,
                    customer_id,
                    invalidate_cache=False,
                )
                if result.get("deleted"):
                    deleted += 1
                else:
                    failed.append({"customer_user_id": customer_id, "error": "Not deleted"})
            except Exception as exc:
                failed.append({"customer_user_id": customer_id, "error": str(exc)})
        if deleted > 0:
            await self._dependencies.invalidate(scope)
        return {"deleted": deleted, "failed": failed, "total": len(rows)}

    def customer_channel_grant_identities(
        self,
        channel: str,
        profile: Document,
        *,
        linked_external_id: str = "",
        profile_field: str = "",
    ) -> list[str]:
        channel_key = (channel or "").strip().lower()
        candidates: list[str] = []

        def append_identity(raw: Any) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            normalized = self._dependencies.normalize_identity(channel_key, text)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        normalized_profile_field = (profile_field or "").strip()
        if normalized_profile_field:
            append_identity(profile.get(normalized_profile_field))
        append_identity(linked_external_id)

        identity_field = self._dependencies.channel_identity_fields.get(channel_key, "")
        if identity_field == "email":
            for key in ("email", "profile_email", "work_email"):
                append_identity(profile.get(key))
            if normalized_profile_field:
                append_identity(profile.get(normalized_profile_field))
        elif identity_field in ("phone_e164", "phone"):
            for key in ("phone", "phone_e164"):
                append_identity(profile.get(key))
            if normalized_profile_field:
                append_identity(profile.get(normalized_profile_field))
        elif normalized_profile_field:
            append_identity(profile.get(normalized_profile_field))
        return candidates

    async def find_direct_grant_for_customer(
        self,
        scope: Scope,
        channel: str,
        identities: list[str],
    ) -> tuple[Document | None, str | None]:
        if not identities:
            return None, None
        access_store = self._dependencies.channel_access_store(scope, channel)
        for identity in identities:
            grant = await access_store.find_direct_grant(identity)
            if grant is not None:
                return cast(Document, grant), identity
        return None, None

    async def set_direct_grant_enabled_for_customer(
        self,
        scope: Scope,
        channel: str,
        identities: list[str],
        enabled: bool,
        *,
        updated_by: str | None = None,
    ) -> bool:
        access_store = self._dependencies.channel_access_store(scope, channel)
        for identity in identities:
            try:
                await access_store.set_grant_enabled(
                    identity,
                    enabled,
                    updated_by=updated_by,
                )
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def serialize_user_document(document: Document) -> Document:
        row = dict(document)
        row.pop("_id", None)
        profile = dict(row.get("profile") or {})
        row["connector_enabled"] = dict(row.get("connector_enabled") or {})
        row["display_name"] = str(profile.get("name") or profile.get("full_name") or "").strip()
        return row

    async def get_customer_record(
        self,
        scope: Scope,
        customer_user_id: str,
    ) -> Document:
        customer_id = (customer_user_id or "").strip()
        if not customer_id:
            raise ValueError("customer_user_id is required")

        user_store = self._dependencies.user_store(scope)
        user = await user_store.get_user(customer_id)
        if not user:
            raise ValueError(f"customer_user_id '{customer_id}' not found")

        user_row = self.serialize_user_document(user)
        identities = await user_store.list_identities_for_user(customer_id)
        for identity in identities:
            identity.pop("_id", None)
        channel_access = await self._dependencies.list_customer_channel_access(
            scope,
            customer_id,
        )
        return {
            "customer_user_id": customer_id,
            "customer_enabled": bool(user_row.get("enabled", True)),
            "display_name": user_row.get("display_name") or "",
            "user": {key: value for key, value in user_row.items() if key != "display_name"},
            "identities": identities,
            "channel_access": channel_access,
        }

    async def set_customer_connector_enabled(
        self,
        scope: Scope,
        *,
        customer_user_id: str,
        channel: str,
        enabled: bool,
    ) -> Document:
        channel_key = (channel or "").strip().lower()
        user_store = self._dependencies.user_store(scope)
        customer_id = customer_user_id.strip()
        user = await user_store.get_user(customer_id)
        if not user:
            raise ValueError(f"customer_user_id '{customer_id}' not found")

        identity_value = ""
        linked_external_id = ""
        for identity in await user_store.list_identities_for_user(customer_id):
            if str(identity.get("channel") or "").strip().lower() == channel_key:
                linked_external_id = str(identity.get("external_user_id") or "").strip()
                identity_value = linked_external_id
                break
        schema = await user_store.get_schema()
        binding = (
            self._dependencies.resolve_connector_binding(schema, channel_key) if schema else None
        )
        profile_field = str(binding.get("profile_field") or "") if binding else ""
        profile = dict(user.get("profile") or {})
        candidates = self.customer_channel_grant_identities(
            channel_key,
            profile,
            linked_external_id=linked_external_id,
            profile_field=profile_field,
        )
        if not identity_value and candidates:
            identity_value = candidates[0]

        return await self._dependencies.set_customer_channel_access_enabled(
            scope,
            channel=channel_key,
            enabled=enabled,
            customer_user_id=customer_id,
            identity_value=identity_value or None,
        )


__all__ = ["CustomerChannelGrantDependencies", "CustomerChannelGrantOperations"]
