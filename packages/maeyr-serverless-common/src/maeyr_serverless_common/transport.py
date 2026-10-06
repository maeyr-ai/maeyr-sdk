"""Bounded pooled HTTP; no redirects, proxy inheritance, or automatic POST retry."""

import hashlib
import hmac
import json
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import ServiceError
from .security import encode, headers


class SignedClient:
    def __init__(
        self,
        origin: str,
        key: str,
        *,
        builder: bool = False,
        client: httpx.AsyncClient | None = None,
        server_name: str | None = None,
    ) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Internal service URL must be an origin")
        if len(key.encode()) < 32:
            raise ValueError("Internal key must contain at least 32 bytes")
        self.origin, self.key, self.builder = origin.rstrip("/"), key, builder
        self.server_name = server_name
        ca_file = os.environ.get("SERVERLESS_HTTP_CA_FILE")
        if ca_file and Path(ca_file).stat().st_size > 131_072:
            raise ValueError("HTTP TLS trust material exceeds its size bound")
        trust = ssl.create_default_context(cafile=ca_file) if ca_file else True
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3),
            trust_env=False,
            follow_redirects=False,
            verify=trust,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=128),
        )

    async def request(self, method: str, path: str, value: dict[str, Any] | None) -> dict[str, Any]:
        body = encode(value) if value is not None else b""
        signed = self.sign_headers(method, path, body, value)
        if self.builder:
            stamp = signed["X-Maeyr-Timestamp"]
            material = f"{stamp}\n{method}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode()
            signed["X-Maeyr-Signature"] = hmac.new(
                self.key.encode(), material, hashlib.sha256
            ).hexdigest()
            signed.pop("X-Maeyr-Nonce")
        try:
            async with self.client.stream(
                method,
                self.origin + path,
                content=body,
                headers=signed,
                extensions={"sni_hostname": self.server_name} if self.server_name else None,
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8_388_608:
                        raise ServiceError(
                            "output_limit", "Internal response exceeds its size limit", 502
                        )
                if response.status_code >= 300:
                    status = (
                        response.status_code
                        if response.status_code in {401, 403, 404, 409, 422, 429, 503, 504}
                        else 502
                    )
                    raise ServiceError(
                        "upstream_rejected",
                        "The execution dependency rejected this request",
                        status,
                    )
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("Invalid response")
                return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise ServiceError(
                "transport_unavailable", "The execution dependency is temporarily unavailable"
            ) from None

    async def close(self) -> None:
        await self.client.aclose()

    def sign_headers(self, method: str, path: str, body: bytes, value: Any) -> dict[str, str]:
        return headers(self.key, method, path, body)
