import pytest
from pydantic import ValidationError

from maeyr_platform.directory.access_policy import PolicyConditions, VoltAccessPolicy


def test_channel_conditions_round_trip_without_ip_restrictions() -> None:
    policy = VoltAccessPolicy(
        account_id="AC-1",
        org_id="OI-1",
        project_id="PI-1",
        name="Support channel access",
        resources=["support_agent"],
        conditions=PolicyConditions(channels=["slack"], ip_ranges=[]),
    )
    assert VoltAccessPolicy.model_validate(policy.model_dump()) == policy
    assert policy.conditions.ip_ranges == []


@pytest.mark.parametrize("effect", ["allow", "deny"])
@pytest.mark.parametrize("ip_ranges", [["203.0.113.0/24"], ["0.0.0.0/0"], ["::/0"]])
def test_cannot_write_a_policy_that_pretends_to_enforce_customer_ips(
    effect: str, ip_ranges: list[str]
) -> None:
    with pytest.raises(ValidationError, match="trusted customer client IP"):
        VoltAccessPolicy.model_validate(
            {
                "account_id": "AC-1",
                "org_id": "OI-1",
                "project_id": "PI-1",
                "name": "Network restricted policy",
                "effect": effect,
                "resources": ["private_agent"],
                "conditions": {"ip_ranges": ip_ranges},
            }
        )
