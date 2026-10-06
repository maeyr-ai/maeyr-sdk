"""Separate caller keys, exact query/body authentication and Redis replay fencing."""

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

from .errors import ServiceError


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sign(key: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    material = f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(key.encode(), material.encode(), hashlib.sha256).hexdigest()


def headers(key: str, method: str, path: str, body: bytes) -> dict[str, str]:
    stamp, nonce = str(int(time.time())), secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "X-Maeyr-Timestamp": stamp,
        "X-Maeyr-Nonce": nonce,
        "X-Maeyr-Signature": sign(key, stamp, nonce, method, path, body),
    }


def verify(
    key: str,
    method: str,
    path: str,
    body: bytes,
    incoming: Mapping[str, str],
    now: int | None = None,
) -> str:
    stamp, nonce = incoming.get("x-maeyr-timestamp", ""), incoming.get("x-maeyr-nonce", "")
    signature = incoming.get("x-maeyr-signature", "")
    if (
        len(key.encode()) < 32
        or not stamp.isascii()
        or not stamp.isdigit()
        or len(stamp) > 12
        or abs((int(time.time()) if now is None else now) - int(stamp)) > 60
        or len(nonce) != 32
        or any(c not in "0123456789abcdef" for c in nonce)
        or len(signature) != 64
        or not hmac.compare_digest(sign(key, stamp, nonce, method, path, body), signature)
    ):
        raise ServiceError("internal_authentication_failed", "Invalid internal authentication", 401)
    return nonce


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result
