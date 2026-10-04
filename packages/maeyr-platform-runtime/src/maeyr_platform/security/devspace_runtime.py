"""Request-bound encryption for service-to-service DevSpace credentials.

This envelope supplements the authenticated internal request protocol. It does
not replace caller/tenant authorization or replay protection at the endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from importlib import import_module
from typing import Any, cast

from maeyr_platform.runtime.agent_auth import is_safe_runtime_env_name

PATH = "/agent/internal/devspace-runtime"
SCHEMA = "maeyr-devspace-runtime-environment-v1"
_MAX_PLAINTEXT = 128 * 1024
_MAX_CIPHERTEXT = _MAX_PLAINTEXT + 16
_MAX_ENCODED_CIPHERTEXT = ((_MAX_CIPHERTEXT + 2) // 3) * 4
_MAX_REQUEST = 1024 * 1024
_MAX_VARIABLES = 256


class RuntimeEnvironmentError(ValueError):
    """A credential envelope could not be safely constructed or authenticated."""


def _invalid() -> RuntimeEnvironmentError:
    return RuntimeEnvironmentError("DevSpace runtime environment is invalid")


def validate_runtime_environment(environment: object) -> dict[str, str]:
    """Validate an isolated runtime mapping, including process-control injection."""
    if not isinstance(environment, dict) or len(environment) > _MAX_VARIABLES:
        raise _invalid()
    result: dict[str, str] = {}
    size = 0
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not is_safe_runtime_env_name(name)
            or not isinstance(value, str)
            or len(value) > 65536
            or "\x00" in value
        ):
            raise _invalid()
        try:
            value_size = len(value.encode("utf-8"))
        except UnicodeError:
            raise _invalid() from None
        size += len(name) + value_size
        if value_size > 65536 or size > _MAX_PLAINTEXT:
            raise _invalid()
        result[name] = value
    return result


def _cipher(key: str, request_body: bytes) -> tuple[Any, bytes]:
    if (
        not isinstance(key, str)
        or len(key) > 4096
        or not isinstance(request_body, bytes)
        or len(request_body) > _MAX_REQUEST
    ):
        raise _invalid()
    try:
        key_bytes = key.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    if not 32 <= len(key_bytes) <= 4096:
        raise _invalid()
    digest = hashlib.sha256(request_body).digest()
    domain = f"{SCHEMA}\x00{PATH}".encode("ascii")
    aad = domain + b"\x00" + digest
    try:
        hashes: Any = import_module("cryptography.hazmat.primitives.hashes")
        hkdf: Any = import_module("cryptography.hazmat.primitives.kdf.hkdf")
        aes: Any = import_module("cryptography.hazmat.primitives.ciphers.aead")
        derived = hkdf.HKDF(algorithm=hashes.SHA256(), length=32, salt=digest, info=domain).derive(
            key_bytes
        )
        return aes.AESGCM(derived), aad
    except Exception:
        raise RuntimeEnvironmentError("DevSpace runtime encryption is unavailable") from None


def _decode(value: object, *, max_encoded: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > max_encoded:
        raise _invalid()
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        raise _invalid() from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise _invalid()
    return decoded


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise _invalid()
        result[name] = value
    return result


def seal_environment(
    environment: dict[str, str], *, key: str, request_body: bytes
) -> dict[str, str]:
    """Encrypt declared credentials with a fresh nonce for this exact request."""
    validated = validate_runtime_environment(environment)
    plaintext = json.dumps(
        validated, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(plaintext) > _MAX_PLAINTEXT:
        raise _invalid()
    cipher, aad = _cipher(key, request_body)
    nonce = secrets.token_bytes(12)
    try:
        ciphertext = cast(bytes, cipher.encrypt(nonce, plaintext, aad))
    except Exception:
        raise RuntimeEnvironmentError("DevSpace runtime encryption failed") from None
    return {
        "schema": SCHEMA,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_environment(envelope: object, *, key: str, request_body: bytes) -> dict[str, str]:
    """Authenticate/decrypt an envelope without exposing supplied data in errors."""
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema", "nonce", "ciphertext"}
        or envelope.get("schema") != SCHEMA
    ):
        raise _invalid()
    nonce = _decode(envelope.get("nonce"), max_encoded=16)
    ciphertext = _decode(envelope.get("ciphertext"), max_encoded=_MAX_ENCODED_CIPHERTEXT)
    if len(nonce) != 12 or not 16 <= len(ciphertext) <= _MAX_CIPHERTEXT:
        raise _invalid()
    cipher, aad = _cipher(key, request_body)
    try:
        plaintext = cast(bytes, cipher.decrypt(nonce, ciphertext, aad))
    except Exception:
        raise RuntimeEnvironmentError(
            "DevSpace runtime environment could not be authenticated"
        ) from None
    if len(plaintext) > _MAX_PLAINTEXT:
        raise _invalid()
    try:
        environment: object = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_json_object)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    return validate_runtime_environment(environment)


__all__ = [
    "PATH",
    "SCHEMA",
    "RuntimeEnvironmentError",
    "open_environment",
    "seal_environment",
    "validate_runtime_environment",
]
