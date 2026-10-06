"""Static denial identity survives tenant boundaries without caller diagnostics."""

from __future__ import annotations

import pytest

from maeyr_platform.security.tenant_safe_display import (
    sanitize_persisted_event_data,
    sanitize_stream_error_data,
    sanitize_stream_task_error_data,
)
from maeyr_platform.serverless_errors import public_serverless_denial_detail


@pytest.mark.parametrize(
    "status,code",
    [
        (503, "serverless_disabled"),
        (403, "serverless_http_403"),
        (404, "serverless_http_404"),
        (422, "serverless_http_422"),
        (429, "serverless_http_429"),
    ],
)
def test_public_identity_survives_error_and_persisted_task_boundaries(status, code):
    projected = public_serverless_denial_detail(
        status, {"code": code, "message": "private-token", "credentials": "private-token"}
    )
    assert projected is not None
    payload = {"error": projected["message"], "error_code": code}
    for sanitize in (sanitize_stream_error_data, sanitize_stream_task_error_data):
        assert sanitize({**payload, "stack": "private-token", "detail": "private-token"}) == payload
    persisted = sanitize_persisted_event_data({"tasks": [payload]})
    assert persisted == {"tasks": [payload]}
    assert "private-token" not in str(persisted)


@pytest.mark.parametrize(
    "status,detail",
    [
        (403, {"code": "serverless_http_429"}),
        (429, {"code": "serverless_http_403"}),
        (503, {"code": "serverless_http_503"}),
        (404, {"code": ["serverless_http_404"]}),
        ("404", {"code": "serverless_http_404"}),
        (404.0, {"code": "serverless_http_404"}),
        (True, {"code": "serverless_disabled"}),
        (403, "serverless_http_403 private-token"),
        (503, None),
    ],
)
def test_unknown_malformed_or_non_native_denial_is_never_projected(status, detail):
    assert public_serverless_denial_detail(status, detail) is None


def test_projection_returns_a_new_mapping_each_time():
    result = public_serverless_denial_detail(503, {"code": "serverless_disabled"})
    assert result is not None
    result["message"] = "private-token"
    fresh = public_serverless_denial_detail(503, {"code": "serverless_disabled"})
    assert fresh is not None
    assert "private-token" not in fresh["message"]
