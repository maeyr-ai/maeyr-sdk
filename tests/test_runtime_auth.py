"""Tests for MaeyrAuth runtime (ported from builder-service)."""

import os
import types

import pytest

from maeyr.runtime import MaeyrAuth, mcp_endpoint
from maeyr.runtime.inject import to_module_source


@pytest.fixture(scope="module")
def sdk_from_injected():
    mod = types.ModuleType("maeyr_sdk_under_test")
    exec(to_module_source(), mod.__dict__)
    return mod


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ.keys()):
        if k.startswith("MAEYR_") or "." in k:
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


class TestEnabledMethods:
    def test_no_env_returns_empty_list(self, sdk_from_injected, clean_env):
        assert sdk_from_injected.MaeyrAuth.get_enabled_methods() == []

    def test_parses_comma_separated_list(self, sdk_from_injected, clean_env):
        clean_env.setenv("MAEYR_AUTH_ENABLED_METHODS", "bearer_token,oauth_client")
        assert sdk_from_injected.MaeyrAuth.get_enabled_methods() == [
            "bearer_token",
            "oauth_client",
        ]


class TestParamAccessors:
    def test_require_param_raises_when_missing(self, sdk_from_injected, clean_env):
        with pytest.raises(sdk_from_injected.MaeyrAuthError):
            sdk_from_injected.MaeyrAuth.require_param("bearer_token", "api_key")


class TestPackageRuntime:
    def test_require_param_package(self, clean_env):
        clean_env.setenv("bearer_token.api_key", "abc123")
        assert MaeyrAuth.require_param("bearer_token", "api_key") == "abc123"

    def test_mcp_endpoint(self):
        @mcp_endpoint("desc")
        def fn(x):
            return x

        assert fn(2) == 2


class TestMcpEndpointDecorator:
    def test_returns_function_unchanged(self, sdk_from_injected):
        @sdk_from_injected.mcp_endpoint("a description")
        def my_endpoint(x):
            return x * 2

        assert my_endpoint(3) == 6
