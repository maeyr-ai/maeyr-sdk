"""Bounded literal-search helpers for tenant-facing persistence queries."""

from __future__ import annotations

import re


def literal_search_pattern(value: object, *, max_length: int = 200) -> str:
    """Return a bounded regex pattern that treats user input as plain text.

    Mongo regex queries are useful for small tenant-scoped search surfaces, but
    passing caller text through as a regex permits expensive expressions and
    changes the meaning of characters such as ``.`` and ``*``.  Centralizing
    the conversion keeps every service on the same fail-safe contract.
    """

    if type(max_length) is not int or not 1 <= max_length <= 1024:
        raise ValueError("max_length must be an integer between 1 and 1024")
    if not isinstance(value, str):
        return ""
    normalized = value.strip()[:max_length]
    return re.escape(normalized) if normalized else ""


__all__ = ["literal_search_pattern"]
