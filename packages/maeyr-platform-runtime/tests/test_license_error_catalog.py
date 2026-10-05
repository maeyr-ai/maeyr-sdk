"""Canonical license diagnostics do not project authority-provided data."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from maeyr_platform.license_errors import public_license_denial_detail
from maeyr_platform.security.tenant_safe_display import (
    sanitize_stream_error_data,
    tenant_safe_trace_message,
)


@pytest.mark.parametrize("detail", [None, "trial_expired", [], {}, {"code": 403}])
def test_noncanonical_license_detail_is_not_public(detail: object) -> None:
    assert public_license_denial_detail(403, detail) is None


@pytest.mark.parametrize("status_code", [200, 400, 401, 404, 409, 422, 429, 500, 503])
def test_canonical_code_requires_its_exact_http_status(status_code: int) -> None:
    assert public_license_denial_detail(status_code, {"code": "trial_expired"}) is None


def test_public_details_are_static_independent_copies() -> None:
    private: Mapping[str, object] = {
        "code": "trial_expired",
        "message": "private-secret",
        "retryable": True,
        "account_id": "AC-private",
    }
    first = public_license_denial_detail(403, private)
    assert first == {
        "code": "trial_expired",
        "message": "The Free trial has expired.",
        "retryable": False,
    }
    first["message"] = "changed-copy"
    assert public_license_denial_detail(403, private) == {
        "code": "trial_expired",
        "message": "The Free trial has expired.",
        "retryable": False,
    }
    assert private["message"] == "private-secret"


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (403, "trial_expired"),
        (403, "license_disabled"),
        (403, "managed_ai_not_included"),
        (403, "spending_not_authorized"),
        (429, "quota_exceeded"),
        (429, "concurrency_exceeded"),
        (429, "rate_exceeded"),
        (429, "model_call_limit_exceeded"),
        (429, "queue_exceeded"),
        (409, "operation_conflict"),
        (409, "execution_duration_exceeded"),
        (409, "operation_completed"),
        (409, "policy_changed"),
        (409, "trial_cleanup_in_progress"),
    ],
)
def test_safe_license_errors_survive_stream_and_trace_redaction(
    status_code: int, code: str
) -> None:
    detail = public_license_denial_detail(status_code, {"code": code})
    assert detail is not None
    message = str(detail["message"])
    assert tenant_safe_trace_message(message) == message
    event = sanitize_stream_error_data({"error": message, "error_code": code})
    assert event == {"error": message, "error_code": code}


def test_dynamic_authority_details_remain_redacted() -> None:
    assert tenant_safe_trace_message("private-secret") == "Run failed"
    assert sanitize_stream_error_data({"error": "private-secret", "error_code": "AC-private"}) == {
        "error": "Run failed"
    }
