"""Pure project-user identity and schema policies shared by Directory and Volt."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from maeyr_platform.directory.channel_access import _normalize_identity_value

_UNSAFE_IDENTIFIER_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def api_inbound_customer_user_id(channel: str, connector_user_id: str) -> str:
    """Build the stable ID used for connector self-registered customers."""
    normalized_channel = (channel or "").strip().lower()
    external_id = (connector_user_id or "").strip()
    clean_external_id = _UNSAFE_IDENTIFIER_CHARACTER.sub("_", external_id)
    clean_external_id = _REPEATED_UNDERSCORE.sub("_", clean_external_id).strip("_")
    clean_channel = _UNSAFE_IDENTIFIER_CHARACTER.sub("_", normalized_channel)
    clean_channel = _REPEATED_UNDERSCORE.sub("_", clean_channel).strip("_")
    return f"api_{clean_channel or 'channel'}_{clean_external_id or 'user'}"


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        return f"+{digits}" if digits else ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    return f"+{digits}" if digits else ""


def field_type_for_connector_source(source: str) -> str:
    normalized = (source or "").strip().lower()
    if "phone" in normalized:
        return "phone"
    if "email" in normalized:
        return "email"
    return "string"


def source_for_channel(channel: str) -> str:
    normalized = (channel or "").strip().lower()
    if normalized in ("whatsapp", "sms"):
        return "connector.phone"
    if normalized in ("slack", "teams", "web_widget"):
        return "connector.email"
    return "connector.external_user_id"


def normalize_field_value(field_type: str, value: Any) -> Any:
    if value is None:
        return None
    if field_type == "email":
        return normalize_email(str(value))
    if field_type == "phone":
        return normalize_phone(str(value))
    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes")
    if field_type == "number":
        try:
            if "." in str(value):
                return float(value)
            return int(value)
        except (TypeError, ValueError):
            return value
    return str(value).strip()


def identity_link_keys(channel: str, external_id: str) -> set[tuple[str, str]]:
    """Return raw and canonical keys used to compare connector identity links."""
    keys = {(channel, external_id)}
    normalized = _normalize_identity_value(channel, external_id)
    if normalized != external_id:
        keys.add((channel, normalized))
    return keys


async def verify_scim_secret(store: Any, secret: str) -> bool:
    """Verify a SCIM secret against an enabled store configuration in constant time."""
    config = await store.get_scim_config()
    if not config or not config.get("enabled"):
        return False
    token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return hmac.compare_digest(token_hash, str(config.get("token_hash") or ""))


__all__ = [
    "api_inbound_customer_user_id",
    "field_type_for_connector_source",
    "identity_link_keys",
    "normalize_email",
    "normalize_field_value",
    "normalize_phone",
    "source_for_channel",
    "verify_scim_secret",
]
