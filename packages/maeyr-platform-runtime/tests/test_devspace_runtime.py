from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from maeyr_platform.security.devspace_runtime import (
    SCHEMA,
    RuntimeEnvironmentError,
    _cipher,
    open_environment,
    seal_environment,
)

KEY = "synthetic-internal-key-32-bytes-or-more"
REQUEST = b'{"agent_id":"agent-synthetic","org_id":"org-synthetic"}'


def _environment() -> dict[str, str]:
    return {
        "aviationstack_api_key": "synthetic-credential",
        "aviationstack_api.api_key": "synthetic-credential",
        "MAEYR_AUTH_ENABLED_METHODS": "aviationstack_api",
        "MAEYR_AUTH_CONFIGURED_METHODS": "aviationstack_api",
    }


def test_roundtrip_does_not_expose_values_and_uses_fresh_nonce() -> None:
    environment = _environment()
    first = seal_environment(environment, key=KEY, request_body=REQUEST)
    second = seal_environment(environment, key=KEY, request_body=REQUEST)
    assert first["schema"] == SCHEMA
    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]
    assert "synthetic-credential" not in json.dumps(first)
    assert open_environment(first, key=KEY, request_body=REQUEST) == environment
    assert open_environment(second, key=KEY, request_body=REQUEST) == environment


@pytest.mark.parametrize("changed", ["key", "request", "nonce", "ciphertext"])
def test_tampered_or_wrong_context_envelopes_fail_authenticated(changed: str) -> None:
    envelope = seal_environment(_environment(), key=KEY, request_body=REQUEST)
    key, request = KEY, REQUEST
    if changed == "key":
        key = "another-synthetic-key-at-least-32-bytes"
    elif changed == "request":
        request += b" "
    else:
        data = bytearray(base64.b64decode(envelope[changed]))
        data[0] ^= 1
        envelope[changed] = base64.b64encode(data).decode("ascii")
    with pytest.raises(RuntimeEnvironmentError, match="could not be authenticated") as error:
        open_environment(envelope, key=key, request_body=request)
    assert error.value.__suppress_context__
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        [],
        {},
        {"schema": SCHEMA},
        {"schema": "future", "nonce": "A" * 16, "ciphertext": "A" * 24},
        {"schema": SCHEMA, "nonce": 12, "ciphertext": "A" * 24},
        {"schema": SCHEMA, "nonce": "invalid!", "ciphertext": "A" * 24},
        {"schema": SCHEMA, "nonce": "A" * 16, "ciphertext": "!!!"},
        {"schema": SCHEMA, "nonce": "AA==", "ciphertext": "A" * 24},
        {"schema": SCHEMA, "nonce": "A" * 16, "ciphertext": "AA=="},
        {"schema": SCHEMA, "nonce": "A" * 16, "ciphertext": "A" * 180000},
        {"schema": SCHEMA, "nonce": "A" * 16, "ciphertext": "A" * 24, "extra": "x"},
    ],
)
def test_malformed_envelopes_are_rejected(envelope: object) -> None:
    with pytest.raises(RuntimeEnvironmentError, match="environment is invalid"):
        open_environment(envelope, key=KEY, request_body=REQUEST)


@pytest.mark.parametrize(
    "environment",
    [
        {"PATH": "/unsafe"},
        {"path": "/unsafe"},
        {"HOME": "/unsafe"},
        {"UID": "0"},
        {"EUID": "0"},
        {"GID": "0"},
        {"PIP_CONFIG_FILE": "unsafe"},
        {"PIP_INDEX_URL": "unsafe"},
        {"PIP_EXTRA_INDEX_URL": "unsafe"},
        {"pip_user": "unsafe"},
        {"LD_PRELOAD": "unsafe"},
        {"PYTHONPATH": "unsafe"},
        {"BASH_ENV": "unsafe"},
        {"ENV": "unsafe"},
        {"HTTPS_PROXY": "unsafe"},
        {"http_proxy": "unsafe"},
        {"ALL_PROXY": "unsafe"},
        {"no_proxy": "unsafe"},
        {"MAEYR_ORG_ID": "other-tenant"},
        {"nested.auth.key": "value"},
        {"bad-name": "value"},
        {"okay": 2},
        {2: "value"},
        {"okay": "nul\x00value"},
        {"okay": "x" * 65537},
        {"okay": "\ud800"},
        {f"key_{i}": "x" for i in range(257)},
        {"a": "x" * 65536, "b": "x" * 65536},
    ],
)
def test_unsafe_or_oversized_environments_are_rejected(environment: Any) -> None:
    with pytest.raises(RuntimeEnvironmentError, match="environment is invalid"):
        seal_environment(environment, key=KEY, request_body=REQUEST)


def _authenticated_plaintext(plaintext: bytes) -> dict[str, str]:
    cipher, aad = _cipher(KEY, REQUEST)
    nonce = b"synthetic-12b"
    encrypted = cipher.encrypt(nonce, plaintext, aad)
    return {
        "schema": SCHEMA,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
    }


@pytest.mark.parametrize(
    "plaintext",
    [
        b"[]",
        b'{"PATH":"unsafe"}',
        b'{"okay":null}',
        b'{"duplicate":"first","duplicate":"second"}',
        b"invalid-json",
        b'{"okay":"\\u0000"}',
        b"\xff",
    ],
)
def test_authenticated_malformed_or_injected_plaintext_is_still_rejected(plaintext: bytes) -> None:
    with pytest.raises(RuntimeEnvironmentError, match="environment is invalid"):
        open_environment(_authenticated_plaintext(plaintext), key=KEY, request_body=REQUEST)


@pytest.mark.parametrize("key", ["", "too-short", "x" * 4097, "\ud800" * 32])
def test_invalid_keys_fail_without_echoing_material(key: str) -> None:
    with pytest.raises(RuntimeEnvironmentError, match="environment is invalid"):
        seal_environment({}, key=key, request_body=REQUEST)


def test_request_size_is_bounded() -> None:
    with pytest.raises(RuntimeEnvironmentError):
        seal_environment({}, key=KEY, request_body=b"x" * (1024 * 1024 + 1))


def test_module_import_does_not_load_optional_crypto_provider() -> None:
    source = Path(__file__).parents[1] / "src"
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "import maeyr_platform.security.devspace_runtime; "
        "assert not any(n == 'cryptography' or n.startswith('cryptography.') for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-B", "-c", script], check=True, capture_output=True)
