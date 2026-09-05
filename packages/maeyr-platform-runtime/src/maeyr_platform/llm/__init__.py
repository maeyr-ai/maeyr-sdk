"""Provider-neutral LLM configuration and client lifecycle contracts."""

from maeyr_platform.llm.auth_resolver import AuthLLMConfigurationResolver
from maeyr_platform.llm.client import UniversalLLMClient
from maeyr_platform.llm.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMProviderError,
    LLMRateLimitError,
    normalize_provider_error,
)
from maeyr_platform.llm.models import (
    CredentialSource,
    LLMCapability,
    LLMProvider,
    LLMScope,
    LLMScopeType,
    ResolvedLLMConfiguration,
)
from maeyr_platform.llm.providers import provider_connection_defaults
from maeyr_platform.llm.resolver import select_effective_configuration

__all__ = [
    "CredentialSource",
    "AuthLLMConfigurationResolver",
    "LLMAuthenticationError",
    "LLMCapability",
    "LLMConfigurationError",
    "LLMProvider",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMScope",
    "LLMScopeType",
    "ResolvedLLMConfiguration",
    "UniversalLLMClient",
    "normalize_provider_error",
    "provider_connection_defaults",
    "select_effective_configuration",
]
