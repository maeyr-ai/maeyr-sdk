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


def normalize_channel_identity(channel: str, value: str) -> str:
    """Return the canonical lookup value used by every connector access path.

    Incoming webhook identities and values entered in Channel Hub must converge
    on exactly the same key.  In particular, WhatsApp sends digits while admins
    commonly paste formatted E.164 numbers.  Keeping this normalization in the
    shared runtime prevents the grant store, directory resolver, and Redis cache
    from silently using different identities.
    """
    ch = (channel or "").strip().lower()
    v = (value or "").strip()
    if v == CHANNEL_WILDCARD_IDENTITY:
        return CHANNEL_WILDCARD_IDENTITY
    if ch in ("whatsapp", "sms"):
        digits = re.sub(r"\D", "", v)
        if v.startswith("00"):
            digits = digits[2:]
        return f"+{digits}" if digits else ""
    if ch == "telegram" and v.startswith("@"):
        return v.lower()
    if ch == "teams":
        return v.lower()
    if ch == "slack" and "@" in v:
        return v.lower()
    if ch in (WebhookChannelType.WEB_WIDGET.value, "email"):
        return v.lower()
    return v


# Kept for service adapters and older imports. New code should use the public
# name so there is one canonical identity contract across services.
_normalize_identity_value = normalize_channel_identity


