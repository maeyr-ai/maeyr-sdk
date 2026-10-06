import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from maeyr_platform.tracing import recorder, routing, sampling
from maeyr_platform.tracing.context import (
    TraceContext,
    clear_trace_context,
    enrich_trace_tenant,
    get_trace_context,
    set_trace_context,
)
from maeyr_platform.tracing.policy import (
    AI_OPERATIONS,
    AI_PREFIXES,
    MCP_ACTIVITY,
    tenant_trace_category,
)
from maeyr_platform.tracing.remote_recorder import RemoteTraceRecorder


def span(name="llm.provider.request", **values):
    return {
        "span_id": "b" * 16,
        "trace_id": "a" * 32,
        "account_id": "AC-test",
        "org_id": "OI-test",
        "project_id": "PI-test",
        "span_name": name,
        "status": "error",
        "ended_at": datetime.now(timezone.utc),
        **values,
    }


@pytest.mark.parametrize(
    "name", [prefix + "failed" for prefix in AI_PREFIXES] + sorted(MCP_ACTIVITY)
)
def test_important_semantic_failures_are_retained(name):
    assert tenant_trace_category(span(name)) == "ai"


@pytest.mark.parametrize("operation", sorted(AI_OPERATIONS))
def test_actual_producer_operations_are_retained(operation):
    assert tenant_trace_category(span("customer.operation", operation=operation)) == "ai"


@pytest.mark.parametrize(
    "name", ["mcp.tools.list", "mcp.initialize", "auth.login", "marketplace.install"]
)
def test_discovery_is_not_saved_even_with_execution_operation(name):
    assert tenant_trace_category(span(name, operation="tool_call", entity_type="agent")) is None


@pytest.mark.parametrize("name", ["http.server", "http.client"])
@pytest.mark.parametrize(
    "route",
    [
        "/internal/usage/reserve",
        "/internal/usage/settle",
        "/internal/license/grants",
        "/internal/metrics/spans",
    ],
)
def test_trace_bookkeeping_cannot_reenter_tenant_storage(name, route):
    assert (
        tenant_trace_category(
            span(
                name,
                service="auth-service",
                operation="llm_call",
                entity_type="agent",
                parent_span_id="c" * 16,
                _tenant_verified=True,
                attributes={"http.route": route, "http.request.method": "POST"},
            )
        )
        is None
    )


