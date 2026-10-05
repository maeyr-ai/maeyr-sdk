"""Validate public policy construction and the final JSON output boundary."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest

from maeyr_platform.observability import logging as service_logging
from maeyr_platform.observability.log_policy import LoggingPolicy


@pytest.fixture(autouse=True)
def restore_policy() -> Iterator[None]:
    previous = service_logging._logging_policy
    yield
    service_logging.configure_logging(previous)


def test_direct_policy_construction_normalizes_and_copies_immutable_maps() -> None:
    raw = {"projects": {"PI-1": logging.DEBUG}}
    policy = LoggingPolicy(logging.WARNING, raw)
    raw["projects"]["PI-1"] = logging.INFO
    assert policy.minimum_level == logging.DEBUG
    assert policy.level_for(project_id="PI-1") == logging.DEBUG
    assert policy.level_for(account_id="AC-1", org_id="OI-1") == logging.WARNING
    assert set(policy.overrides) == {"accounts", "organizations", "projects"}
    with pytest.raises(TypeError):
        policy.overrides["projects"]["PI-1"] = logging.INFO  # type: ignore[index]


@pytest.mark.parametrize("level", [True, 0, 15, "INFO", 20.0, None])
def test_direct_policy_rejects_invalid_global_levels(level: Any) -> None:
    with pytest.raises(ValueError):
        LoggingPolicy(level, {})


@pytest.mark.parametrize(
    "overrides",
    [
        None,
        [],
        {"unknown": {}},
        {"projects": []},
        {"projects": {"*": logging.DEBUG}},
        {"projects": {"PI-1": True}},
        {"projects": {"PI-1": "DEBUG"}},
        {"projects": {"PI-1": logging.ERROR}},
    ],
)
def test_direct_policy_rejects_invalid_overrides(overrides: Any) -> None:
    with pytest.raises(ValueError):
        LoggingPolicy(logging.WARNING, overrides)


def test_invalid_configure_argument_cannot_replace_the_working_policy() -> None:
    policy = service_logging.configure_logging(LoggingPolicy.from_config())
    with pytest.raises(ValueError):
        service_logging.configure_logging({})  # type: ignore[arg-type]
    assert service_logging._logging_policy is policy
    assert not policy.allows(logging.INFO)
    assert policy.allows(logging.WARNING)


def test_opaque_request_exception_and_typed_values_are_redacted_before_encoding() -> None:
    request = httpx.Request(
        "GET", "https://user:opaque-password@mock.invalid/data?api_key=opaque-key"
    )
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError(
        "Failed URL https://mock.invalid/data?token=opaque-token",
        request=request,
        response=response,
    )

    @dataclass
    class Diagnostic:
        name: str
        api_key: str
        when: date

    record = logging.LogRecord("fixture", logging.DEBUG, __file__, 1, "diagnostic", (), None)
    record.request = request
    record.error = error
    record.diagnostic = Diagnostic("visible", "typed-key", date(2026, 10, 5))
    output = service_logging.log_format.format(record)
    for secret in ("opaque-password", "opaque-key", "opaque-token", "typed-key"):
        assert secret not in output
    decoded = json.loads(output)
    assert "mock.invalid/data" in decoded["request"]
    assert decoded["diagnostic"]["name"] == "visible"
    assert decoded["diagnostic"]["when"] == "2026-10-05"


def test_failed_opaque_conversion_does_not_reveal_the_value_or_formatter_error() -> None:
    class BadLogValue:
        def __str__(self) -> str:
            raise RuntimeError("private-value-in-conversion-error")

        def __repr__(self) -> str:
            raise RuntimeError("private-value-in-conversion-error")

    record = logging.LogRecord("fixture", logging.WARNING, __file__, 1, "diagnostic", (), None)
    record.details = BadLogValue()
    output = service_logging.log_format.format(record)
    assert "private-value" not in output
    assert json.loads(output)["details"] == "__could_not_encode__"


def test_actual_httpx_logger_created_after_configuration_respects_scope_and_redaction() -> None:
    # A separate interpreter guarantees httpx creates its real logger after
    # configuration; MockTransport performs requests without any sockets.
    program = """
import io, json, logging
from maeyr_platform.observability import logging as service_logging
from maeyr_platform.observability.log_policy import LoggingPolicy
assert 'httpx' not in logging.Logger.manager.loggerDict
service_logging.configure_logging(LoggingPolicy.from_config(overrides={
    'projects': {'PI-debug': 'DEBUG', 'PI-info': 'INFO'},
}))
output = io.StringIO()
handler = logging.StreamHandler(output)
handler.setFormatter(service_logging.log_format)
handler.addFilter(service_logging.ContextLogFilter())
service_logging.root_logger.handlers[:] = [handler]
import httpx
from maeyr_platform.tracing.context import TraceContext, trace_context_scope
requests = []
def respond(request):
    requests.append(request)
    return httpx.Response(200, json={'ok': True})
with httpx.Client(transport=httpx.MockTransport(respond)) as client:
    client.get('https://mock.invalid/default?access_key=private-default')
    for project, verified in (
        ('PI-debug', False), ('PI-quiet', True), ('PI-info', True), ('PI-debug', True),
    ):
        with trace_context_scope(TraceContext(
            'trace', 'span', project_id=project, tenant_verified=verified,
        )):
            assert client.get(
                f'https://mock.invalid/{project}?access_key=private-{project}'
            ).json() == {'ok': True}
records = [json.loads(line) for line in output.getvalue().splitlines()]
assert len(requests) == 5
assert [r['project_id'] for r in records] == ['PI-info', 'PI-debug'], records
assert all(r['severity'] == 'INFO' and r['logger'] == 'httpx' for r in records)
assert 'private-' not in output.getvalue()
assert logging.getLogger('httpx').getEffectiveLevel() == logging.DEBUG
"""
    environment = dict(os.environ)
    environment.update(
        {
            "MAEYR_LOG_LEVEL": "WARNING",
            "MAEYR_LOG_OVERRIDES": "{}",
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
