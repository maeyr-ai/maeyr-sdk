from __future__ import annotations

from viksa_platform.directory.channel_platform import (
    ChannelConnectionStatusResponse,
    ChannelConnectorType,
    ChannelInstallResponse,
    ChannelStatusesResponse,
    ChannelUninstallResponse,
)


def test_status_contract_defaults_untyped_rows_to_agent() -> None:
    status = ChannelConnectionStatusResponse(
        account_id="AC-1",
        org_id="OI-1",
        project_id="PI-1",
        channel="slack",
        installed=True,
        lifecycle_state="active",
    )

    assert status.connector_type is ChannelConnectorType.AGENT
    dumped = status.model_dump(mode="json")
    assert dumped["connector_type"] == "agent"


def test_status_list_and_install_contracts_require_one_record_per_mode() -> None:
    status = ChannelConnectionStatusResponse.model_validate(
        {
            "account_id": "AC-1",
            "org_id": "OI-1",
            "project_id": "PI-1",
            "channel": "telegram",
            "installed": True,
            "lifecycle_state": "active",
            "connector_type": "api",
        }
    )
    listed = ChannelStatusesResponse.model_validate(
        {
            "scope": {"account_id": "AC-1", "org_id": "OI-1", "project_id": "PI-1"},
            "channels": {"telegram_api": status.model_dump(mode="json")},
        }
    )
    installed = ChannelInstallResponse.model_validate(
        {
            "installation": {"channel": "telegram", "connector_type": "api"},
            "status": status.model_dump(mode="json"),
            "validation": {"valid": True, "reason": "ok"},
        }
    )
    removed = ChannelUninstallResponse.model_validate(
        {"channel": "telegram", "deleted": True, "connector_type": "api"}
    )

    assert listed.channels["telegram_api"].connector_type is ChannelConnectorType.API
    assert installed.status.connector_type is ChannelConnectorType.API
    assert removed.connector_type is ChannelConnectorType.API
