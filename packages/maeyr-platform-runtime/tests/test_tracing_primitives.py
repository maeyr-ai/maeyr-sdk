from __future__ import annotations

import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from maeyr_platform.compat.module_alias import ImportAlias, install_module_alias
from maeyr_platform.tracing import constants
from maeyr_platform.tracing.errors import (
    attach_error_to_span_kwargs,
    error_attributes_for_status,
    error_attributes_from_exception,
    merge_error_attributes,
)
from maeyr_platform.tracing.ids import (
    generate_span_id,
    generate_trace_id,
    normalize_parent_span_id,
    normalize_span_id,
    normalize_trace_id,
)
from maeyr_platform.tracing.internal_headers import (
    internal_tenant_headers,
    internal_tenant_headers_from_span,
)
from maeyr_platform.tracing.labels import derive_labels
from maeyr_platform.tracing.sampling import (
    configure_sampling,
    sample_rate,
    should_sample,
    traceparent_sampled,
)
from maeyr_platform.tracing.semconv import (
    ATTR_GEN_AI_MODEL,
    ATTR_SERVICE_NAME,
    enrich_span_attributes,
    operation_for_span_name,
)
from maeyr_platform.tracing.tenant import span_ref, valid_span_tenant_scope, valid_tenant_id
from maeyr_platform.tracing.tracestate import (
    build_tracestate,
    merge_tracestate_into_headers,
    parse_tracestate,
    tenant_from_tracestate,
)
from maeyr_platform.tracing.workflow import (
    TRACE_PAYLOAD_KEY,
    enrich_workflow_inputs,
    split_trace_payload,
)


def test_ids_preserve_exact_legacy_normalization_contract() -> None:
    trace_id = generate_trace_id()
    span_id = generate_span_id()
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert re.fullmatch(r"[0-9a-f]{16}", span_id)
    assert normalize_trace_id("TR-89ABCDEF") == "0" * 24 + "89abcdef"
    assert normalize_trace_id("12345678-1234-1234-1234-123456789ABC") == (
        "12345678123412341234123456789abc"
    )
    assert normalize_span_id("SP-89ABCDEF") == "0" * 8 + "89abcdef"
    assert normalize_parent_span_id(None) is None
    assert normalize_parent_span_id("  ") is None
    assert normalize_parent_span_id("SP-89ABCDEF") == "0" * 8 + "89abcdef"


def test_constants_preserve_taxonomy_and_trace_durability_keys() -> None:
    assert constants.REDIS_QUEUE_KEY == "platform:trace_spans:pending"
    assert constants.REDIS_PROCESSING_QUEUE_KEY == "platform:trace_spans:processing"
    assert constants.SpanStatus.TIMEOUT.value == "timeout"
    assert constants.SpanKind.PRODUCER.value == "producer"
    assert constants.SpanOperation.EXECUTION_RESUME.value == "execution_resume"
    assert constants.SPAN_WORKFLOW_EXECUTE == "workflow.execute"


def test_tracestate_round_trip_and_header_merge_are_non_mutating() -> None:
    extra = {"vendor": "v1"}
    value = build_tracestate(org_id="org", project_id="project", extra=extra)
    assert value == "vendor=v1,org_id=org,project_id=project"
    state = parse_tracestate(f" {value}, malformed ")
    assert state == {"vendor": "v1", "org_id": "org", "project_id": "project"}
    assert tenant_from_tracestate(state) == ("org", "project")

    original = {"X-Test": "yes"}
    merged = merge_tracestate_into_headers(original, org_id="org", project_id="project")
    assert original == {"X-Test": "yes"}
    assert merged[constants.HEADER_TENANT_ORG_ID] == "org"
    assert merged[constants.HEADER_TENANT_PROJECT_ID] == "project"
    assert merged["tracestate"] == "org_id=org,project_id=project"


def test_tenant_scope_and_reference_contract() -> None:
    assert valid_tenant_id(" tenant ")
    assert not valid_tenant_id("unknown")
    assert not valid_tenant_id(None)
    document = {
        "account_id": "account",
        "org_id": "org",
        "project_id": "project",
        "trace_id": "trace",
        "span_name": "span",
        "service": "service",
    }
    assert valid_span_tenant_scope(doc=document)
    assert span_ref(doc=document) == {
        "trace_id": "trace",
        "span_name": "span",
        "service": "service",
    }


