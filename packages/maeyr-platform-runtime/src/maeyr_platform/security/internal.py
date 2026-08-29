"""Exact-body HMAC contracts for authenticated service-to-service requests."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

HDR_SERVICE = "x-internal-service"
HDR_TIMESTAMP = "x-internal-timestamp"
HDR_SIGNATURE = "x-internal-signature"
HDR_NONCE = "x-internal-nonce"
HDR_ACCOUNT_ID = "x-internal-account-id"
HDR_ORGANIZATION_ID = "x-internal-org-id"
HDR_PROJECT_ID = "x-internal-project-id"

DEFAULT_MAX_SKEW_SECONDS = 300
MINIMUM_KEY_BYTES = 32

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,255}$")
_METHOD_PATTERN = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")


def _validated_identifier(value: str, *, label: str, optional: bool = False) -> str:
    normalized = value.strip()
    if optional and not normalized:
        return ""
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _validated_key(key: bytes, *, label: str) -> bytes:
    if not isinstance(key, bytes) or len(key) < MINIMUM_KEY_BYTES:
        raise ValueError(f"{label} must contain at least {MINIMUM_KEY_BYTES} bytes")
    return key


def _header(headers: Mapping[str, str], *names: str) -> str:
    accepted = {name.lower() for name in names}
    matches = [str(value).strip() for name, value in headers.items() if name.lower() in accepted]
    if len(matches) > 1:
        raise ValueError("duplicate canonical header")
    return matches[0] if matches else ""


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Canonical tenant identity signed into an internal request."""

    account_id: str = ""
    organization_id: str = ""
    project_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_id",
            _validated_identifier(self.account_id, label="account_id", optional=True),
        )
        object.__setattr__(
            self,
            "organization_id",
            _validated_identifier(
                self.organization_id,
                label="organization_id",
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "project_id",
            _validated_identifier(self.project_id, label="project_id", optional=True),
        )

    @property
    def org_id(self) -> str:
        """Compatibility name used by existing internal-call code."""

        return self.organization_id

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> TenantContext:
        return cls(
            account_id=_header(headers, HDR_ACCOUNT_ID),
            organization_id=_header(headers, HDR_ORGANIZATION_ID),
            project_id=_header(headers, HDR_PROJECT_ID),
        )

    def as_headers(self) -> dict[str, str]:
        values = (
            (HDR_ACCOUNT_ID, self.account_id),
            (HDR_ORGANIZATION_ID, self.organization_id),
            (HDR_PROJECT_ID, self.project_id),
        )
        return {name: value for name, value in values if value}


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Authenticated service identity; route authorization remains service-owned."""

    service: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service",
            _validated_identifier(self.service, label="service"),
        )


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Immutable current/previous HMAC keys for receiver-first rotation."""

    current: bytes = field(repr=False)
    previous: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        current = _validated_key(self.current, label="current key")
        previous = tuple(
            _validated_key(key, label=f"previous key {index}")
            for index, key in enumerate(self.previous)
        )
        if current in previous or len(set(previous)) != len(previous):
            raise ValueError("key ring contains duplicate key material")
        object.__setattr__(self, "current", current)
        object.__setattr__(self, "previous", previous)

    @classmethod
    def from_strings(cls, current: str, *previous: str) -> KeyRing:
        return cls(
            current=current.encode("utf-8"),
            previous=tuple(value.encode("utf-8") for value in previous),
        )

    def _candidates(self) -> tuple[tuple[str, bytes], ...]:
        return (("current", self.current),) + tuple(
            (f"previous:{index}", key) for index, key in enumerate(self.previous)
        )


@dataclass(frozen=True, slots=True)
class SigningHeaders:
    """Typed authentication headers returned by a signer."""

    caller: CallerContext
    tenant: TenantContext
    timestamp: int
    nonce: str
    signature: str

    def __post_init__(self) -> None:
        if self.timestamp < 0:
            raise ValueError("timestamp is invalid")
        if self.nonce and not _NONCE_PATTERN.fullmatch(self.nonce):
            raise ValueError("nonce is invalid")
        if not _SIGNATURE_PATTERN.fullmatch(self.signature):
            raise ValueError("signature is invalid")

    def as_dict(self) -> dict[str, str]:
        headers = {
            HDR_SERVICE: self.caller.service,
            HDR_TIMESTAMP: str(self.timestamp),
            HDR_SIGNATURE: self.signature,
        }
        if self.nonce:
            headers[HDR_NONCE] = self.nonce
        headers.update(self.tenant.as_headers())
        return headers


@dataclass(frozen=True, slots=True)
class VerifiedCaller:
    """Successful verification result for subsequent service authorization."""

    caller: CallerContext
    tenant: TenantContext
    signed_at: int
    key_slot: str


class SignatureVerificationError(ValueError):
    """A deliberately non-specific authentication failure."""


class RequestSigner(Protocol):
    """Injected internal-request signing port."""

    def sign(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        tenant: TenantContext = TenantContext(),
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> SigningHeaders:
        """Sign the exact transmitted body bytes."""


class RequestVerifier(Protocol):
    """Injected internal-request verification port."""

    def verify(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        tenant: TenantContext | None = None,
        now: int | None = None,
    ) -> VerifiedCaller:
        """Verify the exact received body bytes or raise."""


def _canonical_bytes_from_fields(
    *,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    account_id: str,
    organization_id: str,
    project_id: str,
    service: str,
    nonce: str = "",
) -> bytes:
    normalized_method = method.upper()
    if not _METHOD_PATTERN.fullmatch(normalized_method):
        raise ValueError("method is invalid")
    if (
        not path.startswith("/")
        or len(path) > 4_096
        or "\n" in path
        or "\r" in path
        or "\x00" in path
    ):
        raise ValueError("path is invalid")
    if not timestamp.isascii() or not timestamp.isdigit():
        raise ValueError("timestamp is invalid")
    identity_fields = tuple(
        value.strip() for value in (account_id, organization_id, project_id, service)
    )
    if any("\n" in value or "\r" in value for value in identity_fields):
        raise ValueError("canonical identity field is invalid")
    body_digest = hashlib.sha256(body).hexdigest()
    fields = (
        normalized_method,
        path,
        timestamp,
        body_digest,
        *identity_fields,
    )
    # An empty nonce keeps receiver-first upgrades compatible with legacy v1
    # callers. All current signers emit and bind a random nonce.
    if nonce:
        if not _NONCE_PATTERN.fullmatch(nonce):
            raise ValueError("nonce is invalid")
        fields = (*fields, nonce)
    return "\n".join(fields).encode("utf-8")


def build_canonical_bytes(
    *,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    tenant: TenantContext = TenantContext(),
    caller: CallerContext,
    nonce: str = "",
) -> bytes:
    """Build the canonical representation without altering body bytes."""

    return _canonical_bytes_from_fields(
        method=method,
        path=path,
        timestamp=timestamp,
        body=body,
        account_id=tenant.account_id,
        organization_id=tenant.organization_id,
        project_id=tenant.project_id,
        service=caller.service,
        nonce=nonce,
    )


def _compute_signature(key: bytes, canonical: bytes) -> str:
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


class InternalRequestSigner:
    """Exact-body HMAC signer constructed in a service composition root."""

    def __init__(
        self,
        key: bytes,
        caller: CallerContext,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._key = _validated_key(key, label="signing key")
        self._caller = caller
        self._clock = clock or (lambda: int(time.time()))

    def sign(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        tenant: TenantContext = TenantContext(),
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> SigningHeaders:
        signed_at = self._clock() if timestamp is None else timestamp
        request_nonce = "" if nonce is None else nonce
        canonical = build_canonical_bytes(
            method=method,
            path=path,
            timestamp=str(signed_at),
            body=body,
            tenant=tenant,
            caller=self._caller,
            nonce=request_nonce,
        )
        return SigningHeaders(
            caller=self._caller,
            tenant=tenant,
            timestamp=signed_at,
            nonce=request_nonce,
            signature=_compute_signature(self._key, canonical),
        )


class InternalRequestVerifier:
    """Current/previous key verifier with an injected clock and skew bound."""

    def __init__(
        self,
        key_ring: KeyRing,
        *,
        max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if max_skew_seconds < 0 or max_skew_seconds > 3_600:
            raise ValueError("max_skew_seconds must be between 0 and 3600")
        self._key_ring = key_ring
        self._max_skew_seconds = max_skew_seconds
        self._clock = clock or (lambda: int(time.time()))

    def verify(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        tenant: TenantContext | None = None,
        now: int | None = None,
    ) -> VerifiedCaller:
        try:
            caller = CallerContext(_header(headers, HDR_SERVICE))
            header_tenant = TenantContext.from_headers(headers)
            resolved_tenant = header_tenant if tenant is None else tenant
            if tenant is not None and header_tenant != tenant:
                raise ValueError("tenant")
            timestamp_text = _header(headers, HDR_TIMESTAMP)
            nonce = _header(headers, HDR_NONCE)
            signature = _header(headers, HDR_SIGNATURE).lower()
            if not timestamp_text.isascii() or not timestamp_text.isdigit():
                raise ValueError("timestamp")
            if not _SIGNATURE_PATTERN.fullmatch(signature):
                raise ValueError("signature")
            signed_at = int(timestamp_text)
            checked_at = self._clock() if now is None else now
            if abs(checked_at - signed_at) > self._max_skew_seconds:
                raise ValueError("timestamp")
            canonical = build_canonical_bytes(
                method=method,
                path=path,
                timestamp=timestamp_text,
                body=body,
                tenant=resolved_tenant,
                caller=caller,
                nonce=nonce,
            )
        except (TypeError, ValueError) as exc:
            raise SignatureVerificationError("internal request signature is invalid") from exc

        matched_slot: str | None = None
        for slot, key in self._key_ring._candidates():
            expected = _compute_signature(key, canonical)
            if hmac.compare_digest(expected, signature) and matched_slot is None:
                matched_slot = slot
        if matched_slot is None:
            raise SignatureVerificationError("internal request signature is invalid")
        return VerifiedCaller(
            caller=caller,
            tenant=resolved_tenant,
            signed_at=signed_at,
            key_slot=matched_slot,
        )

    def try_verify(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        tenant: TenantContext | None = None,
        now: int | None = None,
    ) -> VerifiedCaller | None:
        try:
            return self.verify(
                method=method,
                path=path,
                body=body,
                headers=headers,
                tenant=tenant,
                now=now,
            )
        except SignatureVerificationError:
            return None


# Compatibility facade for the copied v1 common.utils.internal_request_signing API.
def build_canonical_string(
    *,
    method: str,
    path: str,
    timestamp: str,
    body: bytes,
    account_id: str = "",
    org_id: str = "",
    project_id: str = "",
    service: str = "",
    nonce: str = "",
) -> str:
    return _canonical_bytes_from_fields(
        method=method,
        path=path,
        timestamp=timestamp,
        body=body,
        account_id=account_id,
        organization_id=org_id,
        project_id=project_id,
        service=service,
        nonce=nonce,
    ).decode("utf-8")


def compute_signature(secret: str, canonical: str) -> str:
    return _compute_signature(secret.encode("utf-8"), canonical.encode("utf-8"))


def sign_internal_request(
    secret: str,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    service: str,
    account_id: str = "",
    org_id: str = "",
    project_id: str = "",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    signed_at = int(time.time()) if timestamp is None else timestamp
    request_nonce = "" if nonce is None else nonce
    canonical = build_canonical_string(
        method=method,
        path=path,
        timestamp=str(signed_at),
        body=body,
        account_id=account_id,
        org_id=org_id,
        project_id=project_id,
        service=service,
        nonce=request_nonce,
    )
    headers = {
        HDR_SERVICE: service,
        HDR_TIMESTAMP: str(signed_at),
        HDR_SIGNATURE: compute_signature(secret, canonical),
    }
    if request_nonce:
        headers[HDR_NONCE] = request_nonce
    return headers


def verify_internal_signature(
    secret: str,
    *,
    method: str,
    path: str,
    body: bytes,
    service: str,
    timestamp: str,
    signature: str,
    account_id: str = "",
    org_id: str = "",
    project_id: str = "",
    max_skew_sec: int = DEFAULT_MAX_SKEW_SECONDS,
    now: int | None = None,
    nonce: str = "",
) -> bool:
    if not secret or not service or not timestamp or not signature:
        return False
    try:
        signed_at = int(timestamp)
        checked_at = int(time.time()) if now is None else now
        if abs(checked_at - signed_at) > max_skew_sec:
            return False
        canonical = build_canonical_string(
            method=method,
            path=path,
            timestamp=timestamp,
            body=body,
            account_id=account_id,
            org_id=org_id,
            project_id=project_id,
            service=service,
            nonce=nonce,
        )
        expected = compute_signature(secret, canonical)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature.strip().lower())


def tenant_ids_from_headers(headers: Mapping[str, str]) -> tuple[str, str, str]:
    tenant = TenantContext.from_headers(headers)
    return tenant.account_id, tenant.organization_id, tenant.project_id


def internal_tenant_headers(tenant: TenantContext) -> Mapping[str, str]:
    """Return an immutable canonical tenant-header mapping."""

    return MappingProxyType(tenant.as_headers())


__all__ = [
    "CallerContext",
    "DEFAULT_MAX_SKEW_SECONDS",
    "HDR_ACCOUNT_ID",
    "HDR_NONCE",
    "HDR_ORGANIZATION_ID",
    "HDR_PROJECT_ID",
    "HDR_SERVICE",
    "HDR_SIGNATURE",
    "HDR_TIMESTAMP",
    "InternalRequestSigner",
    "InternalRequestVerifier",
    "KeyRing",
    "MINIMUM_KEY_BYTES",
    "RequestSigner",
    "RequestVerifier",
    "SignatureVerificationError",
    "SigningHeaders",
    "TenantContext",
    "VerifiedCaller",
    "build_canonical_bytes",
    "build_canonical_string",
    "compute_signature",
    "internal_tenant_headers",
    "sign_internal_request",
    "tenant_ids_from_headers",
    "verify_internal_signature",
]
