"""Security-boundary contracts for tenant-facing display and resume-state data."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from viksa_platform.security import tenant_safe_display as display

PUBLIC_CALLABLES = {
    "chat_message_metadata_for_client",
    "chat_stream_event_data_for_client",
    "http_exception_detail_text",
    "project_resume_state_public",
    "public_http_detail",
    "redact_condition_detail_rows",
    "redact_inline_secrets_in_text",
    "redact_resume_state_for_storage",
    "redact_resume_state_for_tenant_view",
    "redact_sensitive_structure",
    "resolve_pause_resume_from_messages",
    "sanitize_approval_document_for_tenant",
    "sanitize_chat_message_for_tenant",
    "sanitize_chat_message_metadata_for_tenant",
    "sanitize_execution_document_for_tenant",
    "sanitize_execution_persisted_blob",
    "sanitize_message_metadata_for_llm",
    "sanitize_persisted_event_data",
    "sanitize_resume_event_payload",
    "sanitize_resume_execution_api_result",
    "sanitize_resume_input_request",
    "sanitize_run_event_document_for_tenant",
    "sanitize_stream_asking_input_data",
    "sanitize_stream_error_data",
    "sanitize_stream_event_payload",
    "sanitize_stream_execution_summary_data",
    "sanitize_stream_generic_event_data",
    "sanitize_stream_task_complete_data",
    "sanitize_stream_task_error_data",
    "sanitize_stream_task_starting_data",
    "sanitize_stream_thought_complete_data",
    "sanitize_task_result_for_stream",
    "sanitize_tenant_display_text",
    "sanitize_trigger_dry_run_preview",
    "strip_resume_state_secrets",
    "tenant_safe_trace_message",
}

PUBLIC_CONSTANTS = {
    "CHAT_INTERNAL_RESUME_STATE_KEY",
    "CHAT_INTERNAL_RESUME_STATE_SEALED_KEY",
    "RESUME_STATE_PUBLIC_KEY",
    "RESUME_STATE_SEALED_KEY",
    "TRACE_AGENT_NOT_FOUND",
    "TRACE_ERROR_AI_SERVICE",
    "TRACE_ERROR_EXECUTION_TIMEOUT",
    "TRACE_ERROR_INTERNAL_EXECUTION",
    "TRACE_ERROR_REQUEST_FAILED",
    "TRACE_ERROR_RUN_FAILED",
    "TRACE_MAX_RETRIES",
    "TRACE_TASK_FAILED",
    "TRACE_TASK_TIMED_OUT",
    "TRACE_TASK_UNKNOWN",
}


def _resume_state() -> dict[str, Any]:
    return {
        "task_outputs": {"task-1": {"api_token": "task-secret"}},
        "harness_messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "run the tool"},
            {
                "role": "assistant",
                "content": "private assistant content",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "payments_charge",
                            "arguments": json.dumps(
                                {
                                    "card": "4111111111111111",
                                    "api_key": "argument-secret",
                                }
                            ),
                        },
                    }
                ],
                "function_call": {
                    "name": "legacy_call",
                    "arguments": json.dumps({"password": "legacy-secret"}),
                },
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"access_token": "tool-secret"}',
            },
        ],
        "harness_active_agents": [
            {"agent_alias": "payments", "api_token": "agent-secret"}
        ],
        "harness_pending_approvals": [
            {
                "tool_call_id": "call-1",
                "inputs": {"amount": 10, "password": "approval-secret"},
                "execution_config": {"access_token": "config-secret"},
            }
        ],
        "harness_pending_approval": {
            "inputs": {"amount": 20, "api_key": "legacy-approval-secret"}
        },
        "pending_endpoint_approval": {
            "inputs": {"amount": 30, "secret": "endpoint-secret"}
        },
    }


def test_public_contract_preserves_every_callable_dataclass_and_constant() -> None:
    for name in PUBLIC_CALLABLES:
        assert callable(getattr(display, name))
    assert display.PauseResumeScanResult.__dataclass_fields__
    for name in PUBLIC_CONSTANTS:
        assert isinstance(getattr(display, name), str)


def test_http_details_only_expose_allowlisted_messages() -> None:
    exc = HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", "name"],
                "msg": "field required",
                "type": "value_error",
            }
        ],
    )

    assert display.http_exception_detail_text(exc) == "body.name: field required"
    assert display.public_http_detail(exc) == display.TRACE_ERROR_REQUEST_FAILED
    assert display.public_http_detail(message="Trigger not found") == "Trigger not found"
    assert (
        display.public_http_detail(message="database.internal:27017")
        == display.TRACE_ERROR_REQUEST_FAILED
    )


def test_sensitive_keys_and_inline_credentials_are_redacted_without_mutation() -> None:
    raw = {
        "token_count": 42,
        "foo.token.bar": "tenant value",
        "api_token": "token-secret",
        "nested": {"password": "password-secret"},
    }

    safe = display.redact_sensitive_structure(raw)
    inline = display.redact_inline_secrets_in_text(
        "Authorization: Bearer abcdefghijklmnop and sk-live-abcdefghij"
    )

    assert safe["token_count"] == 42
    assert safe["foo.token.bar"] == "tenant value"
    assert safe["api_token"] == "[redacted]"
    assert safe["nested"]["password"] == "[redacted]"
    assert raw["api_token"] == "token-secret"
    assert "abcdefghijklmnop" not in inline
    assert "sk-live" not in inline


def test_condition_rows_and_trigger_preview_are_bounded_and_redacted() -> None:
    rows = display.redact_condition_detail_rows(
        [{"field": "x", "actual_value": "sk-live-abcdefghij", "passed": True}]
    )
    preview = display.sanitize_trigger_dry_run_preview(
        {
            "combined_prompt": "p" * 800,
            "body_passed_to_prompt": {
                "repo": "tenant-repo",
                "api_token": "short-secret",
            },
            "test_payload": {"password": "hunter2"},
        }
    )

    assert "sk-live" not in str(rows[0]["actual_value"])
    assert len(preview["combined_prompt"]) <= 500 + len("… [truncated]")
    assert preview["body_passed_to_prompt"]["repo"] == "tenant-repo"
    assert preview["body_passed_to_prompt"]["api_token"] == "[redacted]"
    assert preview["test_payload"]["password"] == "[redacted]"


def test_server_storage_preserves_complete_resume_state_as_a_copy() -> None:
    state = _resume_state()

    stored = display.redact_resume_state_for_storage(state)

    assert stored == state
    assert stored is not state
    assert stored["harness_pending_approvals"][0]["inputs"] == {
        "amount": 10,
        "password": "approval-secret",
    }


def test_tenant_resume_view_redacts_messages_approvals_and_outputs() -> None:
    state = _resume_state()

    safe = display.redact_resume_state_for_tenant_view(state)
    serialized = json.dumps(safe)

    assistant = safe["harness_messages"][2]
    assert assistant["content"] == "[redacted]"
    assert assistant["tool_calls"][0]["function"]["arguments"] == "[redacted]"
    assert safe["harness_messages"][1]["content"] == "run the tool"
    assert safe["harness_pending_approvals"][0]["inputs"]["password"] == "[redacted]"
    assert safe["harness_active_agents"][0]["api_token"] == "[redacted]"
    assert safe["task_outputs"]["task-1"]["api_token"] == "[redacted]"
    for secret in (
        "private assistant content",
        "argument-secret",
        "legacy-secret",
        "tool-secret",
        "approval-secret",
        "config-secret",
        "agent-secret",
        "task-secret",
    ):
        assert secret not in serialized


def test_public_resume_projection_contains_only_renderable_pause_fields() -> None:
    public = display.project_resume_state_public(_resume_state())
    serialized = json.dumps(public)

    assert public["kind"] == "approval"
    assert public["pending_count"] == 1
    assert public["harness_pending_approvals"][0]["inputs"]["password"] == "[redacted]"
    assert "harness_messages" not in serialized
    assert "harness_active_agents" not in serialized
    assert "task_outputs" not in serialized
    assert "approval-secret" not in serialized


def test_private_resume_state_is_stripped_recursively_but_public_state_remains() -> None:
    payload = {
        "events": [
            {
                "payload": {
                    "resume_state": {"private": "raw"},
                    "resume_state_sealed": {"ciphertext": "sealed"},
                    "_resume_state": {"private": "legacy"},
                    "_resume_state_claim": {"claim_id": "internal"},
                    "resumeState": {"private": "camel-raw"},
                    "resume_state_public": {"kind": "approval"},
                }
            }
        ]
    }

    safe = display.strip_resume_state_secrets(payload)

    assert safe == {
        "events": [{"payload": {"resume_state_public": {"kind": "approval"}}}]
    }


def test_pause_resolution_prefers_internal_state_and_rejects_legacy_approval() -> None:
    internal = {"task_outputs": {"t1": {"api_token": "full"}}, "iteration": 1}
    internal_scan = display.resolve_pause_resume_from_messages(
        [
            {
                "role": "assistant",
                "metadata": {
                    display.CHAT_INTERNAL_RESUME_STATE_KEY: internal,
                    "trace_state": {"trace_id": "trace-1"},
                },
            }
        ]
    )
    approval_scan = display.resolve_pause_resume_from_messages(
        [
            {
                "role": "assistant",
                "metadata": {
                    "execution_log": [
                        {
                            "event": "execution_summary",
                            "data": {
                                "awaiting_approval": True,
                                "resume_state": {"api_token": "redacted"},
                            },
                        }
                    ]
                },
            }
        ]
    )

    assert internal_scan == display.PauseResumeScanResult(
        resume_state=internal,
        trace_state={"trace_id": "trace-1"},
    )
    assert approval_scan.resume_state is None


def test_pause_resolution_keeps_legacy_input_resume_contract() -> None:
    legacy = {"iteration": 2, "original_query": "continue"}

    scan = display.resolve_pause_resume_from_messages(
        [
            {
                "role": "assistant",
                "metadata": {
                    "execution_log": [
                        {
                            "event": "execution_summary",
                            "data": {
                                "awaiting_input": True,
                                "resume_state": legacy,
                            },
                        }
                    ]
                },
            }
        ]
    )

    assert scan.resume_state == legacy


def test_chat_client_and_llm_metadata_strip_server_only_resume_state() -> None:
    metadata = {
        display.CHAT_INTERNAL_RESUME_STATE_KEY: {"api_token": "secret"},
        "_resume_state_claim": {"claim_id": "internal-claim"},
        "execution_log": [
            {
                "event": "execution_summary",
                "data": {
                    "summary": "done",
                    "resume_state": {"api_token": "secret"},
                },
            }
        ],
    }

    client = display.chat_message_metadata_for_client(metadata)
    tenant = display.sanitize_chat_message_metadata_for_tenant(metadata)
    llm = display.sanitize_message_metadata_for_llm(metadata)
    message = display.sanitize_chat_message_for_tenant(
        {"role": "assistant", "metadata": metadata}
    )

    for safe in (client, tenant, llm, message["metadata"]):
        serialized = json.dumps(safe)
        assert "_resume_state" not in serialized
        assert "_resume_state_claim" not in serialized
        assert '"resume_state"' not in serialized
        assert "secret" not in serialized
    assert client["execution_log"][0]["data"]["summary"] == "done"


def test_stream_failure_and_task_results_fail_closed() -> None:
    error = display.sanitize_stream_event_payload(
        "error",
        {
            "error": "secret upstream failure",
            "reason": "internal stack trace",
            "stack": "Traceback /private/path",
        },
    )
    task = display.sanitize_task_result_for_stream(
        {"status": "error", "error": "connection refused: pulse:7233"}
    )

    assert error == {"error": "Run failed", "reason": "Run failed"}
    assert task == {"status": "error", "error": "Unknown error"}


def test_stream_event_specific_fields_are_preserved_or_redacted_by_policy() -> None:
    completed = display.sanitize_stream_event_payload(
        "task_complete",
        {
            "task_id": "t1",
            "reason": "Max retries exceeded. Skipping by configured policy.",
        },
    )
    starting = display.sanitize_stream_event_payload(
        "task_starting",
        {"inputs": {"cluster": "tenant-cluster", "api_token": "secret"}},
    )
    thought = display.sanitize_stream_event_payload(
        "thought_complete",
        {"observation": "token sk-live-abcdefghij", "plan": "continue"},
    )

    assert completed["reason"] == "Max retries exceeded. Skipping by configured policy."
    assert starting["inputs"]["cluster"] == "tenant-cluster"
    assert starting["inputs"]["api_token"] == "[redacted]"
    assert "sk-live" not in str(thought["observation"])


def test_execution_summary_projects_public_state_without_private_resume_blob() -> None:
    output = display.sanitize_stream_event_payload(
        "execution_summary",
        {
            "awaiting_approval": True,
            "resume_state": {
                "task_outputs": {"t1": {"api_token": "secret"}},
                "pending_endpoint_approval": {
                    "inputs": {"api_key": "approval-key"}
                },
            },
            "summary": "tenant-visible summary",
        },
    )

    assert "resume_state" not in output
    assert output["summary"] == "tenant-visible summary"
    assert output["resume_state_public"]["kind"] == "approval"
    assert (
        output["resume_state_public"]["pending_endpoint_approval"]["inputs"][
            "api_key"
        ]
        == "[redacted]"
    )
    assert "secret" not in json.dumps(output)


def test_approval_run_and_resume_api_documents_never_leak_private_state() -> None:
    approval = display.sanitize_approval_document_for_tenant(
        {
            "resume_state": {"secret": "raw-approval-state"},
            "resume_state_sealed": {"ciphertext": "sealed-approval-state"},
            "inputs": {"password": "approval-password"},
            "reason": "Bearer abcdef123456",
        }
    )
    run = display.sanitize_run_event_document_for_tenant(
        {
            "payload": {
                "resume_state": {"secret": "raw-run-state"},
                "inputs": {"api_token": "run-token"},
            },
            "error": {"message": "connection refused: internal"},
        }
    )
    resumed = display.sanitize_resume_execution_api_result(
        {
            "status": "waiting_input",
            "message": "Bearer abcdef123456",
            "input_request": {
                "question": "Use sk-live-abcdefghij?",
                "options": ["continue", "stop"],
            },
        }
    )
    serialized = json.dumps({"approval": approval, "run": run, "resumed": resumed})

    for secret in (
        "raw-approval-state",
        "sealed-approval-state",
        "approval-password",
        "raw-run-state",
        "run-token",
        "abcdef123456",
        "sk-live",
    ):
        assert secret not in serialized
    assert run["error"]["message"] == "Run failed"
    assert resumed["status"] == "waiting_input"
    assert resumed["input_request"]["options"] == ["continue", "stop"]
