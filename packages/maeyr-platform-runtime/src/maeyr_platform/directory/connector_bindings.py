"""Canonical connector-binding catalog and resolution policy."""

from __future__ import annotations

from typing import Any, Dict, List

CONNECTOR_KEY_CATALOG: Dict[str, Dict[str, Any]] = {
    "whatsapp": {
        "fact": "connector.phone",
        "validation": "phone",
        "label": "WhatsApp number",
        "suggest_fields": ["phone", "mobile", "mobilephone", "mobile_phone", "whatsapp", "cell"],
    },
    "slack": {
        "fact": "connector.email",
        "validation": "email",
        "label": "Slack email",
        "suggest_fields": ["email", "work_email", "mail", "user_email"],
    },
    "teams": {
        "fact": "connector.email",
        "validation": "email",
        "label": "Teams email",
        "suggest_fields": ["email", "work_email", "mail", "user_email"],
    },
    "telegram": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Telegram user ID",
        "suggest_fields": ["telegram_id", "telegram_user_id", "telegram", "telegramid"],
    },
    "instagram": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Instagram ID",
        "suggest_fields": ["instagram_id", "instagram"],
    },
    "messenger": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Messenger ID",
        "suggest_fields": ["messenger_id", "facebook_id", "psid"],
    },
    "discord": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Discord user ID",
        "suggest_fields": ["discord_id", "discord_user_id"],
    },
    "sms": {
        "fact": "connector.phone",
        "validation": "phone",
        "label": "SMS phone",
        "suggest_fields": ["phone", "mobile", "mobilephone", "cell"],
    },
    "email": {
        "fact": "connector.email",
        "validation": "email",
        "label": "Email address",
        "suggest_fields": ["email", "mail"],
    },
    "line": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "LINE user ID",
        "suggest_fields": ["line_id", "line_user_id"],
    },
    "viber": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Viber user ID",
        "suggest_fields": ["viber_id"],
    },
    "google_chat": {
        "fact": "connector.external_user_id",
        "validation": "string",
        "label": "Google Chat user ID",
        "suggest_fields": ["google_chat_id", "google_id"],
    },
    "web_widget": {
        "fact": "connector.email",
        "validation": "email",
        "label": "Web chat email",
        "suggest_fields": ["email", "mail"],
        "required": False,
    },
}


def catalog_entry(channel: str) -> Dict[str, Any] | None:
    return CONNECTOR_KEY_CATALOG.get((channel or "").strip().lower())


DEFAULT_CONNECTOR_BINDINGS: List[Dict[str, Any]] = [
    {
        "channel": ch,
        "profile_field": f"{ch}_primary_key"
        if ch not in ("web_widget",)
        else "web_widget_primary_key",
        "source": meta["fact"],
        "required": meta.get("required", True),
        "label": meta.get("label", ch),
    }
    for ch, meta in CONNECTOR_KEY_CATALOG.items()
]


def default_binding_for_channel(channel: str) -> Dict[str, Any] | None:
    entry = catalog_entry(channel)
    if not entry:
        return None
    ch = (channel or "").strip().lower()
    return {
        "channel": ch,
        "profile_field": f"{ch}_primary_key" if ch != "web_widget" else "web_widget_primary_key",
        "source": entry["fact"],
        "required": entry.get("required", True),
        "label": entry.get("label", ch),
    }


def resolve_connector_binding(
    schema: Dict[str, Any] | None,
    channel: str,
) -> Dict[str, Any] | None:
    ch = (channel or "").strip().lower()
    if schema:
        for raw in schema.get("connector_bindings") or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("channel") or "").strip().lower() == ch:
                return {
                    "channel": ch,
                    "profile_field": str(raw.get("profile_field") or ""),
                    "source": str(raw.get("source") or "connector.external_user_id"),
                    "required": bool(raw.get("required", True)),
                    "label": str(raw.get("label") or ""),
                }
        for raw in schema.get("match_rules") or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("channel") or "").strip().lower() == ch:
                return {
                    "channel": ch,
                    "profile_field": str(raw.get("field") or ""),
                    "source": str(raw.get("source") or "connector.external_user_id"),
                    "required": True,
                    "label": "",
                }
    return default_binding_for_channel(ch)


def bindings_to_match_rules(bindings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        ch = str(binding.get("channel") or "").strip().lower()
        field = str(binding.get("profile_field") or "").strip()
        source = str(binding.get("source") or "").strip()
        if not ch or not field or not source:
            continue
        key = (ch, field)
        if key in seen:
            continue
        seen.add(key)
        rules.append({"channel": ch, "field": field, "source": source, "fallback": False})
    return rules
