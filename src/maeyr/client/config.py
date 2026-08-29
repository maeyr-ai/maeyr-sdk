"""HTTP client configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass
class RetryConfig:
    """Retry policy for transient API and transport failures."""

    max_retries: int = 3
    backoff_factor: float = 0.5
    max_backoff_seconds: float = 30.0
    retry_status_codes: FrozenSet[int] = field(
        default_factory=lambda: frozenset({429, 502, 503, 504})
    )
    retry_on_connection_errors: bool = True


@dataclass
class ClientConfig:
    """Top-level Maeyr HTTP client options."""

    timeout: float = 60.0
    retry: RetryConfig = field(default_factory=RetryConfig)
    auto_refresh_on_401: bool = True
    user_agent: str = "maeyr-python/0.2.0"
    idempotency_key: Optional[str] = None
