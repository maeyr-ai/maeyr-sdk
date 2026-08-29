from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from maeyr_platform.directory.channel_access import (
    CHANNEL_WILDCARD_IDENTITY,
    ChannelAccessStoreBase,
    normalize_channel_identity,
)
from maeyr_platform.directory.invoker_cache import _channel_access_key


def test_connector_identity_normalization_is_canonical() -> None:
    assert normalize_channel_identity("whatsapp", "+91 70105-61869") == "+917010561869"
    assert normalize_channel_identity("whatsapp", "00917010561869") == "+917010561869"
    assert normalize_channel_identity("telegram", "@SomeUser") == "@someuser"
    assert normalize_channel_identity("teams", "AAD-OBJECT-ID") == "aad-object-id"
    assert normalize_channel_identity("slack", "USER@EXAMPLE.COM") == "user@example.com"
    assert normalize_channel_identity("web_chat", "Visitor") == "Visitor"
    assert normalize_channel_identity("web_widget", "USER@EXAMPLE.COM") == "user@example.com"


@pytest.mark.asyncio
async def test_disabled_specific_grant_blocks_wildcard_fallback() -> None:
    store = object.__new__(ChannelAccessStoreBase)
    store._channel = "whatsapp"
    store._migrate_legacy_if_needed = AsyncMock()
    store._fetch_grant_entry = AsyncMock(
        return_value={
            "identity_value": "+15551234567",
            "agents": ["blocked-agent"],
            "enabled": False,
        }
    )
    store._fetch_active_grant_entry = AsyncMock(
        return_value={
            "identity_value": CHANNEL_WILDCARD_IDENTITY,
            "agents": ["allowed-agent"],
            "enabled": True,
        }
    )

    result = await store.find_effective_grant("+1 (555) 123-4567")

    assert result is not None
    assert result["enabled"] is False
    assert result["grant_source"] == "specific"
    store._fetch_active_grant_entry.assert_not_awaited()


def test_redis_channel_access_key_hides_connector_pii_and_is_tenant_isolated() -> None:
    phone = "+917010561869"
    first = _channel_access_key("acct-a", "org", "project", "whatsapp", phone)
    second = _channel_access_key("acct-b", "org", "project", "whatsapp", phone)

    assert phone not in first
    assert first != second
    assert first.startswith("volt:channel_access:acct-a:org:project:whatsapp:")
