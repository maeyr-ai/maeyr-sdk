from __future__ import annotations

import pytest

from viksa_platform.security.tenant_identity import (
    canonical_tenant_id,
    is_canonical_tenant_id,
)
from viksa_platform.tracing.tenant import valid_tenant_id as valid_trace_tenant_id


@pytest.mark.parametrize(
    "value",
    [
        "AC-2489639C",
        "OI-43D94ED1",
        "PI-A7BC9331",
        "tenant_1.eu-west",
        "550e8400-e29b-41d4-a716-446655440000",
    ],
)
def test_canonical_tenant_ids_cover_platform_identifiers(value: str) -> None:
    assert canonical_tenant_id(f" {value} ") == value
    assert is_canonical_tenant_id(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "",
        "unknown",
        "../other",
        "AC/other",
        "AC:other",
        "AC@other",
        "AC-1\nforged",
        ".hidden",
        "A" * 129,
    ],
)
def test_canonical_tenant_ids_reject_ambiguous_or_unsafe_values(value: object) -> None:
    assert not is_canonical_tenant_id(value)
    with pytest.raises(ValueError, match="tenant_id is invalid"):
        canonical_tenant_id(value)


def test_trace_tenant_validation_remains_intentionally_permissive() -> None:
    assert valid_trace_tenant_id("../legacy-trace-tenant")
    assert not is_canonical_tenant_id("../legacy-trace-tenant")
