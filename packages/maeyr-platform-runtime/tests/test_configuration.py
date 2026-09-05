from __future__ import annotations

import pytest

from maeyr_platform.configuration import (
    LOCAL_BROWSER_ORIGINS,
    allowed_origins_from_env,
    deployment_service_url,
)


def test_allowed_origins_uses_local_defaults_outside_production() -> None:
    assert allowed_origins_from_env("development", environ={}) == list(LOCAL_BROWSER_ORIGINS)


def test_allowed_origins_requires_explicit_production_configuration() -> None:
    with pytest.raises(RuntimeError, match="required in production"):
        allowed_origins_from_env("production", environ={})


def test_allowed_origins_normalizes_only_trailing_slashes() -> None:
    assert allowed_origins_from_env(
        "production",
        environ={"ALLOWED_ORIGINS": '["https://app.example.com/"]'},
    ) == ["https://app.example.com"]


@pytest.mark.parametrize(
    "origins",
    [
        '["http://app.example.com"]',
        '["https://user:secret@app.example.com"]',
        '["https://app.example.com/path"]',
        '["https://app.example.com?query=yes"]',
        '["https://app.example.com#fragment"]',
        '["https://app.example.com:invalid"]',
        '["https://app.example.com", "https://app.example.com/"]',
    ],
)
def test_allowed_origins_rejects_unsafe_production_values(origins: str) -> None:
    with pytest.raises(RuntimeError):
        allowed_origins_from_env(
            "production",
            environ={"ALLOWED_ORIGINS": origins},
        )


@pytest.mark.parametrize("raw", ["not-json", "{}", "[]", '[""]'])
def test_allowed_origins_rejects_malformed_configuration(raw: str) -> None:
    with pytest.raises(RuntimeError):
        allowed_origins_from_env(
            "development",
            environ={"ALLOWED_ORIGINS": raw},
        )


def test_deployment_service_url_uses_local_default() -> None:
    assert (
        deployment_service_url(
            "AUTH_SERVICE_URL",
            environment="development",
            local_port=8000,
            environ={},
        )
        == "http://localhost:8000"
    )


def test_deployment_service_url_requires_production_configuration() -> None:
    with pytest.raises(RuntimeError, match="required in production"):
        deployment_service_url(
            "AUTH_SERVICE_URL",
            environment="production",
            local_port=8000,
            environ={},
        )


def test_deployment_service_url_normalizes_trailing_slash() -> None:
    assert (
        deployment_service_url(
            "AUTH_SERVICE_URL",
            environment="production",
            local_port=8000,
            environ={"AUTH_SERVICE_URL": "https://auth.internal/"},
        )
        == "https://auth.internal"
    )


@pytest.mark.parametrize(
    "value",
    [
        "auth.internal",
        "https://user:secret@auth.internal",
        "https://auth.internal/path",
        "https://auth.internal?query=yes",
        "https://auth.internal#fragment",
        "https://auth.internal:invalid",
    ],
)
def test_deployment_service_url_rejects_non_origin_values(value: str) -> None:
    with pytest.raises(RuntimeError):
        deployment_service_url(
            "AUTH_SERVICE_URL",
            environment="production",
            local_port=8000,
            environ={"AUTH_SERVICE_URL": value},
        )
