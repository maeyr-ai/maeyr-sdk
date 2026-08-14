from __future__ import annotations

import pytest

from viksa_platform.tracing import remote_recorder


def test_trace_configuration_ignores_chat_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.delenv("TRACE_SERVICE_URL", raising=False)
    monkeypatch.delenv("TRACE_INTERNAL_KEY", raising=False)
    monkeypatch.setenv("CHAT_SERVICE_URL", "https://chat.example.test")
    monkeypatch.setenv("CHAT_INTERNAL_KEY", "chat-secret-must-not-sign-traces")

    assert remote_recorder._trace_service_url() == "http://localhost:8000"
    assert remote_recorder._trace_internal_key() == ""


def test_production_trace_configuration_requires_dedicated_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("CHAT_SERVICE_URL", "https://chat.example.test")
    monkeypatch.setenv("CHAT_INTERNAL_KEY", "c" * 32)
    monkeypatch.delenv("TRACE_SERVICE_URL", raising=False)
    monkeypatch.delenv("TRACE_INTERNAL_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TRACE_INTERNAL_KEY"):
        remote_recorder.assert_trace_producer_configuration("example-service")

    monkeypatch.setenv(
        "TRACE_INTERNAL_KEY",
        "trace-signing-key-for-production-tests-2026",
    )
    with pytest.raises(RuntimeError, match="TRACE_SERVICE_URL"):
        remote_recorder.assert_trace_producer_configuration("example-service")

    with pytest.raises(RuntimeError, match="TRACE_SERVICE_URL"):
        remote_recorder.RemoteTraceRecorder(
            "example-service",
            internal_key="trace-signing-key-for-production-tests-2026",
        )

    monkeypatch.setenv("TRACE_SERVICE_URL", "http://trace-service:8000")
    remote_recorder.assert_trace_producer_configuration("example-service")


@pytest.mark.parametrize(
    "url",
    [
        "trace-service:8000",
        "https://user:password@trace.example.test",
        "https://trace.example.test?token=secret",
        "https://trace.example.test#fragment",
    ],
)
def test_trace_configuration_rejects_unsafe_urls(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "TRACE_INTERNAL_KEY",
        "trace-signing-key-for-production-tests-2026",
    )
    monkeypatch.setenv("TRACE_SERVICE_URL", url)

    with pytest.raises(RuntimeError, match="TRACE_SERVICE_URL"):
        remote_recorder.assert_trace_producer_configuration("example-service")
