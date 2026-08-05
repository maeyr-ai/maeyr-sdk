from __future__ import annotations

import hashlib
from typing import Any

import pytest

from viksa_platform.directory.project_user import (
    api_inbound_customer_user_id,
    field_type_for_connector_source,
    identity_link_keys,
    normalize_field_value,
    normalize_phone,
    source_for_channel,
    verify_scim_secret,
)


class _ScimStore:
    def __init__(self, config: dict[str, Any] | None) -> None:
        self.config = config

    async def get_scim_config(self) -> dict[str, Any] | None:
        return self.config


def test_project_user_identity_policies_are_canonical() -> None:
    assert api_inbound_customer_user_id(" Slack ", "U 123/$") == "api_slack_U_123"
    assert normalize_phone("00 44 20 1234") == "+44201234"
    assert field_type_for_connector_source("connector.phone") == "phone"
    assert field_type_for_connector_source("connector.email") == "email"
    assert source_for_channel("teams") == "connector.email"
    assert source_for_channel("telegram") == "connector.external_user_id"
    assert normalize_field_value("email", " USER@Example.COM ") == "user@example.com"
    assert normalize_field_value("boolean", "yes") is True
    assert normalize_field_value("number", "12.5") == 12.5
    assert identity_link_keys("whatsapp", "1555-123") == {
        ("whatsapp", "1555-123"),
        ("whatsapp", "+1555123"),
    }


@pytest.mark.asyncio
async def test_scim_secret_verification_is_enabled_and_constant_time_compatible() -> None:
    secret = "one-time-scim-secret"
    enabled = _ScimStore(
        {
            "enabled": True,
            "token_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        }
    )
    disabled = _ScimStore({"enabled": False, "token_hash": "unused"})

    assert await verify_scim_secret(enabled, secret) is True
    assert await verify_scim_secret(enabled, "wrong") is False
    assert await verify_scim_secret(disabled, secret) is False
