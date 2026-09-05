from __future__ import annotations

import pytest

from maeyr_platform.resource_allocation import (
    ALLOCATION_RESOURCE_KEYS,
    PROJECT_ALLOCATION_KEYS,
    PROJECT_RESOURCES,
    RESOURCE_TO_ALLOCATION_KEY,
    RESOURCE_TO_USAGE_KEY,
    ResourceMutationRejected,
    effective_child_limit,
    effective_project_limit,
    resolve_hierarchical_limit,
    retained_operation_id,
    validate_resource_limit,
)


def test_resource_mappings_are_complete_and_one_to_one() -> None:
    assert tuple(RESOURCE_TO_ALLOCATION_KEY) == tuple(PROJECT_RESOURCES)
    assert tuple(RESOURCE_TO_USAGE_KEY) == tuple(PROJECT_RESOURCES)
    assert tuple(RESOURCE_TO_ALLOCATION_KEY.values()) == PROJECT_ALLOCATION_KEYS
    assert ALLOCATION_RESOURCE_KEYS == ("max_projects", *PROJECT_ALLOCATION_KEYS)
    assert len(set(RESOURCE_TO_USAGE_KEY.values())) == len(PROJECT_RESOURCES)


@pytest.mark.parametrize("value", [-1, 0, 1, 2_147_483_647])
def test_validate_resource_limit_accepts_canonical_values(value: int) -> None:
    assert validate_resource_limit(value)


@pytest.mark.parametrize("value", [True, False, -2, 2_147_483_648, 1.0, "1", None])
def test_validate_resource_limit_rejects_ambiguous_values(value: object) -> None:
    assert not validate_resource_limit(value)


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        (-1, -1, -1),
        (-1, 8, 8),
        (10, -1, 10),
        (10, 8, 8),
        (8, 10, 8),
    ],
)
def test_effective_child_limit_never_bypasses_parent(
    parent: int, child: int, expected: int
) -> None:
    assert effective_child_limit(parent, child) == expected


def test_effective_project_limit_uses_smallest_finite_scope() -> None:
    assert effective_project_limit(10, 8, 5) == 5
    assert effective_project_limit(5, 8, 10) == 5
    assert effective_project_limit(-1, 8, -1) == 8


def test_resolve_hierarchy_reports_both_persisted_parent_violations() -> None:
    result = resolve_hierarchical_limit(5, 8, 10)

    assert result.effective == 5
    assert result.constrained_by == "account"
    assert result.violations == (
        "organization_exceeds_account",
        "project_exceeds_organization",
    )
    assert result.valid is False


def test_resolve_hierarchy_treats_unlimited_as_inheritance() -> None:
    result = resolve_hierarchical_limit(5, -1, -1)

    assert result.effective == 5
    assert result.constrained_by == "account"
    assert result.violations == ()
    assert result.valid is True


@pytest.mark.parametrize("bad", [-2, True, 1.5])
def test_resolve_hierarchy_rejects_noncanonical_storage(bad: object) -> None:
    with pytest.raises(ValueError):
        resolve_hierarchical_limit(5, bad, 1)  # type: ignore[arg-type]


def test_retained_operation_id_is_stable_bounded_and_does_not_expose_entity() -> None:
    operation_id = retained_operation_id("secure-worker", "WK/customer supplied id")

    assert operation_id == retained_operation_id("secure-worker", "WK/customer supplied id")
    assert operation_id.startswith("secure-worker:")
    assert "customer" not in operation_id
    assert len(operation_id) <= 128


@pytest.mark.parametrize(
    ("kind", "entity_id"),
    [("", "id"), ("UPPER", "id"), ("bad_kind", "id"), ("worker", "")],
)
def test_retained_operation_id_rejects_invalid_inputs(kind: str, entity_id: str) -> None:
    with pytest.raises(ValueError):
        retained_operation_id(kind, entity_id)


def test_resource_mutation_rejection_preserves_machine_readable_context() -> None:
    error = ResourceMutationRejected(
        status_code=409,
        action="reserve",
        error_code="quota_exhausted",
    )

    assert error.status_code == 409
    assert error.action == "reserve"
    assert error.error_code == "quota_exhausted"
    assert str(error) == "Auth quota reserve was rejected with HTTP 409"
