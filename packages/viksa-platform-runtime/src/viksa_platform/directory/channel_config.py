"""Canonical channel and widget configuration for Directory/Volt."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings


def _production_environment() -> bool:
    return any(
        str(os.environ.get(name) or "").strip().lower() in {"prod", "production"}
        for name in ("APP_ENVIRONMENT", "ENVIRON", "ENV")
    )


def _public_runtime_url(name: str, local_default: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    production = _production_environment()
    if not value:
        if production:
            raise RuntimeError(f"{name} must be set in production")
        value = local_default
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an absolute public URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (production and parsed.scheme != "https")
    ):
        raise RuntimeError(f"{name} must be an absolute public URL")
    return value.rstrip("/")


class ChannelSettings(BaseSettings):
    """Environment-backed channel and public-widget settings."""

    WEBHOOK_BASE_URL: str = _public_runtime_url(
        "VOLT_CHANNEL_WEBHOOK_BASE_URL", "http://localhost:8000"
    )
    WIDGET_JS_BASE_URL: str = _public_runtime_url(
        "VOLT_WIDGET_JS_BASE_URL", "http://localhost:3000/widget/v1"
    )
    WIDGET_SESSION_TTL_SECONDS: int = int(
        os.environ.get("VOLT_WIDGET_SESSION_TTL_SECONDS", str(4 * 3600))
    )
    WIDGET_VISITOR_TTL_SECONDS: int = int(
        os.environ.get("VOLT_WIDGET_VISITOR_TTL_SECONDS", str(15 * 60))
    )
    WIDGET_RATE_LIMIT_PER_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_RATE_LIMIT_PER_MINUTE", "30")
    )
    WIDGET_MESSAGES_PER_SESSION_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_MESSAGES_PER_SESSION_MINUTE", "10")
    )
    WIDGET_SESSION_RATE_LIMIT_PER_IP_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_SESSION_RATE_LIMIT_PER_IP_MINUTE", "5")
    )
    WIDGET_SESSION_RATE_LIMIT_PER_WIDGET_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_SESSION_RATE_LIMIT_PER_WIDGET_MINUTE", "60")
    )
    WIDGET_CONFIG_RATE_LIMIT_PER_IP_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_CONFIG_RATE_LIMIT_PER_IP_MINUTE", "30")
    )
    WIDGET_HISTORY_RATE_LIMIT_PER_SESSION_MINUTE: int = int(
        os.environ.get("VOLT_WIDGET_HISTORY_RATE_LIMIT_PER_SESSION_MINUTE", "20")
    )
    WIDGET_MAX_MESSAGE_LENGTH: int = int(os.environ.get("VOLT_WIDGET_MAX_MESSAGE_LENGTH", "4000"))
    WIDGET_TRUST_PROXY_HEADERS: bool = os.environ.get(
        "VOLT_WIDGET_TRUST_PROXY_HEADERS", "false"
    ).lower() in ("1", "true", "yes")
    WIDGET_PROJECT_MEMBER_BYPASS: bool = os.environ.get(
        "VOLT_WIDGET_PROJECT_MEMBER_BYPASS", "false"
    ).lower() in ("1", "true", "yes")
    WIDGET_INCLUDE_FILE_EVENTS: bool = os.environ.get(
        "VOLT_WIDGET_INCLUDE_FILE_EVENTS", "false"
    ).lower() in ("1", "true", "yes")
    ROUTING_DATABASE: str = os.environ.get("VOLT_CHANNEL_ROUTING_DB", "viksa_channel_routing")
    META_GRAPH_VERSION: str = os.environ.get("VOLT_META_GRAPH_VERSION", "v21.0")
    TELEGRAM_API_BASE: str = os.environ.get(
        "VOLT_TELEGRAM_API_BASE", "https://api.telegram.org"
    ).rstrip("/")


channel_settings = ChannelSettings()

__all__ = ["ChannelSettings", "channel_settings"]
