"""Stable, secret-safe errors for provider SDK failures."""

from __future__ import annotations


class LLMProviderError(RuntimeError):
    """Base error safe to return through product APIs."""

    code = "llm_provider_error"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class LLMConfigurationError(LLMProviderError):
    code = "llm_configuration_error"


class LLMAuthenticationError(LLMProviderError):
    code = "llm_authentication_failed"


class LLMRateLimitError(LLMProviderError):
    code = "llm_rate_limited"


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def normalize_provider_error(
    exc: BaseException,
    *,
    provider: str,
    credential_source: str,
    source_scope: str,
) -> LLMProviderError:
    """Translate SDK-specific failures without leaking response bodies/keys."""

    if isinstance(exc, LLMProviderError):
        return exc
    status = _status_code(exc)
    location = "Maeyr platform" if credential_source == "platform" else source_scope
    if status in {401, 403} or type(exc).__name__ in {
        "AuthenticationError",
        "PermissionDeniedError",
    }:
        return LLMAuthenticationError(
            f"{provider} rejected the {location} LLM credentials; verify or rotate them",
            provider=provider,
        )
    if status == 429 or type(exc).__name__ == "RateLimitError":
        return LLMRateLimitError(
            f"{provider} rate limit was reached; retry after the provider delay",
            provider=provider,
        )
    if status is not None and status >= 500:
        return LLMProviderError(
            f"{provider} is temporarily unavailable",
            provider=provider,
        )
    return LLMProviderError(f"{provider} LLM request failed", provider=provider)
