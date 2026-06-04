"""Generate the canonical ``ViksaAI.py`` source injected by the platform."""

from __future__ import annotations

from importlib import resources


def to_module_source() -> str:
    """
    Return the exact ``ViksaAI.py`` body the platform injects into every agent.

    Sourced from ``_canonical_viksai.txt``, kept in lockstep with
    ``tests/fixtures/ViksaAI.py.expected`` via contract tests.
    """
    return resources.files(__package__).joinpath("_canonical_viksai.txt").read_text()
