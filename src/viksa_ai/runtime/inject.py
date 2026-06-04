"""Generate the canonical ``ViksaAI.py`` source injected by the platform."""

from __future__ import annotations

from pathlib import Path

_CANONICAL_PATH = Path(__file__).with_name("_canonical_viksai.txt")


def to_module_source() -> str:
    """
    Return the exact ``ViksaAI.py`` body the platform injects into every agent.

    Sourced from ``_canonical_viksai.txt``, kept in lockstep with
    ``tests/fixtures/ViksaAI.py.expected`` via contract tests.
    """
    return _CANONICAL_PATH.read_text()
