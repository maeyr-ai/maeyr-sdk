from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from viksa_platform.security.widget_logo import normalize_logo_url, sanitize_logo_svg


class WebhookChannelType(str, Enum):
    """Channels provisioned via shared HTTPS webhook ingress (Tier 1)."""

    SLACK = "slack"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    TEAMS = "teams"
    INSTAGRAM = "instagram"
    WEB_WIDGET = "web_widget"


class ChannelTier(str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"


# Primary identity field used for access grants per channel.
CHANNEL_IDENTITY_FIELD: Dict[str, str] = {
    WebhookChannelType.SLACK.value: "email",
    WebhookChannelType.WHATSAPP.value: "phone_e164",
    WebhookChannelType.TELEGRAM.value: "telegram_user_id",
    WebhookChannelType.TEAMS.value: "email",
    WebhookChannelType.INSTAGRAM.value: "instagram_scoped_id",
    WebhookChannelType.WEB_WIDGET.value: "email",
}

# Shared access-control model for every Tier 1 webhook connector.
CHANNEL_ACCESS_MODEL = "channel_grants_v1"
# Grant identity matching any external user on the channel (same for all connectors).
CHANNEL_WILDCARD_IDENTITY = "*"
# Agent alias in a grant matching every deployed agent (same for all connectors).
CHANNEL_ALL_AGENTS_WILDCARD = "*"

CHANNEL_ACCESS_COLLECTION: Dict[str, str] = {
    WebhookChannelType.SLACK.value: "volt_slack_access",
    WebhookChannelType.WHATSAPP.value: "volt_whatsapp_access",
    WebhookChannelType.TELEGRAM.value: "volt_telegram_access",
    WebhookChannelType.TEAMS.value: "volt_teams_access",
    WebhookChannelType.INSTAGRAM.value: "volt_instagram_access",
    WebhookChannelType.WEB_WIDGET.value: "volt_web_widget_access",
}


class SlackCredentials(BaseModel):
    """HTTPS Event Subscriptions — no Socket Mode app token required."""

    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=8)
    signing_secret: str = Field(min_length=8)
    bot_user_id: Optional[str] = None
    app_id: Optional[str] = None


class WhatsAppCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=8)
    phone_number_id: str = Field(min_length=1)
    waba_id: Optional[str] = None
    app_secret: str = Field(min_length=8)
    verify_token: str = Field(min_length=8)
    display_phone: Optional[str] = None


class TelegramCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=8)
    bot_username: Optional[str] = None


class TeamsCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=8)
    app_password: str = Field(min_length=8)
    tenant_id: Optional[str] = None


class InstagramCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=8)
    instagram_account_id: str = Field(min_length=1)
    page_id: Optional[str] = None
    app_secret: str = Field(min_length=8)
    verify_token: str = Field(min_length=8)
    username: Optional[str] = None


class WidgetAppearance(BaseModel):
    """Public widget chrome — validated, no raw CSS."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Chat with us", min_length=1, max_length=80)
    subtitle: Optional[str] = Field(default=None, max_length=120)
    primary_color: str = Field(default="#d4e633", max_length=7)
    position: str = Field(default="bottom-right")
    greeting: Optional[str] = Field(default=None, max_length=500)
    placeholder: str = Field(default="Type a message…", max_length=120)
    show_branding: bool = True
    dark_mode: str = Field(default="auto")
    logo_url: Optional[str] = Field(default=None, max_length=2048)
    logo_svg: Optional[str] = Field(default=None, max_length=12000)

    @field_validator("logo_url")
    @classmethod
    def _logo_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        return normalize_logo_url(str(v))

    @field_validator("logo_svg")
    @classmethod
    def _logo_svg(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        return sanitize_logo_svg(str(v))

    @field_validator("primary_color")
    @classmethod
    def _hex_color(cls, v: str) -> str:
        s = (v or "").strip()
        if not re.match(r"^#[0-9A-Fa-f]{6}$", s):
            raise ValueError("primary_color must be #RRGGBB")
        return s.lower()

    @field_validator("position")
    @classmethod
    def _position(cls, v: str) -> str:
        p = (v or "bottom-right").strip().lower()
        if p not in ("bottom-right", "bottom-left"):
            raise ValueError("position must be bottom-right or bottom-left")
        return p

    @field_validator("dark_mode")
    @classmethod
    def _dark_mode(cls, v: str) -> str:
        d = (v or "auto").strip().lower()
        if d not in ("auto", "light", "dark"):
            raise ValueError("dark_mode must be auto, light, or dark")
        return d

    @model_validator(mode="after")
    def _logo_exclusive(self) -> "WidgetAppearance":
        if self.logo_url and self.logo_svg:
            raise ValueError("Provide logo_url or logo_svg, not both")
        return self


class WebWidgetCredentials(BaseModel):
    """Embeddable web chat — secrets generated on install if omitted."""

    model_config = ConfigDict(extra="forbid")

    allowed_origins: List[str] = Field(min_length=1, max_length=20)
    allowed_email_domains: List[str] = Field(default_factory=list, max_length=20)
    widget_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    widget_secret: Optional[str] = Field(default=None, min_length=16, max_length=128)
    appearance: WidgetAppearance = Field(default_factory=WidgetAppearance)

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        raise ValueError("allowed_origins required")

    @field_validator("allowed_email_domains", mode="before")
    @classmethod
    def _parse_email_domains(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip().lstrip("@").lower() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return [str(x).strip().lstrip("@").lower() for x in v if str(x).strip()]
        raise ValueError("allowed_email_domains must be a list")


class ChannelAccessGrant(BaseModel):
    """One access grant. ``identity_value`` may be ``*`` (all users) or a channel-specific ID."""

    identity_type: str = Field(min_length=1)
    identity_value: str = Field(min_length=1)
    agents: List[str] = Field(default_factory=list)
    enabled: bool = True
    expires_at: Optional[str] = None
    customer_user_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_enabled: Optional[bool] = None
    effective_enabled: Optional[bool] = None
    data_source: Optional[Dict[str, Any]] = None


class ChannelAccessPolicyResponse(BaseModel):
    account_id: str
    org_id: str
    project_id: str
    channel: str
    members_all_agents: bool = True
    grants: List[ChannelAccessGrant] = Field(default_factory=list)
    identity_field: str = ""
    access_model: str = CHANNEL_ACCESS_MODEL
    wildcard_identity: str = CHANNEL_WILDCARD_IDENTITY
    all_agents_wildcard: str = CHANNEL_ALL_AGENTS_WILDCARD


class ChannelConnectionStatusResponse(BaseModel):
    account_id: str
    org_id: str
    project_id: str
    channel: str
    installed: bool = False
    lifecycle_state: str = "uninstalled"
    routing_key: Optional[str] = None
    webhook_url: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)
    last_error: Optional[str] = None
    updated_at: Optional[str] = None
