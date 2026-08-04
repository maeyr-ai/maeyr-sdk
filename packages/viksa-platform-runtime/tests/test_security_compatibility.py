from __future__ import annotations

import inspect
from importlib import import_module

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from viksa_platform.security.internal import (
    requires_internal_signature,
    sign_internal_request,
    verify_internal_signature,
)
from viksa_platform.security.internal_key_guard import assert_production_internal_key
from viksa_platform.security.internal_tenant_guard import (
    require_internal_tenant_fields,
    validate_internal_tenant_body,
)
from viksa_platform.security.internal_tenant_headers import internal_tenant_headers
from viksa_platform.security.jwt_secret_guard import (
    allows_insecure_jwt_dev,
    assert_production_jwt_secret,
)


def test_security_compatibility_modules_preserve_import_identity() -> None:
    implementation = import_module("viksa_platform.security.internal")
    assert import_module("viksa_platform.security.internal_request_signing") is implementation
    assert import_module("viksa_platform.compat.internal_request_signing") is implementation
    tenant_headers = import_module("viksa_platform.security.internal_tenant_headers")
    assert import_module("viksa_platform.compat.internal_tenant_headers") is tenant_headers


def test_legacy_security_call_shapes_remain_keyword_compatible() -> None:
    assert list(inspect.signature(sign_internal_request).parameters) == [
        "secret",
        "method",
        "path",
        "body",
        "service",
        "account_id",
        "org_id",
        "project_id",
        "timestamp",
    ]
    assert list(inspect.signature(verify_internal_signature).parameters) == [
        "secret",
        "method",
        "path",
        "body",
        "service",
        "timestamp",
        "signature",
        "account_id",
        "org_id",
        "project_id",
        "max_skew_sec",
        "now",
    ]
    assert "minimum_bytes" in inspect.signature(assert_production_internal_key).parameters


def test_production_environment_wins_over_insecure_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ALLOW_INSECURE_JWT", "true")
    assert requires_internal_signature()
    assert not allows_insecure_jwt_dev()
    with pytest.raises(RuntimeError):
        assert_production_internal_key(
            "placeholder",
            env_name="INTERNAL_KEY",
            service_name="service",
        )
    with pytest.raises(RuntimeError):
        assert_production_internal_key(
            "short",
            env_name="INTERNAL_KEY",
            service_name="service",
        )
    with pytest.raises(RuntimeError):
        assert_production_jwt_secret("short", service_name="service")


def test_internal_tenant_helpers_preserve_headers_and_fastapi_errors() -> None:
    assert internal_tenant_headers(
        account_id=" a ", org_id=" o ", project_id=" p "
    ) == {
        "X-Internal-Account-Id": "a",
        "X-Internal-Org-Id": "o",
        "X-Internal-Project-Id": "p",
    }
    with pytest.raises(HTTPException) as missing:
        require_internal_tenant_fields(account_id="", org_id="o", project_id="p")
    assert missing.value.status_code == 400

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-internal-account-id", b"other")],
        }
    )
    with pytest.raises(HTTPException) as mismatch:
        validate_internal_tenant_body(
            request,
            account_id="account",
            org_id="org",
            project_id="project",
        )
    assert mismatch.value.status_code == 403
