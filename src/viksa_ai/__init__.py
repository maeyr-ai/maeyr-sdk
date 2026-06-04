"""Viksa AI platform SDK."""

from typing import TYPE_CHECKING

__version__ = "0.1.0"

__all__ = [
    "ViksaApiError",
    "ViksaClient",
    "__version__",
]


def __getattr__(name: str):
    if name == "ViksaClient":
        from viksa_ai.client import ViksaClient

        return ViksaClient
    if name == "ViksaApiError":
        from viksa_ai.client import ViksaApiError

        return ViksaApiError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from viksa_ai.client import ViksaApiError, ViksaClient
