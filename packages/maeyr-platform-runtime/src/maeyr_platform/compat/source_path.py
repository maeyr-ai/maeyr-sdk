"""Narrow compatibility helper for optional monorepo source packages."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path


def ensure_resolved_source_path(resolve: Callable[[], Path]) -> None:
    """Prepend a resolved directory once when a local source checkout exists."""

    source = resolve()
    source_text = str(source)
    if source.is_dir() and source_text not in sys.path:
        sys.path.insert(0, source_text)


__all__ = ["ensure_resolved_source_path"]
