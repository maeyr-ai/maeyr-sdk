import pytest

from viksa_platform.execution import token_cost
from viksa_platform.execution.token_cost import (
    ModelPrice,
    TokenDetailPrice,
    UnitPrice,
    compute_usage_cost_usd,
)
from viksa_platform.metrics.billing import (
    billing_ledger_descriptor,
    canonicalize_usage_event,
    effective_usage_billing_status,
)


def _event(**overrides):
    event = {
        "account_id": "AC-1",
        "org_id": "OI-1",
        "project_id": "PI-1",
        "activity_id": "ACT-1",
        "operation": "chat.completion",
        "provider": "openai",
        "provider_request_id": "resp_123",
        "model": "gpt-4o-mini",
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "tokens_used": 9,
        "cost_nanos_usd": 11,
        "pricing": {
            "version": "contract-1",
            "currency": "USD",
            "source": "deployment_contract_override",
            "billing_eligible": True,
        },
        "resource_type": "chat",
        "resource_refs": {"agent_ids": ["AI-B", "AI-A", "AI-A"]},
    }
    event.update(overrides)
    return event


def test_provider_request_identity_is_stable_across_delivery_retries():
    first = canonicalize_usage_event(_event())
    replay = canonicalize_usage_event(_event())

    assert first["_id"] == replay["_id"]
    assert first["payload_fingerprint"] == replay["payload_fingerprint"]
    assert first["idempotency_key"] == "provider:openai:resp_123"
    assert first["reconciliation_required"] is False


def test_provider_request_identity_cannot_be_forked_by_producer_ids():
    canonical = canonicalize_usage_event(_event())
    forged = canonicalize_usage_event(
        _event(_id="TU-FORGED", idempotency_key="producer-controlled")
    )

    assert forged["_id"] == canonical["_id"]
    assert forged["idempotency_key"] == "provider:openai:resp_123"


def test_explicit_idempotency_identity_cannot_be_forked_by_event_id():
    first = canonicalize_usage_event(
        _event(provider_request_id=None, _id="TU-ONE", idempotency_key="attempt-1")
    )
    replay = canonicalize_usage_event(
        _event(provider_request_id=None, _id="TU-TWO", idempotency_key="attempt-1")
    )

    assert replay["_id"] == first["_id"]
    assert replay["idempotency_key"] == "attempt-1"


def test_agent_allocations_reconcile_exactly_without_duplication():
    event = canonicalize_usage_event(_event())
    allocations = event["agent_allocations"]

    assert [row["agent_id"] for row in allocations] == ["AI-A", "AI-B"]
    assert sum(row["prompt_tokens"] for row in allocations) == 5
    assert sum(row["completion_tokens"] for row in allocations) == 4
    assert sum(row["tokens_used"] for row in allocations) == 9
    assert sum(row["cost_nanos_usd"] for row in allocations) == 11
    assert sum(row["call_nanos"] for row in allocations) == 1_000_000_000
    assert allocations[0]["tokens_used"] == 5
    assert allocations[1]["tokens_used"] == 4


def test_unpriced_or_unobserved_provider_call_is_flagged_for_reconciliation():
    event = canonicalize_usage_event(
        _event(
            model="unknown-model",
            prompt_tokens=None,
            completion_tokens=None,
            tokens_used=0,
            cost_nanos_usd=None,
            cost_usd=None,
            pricing=None,
        )
    )

    assert event["usage_status"] == "unavailable"
    assert event["cost_status"] == "unpriced"
    assert event["reconciliation_required"] is True


def test_billable_fact_change_changes_fingerprint_but_not_provider_identity():
    first = canonicalize_usage_event(_event())
    changed = canonicalize_usage_event(
        _event(prompt_tokens=6, completion_tokens=4, tokens_used=10)
    )

    assert first["_id"] == changed["_id"]
    assert first["payload_fingerprint"] != changed["payload_fingerprint"]


