"""Validated provider defaults for OpenAI-compatible SDK adapters."""

from __future__ import annotations

from typing import Any

from maeyr_platform.llm.models import LLMProvider

_OPENAI_COMPATIBLE_BASE_URLS: dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "https://api.openai.com/v1",
    LLMProvider.ANTHROPIC: "https://api.anthropic.com/v1",
    LLMProvider.GOOGLE_GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
    LLMProvider.MISTRAL: "https://api.mistral.ai/v1",
    LLMProvider.GROQ: "https://api.groq.com/openai/v1",
    LLMProvider.COHERE: "https://api.cohere.com/compatibility/v1",
    LLMProvider.XAI: "https://api.x.ai/v1",
    LLMProvider.DEEPSEEK: "https://api.deepseek.com/v1",
}


def provider_connection_defaults(
    provider: LLMProvider | str,
    *,
    region: str | None = None,
) -> dict[str, Any]:
    """Return non-secret defaults; deployment-specific fields remain explicit."""

    selected = provider if isinstance(provider, LLMProvider) else LLMProvider(provider)
    if selected in _OPENAI_COMPATIBLE_BASE_URLS:
        return {"base_url": _OPENAI_COMPATIBLE_BASE_URLS[selected]}
    if selected is LLMProvider.AWS_BEDROCK:
        if not region:
            return {}
        return {"base_url": f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"}
    return {}