class ChannelAccessStoreBase:
    """Shared grant store with injected service-owned persistence and side effects."""

    def __init__(self, scope: Dict[str, str], channel: str, *, mongo_client: Any) -> None:
        self._mongo_client = mongo_client
        self._scope = dict(scope)
        self._channel = (channel or "").strip().lower()
        self._policy_collection = CHANNEL_ACCESS_COLLECTION.get(
            self._channel, f"volt_{self._channel}_access"
        )
        self._identity_field = CHANNEL_IDENTITY_FIELD.get(self._channel, "identity_value")
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
        identity_value = normalize_channel_identity(self._channel, raw_identity)
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
        entry = await self._fetch_grant_entry(identity_value)
        if not entry or not entry.get("enabled", True):
            return None
        return entry

    async def _fetch_grant_entry(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Fetch a non-expired grant, including explicit disabled grants."""
        raw = await self._grants_coll().find_one(self._grant_filter(identity_value))
        if not raw:
            return None
        entry = self._normalize_grant_entry(raw)
        if not entry:
            return None
        if grant_is_expired(entry, utc_now()):
            return None
        return entry

    async def find_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Non-expired direct grant, including an explicit disabled record."""
        await self._migrate_legacy_if_needed()
        norm_value = normalize_channel_identity(self._channel, identity_value)
        return await self._fetch_grant_entry(norm_value)

    async def find_grants(
        self,
        identity_values: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch a bounded set of direct grants with one indexed query.

        Disabled grants are returned so the admin access editor can distinguish
        an explicit deny from no grant. Expired grants remain absent. This keeps
        connector-user pages O(1) in database round trips instead of one query
        per user, without ever loading a million-user grant collection.
        """
        await self._migrate_legacy_if_needed()
        normalized: set[str] = set()
        for value in identity_values:
            identity = normalize_channel_identity(self._channel, value)
            if identity:
                normalized.add(identity)
        if not normalized:
            return {}

        await self._mongo_client.initialize()
        query = self._grant_filter()
        query["identity_value"] = {"$in": sorted(normalized)}
        now = utc_now()
        results: Dict[str, Dict[str, Any]] = {}
        async for raw in self._grants_coll().find(query):
            entry = self._normalize_grant_entry(raw)
            if not entry or grant_is_expired(entry, now):
                continue
            results[str(entry["identity_value"])] = entry
        return results

    async def get_members_all_agents(self) -> bool:
        """Read the small policy flag without materializing every grant."""
        await self._migrate_legacy_if_needed()
        policy = await self._get_policy_raw()
        return bool(policy.get("members_all_agents", True))

    async def find_direct_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Active grant for this identity only (no wildcard merge)."""
        await self._migrate_legacy_if_needed()
        norm_value = normalize_channel_identity(self._channel, identity_value)
        return await self._fetch_active_grant_entry(norm_value)

    async def find_effective_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Resolve the least-privilege grant for one connector identity.

        A specific grant replaces the wildcard rather than being unioned with
        it.  A disabled specific record is an explicit deny and therefore also
        blocks wildcard fallback.  This lets an admin safely exclude one user
        while ``Allow all users`` is enabled.
        """
        await self._migrate_legacy_if_needed()
        norm_value = normalize_channel_identity(self._channel, identity_value)
        if not norm_value:
            return None

        if norm_value != CHANNEL_WILDCARD_IDENTITY:
            specific = await self._fetch_grant_entry(norm_value)
            if specific is not None:
                return {
                    **specific,
                    "identity_value": norm_value,
                    "grant_source": "specific",
                }

        wildcard = await self._fetch_active_grant_entry(CHANNEL_WILDCARD_IDENTITY)
        if wildcard is None:
            return None
        return {
            **wildcard,
            "identity_value": norm_value,
            "grant_source": "wildcard",
        }

    async def find_active_grant(self, identity_value: str) -> Optional[Dict[str, Any]]:
        """Lookup grant for a user, falling back to the all-users wildcard grant."""
        await self._migrate_legacy_if_needed()
        effective = await self.find_effective_grant(identity_value)
        if not effective or not effective.get("enabled", True):
            return None
        agents = _normalize_agents(list(effective.get("agents") or []))
        if not agents:
            return None
        return {
            **effective,
            "identity_type": str(effective.get("identity_type") or self._identity_field),
            "agents": agents,
            "enabled": True,
        }

    async def get_document(self) -> Dict[str, Any]:
        """Return the legacy full policy document.

        Administrative list views must use ``list_grants_paginated`` and
        policy/configuration views must use ``get_policy_summary``.  Keeping
        this method only for compatibility avoids silently materializing an
        unbounded grant collection in normal request paths.
        """
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

    async def get_policy_summary(
        self,
        identity_values: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return policy metadata plus only the explicitly requested grants."""
        await self._migrate_legacy_if_needed()
        policy = await self._get_policy_raw()
        identities = identity_values or []
        grants_by_identity = await self.find_grants(identities) if identities else {}
        grants: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw_identity in identities:
            identity = normalize_channel_identity(self._channel, raw_identity)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            grant = grants_by_identity.get(identity)
            if grant is not None:
                grants.append(grant)
        return {
            "account_id": self._scope["account_id"],
            "org_id": self._scope["org_id"],
            "project_id": self._scope["project_id"],
            "channel": self._channel,
            "identity_field": self._identity_field,
            "members_all_agents": bool(policy.get("members_all_agents", True)),
            "grants": grants,
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
        norm_value = normalize_channel_identity(self._channel, identity_value)
        if not norm_value:
            raise ValueError("invalid identity_value")
        norm_agents = _normalize_agents(agents)
        if not norm_agents:
            raise ValueError("at least one agent alias or '*' is required")

        lookup = (
            normalize_channel_identity(self._channel, original_identity)
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
        return await self.get_policy_summary([norm_value])

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
        grant_doc = await self.find_grant(norm_value)
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
        return await self.get_policy_summary([norm_value])

    async def remove_grant(self, identity_value: str) -> Dict[str, Any]:
        await self._migrate_legacy_if_needed()
        norm_value = _normalize_identity_value(self._channel, identity_value)
        await self._mongo_client.initialize()
        await self._grants_coll().delete_one(self._grant_filter(norm_value))
        await self._sync_policy_for_grant(norm_value, [], False)
        return await self.get_policy_summary()

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
        return await self.get_policy_summary([CHANNEL_WILDCARD_IDENTITY])

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
        return await self.get_policy_summary()

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
    "normalize_channel_identity",
]