def test_non_token_billable_units_are_retained_for_reconciliation():
    event = canonicalize_usage_event(
        _event(
            provider_request_id=None,
            call_sequence=1,
            operation="speech.synthesize",
            provider_operation="audio.speech",
            model="tts-1",
            tokens_used=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_nanos_usd=None,
            cost_usd=None,
            pricing=None,
            usage_status="unavailable",
            billable_units={"input_characters": 42},
        )
    )

    assert event["billable_units"] == {"input_characters": 42}
    assert event["cost_status"] == "unpriced"
    assert event["reconciliation_required"] is True


def test_billable_units_reject_invalid_values():
    with pytest.raises(ValueError, match="finite and non-negative"):
        canonicalize_usage_event(
            _event(billable_units={"audio_seconds": float("nan")})
        )


@pytest.mark.parametrize("value", [-1, 1.5, True, float("nan")])
def test_token_details_reject_invalid_values(value):
    with pytest.raises(ValueError, match="non-negative integer"):
        canonicalize_usage_event(_event(token_details={"cached_input": value}))


def test_platform_estimate_cannot_become_invoice_eligible_by_omission():
    event = canonicalize_usage_event(
        _event(
            pricing={
                "version": "test",
                "currency": "USD",
                "source": "platform_estimate",
                "billing_eligible": False,
            },
            estimated=False,
        )
    )

    assert event["estimated"] is True
    assert event["billing_status"] == "estimated"


def test_contract_pricing_with_observed_usage_is_billable():
    event = canonicalize_usage_event(
        _event(
            pricing={
                "version": "contract-1",
                "currency": "USD",
                "source": "deployment_contract_override",
                "billing_eligible": True,
            }
        )
    )

    assert event["billing_status"] == "billable"


@pytest.mark.parametrize(
    "pricing",
    [
        None,
        {
            "version": "forged",
            "currency": "USD",
            "source": "untrusted_producer",
            "billing_eligible": True,
        },
        {
            "version": "",
            "currency": "USD",
            "source": "deployment_contract_override",
            "billing_eligible": True,
        },
        {
            "version": "contract-1",
            "currency": "EUR",
            "source": "deployment_contract_override",
            "billing_eligible": True,
        },
    ],
)
def test_untrusted_or_incomplete_pricing_can_never_enter_invoice(pricing):
    event = canonicalize_usage_event(_event(pricing=pricing, billing_status="billable"))

    assert event["billing_status"] != "billable"
    assert event["reconciliation_required"] is True


def test_read_time_status_ignores_forged_persisted_billable_flag():
    historical = _event(
        pricing={
            "version": "forged",
            "currency": "USD",
            "source": "untrusted_producer",
            "billing_eligible": True,
        },
        billing_status="billable",
    )

    assert effective_usage_billing_status(historical) == "reconciliation"


def test_ledger_contract_declares_fact_based_status_recomputation():
    descriptor = billing_ledger_descriptor()

    assert descriptor["schema_version"] == "3"
    assert descriptor["billing_status_policy"] == "credential_source_recomputed_v3"


def test_special_token_contract_deducts_base_tokens_exactly_once(monkeypatch):
    monkeypatch.setitem(
        token_cost._OVERRIDES,
        "contract-model",
        ModelPrice(
            prompt_per_1k="0.01",
            completion_per_1k="0.02",
            token_details={
                "prompt_cached_tokens": TokenDetailPrice(
                    per_1k="0.002",
                    deduct_from="prompt",
                ),
                "completion_reasoning_tokens": TokenDetailPrice(
                    included_in="completion",
                ),
            },
        ),
    )

    result = compute_usage_cost_usd(
        "contract-model",
        1_000,
        100,
        token_details={
            "prompt_cached_tokens": 400,
            "completion_reasoning_tokens": 25,
        },
    )

    assert result.cost_usd == pytest.approx(0.0088)
    assert result.uncovered_dimensions == ()
    assert result.pricing is not None
    assert result.pricing["billing_eligible"] is True


