from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from maeyr_platform.security.encryption import (
    fingerprint_and_zero_secret_buffer,
    get_aws_kms_client,
    normalize_fernet_key,
)


def test_normalize_fernet_key_preserves_canonical_key() -> None:
    canonical = base64.urlsafe_b64encode(bytes(range(32)))

    assert normalize_fernet_key(canonical.decode("ascii")) == canonical


def test_normalize_fernet_key_derives_legacy_platform_secret_deterministically() -> None:
    legacy_bytes = bytes(range(48))
    legacy = base64.urlsafe_b64encode(legacy_bytes).decode("ascii")
    expected = base64.urlsafe_b64encode(hashlib.sha256(legacy_bytes).digest())

    assert normalize_fernet_key(legacy) == expected
    assert normalize_fernet_key(legacy) == expected


@pytest.mark.parametrize("invalid", ["", "not-base64!", "YQ==", "A" * 60])
def test_normalize_fernet_key_rejects_invalid_material(invalid: str) -> None:
    with pytest.raises(ValueError):
        normalize_fernet_key(invalid)


def test_fingerprint_erases_mutable_secret_buffer() -> None:
    secret = bytearray(b"sensitive-value")

    fingerprint = fingerprint_and_zero_secret_buffer(secret)

    assert fingerprint == hashlib.sha256(b"sensitive-value").hexdigest()
    assert secret == bytearray(len(secret))


def test_aws_kms_uses_ambient_identity_without_static_credentials() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    result = get_aws_kms_client(
        {"key_arn": "arn:aws:kms:ap-south-1:123456789012:key/example"},
        client_factory=factory,
    )

    assert result is not None
    assert calls == [(('kms',), {"region_name": "ap-south-1"})]


def test_aws_kms_passes_complete_explicit_credentials() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    get_aws_kms_client(
        {
            "key_arn": "arn:aws:kms:us-east-1:123456789012:key/example",
            "access_key_id": "access",
            "secret_access_key": "secret",
            "session_token": "session",
        },
        client_factory=factory,
    )

    assert calls == [
        (
            ("kms",),
            {
                "region_name": "us-east-1",
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
                "aws_session_token": "session",
            },
        )
    ]


@pytest.mark.parametrize(
    "config",
    [
        {"key_arn": "arn:aws:kms:us-east-1:1:key/x", "access_key_id": "only"},
        {
            "key_arn": "arn:aws:kms:us-east-1:1:key/x",
            "secret_access_key": "only",
        },
    ],
)
def test_aws_kms_rejects_partial_static_credentials(config: dict[str, str]) -> None:
    called = False

    def factory(*args: Any, **kwargs: Any) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(ValueError, match="must be provided together"):
        get_aws_kms_client(config, client_factory=factory)

    assert called is False


def test_aws_kms_requires_key_arn_before_constructing_client() -> None:
    with pytest.raises(ValueError, match="key_arn is required"):
        get_aws_kms_client({}, client_factory=lambda *args, **kwargs: object())
