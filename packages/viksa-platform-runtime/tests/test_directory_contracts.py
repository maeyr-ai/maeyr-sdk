from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from viksa_platform.directory.access_policy import VoltAccessPolicy
from viksa_platform.directory.project_user_csv import (
    format_project_users_csv,
    parse_project_users_csv,
)
from viksa_platform.directory.slack_access_grant import (
    coerce_expires_at,
    grant_is_active,
    grant_is_expired,
)
from viksa_platform.directory.tenant_database import (
    database_for_account,
    document_scope,
    extract_tenant_scope,
    project_filter,
)


def test_project_user_csv_round_trip_preserves_flat_contract() -> None:
    rendered = format_project_users_csv(
        [
            {
                "customer_user_id": "customer-1",
                "profile": {"email": "user@example.test"},
                "enabled": False,
            }
        ],
        fieldnames=["customer_user_id", "email", "enabled"],
    )

    rows, errors = parse_project_users_csv(rendered)

    assert errors == []
    assert rows == [
        {
            "customer_user_id": "customer-1",
            "email": "user@example.test",
            "enabled": "False",
        }
    ]


def test_project_user_csv_rejects_empty_input_and_header_only_input() -> None:
    assert parse_project_users_csv("") == ([], ["empty CSV"])
    assert parse_project_users_csv("customer_user_id,email") == (
        [],
        ["no data rows found"],
    )


def test_slack_grant_policy_normalizes_utc_and_expiry_boundary() -> None:
    now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    expires_at = now + timedelta(seconds=1)
    grant = {"enabled": True, "expires_at": expires_at.isoformat()}

    assert coerce_expires_at("2026-08-04T12:00:01Z") == expires_at
    assert grant_is_active(grant, now)
    assert not grant_is_expired(grant, now)
    assert not grant_is_active(grant, expires_at)
    assert grant_is_expired(grant, expires_at)


def test_access_policy_defaults_are_not_shared_between_instances() -> None:
    first = VoltAccessPolicy(
        account_id="account-1",
        org_id="org-1",
        project_id="project-1",
        name="default",
    )
    second = VoltAccessPolicy(
        account_id="account-1",
        org_id="org-1",
        project_id="project-1",
        name="other",
    )

    first.principals.users.append("user-1")

    assert second.principals.users == []
    assert first.effect == "allow"


def test_tenant_database_policy_validates_and_fences_project_scope() -> None:
    scope = extract_tenant_scope(
        {
            "account_id": " account-1 ",
            "org_id": " org-1 ",
            "project_id": " project-1 ",
        }
    )

    assert database_for_account(scope["account_id"]) == "account-1"
    assert project_filter(scope) == {"org_id": "org-1", "project_id": "project-1"}
    assert document_scope(scope) == scope
    with pytest.raises(ValueError, match="reserved Mongo database"):
        database_for_account("admin")
