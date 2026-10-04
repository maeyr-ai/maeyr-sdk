"""Plan and resolve declared agent credentials without I/O or ambient environment.

Vault lookup and decryption remain service-owned. Plans contain only effective
references, so consumers can query the project vault before resolving values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

CONFIGURED_METHODS_ENV = "MAEYR_AUTH_CONFIGURED_METHODS"
ENABLED_METHODS_ENV = "MAEYR_AUTH_ENABLED_METHODS"
_AUTH_IDENT = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_ENV_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_RESERVED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "USER",
        "LOGNAME",
        "UID",
        "EUID",
        "GID",
        "PWD",
        "OLDPWD",
        "IFS",
        "CDPATH",
        "ENV",
        "BASH_ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "TMPDIR",
        "TMP",
        "TEMP",
        "GLOBIGNORE",
        "PS4",
        "NODE_OPTIONS",
        "NODE_PATH",
        "RUBYOPT",
        "PERL5OPT",
        "PERL5LIB",
        "CLASSPATH",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_USER",
        "SYSTEM_MANAGED_CLOUD_CAPABILITY",
        "TRACE_INTERNAL_KEY",
    }
)
_RESERVED_PREFIXES = (
    "LD_",
    "DYLD_",
    "PYTHON",
    "BASH_",
    "ZSH_",
    "MAEYR_",
    "KUBERNETES_",
    "SYSTEM_MANAGED_",
    "TRACE_",
)


class AgentAuthBindingError(ValueError):
    """A declaration or required credential cannot be safely materialized."""


def is_safe_runtime_env_name(name: str) -> bool:
    """Accept aliases/auth keys, never process controls or platform identity keys."""
    if not isinstance(name, str) or len(name) > 256:
        return False
    if name in (CONFIGURED_METHODS_ENV, ENABLED_METHODS_ENV):
        return True
    if "." in name:
        parts = name.split(".")
        return len(parts) == 2 and all(_AUTH_IDENT.fullmatch(part) for part in parts)
    upper = name.upper()
    return bool(
        _ENV_IDENT.fullmatch(name)
        and upper not in _RESERVED_NAMES
        and not upper.startswith(_RESERVED_PREFIXES)
    )


@dataclass(frozen=True)
class _Binding:
    env_name: str
    secret_ref: str | None = None
    value: str | None = field(default=None, repr=False)
    required_secret: bool = False


@dataclass(frozen=True)
class AgentAuthBindingPlan:
    """Immutable bindings with a deduplicated, ordered vault-query allowlist."""

    secret_refs: tuple[str, ...]
    configured_methods: tuple[str, ...]
    _generic: tuple[_Binding, ...] = field(repr=False)
    _methods: tuple[tuple[str, tuple[_Binding, ...]], ...] = field(repr=False)


def _invalid() -> AgentAuthBindingError:
    return AgentAuthBindingError("Agent credential declarations are invalid")


def _records(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        raise _invalid()
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or not all(isinstance(k, str) for k in item):
            raise _invalid()
        records.append(cast(Mapping[str, object], item))
    return tuple(records)


def _ident(value: object) -> str:
    if not isinstance(value, str) or len(value) > 128 or not _AUTH_IDENT.fullmatch(value):
        raise _invalid()
    return value


def _reference(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 256
        or "\x00" in value
    ):
        raise _invalid()
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 65536 or "\x00" in value:
        raise _invalid()
    try:
        if len(value.encode("utf-8")) > 65536:
            raise _invalid()
    except UnicodeError:
        raise _invalid() from None
    return value


def _by_name(
    records: tuple[Mapping[str, object], ...], field_name: str
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for record in records:
        name = _ident(record.get(field_name))
        if name in result:
            raise _invalid()
        result[name] = record
    return result


def _enabled(config: Mapping[str, object] | None) -> bool:
    enabled = config.get("enabled", True) if config is not None else True
    if not isinstance(enabled, bool):
        raise _invalid()
    return enabled


def _param_binding(
    method_id: str,
    param_name: str,
    definition: Mapping[str, object] | None,
    override: Mapping[str, object] | None,
    aliases: Mapping[str, str],
) -> _Binding:
    param_type = definition.get("type", "text") if definition is not None else "text"
    if param_type not in ("secret", "text", "boolean"):
        raise _invalid()
    env_name = f"{method_id}.{param_name}"
    if override is not None:
        # An explicit binding must never silently fall back to a default secret.
        reference = override.get("secret_ref")
        if reference is not None:
            ref = _reference(reference)
            return _Binding(env_name, aliases.get(ref, ref), required_secret=True)
        return _Binding(
            env_name, value=_text(override.get("value")), required_secret=param_type == "secret"
        )
    default = definition.get("value") if definition is not None else None
    if param_type == "secret":
        if default is None or default == "":
            return _Binding(env_name, required_secret=True)
        ref = _reference(default)
        return _Binding(env_name, aliases.get(ref, ref), required_secret=True)
    return _Binding(env_name, value=_text(default))


def plan_agent_auth_bindings(agent: Mapping[str, object]) -> AgentAuthBindingPlan:
    """Resolve aliases and overrides into a plan; do not read/decrypt credentials."""
    if not isinstance(agent, Mapping):
        raise _invalid()
    aliases: dict[str, str] = {}
    generic: list[_Binding] = []
    for secret in _records(agent.get("secrets")):
        alias = secret.get("id")
        if (
            not isinstance(alias, str)
            or not is_safe_runtime_env_name(alias)
            or "." in alias
            or alias in (CONFIGURED_METHODS_ENV, ENABLED_METHODS_ENV)
            or alias in aliases
        ):
            raise _invalid()
        reference = _reference(secret.get("secret_ref"))
        aliases[alias] = reference
        generic.append(_Binding(alias, reference, required_secret=True))

    methods = _by_name(_records(agent.get("auth_methods")), "id")
    configs = _by_name(_records(agent.get("auth_config")), "method_id")
    configured = tuple(dict.fromkeys((*methods, *configs)))
    method_bindings: list[tuple[str, tuple[_Binding, ...]]] = []
    for method_id in configured:
        definition = methods.get(method_id)
        config = configs.get(method_id)
        params = _by_name(_records(definition.get("params") if definition else None), "name")
        overrides = _by_name(_records(config.get("params") if config else None), "name")
        if definition is not None and overrides.keys() - params.keys():
            raise _invalid()
        if not _enabled(config):
            continue
        names = tuple(params) if definition is not None else tuple(overrides)
        bindings = tuple(
            _param_binding(method_id, name, params.get(name), overrides.get(name), aliases)
            for name in names
        )
        method_bindings.append((method_id, bindings))

    all_bindings = [*generic, *(binding for _, group in method_bindings for binding in group)]
    secret_refs = tuple(dict.fromkeys(b.secret_ref for b in all_bindings if b.secret_ref))
    return AgentAuthBindingPlan(secret_refs, configured, tuple(generic), tuple(method_bindings))


def resolve_agent_auth_environment(
    plan: AgentAuthBindingPlan, secret_values: Mapping[str, str]
) -> dict[str, str]:
    """Materialize only declared env keys, rejecting missing required credentials.

    ``secret_values`` may contain both vault names and document ids. Extra values
    are never injected. Metadata is recomputed instead of copied from saved envs.
    """
    environment: dict[str, str] = {}

    def resolve(binding: _Binding) -> bool:
        value = (
            secret_values.get(binding.secret_ref)
            if binding.secret_ref is not None
            else binding.value
        )
        if binding.required_secret and (not isinstance(value, str) or not value.strip()):
            raise AgentAuthBindingError("A required agent credential is unavailable")
        if value is None:
            return False
        try:
            valid = _text(value)
        except AgentAuthBindingError:
            raise AgentAuthBindingError("Agent credential values are invalid") from None
        assert valid is not None
        environment[binding.env_name] = valid
        return True

    for binding in plan._generic:
        resolve(binding)
    enabled: list[str] = []
    for method_id, bindings in plan._methods:
        resolved = [resolve(binding) for binding in bindings]
        if all(resolved):
            enabled.append(method_id)
    environment[CONFIGURED_METHODS_ENV] = ",".join(plan.configured_methods)
    environment[ENABLED_METHODS_ENV] = ",".join(enabled)
    return environment


__all__ = [
    "AgentAuthBindingError",
    "AgentAuthBindingPlan",
    "CONFIGURED_METHODS_ENV",
    "ENABLED_METHODS_ENV",
    "is_safe_runtime_env_name",
    "plan_agent_auth_bindings",
    "resolve_agent_auth_environment",
]
