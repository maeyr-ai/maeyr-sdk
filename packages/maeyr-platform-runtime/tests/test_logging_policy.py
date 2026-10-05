from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from maeyr_platform.observability import logging as service_logging
from maeyr_platform.observability.log_policy import LoggingPolicy
from maeyr_platform.tracing.context import TraceContext, trace_context_scope


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    old_policy = service_logging._logging_policy
    for clear in (
        service_logging.clear_account_id,
        service_logging.clear_org_id,
        service_logging.clear_project_id,
    ):
        clear()
    service_logging.configure_logging(LoggingPolicy.from_config())
    yield
    service_logging.configure_logging(old_policy)
    for clear in (
        service_logging.clear_account_id,
        service_logging.clear_org_id,
        service_logging.clear_project_id,
    ):
        clear()


def capture() -> tuple[io.StringIO, logging.Handler]:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(service_logging.log_format)
    handler.addFilter(service_logging.ContextLogFilter())
    return output, handler


def test_default_drops_info_debug_including_library_logs_and_keeps_errors() -> None:
    output, handler = capture()
    service_logging.root_logger.addHandler(handler)
    try:
        logger = logging.getLogger("httpx")
        logger.debug("hidden debug")
        logger.info("hidden request")
        logger.warning("visible warning")
        try:
            raise RuntimeError("visible failure")
        except RuntimeError:
            logger.exception("visible error")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [record["severity"] for record in records] == ["WARNING", "ERROR"]
        assert "RuntimeError: visible failure" in records[1]["exc_info"]
    finally:
        service_logging.root_logger.removeHandler(handler)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_global_levels(level: str) -> None:
    policy = LoggingPolicy.from_env({"MAEYR_LOG_LEVEL": level})
    assert policy.minimum_level == getattr(logging, level)
    assert policy.allows(getattr(logging, level))
    assert not policy.allows(getattr(logging, level) - 1)


@pytest.mark.parametrize(
    "scope,field",
    [("accounts", "account_id"), ("organizations", "org_id"), ("projects", "project_id")],
)
@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_exact_tenant_overrides(scope: str, field: str, level: str) -> None:
    policy = LoggingPolicy.from_config(overrides={scope: {"selected": level}})
    assert policy.allows(logging.INFO, **{field: "selected"})
    assert policy.allows(logging.DEBUG, **{field: "selected"}) == (level == "DEBUG")
    for tenant in (None, "", "different", "selected-suffix", "SELECTED"):
        assert not policy.allows(logging.INFO, **{field: tenant})
        assert policy.allows(logging.ERROR, **{field: tenant})


def test_specific_scope_can_reduce_parent_verbosity() -> None:
    policy = LoggingPolicy.from_config(
        overrides={
            "accounts": {"AC-1": "DEBUG"},
            "organizations": {"OI-1": "INFO"},
            "projects": {"PI-1": "WARNING"},
        }
    )
    assert policy.level_for("AC-1", "OI-1", "PI-1") == logging.WARNING
    assert policy.level_for("AC-1", "OI-1", "PI-other") == logging.INFO
    assert policy.level_for("AC-1", "OI-other", "PI-other") == logging.DEBUG
    assert policy.level_for("AC-other", "OI-other", "PI-other") == logging.WARNING


@pytest.mark.parametrize(
    "env",
    [
        {"MAEYR_LOG_LEVEL": "info"},
        {"MAEYR_LOG_LEVEL": ""},
        {"MAEYR_LOG_LEVEL": "NOTSET"},
        {"MAEYR_LOG_LEVEL": "WARN"},
        {"MAEYR_LOG_OVERRIDES": "invalid-private-value"},
        {"MAEYR_LOG_OVERRIDES": "null"},
        {"MAEYR_LOG_OVERRIDES": "[]"},
        {"MAEYR_LOG_OVERRIDES": '{"accounts":[]}'},
        {"MAEYR_LOG_OVERRIDES": '{"unknown":{}}'},
        {"MAEYR_LOG_OVERRIDES": '{"projects":{"":"DEBUG"}}'},
        {"MAEYR_LOG_OVERRIDES": '{"projects":{"PI 1":"DEBUG"}}'},
        {"MAEYR_LOG_OVERRIDES": '{"projects":{"*":"DEBUG"}}'},
        {"MAEYR_LOG_OVERRIDES": '{"accounts":{"AC-1":10}}'},
        {"MAEYR_LOG_OVERRIDES": '{"accounts":{"AC-1":"ERROR"}}'},
    ],
)
def test_invalid_configuration_fails_without_echoing_raw_values(env: dict[str, str]) -> None:
    with pytest.raises(ValueError) as error:
        LoggingPolicy.from_env(env)
    assert "invalid-private-value" not in str(error.value)


