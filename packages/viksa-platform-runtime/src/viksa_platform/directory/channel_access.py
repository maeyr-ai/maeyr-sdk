"""Per-channel access grants — one Mongo document per grant."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from viksa_platform.directory.channel_platform import (
    CHANNEL_ACCESS_COLLECTION,
    CHANNEL_IDENTITY_FIELD,
    CHANNEL_WILDCARD_IDENTITY,
    WebhookChannelType,
)
from viksa_platform.directory.slack_access_grant import (
    coerce_expires_at,
    grant_is_expired,
    utc_now,
)
from viksa_platform.directory.tenant_database import database_for_account, project_filter

CHANNEL_GRANTS_COLLECTION = "channel_access_grants"
_LEGACY_MIGRATION_DONE: set[str] = set()


def _migration_key(scope: Dict[str, str], channel: str) -> str:
    return f"{scope['account_id']}:{scope['org_id']}:{scope['project_id']}:{channel}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_agents(agents: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in agents or []:
        a = (raw or "").strip()
        if not a:
            continue
        key = a.lower() if a != "*" else "*"
        if key in seen:
            continue
        seen.add(key)
        out.append("*" if a == "*" else a)
    return out


def _normalize_identity_value(channel: str, value: str) -> str:
    v = (value or "").strip()
    if v == CHANNEL_WILDCARD_IDENTITY:
        return CHANNEL_WILDCARD_IDENTITY
    if channel == "whatsapp" or channel == "sms":
        v = v.replace(" ", "").replace("-", "")
        if v and not v.startswith("+"):
            v = f"+{v}"
    if channel == "telegram" and v.startswith("@"):
        return v.lower()
    if channel == "teams" and "@" in v:
        return v.lower()
    if channel == "slack" and "@" in v:
        return v.lower()
    if channel == WebhookChannelType.WEB_WIDGET.value:
        return v.lower()
    return v


class ChannelAccessStoreBase:
    """Shared grant store with injected service-owned persistence and side effects."""

    def __init__(self, scope: Dict[str, str], channel: str, *, mongo_client: Any) -> None:
        self._mongo_client = mongo_client
        self._scope = dict(scope)
        self._channel = channel
        self._policy_collection = CHANNEL_ACCESS_COLLECTION.get(
            channel, f"volt_{channel}_access"
        )
        self._identity_field = CHANNEL_IDENTITY_FIELD.get(channel, "identity_value")
        self._db = database_for_account(self._scope["account_id"])

    def _policy_coll(self) -> Any:
        return self._mongo_client.get_collection(self._policy_collection, self._db)

    def _grants_coll(self) -> Any:
        return self._mongo_client.get_collection(CHANNEL_GRANTS_COLLECTION, self._db)

    def _pf(self) -> Dict[str, Any]:
        return project_filter(self._scope)

    def _grant_filter(self, identity_value: Optional[str] = None) -> Dict[str, Any]:
        filt = {**self._pf(), "channel": self._channel}
        if identity_value is not None:
            filt["identity_value"] = identity_value
        return filt

    def _normalize_grant_entry(self, g: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        identity_type = str(g.get("identity_type") or self._identity_field).strip()
        raw_identity = str(g.get("identity_value") or g.get("email") or "")
        identity_value = _normalize_identity_value(self._channel, raw_identity)
        if not identity_value:
            return None
        entry: Dict[str, Any] = {
            "identity_type": identity_type,
            "identity_value": identity_value,
            "agents": _normalize_agents(list(g.get("agents") or [])),
            "enabled": bool(g.get("enabled", True)),
            "updated_at": g.get("updated_at"),
            "updated_by": g.get("updated_by"),
        }
        exp = coerce_expires_at(g.get("expires_at"))
        if exp is not None:
            entry["expires_at"] = exp
        return entry

    async def _get_policy_raw(self) -> Dict[str, Any]:
        await self._mongo_client.initialize()
        return await self._policy_coll().find_one(self._pf()) or {}

    async def _migrate_legacy_if_needed(self) -> None:
        key = _migration_key(self._scope, self._channel)
        if key in _LEGACY_MIGRATION_DONE:
            return
        await self._mongo_client.initialize()
        raw = await self._policy_coll().find_one(self._pf()) or {}
        legacy_grants = raw.get("grants") or []
        if legacy_grants:
            grants_coll = self._grants_coll()
            for g in legacy_grants:
                if not isinstance(g, dict):
                    continue
                entry = self._normalize_grant_entry(g)
                if not entry:
                    continue
                await grants_coll.update_one(
                    self._grant_filter(entry["identity_value"]),
                    {
                        "$set": {
                            **self._pf(),
                            **self._scope,
                            "channel": self._channel,
                            **entry,
                        },
                        "$setOnInsert": {"created_at": _now_iso()},
                    },
                    upsert=True,
                )
            await self._policy_coll().update_one(self._pf(), {"$unset": {"grants": ""}})
        _LEGACY_MIGRATION_DONE.add(key)

    async def _fetch_active_grant_entry(self, identity_value: str) -> Optional[Dict[str, Any]]:
        raw = await self._grants_coll().find_one(self._grant_filter(identity_value))
        if not raw:
            return None
        entry = self._normalize_grant_entry(raw)
        if not entry or not entry.get("enabled", True):
            return None
        if grant_is_expired(entry, utc_now()):
            return None
        return entry

    async def find_direct_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Active grant for this identity only (no wildcard merge)."""
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        return await self._fetch_active_grant_entry(norm_value)

    async def find_active_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Lookup grant for a user, falling back to the all-users wildcard grant."""
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        entries: List[Dict[str, Any]] = []

        if norm_value != CHANNEL_WILDCARD_IDENTITY:
            specific = await self._fetch_active_grant_entry(norm_value)
            if specific:
                entries.append(specific)

        wildcard = await self._fetch_active_grant_entry(CHANNEL_WILDCARD_IDENTITY)
        if wildcard:
            entries.append(wildcard)

        if not entries:
            return None

        merged_agents = _normalize_agents(
            [agent for entry in entries for agent in (entry.get("agents") or [])]
        )
        if not merged_agents:
            return None

        primary = entries[0]
        return {
            "identity_type": str(primary.get("identity_type") or self._identity_field),
            "identity_value": norm_value,
            "agents": merged_agents,
            "enabled": True,
        }

    async def get_document(self) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        policy = await self._get_policy_raw()
        now = utc_now()
        cursor = self._grants_coll().find(self._grant_filter()).sort("identity_value", 1)
        active: List[Dict[str, Any]] = []
        expired_count = 0
        async for raw in cursor:
            entry = self._normalize_grant_entry(raw)
            if not entry:
                continue
            if grant_is_expired(entry, now):
                expired_count += 1
                continue
            active.append(entry)

        if expired_count:
            self._schedule_background(self.prune_expired_grants())

        return {
            "account_id": self._scope["account_id"],
            "org_id": self._scope["org_id"],
            "project_id": self._scope["project_id"],
            "channel": self._channel,
            "identity_field": self._identity_field,
            "members_all_agents": bool(policy.get("members_all_agents", True)),
            "grants": active,
        }

    async def upsert_grant(
        self,
        identity_value: str,
        agents: List[str],
        *,
        identity_type: Optional[str] = None,
        enabled: bool = True,
        expires_at: Optional[datetime] = None,
        original_identity: Optional[str] = None,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        if not norm_value:
            raise ValueError("invalid identity_value")
        norm_agents = _normalize_agents(agents)
        if not norm_agents:
            raise ValueError("at least one agent alias or '*' is required")

        lookup = (
            _normalize_identity_value(self._channel, original_identity)
            if original_identity
            else norm_value
        )
        if lookup and lookup != norm_value:
            await self._grants_coll().delete_one(self._grant_filter(lookup))

        grant_doc: Dict[str, Any] = {
            **self._pf(),
            **self._scope,
            "channel": self._channel,
            "identity_type": identity_type or self._identity_field,
            "identity_value": norm_value,
            "agents": norm_agents,
            "enabled": bool(enabled),
            "updated_at": _now_iso(),
            "updated_by": updated_by,
        }
        exp = coerce_expires_at(expires_at)
        if exp is not None:
            grant_doc["expires_at"] = exp
        else:
            grant_doc["expires_at"] = None

        await self._mongo_client.initialize()
        await self._grants_coll().update_one(
            self._grant_filter(norm_value),
            {
                "$set": grant_doc,
                "$setOnInsert": {"created_at": _now_iso()},
            },
            upsert=True,
        )
        await self._sync_policy_for_grant(norm_value, norm_agents, enabled)
        return await self.get_document()

    async def set_grant_enabled(
        self,
        identity_value: str,
        enabled: bool,
        *,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        await self._mongo_client.initialize()
        grant_doc = await self.find_direct_grant(norm_value)
        agents = [str(agent) for agent in (grant_doc.get("agents") or [])] if grant_doc else []

        result = await self._grants_coll().update_one(
            self._grant_filter(norm_value),
            {
                "$set": {
                    "enabled": bool(enabled),
                    "updated_at": _now_iso(),
                    "updated_by": updated_by,
                }
            },
        )
        if result.matched_count == 0:
            raise ValueError("grant not found")
        await self._sync_policy_for_grant(norm_value, agents, enabled)
        return await self.get_document()

    async def remove_grant(self, identity_value: str) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        await self._mongo_client.initialize()
        await self._grants_coll().delete_one(self._grant_filter(norm_value))
        await self._sync_policy_for_grant(norm_value, [], False)
        return await self.get_document()

    async def _sync_policy_for_grant(
        self,
        norm_value: str,
        norm_agents: List[str],
        enabled: bool,
    ) -> None:
        """Service hook for synchronizing a secondary access-policy projection."""
        del norm_value, norm_agents, enabled

    def _schedule_background(self, awaitable: Any) -> None:
        """Service hook for scheduling best-effort expiry cleanup."""
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise RuntimeError("channel access background scheduler is not configured")

    async def update_members_all_agents(self, members_all_agents: bool) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        await self._mongo_client.initialize()
        await self._policy_coll().update_one(
            self._pf(),
            {
                "$set": {
                    **self._pf(),
                    **self._scope,
                    "members_all_agents": bool(members_all_agents),
                    "updated_at": _now_iso(),
                }
            },
            upsert=True,
        )
        return await self.get_document()

    def _build_list_query(
        self,
        *,
        search: str = "",
        agent: str = "",
        active_only: bool = True,
    ) -> tuple[Dict[str, Any], datetime]:
        query: Dict[str, Any] = dict(self._grant_filter())
        q = (search or "").strip()
        if q:
            query["identity_value"] = {"$regex": re.escape(q), "$options": "i"}

        agent_f = (agent or "").strip()
        if agent_f and agent_f != "all":
            if agent_f == "*":
                query["agents"] = {"$in": ["*"]}
            else:
                query["agents"] = agent_f

        now = utc_now()
        if active_only:
            query["$or"] = [
                {"expires_at": {"$exists": False}},
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
            ]
        return query, now

    async def list_grants_paginated(
        self,
        *,
        search: str = "",
        agent: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> tuple[List[Dict[str, Any]], int, bool]:
        await self._migrate_legacy_if_needed()
        policy = await self._get_policy_raw()
        query, now = self._build_list_query(search=search, agent=agent, active_only=True)

        await self._mongo_client.initialize()
        coll = self._grants_coll()
        skip = (max(1, page) - 1) * max(1, page_size)
        limit = max(1, page_size)

        total = await coll.count_documents(query)
        cursor = coll.find(query).sort("identity_value", 1).skip(skip).limit(limit)
        page_rows: List[Dict[str, Any]] = []
        async for raw in cursor:
            entry = self._normalize_grant_entry(raw)
            if not entry or grant_is_expired(entry, now):
                continue
            page_rows.append(entry)

        return page_rows, total, bool(policy.get("members_all_agents", True))

    async def list_grants_filtered(
        self,
        *,
        search: str = "",
        agent: str = "",
    ) -> tuple[List[Dict[str, Any]], bool]:
        await self._migrate_legacy_if_needed()
        policy = await self._get_policy_raw()
        query, now = self._build_list_query(search=search, agent=agent, active_only=True)

        await self._mongo_client.initialize()
        coll = self._grants_coll()
        rows: List[Dict[str, Any]] = []
        cursor = coll.find(query).sort("identity_value", 1)
        async for raw in cursor:
            entry = self._normalize_grant_entry(raw)
            if not entry or grant_is_expired(entry, now):
                continue
            rows.append(entry)

        return rows, bool(policy.get("members_all_agents", True))

    async def bulk_upsert_grants(
        self,
        rows: List[Dict[str, Any]],
        *,
        updated_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        await self._mongo_client.initialize()
        grants_coll = self._grants_coll()
        for row in rows:
            entry = self._normalize_grant_entry(row)
            if not entry:
                continue
            entry["updated_by"] = updated_by
            await grants_coll.update_one(
                self._grant_filter(entry["identity_value"]),
                {
                    "$set": {
                        **self._pf(),
                        **self._scope,
                        "channel": self._channel,
                        **entry,
                    },
                    "$setOnInsert": {"created_at": _now_iso()},
                },
                upsert=True,
            )
        return await self.get_document()

    async def prune_expired_grants(self) -> int:
        await self._migrate_legacy_if_needed()
        await self._mongo_client.initialize()
        now = utc_now()
        result = await self._grants_coll().delete_many(
            {
                **self._grant_filter(),
                "expires_at": {"$lte": now},
            }
        )
        return int(result.deleted_count)

__all__ = [
    "CHANNEL_GRANTS_COLLECTION",
    "ChannelAccessStoreBase",
]
