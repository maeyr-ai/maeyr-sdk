from __future__ import annotations

import dataclasses

import pytest

from maeyr_platform.security import SecretStrengthPolicy


def test_secret_strength_policy_rejects_placeholders_and_low_entropy() -> None:
    policy = SecretStrengthPolicy(minimum_length=16)

    assert policy.rejects("")
    assert policy.rejects("replace-with-secret-value")
    assert policy.rejects("aaaaaaaaaaaaaaaa")
    assert not policy.rejects("e7$W9q!2Lx#4Vc@8")


def test_secret_strength_policy_is_validated_and_immutable() -> None:
    with pytest.raises(ValueError, match="minimum_length"):
        SecretStrengthPolicy(minimum_length=0)
    with pytest.raises(ValueError, match="placeholder_tokens"):
        SecretStrengthPolicy(minimum_length=8, placeholder_tokens=frozenset({"Mixed"}))

    policy = SecretStrengthPolicy(minimum_length=8)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.minimum_length = 12  # type: ignore[misc]  # runtime immutability assertion
