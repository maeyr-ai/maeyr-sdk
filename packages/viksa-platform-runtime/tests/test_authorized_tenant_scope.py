from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from viksa_platform.auth.tenant_context import (
    apply_ws_tenant,
    permission_data_scope,
    resolve_tenant_ids,
)


def _user(*, account=False, organization=False, context=True):
    permission = [{"module": "analytics", "actions": ["view"]}]
    return {
        "org_id": "OI-A",
        "project_id": "PI-A",
        "access": {
            "account_permissions": permission if account else [],
            "organization_permissions": permission if organization else [],
            "permissions": permission if context else [],
        },
    }


def test_query_parameter_cannot_switch_the_validated_tenant() -> None:
    request = SimpleNamespace(
        headers={"x-tenant-org-id": "OI-A", "x-tenant-project-id": "PI-A"},
        query_params={"project_id": "PI-B"},
    )
    with pytest.raises(HTTPException) as exc:
        resolve_tenant_ids(request, _user())  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def test_websocket_payload_cannot_switch_the_validated_tenant() -> None:
    with pytest.raises(HTTPException) as exc:
        apply_ws_tenant(
            {"headers": []},
            _user(),
            {"org_id": "OI-A", "project_id": "PI-B"},
        )
    assert exc.value.status_code == 403


def test_context_permission_is_pinned_to_the_exact_project() -> None:
    assert permission_data_scope(_user(), "analytics", "view") == {
        "org_id": "OI-A",
        "project_id": "PI-A",
    }
    with pytest.raises(HTTPException):
        permission_data_scope(
            _user(), "analytics", "view", project_id="PI-B"
        )


def test_organization_permission_can_narrow_to_a_project_in_its_org() -> None:
    assert permission_data_scope(
        _user(organization=True),
        "analytics",
        "view",
        project_id="PI-B",
    ) == {"org_id": "OI-A", "project_id": "PI-B"}
    with pytest.raises(HTTPException):
        permission_data_scope(
            _user(organization=True),
            "analytics",
            "view",
            org_id="OI-B",
        )


def test_account_permission_may_span_the_account_database() -> None:
    assert permission_data_scope(
        _user(account=True),
        "analytics",
        "view",
        org_id="OI-B",
        project_id="PI-B",
    ) == {"org_id": "OI-B", "project_id": "PI-B"}


def test_missing_permission_fails_closed() -> None:
    with pytest.raises(HTTPException) as exc:
        permission_data_scope(
            _user(context=False), "analytics", "view"
        )
    assert exc.value.status_code == 403
