"""Canonical channel and widget configuration for Directory/Volt."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings


class ChannelSettings(BaseSettings):
    """Environment-backed channel and public-widget settings."""

    WEBHOOK_BASE_URL: str = os.environ.get(
        "VOLT_CHANNEL_WEBHOOK_BASE_URL", "https://api.viksaai.com"
    ).rstrip("/")
    WIDGET_JS_BASE_URL: str = os.environ.get(
        "VOLT_WIDGET_JS_BASE_URL", "https://app.viksaai.com/widget/v1"
    ).rstrip("/")
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
    WIDGET_MAX_MESSAGE_LENGTH: int = int(
        os.environ.get("VOLT_WIDGET_MAX_MESSAGE_LENGTH", "4000")
    )
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
