"""Dependency-free secret-strength classification for service-owned policy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_PLACEHOLDER_TOKENS = frozenset(
    {
        "changeme",
        "dummy",
        "example",
        "fake",
        "password",
        "placeholder",
        "replace",
        "secret",
        "test",
        "your",
    }
)


@dataclass(frozen=True, slots=True)
class SecretStrengthPolicy:
    """Classify obvious placeholders without reading environment state."""

    minimum_length: int
    minimum_unique_characters: int = 6
    placeholder_tokens: frozenset[str] = field(
        default=DEFAULT_PLACEHOLDER_TOKENS,
    )

    def __post_init__(self) -> None:
        if self.minimum_length < 1:
            raise ValueError("minimum_length must be positive")
        if self.minimum_unique_characters < 1:
            raise ValueError("minimum_unique_characters must be positive")
        if any(not token or token != token.lower() for token in self.placeholder_tokens):
            raise ValueError("placeholder_tokens must be non-empty lowercase values")

    def rejects(self, value: str) -> bool:
        """Return true for a short, low-entropy, or placeholder-like value."""

        clean = value.strip()
        tokens = frozenset(token for token in re.split(r"[^a-z0-9]+", clean.lower()) if token)
        return (
            len(clean) < self.minimum_length
            or not self.placeholder_tokens.isdisjoint(tokens)
            or len(set(clean)) < self.minimum_unique_characters
        )