def test_policy_is_immutable_and_configuration_input_is_not_retained() -> None:
    settings = {"projects": {"PI-1": "INFO"}}
    policy = LoggingPolicy.from_config(overrides=settings)
    settings["projects"]["PI-1"] = "DEBUG"
    assert policy.level_for(project_id="PI-1") == logging.INFO
    with pytest.raises(TypeError):
        policy.overrides["projects"]["PI-2"] = logging.DEBUG  # type: ignore[index]


def test_context_is_authoritative_and_stale_fallback_cannot_enable_new_tenant() -> None:
    service_logging.configure_logging(
        LoggingPolicy.from_config(overrides={"projects": {"PI-OLD": "DEBUG"}})
    )
    service_logging.set_project_id("PI-OLD")
    record = logging.LogRecord("httpx", logging.INFO, __file__, 0, "request", (), None)
    record.project_id = "PI-OLD"
    with trace_context_scope(TraceContext("trace", "span", account_id="AC-new")):
        assert service_logging.ContextLogFilter().filter(record) is False
        assert getattr(record, "project_id") is None
        assert getattr(record, "account_id") == "AC-new"


def test_background_record_context_is_preserved_without_active_trace() -> None:
    service_logging.configure_logging(
        LoggingPolicy.from_config(overrides={"projects": {"PI-job": "DEBUG"}})
    )
    record = logging.LogRecord("job", logging.DEBUG, __file__, 0, "job", (), None)
    record.project_id = "PI-job"
    assert service_logging.ContextLogFilter().filter(record)
    assert getattr(record, "project_id") == "PI-job"