def test_non_token_provider_units_can_be_contract_priced(monkeypatch):
    monkeypatch.setitem(
        token_cost._OVERRIDES,
        "tts-contract",
        ModelPrice(
            billable_units={
                "input_characters": UnitPrice(price="15", unit_size="1000000"),
            }
        ),
    )

    result = compute_usage_cost_usd(
        "tts-contract",
        0,
        0,
        billable_units={"input_characters": 2_000_000},
    )

    assert result.cost_usd == 30.0
    assert result.complete is True


def test_override_snapshot_has_stable_contract_derived_version(monkeypatch):
    monkeypatch.delenv("MODEL_PRICING_VERSION", raising=False)
    monkeypatch.setitem(
        token_cost._OVERRIDES,
        "stable-contract",
        ModelPrice(prompt_per_1k="0.01", completion_per_1k="0.02"),
    )

    first = token_cost.pricing_snapshot("stable-contract")
    second = token_cost.pricing_snapshot("stable-contract")

    assert first == second
    assert first is not None
    assert first["version"].startswith(f"{token_cost.PRICING_VERSION}+")


def test_partial_pricing_is_never_returned_for_uncovered_dimension(monkeypatch):
    monkeypatch.setitem(
        token_cost._OVERRIDES,
        "incomplete-contract",
        ModelPrice(prompt_per_1k="0.01", completion_per_1k="0.02"),
    )

    result = compute_usage_cost_usd(
        "incomplete-contract",
        1_000,
        100,
        token_details={"prompt_cached_tokens": 400},
    )

    assert result.cost_usd is None
    assert result.uncovered_dimensions == ("token_details.prompt_cached_tokens",)


def test_detail_larger_than_provider_total_requires_reconciliation(monkeypatch):
    monkeypatch.setitem(
        token_cost._OVERRIDES,
        "bad-detail-contract",
        ModelPrice(
            prompt_per_1k="0.01",
            completion_per_1k="0.02",
            token_details={
                "prompt_cached_tokens": TokenDetailPrice(
                    per_1k="0.002",
                    deduct_from="prompt",
                )
            },
        ),
    )

    result = compute_usage_cost_usd(
        "bad-detail-contract",
        100,
        0,
        token_details={"prompt_cached_tokens": 101},
    )

    assert result.cost_usd is None
    assert result.uncovered_dimensions == (
        "token_details.prompt_cached_tokens:exceeds_prompt",
    )


def test_customer_llm_usage_is_visible_but_never_billable():
    document = canonicalize_usage_event(
        _event(
            provider_request_id="req-byollm",
            credential_source="customer",
            llm_source_scope="project",
            cost_nanos_usd=None,
            cost_usd="0.012345678",
            pricing=None,
        )
    )

    assert document["billable_to_customer"] is False
    assert document["billing_status"] == "non_billable"
    assert document["cost_nanos_usd"] == 0
    assert document["cost_usd"] == "0"
    assert document["estimated_cost_nanos_usd"] == 12_345_678
    assert document["estimated_cost_usd"] == "0.012345678"
    assert document["provider_equivalent_cost_status"] == "priced"
    assert document["reconciliation_required"] is False


def test_unknown_customer_model_is_non_billable_without_claiming_zero_provider_cost():
    document = canonicalize_usage_event(
        _event(
            provider_request_id="req-byollm-unknown",
            credential_source="customer",
            llm_source_scope="organization",
            cost_nanos_usd=None,
            cost_usd=None,
            pricing=None,
        )
    )

    assert document["billing_status"] == "non_billable"
    assert document["cost_nanos_usd"] == 0
    assert document["provider_equivalent_cost_status"] == "unpriced"
    assert document["estimated_cost_nanos_usd"] is None
    assert document["reconciliation_required"] is False


def test_producer_cannot_mark_platform_usage_non_billable():
    document = canonicalize_usage_event(
        _event(
            credential_source="platform",
            llm_source_scope="platform",
            billable_to_customer=False,
        )
    )
    assert document["billable_to_customer"] is True
