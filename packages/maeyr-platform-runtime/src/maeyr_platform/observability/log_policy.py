"""Dependency-free logging policy shared by services and hosted worker images."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
SCOPES = ("accounts", "organizations", "projects")
OVERRIDE_LEVELS = frozenset({"DEBUG", "INFO", "WARNING"})


def _validated_tenant_id(tenant_id: object) -> str:
    if (
        not isinstance(tenant_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", tenant_id) is None
    ):
        raise ValueError(
            "Logging override IDs must be 1–128 letters, digits or ._:-, "
            "starting with a letter or digit"
        )
    return tenant_id


@dataclass(frozen=True)
class LoggingPolicy:
    """Exact tenant matches; the most specific configured scope wins."""

    level: int
    overrides: Mapping[str, Mapping[str, int]]

    def __post_init__(self) -> None:
        """Keep direct construction as validated and immutable as the factories."""
        if type(self.level) is not int or self.level not in LEVELS.values():
            raise ValueError("Logging policy level must be a supported numeric severity")
        if not isinstance(self.overrides, Mapping) or set(self.overrides) - set(SCOPES):
            raise ValueError("Logging policy overrides must contain only supported scopes")
        normalized: dict[str, Mapping[str, int]] = {}
        for scope in SCOPES:
            entries = self.overrides.get(scope, {})
            if not isinstance(entries, Mapping):
                raise ValueError("Each logging override scope must map tenant IDs to levels")
            values: dict[str, int] = {}
            for tenant_id, level in entries.items():
                identifier = _validated_tenant_id(tenant_id)
                if type(level) is not int or level not in {
                    LEVELS[name] for name in OVERRIDE_LEVELS
                }:
                    raise ValueError("Tenant logging overrides must be DEBUG, INFO or WARNING")
                values[identifier] = level
            normalized[scope] = MappingProxyType(values)
        object.__setattr__(self, "overrides", MappingProxyType(normalized))

    @classmethod
    def from_config(
        cls, level: str = "WARNING", overrides: Mapping[str, Any] | None = None
    ) -> LoggingPolicy:
        if not isinstance(level, str) or level not in LEVELS:
            raise ValueError("MAEYR_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        raw = {} if overrides is None else overrides
        if not isinstance(raw, Mapping) or set(raw) - set(SCOPES):
            raise ValueError(
                "MAEYR_LOG_OVERRIDES must contain only accounts, organizations, projects"
            )
        validated: dict[str, Mapping[str, int]] = {}
        for scope in SCOPES:
            entries = raw.get(scope, {})
            if not isinstance(entries, Mapping):
                raise ValueError("Each logging override scope must map tenant IDs to levels")
            values: dict[str, int] = {}
            for tenant_id, scoped_level in entries.items():
                identifier = _validated_tenant_id(tenant_id)
                if not isinstance(scoped_level, str) or scoped_level not in OVERRIDE_LEVELS:
                    raise ValueError("Tenant logging overrides must be DEBUG, INFO or WARNING")
                values[identifier] = LEVELS[scoped_level]
            validated[scope] = MappingProxyType(values)
        return cls(LEVELS[level], MappingProxyType(validated))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LoggingPolicy:
        environment = os.environ if env is None else env
        try:
            overrides = json.loads(environment.get("MAEYR_LOG_OVERRIDES", "{}"))
        except (ValueError, TypeError):
            raise ValueError("MAEYR_LOG_OVERRIDES must be a valid JSON object") from None
        # JSON null is a malformed environment value, rather than an omitted setting.
        if not isinstance(overrides, dict):
            raise ValueError("MAEYR_LOG_OVERRIDES must be a JSON object")
        return cls.from_config(environment.get("MAEYR_LOG_LEVEL", "WARNING"), overrides)

    @property
    def minimum_level(self) -> int:
        """Lowest possible threshold, used before per-record tenant filtering."""
        return min(
            [
                self.level,
                *(value for entries in self.overrides.values() for value in entries.values()),
            ]
        )

    def level_for(
        self,
        account_id: str | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        for scope, tenant_id in (
            ("projects", project_id),
            ("organizations", org_id),
            ("accounts", account_id),
        ):
            if tenant_id and tenant_id in self.overrides[scope]:
                return self.overrides[scope][tenant_id]
        return self.level

    def allows(
        self,
        levelno: int,
        *,
        account_id: str | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        return levelno >= self.level_for(account_id, org_id, project_id)
