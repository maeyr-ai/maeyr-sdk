"""Generate the canonical ``Maeyr.py`` source injected by the platform."""

from __future__ import annotations

from importlib import resources


def to_module_source() -> str:
    """
    Return the exact ``Maeyr.py`` body the platform injects into every agent.

    Sourced from ``_canonical_maeyr.txt``, kept in lockstep with
    ``tests/fixtures/Maeyr.py.expected`` via contract tests.
    """
    return resources.files(__package__).joinpath("_canonical_maeyr.txt").read_text()
