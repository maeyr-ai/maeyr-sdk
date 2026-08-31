from __future__ import annotations

import pytest

from maeyr_platform.directory import channel_config
from maeyr_platform.directory.runtime_config import RuntimeSettings


def test_channel_public_urls_fail_closed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.delenv("VOLT_CHANNEL_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("VOLT_WIDGET_JS_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="VOLT_CHANNEL_WEBHOOK_BASE_URL"):
        channel_config._public_runtime_url("VOLT_CHANNEL_WEBHOOK_BASE_URL", "http://localhost:8000")
    with pytest.raises(RuntimeError, match="VOLT_WIDGET_JS_BASE_URL"):
        channel_config._public_runtime_url(
            "VOLT_WIDGET_JS_BASE_URL", "http://localhost:3000/widget/v1"
        )


def test_channel_public_urls_accept_master_derived_https_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("VOLT_CHANNEL_WEBHOOK_BASE_URL", "https://api.example.test/")
    monkeypatch.setenv("VOLT_WIDGET_JS_BASE_URL", "https://ui.example.test/widget/v1/")

    assert (
        channel_config._public_runtime_url("VOLT_CHANNEL_WEBHOOK_BASE_URL", "http://localhost:8000")
        == "https://api.example.test"
    )
    assert (
        channel_config._public_runtime_url(
            "VOLT_WIDGET_JS_BASE_URL", "http://localhost:3000/widget/v1"
        )
        == "https://ui.example.test/widget/v1"
    )


def test_channel_local_defaults_are_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.delenv("VOLT_CHANNEL_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("VOLT_WIDGET_JS_BASE_URL", raising=False)

    assert (
        channel_config._public_runtime_url("VOLT_CHANNEL_WEBHOOK_BASE_URL", "http://localhost:8000")
        == "http://localhost:8000"
    )
    assert (
        channel_config._public_runtime_url(
            "VOLT_WIDGET_JS_BASE_URL", "http://localhost:3000/widget/v1"
        )
        == "http://localhost:3000/widget/v1"
    )


def test_dead_runtime_pod_contract_is_not_exposed_by_shared_settings() -> None:
    settings = RuntimeSettings()

    for name in (
        "RUNTIME_NAMESPACE",
        "RUNTIME_REPLICAS",
        "RUNTIME_DEPLOYMENT_PREFIX",
        "RUNTIME_SECRET_PREFIX",
        "RUNTIME_IMAGE",
        "RUNTIME_PULL_SECRET",
        "RUNTIME_PULL_POLICY",
        "ENGINE_PUBLIC_URL",
        "ENGINE_INTERNAL_KEY",
        "ENABLED_CHANNELS",
    ):
        assert not hasattr(settings, name)

    assert settings.SLACK_API_BASE_URL == "https://slack.com/api"
