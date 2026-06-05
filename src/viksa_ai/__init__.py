"""Viksa AI platform SDK."""

from typing import TYPE_CHECKING

__version__ = "0.2.4"

__all__ = [
    "ViksaApiError",
    "ViksaAuthenticationError",
    "ViksaClient",
    "ViksaNotFoundError",
    "ViksaRateLimitError",
    "ViksaTransportError",
    "ViksaValidationError",
    "WebhookClient",
    "__version__",
]


def __getattr__(name: str):
    if name == "ViksaClient":
        from viksa_ai.client import ViksaClient

        return ViksaClient
    if name == "WebhookClient":
        from viksa_ai.client import WebhookClient

        return WebhookClient
    for exc_name, exc_path in (
        ("ViksaApiError", "ViksaApiError"),
        ("ViksaAuthenticationError", "ViksaAuthenticationError"),
        ("ViksaNotFoundError", "ViksaNotFoundError"),
        ("ViksaRateLimitError", "ViksaRateLimitError"),
        ("ViksaTransportError", "ViksaTransportError"),
        ("ViksaValidationError", "ViksaValidationError"),
    ):
        if name == exc_name:
            from viksa_ai.client import errors as err

            return getattr(err, exc_path)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from viksa_ai.client import (
        ViksaApiError,
        ViksaAuthenticationError,
        ViksaClient,
        ViksaNotFoundError,
        ViksaRateLimitError,
        ViksaTransportError,
        ViksaValidationError,
        WebhookClient,
    )
