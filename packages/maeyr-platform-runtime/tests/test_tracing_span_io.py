from __future__ import annotations

import json

from maeyr_platform.tracing.span_io import serialize_trace_value


def test_nested_credentials_are_redacted_without_hiding_safe_token_metrics() -> None:
    serialized = serialize_trace_value(
        {
            "query": "safe",
            "credentials": {
                "access_token": "top-secret-token",
                "password": "top-secret-password",
            },
            "nested": {"client_secret": "client-secret-value"},
            "token_count": 42,
        }
    )
    payload = json.loads(serialized)

    assert payload == {
        "query": "safe",
        "credentials": "<redacted>",
        "nested": {"client_secret": "<redacted>"},
        "token_count": 42,
    }
    assert "top-secret" not in serialized
    assert "client-secret-value" not in serialized


def test_wide_collections_are_bounded_before_json_encoding() -> None:
    mapping = json.loads(
        serialize_trace_value(
            {str(index): index for index in range(1_000)},
            max_chars=20_000,
        )
    )
    sequence = json.loads(serialize_trace_value(list(range(1_000)), max_chars=20_000))

    assert len(mapping) == 51
    assert mapping["<truncated>"] == "<truncated>"
    assert sequence == [*range(50), "<truncated>"]


def test_inline_assignments_authorization_and_uri_credentials_are_redacted() -> None:
    serialized = serialize_trace_value(
        {
            "message": "token=abc123 continue",
            "header": "Authorization: Bearer bearer-secret-value",
            "url": "https://user:password@example.test/path",
            "raw": "sk-abcdefghijklmno",
        }
    )

    assert "abc123" not in serialized
    assert "bearer-secret-value" not in serialized
    assert "user:password" not in serialized
    assert "sk-abcdefghijklmno" not in serialized
    assert "token=<redacted>" in serialized


def test_cycles_and_depth_are_replaced_with_safe_sentinels() -> None:
    cyclic: dict[str, object] = {"safe": True}
    cyclic["self"] = cyclic
    deep: object = "leaf"
    for _ in range(20):
        deep = [deep]

    cyclic_payload = json.loads(serialize_trace_value(cyclic))
    deep_payload = serialize_trace_value(deep)

    assert cyclic_payload == {"safe": True, "self": "<circular>"}
    assert "<truncated>" in deep_payload


def test_final_serialization_respects_small_character_budgets() -> None:
    for limit in (0, 1, 8, 64):
        assert len(serialize_trace_value({"message": "x" * 1_000}, max_chars=limit)) <= limit
