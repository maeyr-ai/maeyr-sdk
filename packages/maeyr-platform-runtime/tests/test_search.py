from __future__ import annotations

import re

import pytest

from maeyr_platform.search import literal_search_pattern


def test_literal_search_escapes_regex_control_characters() -> None:
    pattern = literal_search_pattern("  a.*(b)+  ")

    assert re.fullmatch(pattern, "a.*(b)+") is not None
    assert re.fullmatch(pattern, "axxxb") is None


def test_literal_search_is_bounded_and_empty_safe() -> None:
    assert literal_search_pattern("abcdef", max_length=3) == "abc"
    assert literal_search_pattern("   ") == ""
    assert literal_search_pattern(None) == ""


@pytest.mark.parametrize("value", [0, -1, 1025, True, 1.5])
def test_literal_search_rejects_invalid_bounds(value: object) -> None:
    with pytest.raises(ValueError):
        literal_search_pattern("query", max_length=value)  # type: ignore[arg-type]