@pytest.mark.parametrize("action", ["reserve", "settle", "release"])
def test_meaningful_billing_survives_but_trace_ingestion_is_excluded(action):
    assert (
        tenant_trace_category(
            span("billing.usage." + action, attributes={"billing.resource": "executions"})
        )
        == "billing"
    )
    assert (
        tenant_trace_category(
            span(
                "billing.usage." + action, attributes={"billing.resource": "trace_ingestion_bytes"}
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "route",
    [
        "/auth/me",
        "/auth/usage",
        "/agent/list",
        "/analytics/traces",
        "/api/v1/marketplace/share-requests/pending-for-me",
    ],
)
def test_administration_is_not_saved_even_with_ambient_entity(route):
    assert (
        tenant_trace_category(
            span(
                "http.server",
                operation="http_server",
                entity_type="agent",
                _tenant_verified=True,
                attributes={"http.route": route},
            )
        )
        is None
    )


def test_execution_http_requires_authenticated_scope_and_exact_execution_route():
    doc = span(
        "http.server",
        service="headless-service",
        attributes={"http.route": "/headless/execute", "http.request.method": "POST"},
    )
    assert tenant_trace_category(doc) is None
    doc["_tenant_verified"] = True
    assert tenant_trace_category(doc) == "ai"
    doc["attributes"]["http.route"] += "/list"
    assert tenant_trace_category(doc) is None


def test_custom_execution_dependency_and_error_remain_visible():
    assert (
        tenant_trace_category(span("customer.parse", resource_refs={"execution_id": "run-1"}))
        == "ai"
    )
    assert (
        tenant_trace_category(
            span(
                "http.client",
                parent_span_id="c" * 16,
                resource_refs={"execution_id": "run-1"},
                _tenant_verified=True,
            )
        )
        == "ai"
    )
    assert tenant_trace_category(span("unknown", trace_category="ai")) is None


@pytest.mark.parametrize(
    "host,route",
    [
        ("auth-service.preview.svc.cluster.local", "/auth/validate-token"),
        ("builder-service.preview.svc.cluster.local", "/agents/describe"),
        ("marketplace-service", "/api/v1/marketplace/list"),
        ("TRACE-SERVICE.preview.svc.cluster.local", "/analytics/traces"),
        ("serverless-service.preview.svc.cluster.local", "/internal/workers/heartbeat"),
        ("serverless-service.preview.svc.cluster.local", "/internal/executions/run-1"),
    ],
)
def test_platform_client_administration_cannot_leak_from_execution(host, route):
    assert (
        tenant_trace_category(
            span(
                "http.client",
                parent_span_id="c" * 16,
                entity_type="agent",
                _tenant_verified=True,
                attributes={
                    "server.address": host,
                    "http.route": route,
                    "http.request.method": "POST",
                },
            )
        )
        is None
    )


def test_execution_client_and_external_provider_failures_remain_visible():
    for host, route in [
        ("pulse-service.preview.svc.cluster.local", "/executor/execute"),
        ("api.provider.example", "/v1/messages"),
    ]:
        assert (
            tenant_trace_category(
                span(
                    "http.client",
                    parent_span_id="c" * 16,
                    entity_type="agent",
                    _tenant_verified=True,
                    attributes={
                        "server.address": host,
                        "http.route": route,
                        "http.request.method": "POST",
                    },
                )
            )
            == "ai"
        )


def test_failed_platform_export_cannot_change_tenant_routing(monkeypatch):
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(routing, "schedule_otlp_export", Mock(side_effect=RuntimeError("offline")))
    assert routing.route_span_for_recording(span("http.server")) is False


@pytest.mark.parametrize("path", ["/data", ""])
async def test_http_diagnostics_never_record_url_credentials_or_query(monkeypatch, path):
    from maeyr_platform.tracing import http_client

    recorded = AsyncMock()
    monkeypatch.setattr(http_client, "record_span", recorded)
    tasks = []
    monkeypatch.setattr(http_client, "schedule_trace_task", lambda fn, **_: tasks.append(fn))
    token = set_trace_context(
        TraceContext(
            "a" * 32,
            "b" * 16,
            account_id="AC-test",
            org_id="OI-test",
            project_id="PI-test",
            entity_type="agent",
            tenant_verified=True,
        )
    )
    try:
        http_client.schedule_http_client_span(
            method="GET",
            url=f"https://user:private@api.example:8443{path}?access_key=private#token",
            status_code=200,
            duration_ms=1,
            started_at=datetime.now(timezone.utc),
        )
        await tasks[0]()
        attrs = recorded.await_args.kwargs["attributes"]
        assert attrs["url.full"] == f"https://api.example:8443{path}"
        assert attrs["http.route"] == (path or "/")
    finally:
        clear_trace_context(token)


@pytest.mark.parametrize(
    "name",
    ["llm.chat", "worker.execute", "approval.required", "execution.wait", "billing.usage.settle"],
)
async def test_important_start_and_completion_bypass_sample_zero(monkeypatch, name):
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(recorder, "enqueue_span", enqueue)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 0.0)
    args = {
        "span_name": name,
        "account_id": "AC-test",
        "org_id": "OI-test",
        "project_id": "PI-test",
    }
    sid = await recorder.record_span_start(**args)
    await recorder.record_span_end(span_id=sid, **args)
    assert enqueue.await_count == 2
    assert enqueue.await_args_list[0].args[0]["status"] == "running"
    assert enqueue.await_args_list[1].args[0]["_is_completion"] is True


async def test_platform_http_goes_to_only_otlp_not_queue_or_remote(monkeypatch):
    enqueue = AsyncMock(return_value=True)
    export = Mock()
    monkeypatch.setattr(recorder, "enqueue_span", enqueue)
    monkeypatch.setattr(routing, "schedule_otlp_export", export)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 1.0)
    await recorder.record_span(
        **span("http.server", attributes={"http.route": "/auth/me"}, _tenant_verified=False)
    )
    enqueue.assert_not_awaited()
    export.assert_called_once()
    doc = export.call_args.args[0][0]
    assert "account_id" not in doc and "project_id" not in doc