def test_sampling_preserves_clamping_environment_and_force(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        configure_sampling(-1.0)
        assert sample_rate() == 0.0
        assert not should_sample("f" * 32)
        assert should_sample("f" * 32, force=True)
        configure_sampling(2.0)
        assert sample_rate() == 1.0
        assert should_sample("f" * 32)
        monkeypatch.setenv("TRACE_SAMPLE_RATE", "0.25")
        configure_sampling()
        assert sample_rate() == 0.25
        assert traceparent_sampled() == "01"
        assert traceparent_sampled(False) == "00"
    finally:
        configure_sampling(1.0)


def test_labels_and_semantic_conventions_preserve_exact_mappings() -> None:
    labels = derive_labels(
        status="error",
        operation="worker_execute",
        attributes={"error.type": "HTTPError", "tool.execution_mode": "secure", "task_queue": "q"},
    )
    assert labels == ["api_error", "worker_error", "secure", "queue:q"]
    assert operation_for_span_name(constants.SPAN_AGENT_STEP) == (
        constants.SpanOperation.AGENT_ACT.value
    )
    assert operation_for_span_name("custom", explicit="explicit") == "explicit"
    original: dict[str, object] = {ATTR_SERVICE_NAME: "caller"}
    enriched = enrich_span_attributes(original, service="ignored", model="gpt")
    assert original == {ATTR_SERVICE_NAME: "caller"}
    assert enriched == {ATTR_SERVICE_NAME: "caller", ATTR_GEN_AI_MODEL: "gpt"}


def test_internal_headers_are_trimmed_and_require_complete_scope() -> None:
    assert internal_tenant_headers(account_id=" a ", org_id=" o ", project_id=" p ") == {
        "X-Internal-Account-Id": "a",
        "X-Internal-Org-Id": "o",
        "X-Internal-Project-Id": "p",
    }
    assert internal_tenant_headers_from_span({"account_id": "a"}) is None
    assert internal_tenant_headers_from_span(
        {"account_id": " a ", "org_id": " o ", "project_id": " p "}
    ) == internal_tenant_headers(account_id="a", org_id="o", project_id="p")


def test_workflow_envelope_uses_service_context_and_never_mutates_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_module = ModuleType("common.platform_traces.context")
    context = SimpleNamespace(
        trace_id="trace",
        span_id="span",
        activity_id="activity",
        account_id="account",
        org_id="org",
        project_id="project",
    )
    context_module.get_trace_context = lambda: context  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "common.platform_traces.context", context_module)
    original: dict[str, Any] = {"input": 1}
    enriched = enrich_workflow_inputs(original)
    assert original == {"input": 1}
    assert enriched[TRACE_PAYLOAD_KEY] == {
        "trace_id": "trace",
        "parent_span_id": "span",
        "activity_id": "activity",
        "account_id": "account",
        "org_id": "org",
        "project_id": "project",
    }
    payload, metadata = split_trace_payload(enriched)
    assert payload == original
    assert metadata == enriched[TRACE_PAYLOAD_KEY]


def test_error_attributes_delegate_to_service_allowlist_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display_module = ModuleType("common.platform_traces.display_messages")
    display_module.TRACE_ERROR_REQUEST_FAILED = "Request failed"  # type: ignore[attr-defined]
    display_module.http_exception_detail_text = (  # type: ignore[attr-defined]
        lambda exc, max_message=500: str(exc)[:max_message]
    )
    display_module.tenant_safe_trace_message = (  # type: ignore[attr-defined]
        lambda message, fallback: message if message == "allowlisted" else fallback
    )
    monkeypatch.setitem(sys.modules, "common.platform_traces.display_messages", display_module)

    attributes = error_attributes_for_status(
        "error", message="database password=secret", error_type="RuntimeError"
    )
    assert attributes["error.message"] == "Request failed"
    assert attributes["error.message_internal"] == "database password=secret"
    assert merge_error_attributes({"error.type": "caller"}, attributes)["error.type"] == "caller"

    try:
        raise ValueError("private path")
    except ValueError as exc:
        exception_attributes = error_attributes_from_exception(exc, include_stack=True)
    assert exception_attributes["error.message"] == "Request failed"
    assert exception_attributes["error.type"] == "ValueError"
    assert "/Users/" not in exception_attributes.get("error.stack", "")

    kwargs: dict[str, Any] = {"status": "error", "exc": ValueError("private")}
    assert "error.type" in attach_error_to_span_kwargs(kwargs)["attributes"]


def test_import_alias_forwards_identity_and_mutation() -> None:
    target = ModuleType("canonical")
    setattr(target, "value", 1)
    alias = ModuleType("legacy")
    ImportAlias.bind(alias, target)
    assert getattr(alias, "value") == 1
    setattr(alias, "value", 2)
    assert getattr(target, "value") == 2


def test_install_module_alias_registers_identity_and_forwards_existing_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ModuleType("canonical_for_install")
    setattr(target, "value", 1)
    alias = ModuleType("legacy_for_install")
    monkeypatch.setitem(sys.modules, target.__name__, target)
    monkeypatch.setitem(sys.modules, alias.__name__, alias)

    installed = install_module_alias(vars(alias), target.__name__)

    assert installed is target
    assert sys.modules[alias.__name__] is target
    setattr(alias, "value", 2)
    assert getattr(target, "value") == 2
