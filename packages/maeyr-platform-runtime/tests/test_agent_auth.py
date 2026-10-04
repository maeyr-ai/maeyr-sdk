from __future__ import annotations

from typing import Any

import pytest

from maeyr_platform.runtime.agent_auth import (
    CONFIGURED_METHODS_ENV,
    ENABLED_METHODS_ENV,
    AgentAuthBindingError,
    is_safe_runtime_env_name,
    plan_agent_auth_bindings,
    resolve_agent_auth_environment,
)


def _aviation() -> dict[str, object]:
    return {
        "secrets": [{"id": "aviationstack_api_key", "secret_ref": "AVIATIONSTACK_API"}],
        "auth_methods": [
            {
                "id": "aviationstack_api",
                "params": [{"name": "api_key", "type": "secret", "value": "AVIATIONSTACK_API"}],
                "is_default": True,
            }
        ],
        "auth_config": [
            {
                "method_id": "aviationstack_api",
                "enabled": True,
                "params": [
                    {"name": "api_key", "value": None, "secret_ref": "aviationstack_api_key"}
                ],
            }
        ],
    }


def test_pasted_alias_binding_materializes_sdk_key_and_generic_alias() -> None:
    agent = _aviation()
    agent[ENABLED_METHODS_ENV] = "stale_method"
    agent[CONFIGURED_METHODS_ENV] = "stale_method"
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ("AVIATIONSTACK_API",)
    assert resolve_agent_auth_environment(plan, {"AVIATIONSTACK_API": "synthetic-key"}) == {
        "aviationstack_api_key": "synthetic-key",
        "aviationstack_api.api_key": "synthetic-key",
        CONFIGURED_METHODS_ENV: "aviationstack_api",
        ENABLED_METHODS_ENV: "aviationstack_api",
    }


def test_disabled_method_is_configured_but_does_not_query_or_inject_params() -> None:
    agent: dict[str, object] = {
        "auth_methods": [
            {"id": "disabled", "params": [{"name": "key", "type": "secret", "value": "MISSING"}]}
        ],
        "auth_config": [{"method_id": "disabled", "enabled": False}],
    }
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ()
    assert resolve_agent_auth_environment(plan, {"MISSING": "unrelated"}) == {
        CONFIGURED_METHODS_ENV: "disabled",
        ENABLED_METHODS_ENV: "",
    }


def test_explicit_vault_override_wins_and_only_effective_ref_is_queried() -> None:
    agent: dict[str, object] = {
        "auth_methods": [
            {"id": "bearer", "params": [{"name": "token", "type": "secret", "value": "OLD"}]}
        ],
        "auth_config": [
            {"method_id": "bearer", "params": [{"name": "token", "secret_ref": "NEW"}]}
        ],
    }
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ("NEW",)
    with pytest.raises(AgentAuthBindingError, match="unavailable"):
        resolve_agent_auth_environment(plan, {"OLD": "must-not-fall-back"})
    assert resolve_agent_auth_environment(plan, {"NEW": "new"})["bearer.token"] == "new"


def test_defaults_static_overrides_partial_methods_and_standalone_configs() -> None:
    agent: dict[str, object] = {
        "auth_methods": [
            {
                "id": "default_method",
                "is_default": False,
                "params": [
                    {"name": "url", "type": "text", "value": "original"},
                    {"name": "enabled", "type": "boolean", "value": "false"},
                    {"name": "empty", "type": "text", "value": ""},
                ],
            },
            {"id": "partial", "params": [{"name": "missing", "type": "text"}]},
        ],
        "auth_config": [
            {"method_id": "default_method", "params": [{"name": "url", "value": "override"}]},
            {"method_id": "standalone", "params": [{"name": "token", "secret_ref": "vault-id"}]},
        ],
    }
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ("vault-id",)
    env = resolve_agent_auth_environment(plan, {"vault-id": "id-value", "OTHER": "not-injected"})
    assert env == {
        "default_method.url": "override",
        "default_method.enabled": "false",
        "default_method.empty": "",
        "standalone.token": "id-value",
        CONFIGURED_METHODS_ENV: "default_method,partial,standalone",
        ENABLED_METHODS_ENV: "default_method,standalone",
    }


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_null_empty_and_missing_required_secrets_fail_closed(missing: Any) -> None:
    values = {} if missing is None else {"AVIATIONSTACK_API": missing}
    with pytest.raises(AgentAuthBindingError, match="unavailable"):
        resolve_agent_auth_environment(plan_agent_auth_bindings(_aviation()), values)


