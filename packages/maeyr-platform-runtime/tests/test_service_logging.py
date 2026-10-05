"""Execute each service's actual facade with the canonical logging runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[4]
RUNTIME = WORKSPACE / "maeyr-sdk/packages/maeyr-platform-runtime/src"
SERVICES = (
    "auth-service",
    "builder-service",
    "chat-service",
    "devspace-service",
    "directory-sync-service",
    "headless-service",
    "marketplace-service",
    "mcp-gateway-service",
    "pulse-service",
    "scheduler-service",
    "trace-service",
    "volt-engine-service",
    "worker-service",
)


@pytest.mark.parametrize("service", SERVICES)
def test_service_facade_applies_global_and_scoped_policy(service: str) -> None:
    root = WORKSPACE / service
    if not (root / "common/logging/logger.py").is_file():
        pytest.skip("Cross-service integration requires the multi-repository workspace")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(RUNTIME), str(root / "src"), str(root)))
    env["MAEYR_LOG_LEVEL"] = "WARNING"
    env["MAEYR_LOG_OVERRIDES"] = '{"projects":{"PI-selected":"DEBUG"}}'
    # MCP settings are constructed by its infrastructure package at import.
    # Use synthetic offline values; these facade checks never contact services.
    env.update(
        {
            "APP_NAME": "logging-test",
            "APP_TITLE": "Logging test",
            "APP_DESCRIPTION": "Logging test",
            "APP_VERSION": "test",
            "APP_ENVIRONMENT": "test",
            "APP_PORT": "8000",
            "AUTH_SERVICE_URL": "http://localhost:8001",
            "AUTH_INTERNAL_KEY": "test-key",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
from common.logging import logger as facade
from maeyr_platform.observability import logging as canonical
from maeyr_platform.tracing.context import TraceContext, trace_context_scope
assert facade is canonical
logger = facade.get_logger('service.integration')
logger.info('hidden default')
logger.warning('default warning')
ctx = TraceContext('trace','span', account_id='AC-1', org_id='OI-1', project_id='PI-selected')
with trace_context_scope(ctx):
    logger.debug('selected debug')
    logging.getLogger('httpx').info('selected httpx')
with trace_context_scope(TraceContext('trace','span', project_id='PI-other')):
    logging.getLogger('httpx').info('hidden other')
""",
        ],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["message"] for record in records] == [
        "default warning",
        "selected debug",
        "selected httpx",
    ]
    assert [record["severity"] for record in records] == ["WARNING", "DEBUG", "INFO"]
    assert records[2]["project_id"] == "PI-selected"
