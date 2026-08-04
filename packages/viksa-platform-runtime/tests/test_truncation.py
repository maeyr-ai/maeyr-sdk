from __future__ import annotations

import json

from viksa_platform.truncation import DEFAULT_SYNTHESIS_BUDGET, smart_truncate


def _records(count: int) -> dict[str, object]:
    return {
        "result": {
            "items": [
                {"id": index, "body": "x" * 300}
                for index in range(count)
            ]
        }
    }


def test_passthrough_and_plain_string_contracts() -> None:
    assert json.loads(smart_truncate({"ok": True})) == {"ok": True}
    assert smart_truncate(None) == ""
    assert smart_truncate("short", max_chars=20) == "short"
    assert "[truncated" in smart_truncate("x" * 100, max_chars=10)


def test_nested_list_is_bounded_as_valid_json_with_explanatory_note() -> None:
    result = smart_truncate(_records(100), max_chars=4_000)

    assert len(result) <= 4_000
    parsed = json.loads(result)
    items = parsed["result"]["items"]
    assert 3 <= len(items) < 100
    assert all(isinstance(item, dict) for item in items)
    assert "Showing" in parsed["result"]["_items_truncation_note"]


def test_top_level_list_keeps_whole_items_and_remains_valid_json() -> None:
    value = [{"id": index, "body": "y" * 250} for index in range(80)]
    result = smart_truncate(value, max_chars=3_000)

    assert len(result) <= 3_000
    parsed = json.loads(result)
    assert 3 <= len(parsed) < len(value)
    assert all(isinstance(item, dict) for item in parsed)


def test_default_budget_remains_legacy_compatible() -> None:
    assert DEFAULT_SYNTHESIS_BUDGET == 60_000
