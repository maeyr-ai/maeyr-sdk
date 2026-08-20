"""Immutable models shared by LLM control-plane and runtime consumers."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class LLMProvider(str, Enum):
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    ANTHROPIC = "anthropic"
    GOOGLE_GEMINI = "google_gemini"
    AWS_BEDROCK = "aws_bedrock"
    GOOGLE_VERTEX = "google_vertex"
    MISTRAL = "mistral"
    GROQ = "groq"
    COHERE = "cohere"
    XAI = "xai"
    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"


class LLMCapability(str, Enum):
    CHAT = "chat"
    ORCHESTRATION = "orchestration"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    TRANSCRIPTION = "transcription"
    SPEECH = "speech"
    IMAGE = "image"


class LLMScopeType(str, Enum):
    ACCOUNT = "account"
    ORGANIZATION = "organization"
    PROJECT = "project"
    PLATFORM = "platform"


class CredentialSource(str, Enum):
    CUSTOMER = "customer"
    PLATFORM = "platform"


class LLMScope(BaseModel):
    """A fully-qualified tenant scope; parent-child consistency is mandatory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=4, max_length=128, pattern=r"^AC-")
    org_id: str | None = Field(default=None, min_length=4, max_length=128, pattern=r"^OI-")
    project_id: str | None = Field(default=None, min_length=4, max_length=128, pattern=r"^PI-")

    @model_validator(mode="after")
    def validate_hierarchy(self) -> "LLMScope":
        if self.project_id and not self.org_id:
            raise ValueError("project scope requires an organization")
        return self


class ResolvedLLMConfiguration(BaseModel):
    """One effective, immutable configuration used for exactly one LLM call.

    Secrets are excluded from repr/serialization by default. Runtime callers
    must explicitly request them when constructing a provider SDK client.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str
    revision: int = Field(ge=1)
    provider: LLMProvider
    source_scope: LLMScopeType
    credential_source: CredentialSource
    models: Mapping[str, str]
    connection: Mapping[str, Any] = Field(default_factory=dict)
    credentials: Mapping[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    credential_fingerprint: str = Field(min_length=12, max_length=128)

    @field_validator("models")
    @classmethod
    def validate_models(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        clean = {
            str(key): str(model).strip()
            for key, model in value.items()
            if str(model).strip()
        }
        if LLMCapability.CHAT.value not in clean:
            raise ValueError("chat model is required")
        return MappingProxyType(clean)

    @field_validator("connection")
    @classmethod
    def freeze_connection(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_validator("credentials")
    @classmethod
    def freeze_credentials(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        clean = {str(key): str(secret) for key, secret in value.items() if str(secret)}
        return MappingProxyType(clean)

    @field_serializer("models", "connection")
    def serialize_public_mappings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    @model_validator(mode="after")
    def validate_source(self) -> "ResolvedLLMConfiguration":
        if self.source_scope is LLMScopeType.PLATFORM:
            if self.credential_source is not CredentialSource.PLATFORM:
                raise ValueError("platform scope requires platform credentials")
        elif self.credential_source is not CredentialSource.CUSTOMER:
            raise ValueError("tenant scope requires customer credentials")
        return self

    def model_for(self, capability: LLMCapability | str) -> str:
        """Return the configured model without crossing the billing boundary.

        Orchestration and vision may deliberately share the tenant's chat
        model. Other capabilities require an explicit model so an incomplete
        BYOLLM setup cannot silently fall back to a billable platform model.
        """

        key = capability.value if isinstance(capability, LLMCapability) else str(capability)
        value = self.models.get(key)
        if value:
            return value
        if key in {LLMCapability.ORCHESTRATION.value, LLMCapability.VISION.value}:
            return self.models[LLMCapability.CHAT.value]
        from viksa_platform.llm.errors import LLMConfigurationError

        raise LLMConfigurationError(
            f"{key} model is not configured for {self.source_scope.value} {self.provider.value}"
        )

    @property
    def billable_to_customer(self) -> bool:
        return self.credential_source is CredentialSource.PLATFORM

    @property
    def client_cache_key(self) -> tuple[str, int, str]:
        return self.config_id, self.revision, self.credential_fingerprint

    def provider_client_kwargs(self) -> dict[str, Any]:
        """Build explicit SDK arguments; callers must never log the result."""

        return {**dict(self.connection), **dict(self.credentials)}
