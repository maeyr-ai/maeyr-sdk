"""Shared resource-allocation contract for every Maeyr service.

The account is the licensed pool, organizations receive slices of that pool,
and projects receive slices of an organization pool.  This module intentionally
contains no database or HTTP code so policy writers, runtime gates, clients,
and tests all use the same resource names and limit semantics.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

UNLIMITED: Final[int] = -1
MAX_RESOURCE_VALUE: Final[int] = 2_147_483_647
_OPERATION_KIND = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


@dataclass(frozen=True, slots=True)
class ProjectResource:
    """Canonical mapping between a resource, its limit, and its usage field."""

    name: str
    allocation_key: str
    usage_key: str


@dataclass(frozen=True, slots=True)
class HierarchicalLimit:
    """Resolved account -> organization -> project quota information."""

    account: int
    organization: int
    project: int
    effective: int
    constrained_by: str
    violations: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.violations


class ResourceMutationRejected(RuntimeError):
    """A quota authority definitively rejected a resource mutation."""

    def __init__(self, *, status_code: int, action: str, error_code: str | None = None):
        self.status_code = status_code
        self.action = action
        self.error_code = error_code
        super().__init__(f"Auth quota {action} was rejected with HTTP {status_code}")


_PROJECT_RESOURCES = (
    ProjectResource("agents", "max_agents", "agents_count"),
    ProjectResource("triggers", "max_triggers", "triggers_count"),
    ProjectResource("schedules", "max_schedules", "schedules_count"),
    ProjectResource("devspaces", "max_devspaces", "devspaces_count"),
    ProjectResource(
        "chrona_workers_secure",
        "max_chrona_workers_secure",
        "chrona_workers_secure_count",
    ),
    ProjectResource("maeyr_force", "max_maeyr_force", "maeyr_force_count"),
    ProjectResource(
        "cloud_worker_cpu",
        "max_cloud_worker_cpu_millicores",
        "cloud_worker_cpu_millicores_usage",
    ),
    ProjectResource(
        "cloud_worker_memory",
        "max_cloud_worker_memory_mb",
        "cloud_worker_memory_mb_usage",
    ),
)

PROJECT_RESOURCES: Mapping[str, ProjectResource] = MappingProxyType(
    {resource.name: resource for resource in _PROJECT_RESOURCES}
)
RESOURCE_TO_ALLOCATION_KEY: Mapping[str, str] = MappingProxyType(
    {resource.name: resource.allocation_key for resource in _PROJECT_RESOURCES}
)
RESOURCE_TO_USAGE_KEY: Mapping[str, str] = MappingProxyType(
    {resource.name: resource.usage_key for resource in _PROJECT_RESOURCES}
)
ALLOCATION_TO_USAGE_KEY: Mapping[str, str] = MappingProxyType(
    {resource.allocation_key: resource.usage_key for resource in _PROJECT_RESOURCES}
)

PROJECT_ALLOCATION_KEYS: tuple[str, ...] = tuple(
    resource.allocation_key for resource in _PROJECT_RESOURCES
)
ALLOCATION_RESOURCE_KEYS: tuple[str, ...] = (
    "max_projects",
    *PROJECT_ALLOCATION_KEYS,
)
DEFAULT_ALLOCATION: Mapping[str, int] = MappingProxyType(
    {key: UNLIMITED for key in ALLOCATION_RESOURCE_KEYS}
)
DEFAULT_RESOURCE_USAGE: Mapping[str, int] = MappingProxyType(
    {resource.usage_key: 0 for resource in _PROJECT_RESOURCES}
)


def validate_resource_limit(value: object) -> bool:
    """Return whether a stored or requested limit has canonical semantics."""

    return (
        type(value) is int
        and UNLIMITED <= value <= MAX_RESOURCE_VALUE
    )


def effective_child_limit(parent: int, child: int) -> int:
    """Resolve a child limit while never allowing it to exceed its parent.

    ``-1`` means inherit/unlimited within the parent rather than bypassing it.
    A negative parent therefore remains unlimited; otherwise the smaller
    materialized value is authoritative.
    """

    if not validate_resource_limit(parent) or not validate_resource_limit(child):
        raise ValueError("resource limits must be integers between -1 and 2147483647")
    if parent == UNLIMITED:
        return child
    if child == UNLIMITED:
        return parent
    return min(parent, child)


def effective_project_limit(account: int, organization: int, project: int) -> int:
    """Resolve account → organization → project into one authoritative limit."""

    return effective_child_limit(
        effective_child_limit(account, organization),
        project,
    )


def resolve_hierarchical_limit(
    account: int,
    organization: int,
    project: int,
) -> HierarchicalLimit:
    """Resolve a quota and diagnose persisted child allocations above a parent.

    ``-1`` is inheritance/unlimited. It is therefore never treated as an
    over-allocation, while every finite child allocation must fit inside its
    nearest finite parent. The effective limit is always safe even when legacy
    or administratively downgraded data violates that invariant.
    """

    for value in (account, organization, project):
        if not validate_resource_limit(value):
            raise ValueError("resource limits must be integers between -1 and 2147483647")

    violations: list[str] = []
    if account >= 0 and organization >= 0 and organization > account:
        violations.append("organization_exceeds_account")
    organization_effective = effective_child_limit(account, organization)
    if organization_effective >= 0 and project >= 0 and project > organization_effective:
        violations.append("project_exceeds_organization")

    candidates = (
        ("account", account),
        ("organization", organization),
        ("project", project),
    )
    finite = tuple((scope, value) for scope, value in candidates if value >= 0)
    if finite:
        constrained_by, effective = min(finite, key=lambda item: item[1])
    else:
        constrained_by, effective = "unlimited", UNLIMITED
    return HierarchicalLimit(
        account=account,
        organization=organization,
        project=project,
        effective=effective,
        constrained_by=constrained_by,
        violations=tuple(violations),
    )


def allocation_resource_dict() -> dict[str, int]:
    """Return a mutable default allocation for storage models."""

    return dict(DEFAULT_ALLOCATION)


def retained_operation_id(kind: str, entity_id: str) -> str:
    """Return a bounded, non-sensitive identity for one retained resource.

    Resource IDs can be user supplied, exceed Auth's operation-id limit, or
    contain characters that are unsafe to persist in a cross-service ledger.
    Every create/delete retry for the same entity must nevertheless address the
    same reservation.  Hashing only the entity portion preserves that identity
    without leaking it into Auth's quota ledger.
    """

    if not _OPERATION_KIND.fullmatch(kind):
        raise ValueError("operation kind must be 1-32 lowercase safe characters")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("entity_id must be a non-empty string")
    digest = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


__all__ = [
    "ALLOCATION_RESOURCE_KEYS",
    "ALLOCATION_TO_USAGE_KEY",
    "DEFAULT_ALLOCATION",
    "DEFAULT_RESOURCE_USAGE",
    "HierarchicalLimit",
    "MAX_RESOURCE_VALUE",
    "PROJECT_ALLOCATION_KEYS",
    "PROJECT_RESOURCES",
    "ProjectResource",
    "ResourceMutationRejected",
    "RESOURCE_TO_ALLOCATION_KEY",
    "RESOURCE_TO_USAGE_KEY",
    "UNLIMITED",
    "allocation_resource_dict",
    "effective_child_limit",
    "effective_project_limit",
    "resolve_hierarchical_limit",
    "retained_operation_id",
    "validate_resource_limit",
]
