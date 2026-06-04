import httpx
import pytest

from viksa_ai.client.errors import (
    ViksaAuthenticationError,
    ViksaNotFoundError,
    ViksaRateLimitError,
    ViksaValidationError,
    parse_error_details,
    raise_for_response,
)


def test_parse_string_detail():
    assert parse_error_details({"detail": "Not allowed"})[0].message == "Not allowed"


def test_parse_validation_list():
    body = {
        "detail": [
            {"loc": ["body", "email"], "msg": "field required", "type": "value_error.missing"}
        ]
    }
    details = parse_error_details(body)
    assert len(details) == 1
    assert "required" in details[0].message
    assert details[0].field == "email"


def test_raise_for_response_maps_types():
    response = httpx.Response(404, json={"detail": "Agent not found"})
    with pytest.raises(ViksaNotFoundError) as exc:
        raise_for_response(response, service="builder", method="GET", path="/agent/x")
    assert exc.value.status_code == 404
    assert exc.value.detail_message == "Agent not found"


def test_raise_401_authentication():
    response = httpx.Response(401, json={"detail": "Invalid token"})
    with pytest.raises(ViksaAuthenticationError):
        raise_for_response(response, service="auth", method="GET", path="/me")


def test_raise_422_validation():
    response = httpx.Response(
        422,
        json={
            "detail": [{"loc": ["body", "name"], "msg": "too short", "type": "string_too_short"}]
        },
    )
    with pytest.raises(ViksaValidationError) as exc:
        raise_for_response(response, service="builder", method="POST", path="/agent/create")
    assert len(exc.value.details) == 1


def test_raise_429_retry_after():
    response = httpx.Response(
        429,
        json={"detail": "Rate limited"},
        headers={"Retry-After": "2"},
    )
    with pytest.raises(ViksaRateLimitError) as exc:
        raise_for_response(response, service="chat", method="POST", path="/chat/message")
    assert exc.value.retry_after == 2.0