def test_public_http_diagnostics_do_not_consume_important_task_slots(monkeypatch):
    from maeyr_platform.tracing import server_span

    scheduled, export = Mock(), Mock()
    monkeypatch.setattr(server_span, "schedule_trace_task", scheduled)
    monkeypatch.setattr(routing, "schedule_otlp_export", export)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 1.0)
    server_span.schedule_http_server_span(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        service="auth-service",
        method="POST",
        route="/auth/login",
        status_code=401,
        duration_ms=1,
        started_at=datetime.now(timezone.utc),
    )
    scheduled.assert_not_called()
    export.assert_called_once()
    assert export.call_args.args[0][0]["status"] == "error"
    assert "account_id" not in export.call_args.args[0][0]


def test_platform_client_diagnostics_do_not_consume_important_task_slots(monkeypatch):
    from maeyr_platform.tracing import http_client

    scheduled, export = Mock(), Mock()
    monkeypatch.setattr(http_client, "schedule_trace_task", scheduled)
    monkeypatch.setattr(routing, "schedule_otlp_export", export)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 1.0)
    token = set_trace_context(
        TraceContext(
            "a" * 32,
            "b" * 16,
            entity_type="agent",
            account_id="AC-test",
            org_id="OI-test",
            project_id="PI-test",
            tenant_verified=True,
        )
    )
    try:
        http_client.schedule_http_client_span(
            method="POST",
            url="http://auth-service.preview.svc.cluster.local:8000/auth/validate-token",
            status_code=403,
            duration_ms=1,
            started_at=datetime.now(timezone.utc),
        )
        scheduled.assert_not_called()
        export.assert_called_once()
    finally:
        clear_trace_context(token)


async def test_remote_ack_mixed_batch_preserves_important_events_only(monkeypatch):
    export = Mock()
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(routing, "schedule_otlp_export", export)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 1.0)
    monkeypatch.setattr("maeyr_platform.tracing.transport.enqueue_span", enqueue)
    remote = RemoteTraceRecorder("chat-service", internal_key="k" * 32)
    assert await remote.push_spans_acknowledged(
        [span("llm.chat"), span("http.server"), span("billing.usage.reserve")]
    )
    assert enqueue.await_count == 2
    assert {call.args[0]["span_name"] for call in enqueue.await_args_list} == {
        "llm.chat",
        "billing.usage.reserve",
    }
    export.assert_called_once()


async def test_direct_transport_and_retry_cannot_persist_platform_http(monkeypatch):
    from collections import deque

    from maeyr_platform.tracing import transport

    redis = Mock()
    fallback = AsyncMock(return_value=True)
    monkeypatch.setattr(transport, "_use_redis", True)
    monkeypatch.setattr(transport, "_redis_client", redis)
    monkeypatch.setattr(transport, "_durable_fallback", fallback)
    monkeypatch.setattr(transport, "_dead_letter_memory", deque())
    document = span("http.server", attributes={"http.route": "/marketplace/list"})
    assert await transport.enqueue_span(document) is True
    assert await transport.re_enqueue_spans([document]) == 0
    assert await transport._persist_externally(document) is True
    fallback.assert_not_awaited()
    assert not redis.mock_calls
    assert not transport._dead_letter_memory


async def test_bad_diagnostic_id_cannot_drop_important_mixed_batch(monkeypatch):
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(sampling, "_SAMPLE_RATE", 0.5)
    monkeypatch.setattr("maeyr_platform.tracing.transport.enqueue_span", enqueue)
    remote = RemoteTraceRecorder("chat-service", internal_key="k" * 32)
    assert await remote.push_spans_acknowledged(
        [span("http.server", trace_id="not-a-valid-hex-id"), span("worker.execute")]
    )
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[0]["span_name"] == "worker.execute"


