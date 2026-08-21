from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from viksa_platform.tracing.server_span import schedule_http_server_span


def test_http_server_span_is_not_scheduled_before_full_tenant_resolution() -> None:
    with patch("asyncio.create_task") as create_task:
        schedule_http_server_span(
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
            parent_span_id=None,
            service="auth-service",
            method="POST",
            route="/login",
            status_code=200,
            duration_ms=12,
            started_at=datetime.now(timezone.utc),
            account_id=None,
            org_id=None,
            project_id=None,
        )

    create_task.assert_not_called()


def test_http_server_span_rejects_partial_tenant_resolution() -> None:
    with patch("asyncio.create_task") as create_task:
        schedule_http_server_span(
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="0123456789abcdef",
            parent_span_id=None,
            service="worker-service",
            method="GET",
            route="/agents",
            status_code=200,
            duration_ms=8,
            started_at=datetime.now(timezone.utc),
            account_id="AC-1",
            org_id="OI-1",
            project_id=None,
        )

    create_task.assert_not_called()
