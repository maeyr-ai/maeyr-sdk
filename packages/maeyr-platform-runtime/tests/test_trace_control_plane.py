"""Authorization of trace storage cannot create another metered tenant trace."""

from collections import deque
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.responses import JSONResponse

from maeyr_platform.tracing import recorder, server_span
from maeyr_platform.tracing.context import get_trace_context
from maeyr_platform.tracing.control_plane import is_trace_control_request, is_trace_control_span
from maeyr_platform.tracing.middleware_factory import (
    create_asgi_trace_middleware,
    create_http_trace_middleware,
)


@pytest.mark.parametrize("factory", [create_asgi_trace_middleware, create_http_trace_middleware])
@pytest.mark.parametrize("route", ["/internal/usage/reserve", "/internal/license/grants"])
async def test_metering_keeps_context_and_response_but_never_schedules_a_span(
    monkeypatch, factory, route
):
    scheduled = Mock()
    monkeypatch.setattr(server_span, "schedule_trace_task", scheduled)
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        context = get_trace_context()
        assert context is not None
        assert context.account_id == "AC-test"
        assert context.project_id == "PI-test"
        await JSONResponse({"authorized": True})(scope, receive, send)

    middleware = factory("auth-service")(app)
    headers = {
        "X-Trace-ID": "a" * 32,
        "X-Internal-Account-Id": "AC-test",
        "X-Internal-Org-Id": "OI-test",
        "X-Internal-Project-Id": "PI-test",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware), base_url="http://auth.test"
    ) as client:
        for _ in range(20):
            response = await client.post(route, headers=headers)
            assert response.json() == {"authorized": True}
            assert response.headers["X-Trace-ID"] == "a" * 32
    assert calls == 20
    scheduled.assert_not_called()


@pytest.mark.parametrize(
    "service,route,excluded",
    [
        ("auth-service", "/internal/usage", True),
        ("auth-service", "/internal/license/admit", True),
        ("trace-service", "/internal/metrics/spans", True),
        ("auth-service", "/internal/usage-customer", False),
        ("worker-service", "/internal/usage/reserve", False),
        ("auth-service", "/auth/me", False),
        ("pulse-service", "/executor/execute", False),
    ],
)
def test_exclusion_is_specific_to_the_owning_service_namespace(service, route, excluded):
    assert is_trace_control_request(service, route) is excluded
    document = {
        "service": service,
        "span_name": "http.server",
        "attributes": {"http.route": route},
    }
    assert is_trace_control_span(document) is excluded
    document["span_name"] = "worker.execute"
    assert not is_trace_control_span(document)


async def test_customer_activity_still_schedules_server_spans(monkeypatch):
    scheduled = Mock()
    monkeypatch.setattr(server_span, "schedule_trace_task", scheduled)
    from datetime import datetime, timezone

    server_span.schedule_http_server_span(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        service="headless-service",
        method="POST",
        route="/headless/execute",
        status_code=200,
        duration_ms=1,
        started_at=datetime.now(timezone.utc),
        account_id="AC-test",
        org_id="OI-test",
        project_id="PI-test",
        tenant_verified=True,
    )
    scheduled.assert_called_once()


async def test_transport_failure_logging_is_bounded_and_counters_remain_exact(monkeypatch):
    monkeypatch.setattr(recorder, "enqueue_span", AsyncMock(return_value=False))
    monkeypatch.setattr(recorder, "_memory_queue", deque(maxlen=128))
    monkeypatch.setattr(recorder, "_MAX_QUEUE_SIZE", 128)
    monkeypatch.setattr(recorder, "_redis_enqueue_failures", 0)
    monkeypatch.setattr(recorder, "_spans_dropped_queue_overflow", 0)
    monkeypatch.setattr(recorder, "_flush_handler", None)
    monkeypatch.setattr(recorder, "_remote_recorder", None)
    monkeypatch.setattr(recorder, "_schedule_immediate_flush", Mock())
    logger = Mock()
    monkeypatch.setattr(recorder, "logger", logger)
    document = {
        "account_id": "AC-test",
        "org_id": "OI-test",
        "project_id": "PI-test",
        "span_name": "worker.execute",
    }
    for _ in range(129):
        await recorder._enqueue_or_buffer(document)
    assert recorder._redis_enqueue_failures == 129
    assert recorder._spans_dropped_queue_overflow == 1
    assert len(recorder._memory_queue) == 128
    assert logger.error.call_count == 9
    recorder._memory_queue.clear()
    for _ in range(20):
        await recorder._enqueue_or_buffer(document)
    # A historical overflow count must not make subsequent logs noisy again.
    assert logger.error.call_count == 9