def test_unverified_inbound_headers_cannot_enable_tenant_debug() -> None:
    from maeyr_platform.tracing.context import (
        clear_trace_context,
        enrich_trace_tenant,
        get_trace_context,
    )
    from maeyr_platform.tracing.inbound import bind_inbound_trace

    service_logging.configure_logging(
        LoggingPolicy.from_config(
            overrides={"projects": {"PI-selected": "DEBUG"}, "accounts": {"AC-real": "INFO"}}
        )
    )
    token = bind_inbound_trace(
        {
            "x-trace-id": "a" * 32,
            "x-tenant-account-id": "AC-fake",
            "x-tenant-org-id": "OI-fake",
            "x-tenant-project-id": "PI-selected",
        },
        service="test",
    )
    assert token is not None
    try:
        record = logging.LogRecord("httpx", logging.DEBUG, __file__, 0, "request", (), None)
        assert service_logging.ContextLogFilter().filter(record) is False
        context = get_trace_context()
        assert context is not None and context.tenant_verified is False
        # Verified account-only decisions must discard every forged child ID.
        enrich_trace_tenant(account_id="AC-real")
        context = get_trace_context()
        assert context is not None and context.tenant_verified is True
        assert context.project_id is None
        assert context.org_id is None
        record.levelno = logging.INFO
        assert service_logging.ContextLogFilter().filter(record) is True
        assert getattr(record, "account_id") == "AC-real"
        assert getattr(record, "project_id") is None
    finally:
        clear_trace_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_shared_auth_validation_enables_only_verified_response_tenant(
    monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from maeyr_platform.auth import fastapi_validator
    from maeyr_platform.tracing.context import get_trace_context

    monkeypatch.setattr(
        fastapi_validator,
        "auth_settings",
        SimpleNamespace(
            AUTH_SERVICE_URL="http://auth.test",
            AUTH_INTERNAL_KEY="test-key",
            AUTH_API_TIMEOUT=10,
            AUTH_API_RETRIES=1,
            AUTH_API_RETRY_BACKOFF=0,
        ),
    )
    monkeypatch.setattr(fastapi_validator, "_get_auth_session", AsyncMock(return_value=object()))
    response = {"valid": True, "account_id": "AC-1", "org_id": "OI-1", "project_id": "PI-real"}
    monkeypatch.setattr(
        fastapi_validator,
        "_call_auth_validate",
        AsyncMock(
            return_value=response, side_effect=None if valid else HTTPException(status_code=401)
        ),
    )
    with trace_context_scope(
        TraceContext("trace", "span", project_id="PI-forged", tenant_verified=False)
    ):
        if valid:
            assert (
                await fastapi_validator._validate_credential_with_retries("test-credential")
                == response
            )
            context = get_trace_context()
            assert context is not None and context.project_id == "PI-real"
            assert context.tenant_verified is True
        else:
            with pytest.raises(HTTPException):
                await fastapi_validator._validate_credential_with_retries("test-credential")
            context = get_trace_context()
            assert context is not None and context.tenant_verified is False


@pytest.mark.parametrize("valid", [True, False])
def test_signature_math_does_not_promote_tenant_before_request_authorization(valid: bool) -> None:
    import time

    from maeyr_platform.security.internal import sign_internal_request, verify_internal_signature
    from maeyr_platform.tracing.context import get_trace_context

    service_logging.configure_logging(
        LoggingPolicy.from_config(overrides={"projects": {"PI-real": "DEBUG"}})
    )
    timestamp = int(time.time())
    headers = sign_internal_request(
        "test-key",
        method="POST",
        path="/internal/test",
        body=b"{}",
        service="test-service",
        account_id="AC-1",
        org_id="OI-1",
        project_id="PI-real",
        timestamp=timestamp,
    )
    with trace_context_scope(
        TraceContext("trace", "span", project_id="PI-forged", tenant_verified=False)
    ):
        assert (
            verify_internal_signature(
                "test-key" if valid else "wrong-key",
                method="POST",
                path="/internal/test",
                body=b"{}",
                service="test-service",
                account_id="AC-1",
                org_id="OI-1",
                project_id="PI-real",
                timestamp=headers["x-internal-timestamp"],
                signature=headers["x-internal-signature"],
                now=timestamp,
            )
            == valid
        )
        context = get_trace_context()
        assert context is not None and context.tenant_verified is False
        assert context.project_id == "PI-forged"


@pytest.mark.asyncio
async def test_concurrent_requests_keep_service_and_library_logs_tenant_isolated() -> None:
    service_logging.configure_logging(
        LoggingPolicy.from_config(overrides={"projects": {"PI-debug": "DEBUG", "PI-info": "INFO"}})
    )
    output, handler = capture()
    service_logging.root_logger.addHandler(handler)

    async def emit(project: str) -> None:
        with trace_context_scope(
            TraceContext(project, "span", account_id="AC-1", org_id="OI-1", project_id=project)
        ):
            await asyncio.sleep(0)
            for logger in (service_logging.get_logger("service.test"), logging.getLogger("httpx")):
                logger.debug("debug")
                logger.info("info")
                logger.warning("warning")

    try:
        await asyncio.gather(*(emit(project) for project in ("PI-debug", "PI-info", "PI-quiet")))
        logging.getLogger("httpx").info("outside request")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        for project, severities in (
            ("PI-debug", ["DEBUG", "INFO", "WARNING"]),
            ("PI-info", ["INFO", "WARNING"]),
            ("PI-quiet", ["WARNING"]),
        ):
            assert [r["severity"] for r in records if r["project_id"] == project] == severities * 2
            assert all(r["trace_id"] == project for r in records if r["project_id"] == project)
        assert not any(r["message"] == "outside request" for r in records)
    finally:
        service_logging.root_logger.removeHandler(handler)


def test_rotating_file_handler_uses_same_policy_and_is_not_duplicated(tmp_path: Path) -> None:
    path = str(tmp_path / "service.log")
    logger = service_logging.get_logger("file.test", log_file=path)
    try:
        service_logging.get_logger("file.test", log_file=path)
        assert len(logger.handlers) == 1
        logger.info("hidden")
        logger.warning("visible")
        service_logging.configure_logging(
            LoggingPolicy.from_config(overrides={"accounts": {"AC-1": "DEBUG"}})
        )
        with trace_context_scope(TraceContext("trace", "span", account_id="AC-1")):
            logger.debug("selected debug")
        assert [
            json.loads(line)["message"]
            for line in (tmp_path / "service.log").read_text().splitlines()
        ] == ["visible", "selected debug"]
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_uvicorn_diagnostics_use_shared_handler_and_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from maeyr_platform.server import configure_uvicorn_logging, run_uvicorn_app

    monkeypatch.setenv("MAEYR_LOG_OVERRIDES", '{"projects":{"PI-1":"DEBUG"}}')
    configure_uvicorn_logging()
    assert logging.getLogger("uvicorn.error").handlers == []
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.DEBUG
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    run_uvicorn_app(object(), host="127.0.0.1", port=8000, web_concurrency=1)
    assert calls[0]["log_config"] is None
    assert calls[0]["log_level"] is None


def test_scoped_debug_keeps_secret_redaction_including_http_client_urls() -> None:
    service_logging.configure_logging(
        LoggingPolicy.from_config(overrides={"projects": {"PI-1": "DEBUG"}})
    )
    output, handler = capture()
    service_logging.root_logger.addHandler(handler)
    try:
        with trace_context_scope(TraceContext("trace", "span", project_id="PI-1")):
            logging.getLogger("httpx").debug(
                "HTTP GET https://user:private-password@api.test/flights?access_key=private-api-key"
                " Authorization: Bearer private-bearer",
                extra={"details": {"password": "private-password", "api_key": "private-api-key"}},
            )
        text = output.getvalue()
        for secret in ("private-password", "private-api-key", "private-bearer"):
            assert secret not in text
        assert "api.test/flights" in text
        assert json.loads(text)["severity"] == "DEBUG"
    finally:
        service_logging.root_logger.removeHandler(handler)
