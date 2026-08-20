from __future__ import annotations

from viksa_platform.auth.permission_checker import has_permission


def _access(module: str, action: str) -> dict[str, object]:
    return {
        "is_admin": False,
        "is_account_owner": False,
        "permissions": [{"module": module, "actions": [action]}],
        "denied": [],
    }


def test_historical_api_alias_only_grants_api_fleet_access() -> None:
    access = _access("api_fleet", "create")

    assert has_permission(access, "api", "create") is True
    assert has_permission(access, "api_fleet", "create") is True
    assert has_permission(access, "api_keys", "create") is False


def test_api_key_administration_never_grants_api_fleet_access() -> None:
    access = _access("api_keys", "create")

    assert has_permission(access, "api_keys", "create") is True
    assert has_permission(access, "api_fleet", "create") is False
    assert has_permission(access, "api", "create") is False


def test_denies_remain_canonical_across_historical_api_alias() -> None:
    access = _access("api_fleet", "delete")
    access["denied"] = [{"module": "api", "actions": ["delete"]}]

    assert has_permission(access, "api_fleet", "delete") is False
    assert has_permission(access, "api", "delete") is False


def test_analytics_view_does_not_grant_account_wide_analytics() -> None:
    access = _access("analytics", "view")

    assert has_permission(access, "analytics", "view") is True
    assert has_permission(access, "analytics", "view_all") is False
    assert has_permission(access, "analytics", "export") is False


def test_directory_read_access_cannot_mutate_execute_or_export() -> None:
    access = _access("directory_sync", "view")

    assert has_permission(access, "directory_sync", "view") is True
    assert has_permission(access, "directory_sync", "configure") is False
    assert has_permission(access, "directory_sync", "execute") is False
    assert has_permission(access, "directory_sync", "export") is False


def test_permission_grants_are_scoped_to_the_requested_level() -> None:
    access = _access("billing", "view")
    access["organization_permissions"] = [
        {"module": "billing", "actions": ["export"]}
    ]
    access["account_permissions"] = [
        {"module": "billing", "actions": ["manage"]}
    ]

    assert has_permission(access, "billing", "view") is True
    assert has_permission(access, "billing", "export") is False
    assert (
        has_permission(access, "billing", "export", grant_scope="organization")
        is True
    )
    assert has_permission(access, "billing", "manage") is False
    assert (
        has_permission(access, "billing", "manage", grant_scope="account")
        is True
    )


def test_wildcard_or_similar_action_names_do_not_escalate() -> None:
    access = _access("analytics", "view*")

    assert has_permission(access, "analytics", "view") is False
    assert has_permission(access, "analytics", "view_all") is False


def test_invalid_grant_scope_fails_closed() -> None:
    access = _access("analytics", "view")

    assert has_permission(access, "analytics", "view", grant_scope="tenant") is False


def test_truthy_strings_cannot_impersonate_admin_or_owner() -> None:
    access = _access("analytics", "view")
    access["permissions"] = []
    access["is_admin"] = "true"
    access["is_account_owner"] = 1

    assert has_permission(access, "billing", "manage", grant_scope="account") is False


def test_malformed_permission_collections_fail_closed_without_raising() -> None:
    malformed = [
        {"permissions": "analytics:view", "denied": []},
        {"permissions": ["analytics:view"], "denied": []},
        {"permissions": [{"module": "analytics", "actions": "view"}], "denied": []},
        {"permissions": [{"module": "analytics", "actions": [None, "view"]}], "denied": []},
        {"permissions": [{"module": {}, "actions": ["view"]}], "denied": []},
        {"permissions": [{"module": "analytics", "actions": ["view"]}], "denied": {}},
        {
            "permissions": [{"module": "analytics", "actions": ["view"]}],
            "denied": [{"module": "analytics", "actions": "delete"}],
        },
        {
            "permissions": [{"module": "analytics", "actions": ["view"]}],
            "denied": ["invalid-deny"],
        },
    ]

    for access in malformed:
        assert has_permission(access, "analytics", "view") is False


def test_non_string_or_empty_requested_permissions_fail_closed() -> None:
    access = _access("analytics", "view")

    assert has_permission(access, "", "view") is False
    assert has_permission(access, "analytics", "") is False
    assert has_permission(access, None, "view") is False  # type: ignore[arg-type]
    assert has_permission(access, "analytics", None) is False  # type: ignore[arg-type]