@pytest.mark.parametrize("override", [{"value": None}, {"secret_ref": None, "value": None}])
def test_explicit_unset_secret_override_never_reuses_default(override: dict[str, object]) -> None:
    agent: dict[str, object] = {
        "auth_methods": [
            {"id": "bearer", "params": [{"name": "key", "type": "secret", "value": "DEFAULT"}]}
        ],
        "auth_config": [{"method_id": "bearer", "params": [{"name": "key", **override}]}],
    }
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ()
    with pytest.raises(AgentAuthBindingError, match="unavailable"):
        resolve_agent_auth_environment(plan, {"DEFAULT": "do-not-use"})


def test_static_secret_override_is_authoritative() -> None:
    agent: dict[str, object] = {
        "auth_methods": [
            {"id": "bearer", "params": [{"name": "key", "type": "secret", "value": "OLD"}]}
        ],
        "auth_config": [{"method_id": "bearer", "params": [{"name": "key", "value": "static"}]}],
    }
    plan = plan_agent_auth_bindings(agent)
    assert plan.secret_refs == ()
    assert "static" not in repr(plan)
    assert resolve_agent_auth_environment(plan, {})["bearer.key"] == "static"


@pytest.mark.parametrize(
    "alias",
    [
        "PATH",
        "path",
        "HOME",
        "UID",
        "EUID",
        "GID",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_USER",
        "PYTHONPATH",
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "MAEYR_API_KEY",
        ENABLED_METHODS_ENV,
        "x.y",
        "bad-name",
    ],
)
def test_unsafe_aliases_cannot_change_process_or_platform_environment(alias: str) -> None:
    with pytest.raises(AgentAuthBindingError):
        plan_agent_auth_bindings({"secrets": [{"id": alias, "secret_ref": "VAULT"}]})


@pytest.mark.parametrize(
    "agent",
    [
        {"auth_methods": [{"id": "bad.method"}]},
        {"auth_methods": [{"id": "duplicate"}, {"id": "duplicate"}]},
        {"auth_methods": [{"id": "good", "params": [{"name": "x"}, {"name": "x"}]}]},
        {"auth_config": [{"method_id": "good", "enabled": "false"}]},
        {
            "auth_methods": [{"id": "good"}],
            "auth_config": [{"method_id": "good", "params": [{"name": "unknown"}]}],
        },
        {"secrets": [{"id": "good", "secret_ref": ""}]},
        {"auth_methods": "not-a-list"},
    ],
)
def test_malformed_declarations_have_sanitized_errors(agent: dict[str, object]) -> None:
    with pytest.raises(AgentAuthBindingError, match="declarations are invalid"):
        plan_agent_auth_bindings(agent)


def test_secret_values_are_not_in_error_messages_or_extra_env() -> None:
    secret = "sensitive-synthetic\x00value"
    with pytest.raises(AgentAuthBindingError) as error:
        resolve_agent_auth_environment(
            plan_agent_auth_bindings(_aviation()), {"AVIATIONSTACK_API": secret}
        )
    assert "sensitive-synthetic" not in str(error.value)
    assert is_safe_runtime_env_name("aviationstack_api.api_key")
    assert not is_safe_runtime_env_name("nested.auth.key")
