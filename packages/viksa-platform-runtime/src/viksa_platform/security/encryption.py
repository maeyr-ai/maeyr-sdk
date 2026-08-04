"""Dependency-light primitives shared by service-owned encryption adapters."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any, Generic, TypeVar, cast

KeyT = TypeVar("KeyT")


class RequiredPrimaryKeyMixin(Generic[KeyT]):
    """Resolve the active key from a service-owned, versioned keyring."""

    _primary_version: str
    _keyring: Mapping[str, KeyT]
    _missing_primary_key_exception: type[Exception]
    _missing_primary_key_message: str

    def _require_primary(self) -> KeyT:
        key = self._keyring.get(self._primary_version)
        if key is None:
            raise self._missing_primary_key_exception(self._missing_primary_key_message)
        return key


def derive_fernet_key(
    seed: str,
    salt: str,
    *,
    master_salt: str,
    iterations: int,
) -> bytes:
    """Derive a Fernet-compatible key while loading cryptography only on demand."""

    hashes: Any = import_module("cryptography.hazmat.primitives.hashes")
    backends: Any = import_module("cryptography.hazmat.backends")
    pbkdf2: Any = import_module("cryptography.hazmat.primitives.kdf.pbkdf2")
    kdf = pbkdf2.PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=(master_salt + salt).encode(),
        iterations=iterations,
        backend=backends.default_backend(),
    )
    return base64.urlsafe_b64encode(cast(bytes, kdf.derive(seed.encode())))


def generate_recovery_key(
    hash_passphrase: Callable[[str], str],
) -> tuple[str, str]:
    """Create a one-time recovery key and its caller-defined durable hash."""

    recovery_key = secrets.token_urlsafe(18)[:24]
    return recovery_key, hash_passphrase(recovery_key)


def get_gcp_kms_client(kms_config: Mapping[str, Any]) -> Any:
    """Build a GCP KMS client from validated configuration or ambient identity."""

    required_fields = ("project_id", "location", "key_ring", "key_name")
    for field in required_fields:
        if not kms_config.get(field):
            raise ValueError(f"{field} is required for GCP KMS")

    kms: Any = import_module("google.cloud.kms")
    service_account_json = kms_config.get("service_account_json")
    if not service_account_json:
        return kms.KeyManagementServiceClient()

    credentials_info = (
        json.loads(service_account_json)
        if isinstance(service_account_json, str)
        else service_account_json
    )
    service_account: Any = import_module("google.oauth2.service_account")
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info
    )
    return kms.KeyManagementServiceClient(credentials=credentials)


__all__ = [
    "RequiredPrimaryKeyMixin",
    "derive_fernet_key",
    "generate_recovery_key",
    "get_gcp_kms_client",
]