async def test_cancelled_execution_emits_cancelled_completion_and_restores_parent(monkeypatch):
    from maeyr_platform.tracing.context import trace_span

    start, end = Mock(), Mock()
    monkeypatch.setattr(recorder, "schedule_span_start", start)
    monkeypatch.setattr(recorder, "schedule_span_end", end)
    parent = TraceContext(
        "a" * 32,
        "b" * 16,
        account_id="AC-test",
        org_id="OI-test",
        project_id="PI-test",
        tenant_verified=True,
    )
    token = set_trace_context(parent)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with trace_span("worker.execute"):
                raise asyncio.CancelledError()
        end.assert_called_once()
        assert end.call_args.kwargs["status"] == "cancelled"
        assert end.call_args.kwargs["attributes"]["error.code"] == "execution_cancelled"
        assert get_trace_context() is parent
    finally:
        clear_trace_context(token)


async def test_cancelled_http_request_keeps_cancellation_and_emits_finite_outcome(monkeypatch):
    from maeyr_platform.tracing import http_client

    scheduled = Mock()
    monkeypatch.setattr(http_client, "schedule_http_client_span", scheduled)
    client = Mock(request=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await http_client.traced_httpx_request(
            client, "POST", "https://provider.example/v1/messages"
        )
    scheduled.assert_called_once()
    assert scheduled.call_args.kwargs["cancelled"] is True
    assert scheduled.call_args.kwargs["status_code"] == 0


async def test_offline_platform_collector_has_independent_bounded_capacity(monkeypatch):
    from maeyr_platform.tracing import otlp_export

    release = asyncio.Event()

    async def blocked(_):
        await release.wait()

    monkeypatch.setattr(otlp_export, "_otlp_endpoint", "https://collector.example/v1/traces")
    monkeypatch.setattr(otlp_export, "_max_in_flight_exports", 2)
    monkeypatch.setattr(otlp_export, "_dropped_export_batches", 0)
    monkeypatch.setattr(otlp_export, "_export_tasks", set())
    monkeypatch.setattr(otlp_export, "export_spans_otlp", blocked)
    for _ in range(1000):
        otlp_export.schedule_otlp_export([span("http.server")])
    assert otlp_export.otlp_export_stats() == {"in_flight": 2, "dropped_batches": 998}
    assert routing.route_span_for_recording(span("worker.execute")) is True
    tasks = list(otlp_export._export_tasks)
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert otlp_export.otlp_export_stats()["in_flight"] == 0


def test_service_bootstrap_preserves_explicit_platform_collector(monkeypatch):
    from maeyr_platform.tracing import bootstrap

    configured = Mock()
    monkeypatch.setattr(bootstrap, "configure_sampling", Mock())
    monkeypatch.setattr(bootstrap, "configure_remote_sink", Mock())
    monkeypatch.setattr(bootstrap, "otlp_export_enabled", Mock(return_value=True))
    monkeypatch.setattr(bootstrap, "configure_otlp_export", configured)
    bootstrap.bootstrap_remote_traces("auth-service")
    configured.assert_not_called()


def test_platform_collector_preserves_client_cancellation_outcome():
    from maeyr_platform.tracing.otlp_export import spans_to_otlp_payload

    payload = spans_to_otlp_payload([span("http.client", span_kind="client", status="cancelled")])
    event = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert event["status"]["code"] == 2
    assert event["kind"] == 3


def test_scope_promotion_does_not_adopt_other_tenant_correlation():
    ctx = TraceContext(
        "a" * 32,
        "b" * 16,
        parent_span_id="c" * 16,
        account_id="AC-other",
        org_id="OI-other",
        project_id="PI-other",
        activity_id="untrusted",
        entity_type="agent",
        resource_refs={"execution_id": "other"},
        tenant_verified=False,
    )
    token = set_trace_context(ctx)
    try:
        enrich_trace_tenant(account_id="AC-test", org_id="OI-test", project_id="PI-test")
        promoted = get_trace_context()
        assert promoted.tenant_verified
        assert promoted.trace_id != ctx.trace_id and promoted.span_id != ctx.span_id
        assert promoted.parent_span_id is None and promoted.activity_id is None
        assert promoted.resource_refs is None and promoted.entity_type is None
    finally:
        clear_trace_context(token)
