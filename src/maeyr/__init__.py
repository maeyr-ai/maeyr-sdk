"""Maeyr platform SDK."""

from typing import TYPE_CHECKING, Any

__version__ = "0.2.8"

__all__ = [
    "MaeyrApiError",
    "MaeyrAuthenticationError",
    "MaeyrClient",
    "MaeyrNotFoundError",
    "MaeyrRateLimitError",
    "MaeyrStreamError",
    "MaeyrTransportError",
    "MaeyrValidationError",
    "WebhookClient",
    "__version__",
]


def __getattr__(name: str) -> Any:
    if name == "MaeyrClient":
        from maeyr.client import MaeyrClient

        return MaeyrClient
    if name == "WebhookClient":
        from maeyr.client import WebhookClient

        return WebhookClient
    for exc_name, exc_path in (
        ("MaeyrApiError", "MaeyrApiError"),
        ("MaeyrAuthenticationError", "MaeyrAuthenticationError"),
        ("MaeyrNotFoundError", "MaeyrNotFoundError"),
        ("MaeyrRateLimitError", "MaeyrRateLimitError"),
        ("MaeyrStreamError", "MaeyrStreamError"),
        ("MaeyrTransportError", "MaeyrTransportError"),
        ("MaeyrValidationError", "MaeyrValidationError"),
    ):
        if name == exc_name:
            from maeyr.client import errors as err

            return getattr(err, exc_path)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from maeyr.client import (
        MaeyrApiError,
        MaeyrAuthenticationError,
        MaeyrClient,
        MaeyrNotFoundError,
        MaeyrRateLimitError,
        MaeyrStreamError,
        MaeyrTransportError,
        MaeyrValidationError,
        WebhookClient,
    )
