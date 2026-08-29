"""Strict path-to-caller allowlists for signed internal APIs."""

from __future__ import annotations

import json
import os
import re
from fnmatch import fnmatchcase

_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def load_caller_allowlist(
    env_name: str,
    builtins: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Load and validate bounded caller overrides layered on built-in routes."""
    result = dict(builtins)
    configured = os.environ.get(env_name, "").strip()
    if not configured:
        return result
    try:
        payload = json.loads(configured)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_name} must be valid JSON") from exc
    if not isinstance(payload, dict) or len(payload) > 128:
        raise RuntimeError(f"{env_name} must be a bounded JSON object")
    for pattern, services in payload.items():
        if (
            not isinstance(pattern, str)
            or not pattern.startswith("/")
            or len(pattern) > 256
            or not isinstance(services, list)
            or not 1 <= len(services) <= 32
        ):
            raise RuntimeError(f"{env_name} entry is invalid")
        normalized = frozenset(
            service.strip().lower()
            for service in services
            if isinstance(service, str) and _SERVICE_NAME_RE.fullmatch(service.strip().lower())
        )
        if len(normalized) != len(services):
            raise RuntimeError(f"{env_name} contains an invalid service")
        result[pattern] = frozenset(set(result.get(pattern, frozenset())) | set(normalized))
    return result


def allowed_callers_for_path(
    allowlist: dict[str, frozenset[str]],
    path: str,
) -> frozenset[str]:
    """Resolve every service allowed by an exact or globbed path rule."""
    callers: set[str] = set()
    for pattern, services in allowlist.items():
        if fnmatchcase(path, pattern):
            callers.update(services)
    return frozenset(callers)


__all__ = ["allowed_callers_for_path", "load_caller_allowlist"]
