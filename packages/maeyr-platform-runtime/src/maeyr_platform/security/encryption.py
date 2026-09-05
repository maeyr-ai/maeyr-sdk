"""Dependency-light primitives shared by service-owned encryption adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any, Generic, TypeVar, cast

KeyT = TypeVar("KeyT")

_URLSAFE_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


def normalize_fernet_key(key_material: str) -> bytes:
    """Return a Fernet key, including compatibility for legacy platform secrets.

    Early platform releases generated ``RUNTIME_CREDENTIAL_ENCRYPTION_KEY`` with
    ``token_urlsafe(48)``.  That is strong secret material, but Fernet accepts
    exactly 32 decoded bytes.  Deriving a 32-byte key from the decoded legacy
    material is deterministic, so existing installations do not need to rotate
    their stable generated secret merely to correct its representation.
    """

    value = key_material.strip()
    if not value:
        raise ValueError("Fernet key material is empty")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Fernet key material must be ASCII") from exc

    try:
        decoded = base64.urlsafe_b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("Fernet key material is not URL-safe base64") from exc
    if len(decoded) == 32:
        return base64.urlsafe_b64encode(decoded)

    if len(value) == 64 and _URLSAFE_SECRET_RE.fullmatch(value) and len(decoded) == 48:
        return base64.urlsafe_b64encode(hashlib.sha256(decoded).digest())

    raise ValueError("Fernet key must decode to 32 bytes")


def generate_recovery_key(
    hash_passphrase: Callable[[str], str],
) -> tuple[str, str]:
    """Create a one-time recovery key and its caller-defined durable hash."""

    recovery_key = secrets.token_urlsafe(18)[:24]
    return recovery_key, hash_passphrase(recovery_key)


def fingerprint_and_zero_secret_buffer(secret: bytearray) -> str:
    """Fingerprint a transient secret and always erase the mutable input buffer."""

    try:
        return hashlib.sha256(bytes(secret)).hexdigest()
    finally:
        for index in range(len(secret)):
            secret[index] = 0


def get_aws_kms_client(
    kms_config: Mapping[str, Any],
    *,
    client_factory: Callable[..., Any],
) -> Any:
    """Build an AWS KMS client without making the shared runtime depend on boto3."""

    key_arn = kms_config.get("key_arn")
    if not isinstance(key_arn, str) or not key_arn.strip():
        raise ValueError("key_arn is required for AWS KMS")
    region = kms_config.get("region")
    if not region:
        parts = key_arn.split(":")
        region = parts[3] if len(parts) >= 4 else None

    access_key_id = kms_config.get("access_key_id")
    secret_access_key = kms_config.get("secret_access_key")
    session_token = kms_config.get("session_token")
    if bool(access_key_id) != bool(secret_access_key):
        raise ValueError(
            "access_key_id and secret_access_key must be provided together for AWS KMS"
        )
    if access_key_id and secret_access_key:
        return client_factory(
            "kms",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
        )
    return client_factory("kms", region_name=region)


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
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
    return kms.KeyManagementServiceClient(credentials=credentials)


__all__ = [
    "RequiredPrimaryKeyMixin",
    "derive_fernet_key",
    "fingerprint_and_zero_secret_buffer",
    "get_aws_kms_client",
    "generate_recovery_key",
    "get_gcp_kms_client",
    "normalize_fernet_key",
]
