from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_built_wheel_contains_typed_package_and_license(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    isolated_project = tmp_path / "project"
    isolated_project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(project / name, isolated_project / name)
    shutil.copytree(project / "src", isolated_project / "src")

    output = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
        ],
        cwd=isolated_project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("maeyr_platform_runtime-0.2.1-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert "maeyr_platform/__init__.py" in names
        assert "maeyr_platform/py.typed" in names
        assert "maeyr_platform/truncation.py" in names
        assert "maeyr_platform/aiohttp_lifecycle.py" in names
        assert "maeyr_platform/mongo.py" in names
        assert "maeyr_platform/resource_units.py" in names
        assert "maeyr_platform/server.py" in names
        assert "maeyr_platform/auth/fastapi_validator.py" in names
        assert "maeyr_platform/auth/permission_checker.py" in names
        assert "maeyr_platform/auth/sso_access.py" in names
        assert "maeyr_platform/auth/tenant_context.py" in names
        assert "maeyr_platform/execution/cost_rollup_match.py" in names
        assert "maeyr_platform/execution/envelope.py" in names
        assert "maeyr_platform/execution/execution_config.py" in names
        assert "maeyr_platform/execution/token_cost.py" in names
        assert "maeyr_platform/observability/logging.py" in names
        assert "maeyr_platform/orchestration/budget.py" in names
        assert "maeyr_platform/orchestration/harness.py" in names
        assert "maeyr_platform/orchestration/tool_schema.py" in names
        assert "maeyr_platform/redis/config.py" in names
        assert "maeyr_platform/redis/pubsub.py" in names
        assert "maeyr_platform/tracing/__init__.py" in names
        assert "maeyr_platform/tracing/constants.py" in names
        assert "maeyr_platform/tracing/errors.py" in names
        assert "maeyr_platform/tracing/ids.py" in names
        assert "maeyr_platform/tracing/internal_headers.py" in names
        assert "maeyr_platform/tracing/labels.py" in names
        assert "maeyr_platform/tracing/lifecycle.py" in names
        assert "maeyr_platform/tracing/sampling.py" in names
        assert "maeyr_platform/tracing/semconv.py" in names
        assert "maeyr_platform/tracing/tenant.py" in names
        assert "maeyr_platform/tracing/tracestate.py" in names
        assert "maeyr_platform/tracing/workflow.py" in names
        assert "maeyr_platform/tracing/context.py" in names
        assert "maeyr_platform/tracing/entry_trace.py" in names
        assert "maeyr_platform/tracing/http_client.py" in names
        assert "maeyr_platform/tracing/inbound.py" in names
        assert "maeyr_platform/tracing/middleware_factory.py" in names
        assert "maeyr_platform/tracing/otlp_export.py" in names
        assert "maeyr_platform/tracing/propagation.py" in names
        assert "maeyr_platform/tracing/recorder.py" in names
        assert "maeyr_platform/tracing/remote_recorder.py" in names
        assert "maeyr_platform/tracing/server_span.py" in names
        assert "maeyr_platform/tracing/span_io.py" in names
        assert "maeyr_platform/tracing/transport.py" in names
        assert "maeyr_platform/metrics/__init__.py" in names
        assert "maeyr_platform/metrics/constants.py" in names
        assert "maeyr_platform/metrics/context.py" in names
        assert "maeyr_platform/metrics/propagation.py" in names
        assert "maeyr_platform/metrics/recorder.py" in names
        assert "maeyr_platform/metrics/resource_refs.py" in names
        assert "maeyr_platform/telemetry/__init__.py" in names
        assert "maeyr_platform/telemetry/attribution.py" in names
        assert "maeyr_platform/telemetry/execution.py" in names
        assert "maeyr_platform/metrics/schema.py" in names
        assert "maeyr_platform/metrics/transport.py" in names
        assert "maeyr_platform/security/internal.py" in names
        assert "maeyr_platform/security/internal_key_guard.py" in names
        assert "maeyr_platform/security/internal_request_signing.py" in names
        assert "maeyr_platform/security/internal_tenant_guard.py" in names
        assert "maeyr_platform/security/internal_tenant_headers.py" in names
        assert "maeyr_platform/security/jwt_secret_guard.py" in names
        assert "maeyr_platform/security/tenant_safe_display.py" in names
        assert "maeyr_platform/compat/internal_request_signing.py" in names
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        assert "Name: maeyr-platform-runtime\n" in metadata
        assert "Version: 0.2.1\n" in metadata
        assert "License-Expression: Apache-2.0\n" in metadata
        assert "Requires-Dist: fastapi<1,>=0.109.1\n" in metadata
        assert "Requires-Dist: aiohttp<4,>=3.14.3\n" in metadata
        assert "Requires-Dist: pydantic<3,>=2.4.0\n" in metadata
        assert "Requires-Dist: pymongo<5,>=4.9\n" in metadata
        assert "Requires-Dist: python-json-logger<5,>=4\n" in metadata
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)

    # Exercise the actual built distribution with optional runtime integrations
    # unavailable. The metadata assertion above proves BSON is a core install
    # dependency rather than being supplied incidentally by a Mongo/dev extra.
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import importlib.abc
import sys

class NoOptionalIntegrations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in {'motor', 'redis'}:
            raise ModuleNotFoundError('Optional integration is not installed: ' + fullname)
        return None

sys.meta_path.insert(0, NoOptionalIntegrations())
sys.path.insert(0, sys.argv[1])
from bson.int64 import Int64
from maeyr_platform.resource_allocation import MAX_EXACT_RESOURCE_VALUE
from maeyr_platform.usage_limits import UsageLimitClient
import maeyr_platform
assert maeyr_platform.__file__.startswith(sys.argv[1] + '/')
assert int(Int64(MAX_EXACT_RESOURCE_VALUE)) == 9007199254740991
assert callable(UsageLimitClient.delegated_execution_lease_state)
assert 'motor' not in sys.modules and 'redis' not in sys.modules
""",
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
