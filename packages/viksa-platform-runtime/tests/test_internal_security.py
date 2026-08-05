from __future__ import annotations

import dataclasses
import hashlib
import hmac

import pytest

from viksa_platform.compat.internal_tenant_headers import (
    internal_tenant_headers,
    internal_tenant_headers_from_mapping,
)
from viksa_platform.security.internal import (
    CallerContext,
    InternalRequestSigner,
    InternalRequestVerifier,
    KeyRing,
    SignatureVerificationError,
    TenantContext,
    build_canonical_string,
    sign_internal_request,
    tenant_ids_from_headers,
    verify_internal_signature,
)

CURRENT_KEY = b"c" * 32
PREVIOUS_KEY = b"p" * 32
TENANT = TenantContext("AC-1", "OR-1", "PR-1")
CALLER = CallerContext("chat-service")


def test_exact_body_signing_and_current_key_verification() -> None:
    signer = InternalRequestSigner(CURRENT_KEY, CALLER, clock=lambda: 1_000)
    verifier = InternalRequestVerifier(
        KeyRing(CURRENT_KEY, (PREVIOUS_KEY,)),
        clock=lambda: 1_005,
    )
    body = b'{"message":"hello"}'
    signed = signer.sign(
        method="POST",
        path="/internal/run?mode=sync",
        body=body,
        tenant=TENANT,
    )

    verified = verifier.verify(
        method="POST",
        path="/internal/run?mode=sync",
        body=body,
        headers=signed.as_dict(),
    )
    assert verified.caller == CALLER
    assert verified.tenant == TENANT
    assert verified.key_slot == "current"

    with pytest.raises(SignatureVerificationError):
        verifier.verify(
            method="POST",
            path="/internal/run?mode=sync",
            body=body + b" ",
            headers=signed.as_dict(),
            tenant=TENANT,
        )


def test_previous_key_rotation_is_visible_without_exposing_material() -> None:
    signer = InternalRequestSigner(PREVIOUS_KEY, CALLER, clock=lambda: 2_000)
    ring = KeyRing(CURRENT_KEY, (PREVIOUS_KEY,))
    signed = signer.sign(method="DELETE", path="/internal/item/1", body=b"")
    verified = InternalRequestVerifier(ring, clock=lambda: 2_001).verify(
        method="DELETE",
        path="/internal/item/1",
        body=b"",
        headers=signed.as_dict(),
        tenant=TenantContext(),
    )

    assert verified.key_slot == "previous:0"
    assert "cccc" not in repr(ring)
    assert "pppp" not in repr(ring)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ring, "current", b"x" * 32)


def test_signature_rejects_tenant_path_timestamp_and_unknown_key_drift() -> None:
    signed = InternalRequestSigner(CURRENT_KEY, CALLER, clock=lambda: 3_000).sign(
        method="POST",
        path="/internal/run",
        body=b"{}",
        tenant=TENANT,
    )
    verifier = InternalRequestVerifier(KeyRing(CURRENT_KEY), clock=lambda: 3_500)

    for path, tenant in (
        ("/internal/other", TENANT),
        ("/internal/run", TenantContext("AC-2", "OR-1", "PR-1")),
    ):
        with pytest.raises(SignatureVerificationError):
            verifier.verify(
                method="POST",
                path=path,
                body=b"{}",
                headers=signed.as_dict(),
                tenant=tenant,
            )

    assert (
        InternalRequestVerifier(KeyRing(b"z" * 32), clock=lambda: 3_001).try_verify(
            method="POST",
            path="/internal/run",
            body=b"{}",
            headers=signed.as_dict(),
            tenant=TENANT,
        )
        is None
    )


def test_compatibility_facade_matches_v1_canonical_contract() -> None:
    secret = "s" * 32
    body = b'{"a":1}'
    canonical = "\n".join(
        [
            "POST",
            "/internal/test",
            "4000",
            hashlib.sha256(body).hexdigest(),
            "AC-1",
            "OR-1",
            "PR-1",
            "worker-service",
        ]
    )
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    assert (
        build_canonical_string(
            method="post",
            path="/internal/test",
            timestamp="4000",
            body=body,
            account_id="AC-1",
            org_id="OR-1",
            project_id="PR-1",
            service="worker-service",
        )
        == canonical
    )
    headers = sign_internal_request(
        secret,
        method="post",
        path="/internal/test",
        body=body,
        service="worker-service",
        account_id="AC-1",
        org_id="OR-1",
        project_id="PR-1",
        timestamp=4_000,
    )
    assert headers["x-internal-signature"] == expected
    assert verify_internal_signature(
        secret,
        method="POST",
        path="/internal/test",
        body=body,
        service=headers["x-internal-service"],
        timestamp=headers["x-internal-timestamp"],
        signature=headers["x-internal-signature"],
        account_id="AC-1",
        org_id="OR-1",
        project_id="PR-1",
        now=4_001,
    )


def test_tenant_header_aliases_canonicalize_to_one_typed_context() -> None:
    headers = {
        "X-Caller-Account-ID": "AC-1",
        "x-internal-org-id": "OR-1",
        "X-Caller-Project-ID": "PR-1",
    }
    assert TenantContext.from_headers(headers) == TENANT
    assert tenant_ids_from_headers(headers) == ("AC-1", "OR-1", "PR-1")
    assert internal_tenant_headers(
        account_id="AC-1",
        org_id="OR-1",
        project_id="PR-1",
    ) == {
        "X-Internal-Account-Id": "AC-1",
        "X-Internal-Org-Id": "OR-1",
        "X-Internal-Project-Id": "PR-1",
    }
    assert internal_tenant_headers_from_mapping({"account_id": "AC-1"}) is None


def test_duplicate_authentication_or_tenant_alias_headers_are_rejected() -> None:
    signer = InternalRequestSigner(CURRENT_KEY, CALLER, clock=lambda: 5_000)
    verifier = InternalRequestVerifier(KeyRing(CURRENT_KEY), clock=lambda: 5_001)
    signed = signer.sign(method="POST", path="/internal/run", body=b"{}", tenant=TENANT)

    duplicate_signature = {
        **signed.as_dict(),
        "X-Internal-Signature": signed.signature,
    }
    with pytest.raises(SignatureVerificationError):
        verifier.verify(
            method="POST",
            path="/internal/run",
            body=b"{}",
            headers=duplicate_signature,
        )

    with pytest.raises(ValueError, match="duplicate canonical header"):
        TenantContext.from_headers(
            {
                "x-internal-account-id": "AC-1",
                "X-Caller-Account-ID": "AC-1",
            }
        )
