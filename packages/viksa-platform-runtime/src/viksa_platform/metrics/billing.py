"""Canonical, replay-safe billing event helpers.

The raw usage event is the billing source of truth.  Aggregates and UI views
must always be reproducible from these immutable documents.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

from viksa_platform.metrics.constants import PREFIX_TOKEN_USAGE, entity_type_from_resource_type
from viksa_platform.metrics.resource_refs import build_resource_refs, merge_resource_refs

USD_NANOS = Decimal("1000000000")
_INVOICE_PRICING_SOURCES = frozenset(
    {
        "deployment_contract_override",
        "provider_invoice_reconciliation",
    }
)


class UsageEventCollisionError(ValueError):
    """Raised when one immutable event identity is reused with other facts."""


class UsageDeliveryRejected(RuntimeError):
    """Raised when no durable or bounded local queue accepted a usage event."""


def billing_ledger_descriptor(*, collection: str = "token_usage") -> dict[str, Any]:
    """Describe the billing authority returned by every cost-read surface."""
    return {
        "authority": "immutable_provider_call_ledger",
        "collection": collection,
        "schema_version": "3",
        "currency": "USD",
        "cost_precision": "usd_nanos",
        "attribution_policy": "equal_integer_remainder_v1",
        "rollup_policy": "raw_ledger_only",
        "billing_status_policy": "credential_source_recomputed_v3",
        "trace_role": "diagnostic_reconciliation_evidence",
    }


def pricing_contract_is_invoice_eligible(pricing: Any) -> bool:
    """Return whether a pricing snapshot is trusted for invoicing.

    Persisted ``billing_status`` is intentionally not part of this decision.
    Readers recompute status from immutable usage and pricing facts so legacy,
    buggy, or forged flags cannot promote an arbitrary amount into an invoice.
    """
    return bool(
        isinstance(pricing, Mapping)
        and pricing.get("billing_eligible") is True
        and str(pricing.get("source") or "") in _INVOICE_PRICING_SOURCES
        and str(pricing.get("version") or "").strip()
        and pricing.get("currency") == "USD"
    )


def effective_usage_billing_status(document: Mapping[str, Any]) -> str:
    """Recompute the authoritative status of one immutable ledger event."""
    if document.get("credential_source") == "customer":
        return "non_billable"
    has_cost = (
        document.get("cost_nanos_usd") is not None
        or document.get("cost_usd") is not None
    )
    usage_status = str(document.get("usage_status") or "")
    if (
        not has_cost
        or document.get("reconciliation_required") is True
        or usage_status == "unavailable"
    ):
        return "reconciliation"

    pricing = document.get("pricing")
    pricing_is_estimate = bool(
        isinstance(pricing, Mapping)
        and (
            pricing.get("billing_eligible") is False
            or pricing.get("source") == "platform_estimate"
        )
    )
    if document.get("estimated") is True or usage_status == "estimated" or pricing_is_estimate:
        return "estimated"
    if pricing_contract_is_invoice_eligible(pricing):
        return "billable"
    return "reconciliation"


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def usd_to_nanos(value: Any) -> int | None:
    """Convert USD to integer nanos without binary floating-point arithmetic."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("cost_usd must be a finite non-negative decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost_usd must be a finite non-negative decimal")
    return int((amount * USD_NANOS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def nanos_to_usd(value: Any) -> str | None:
    """Return a lossless decimal USD string for an integer-nanos value."""
    if value is None:
        return None
    nanos = int(value)
    if nanos < 0:
        raise ValueError("cost_nanos_usd must be non-negative")
    return format(Decimal(nanos) / USD_NANOS, "f")


def usage_event_fingerprint(document: Mapping[str, Any]) -> str:
    """Hash immutable billable facts, excluding transport and ingest metadata."""
    excluded = {
        "_id",
        "event_id",
        "idempotency_key",
        "payload_fingerprint",
        # Sequence participates in fallback identity when a provider request ID
        # is unavailable. It is transport context, not a billable fact, and may
        # legitimately differ when the same provider event is replayed.
        "call_sequence",
        "created_at",
        "ingested_at",
        "date_bucket",
    }
    return _digest({key: value for key, value in document.items() if key not in excluded})


def _split_integer(value: int | None, count: int) -> list[int | None]:
    """Split an integer exactly; first rows receive deterministic remainders."""
    if value is None:
        return [None] * count
    quotient, remainder = divmod(int(value), count)
    return [quotient + (1 if index < remainder else 0) for index in range(count)]


def build_agent_allocations(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Allocate a shared call exactly once across participating agents."""
    refs = dict(document.get("resource_refs") or {})
    raw_ids = refs.get("agent_ids") or []
    if not isinstance(raw_ids, (list, tuple, set)):
        raw_ids = [raw_ids]
    raw_ids = [*raw_ids, refs.get("agent_id")]
    agent_ids = sorted(
        {str(value).strip() for value in raw_ids if str(value or "").strip()}
    )
    if not agent_ids:
        return []

    prompt_parts = _split_integer(document.get("prompt_tokens"), len(agent_ids))
    completion_parts = _split_integer(document.get("completion_tokens"), len(agent_ids))
    if document.get("prompt_tokens") is not None and document.get("completion_tokens") is not None:
        total_parts = [
            int(prompt_parts[index] or 0) + int(completion_parts[index] or 0)
            for index in range(len(agent_ids))
        ]
    else:
        total_parts = _split_integer(int(document.get("tokens_used") or 0), len(agent_ids))
    cost_parts = _split_integer(document.get("cost_nanos_usd"), len(agent_ids))
    estimated_cost_parts = _split_integer(
        document.get("estimated_cost_nanos_usd"), len(agent_ids)
    )
    call_parts = _split_integer(1_000_000_000, len(agent_ids))

    return [
        {
            "agent_id": agent_id,
            "allocation_policy": "equal_integer_remainder_v1",
            "allocation_ordinal": index,
            "allocation_count": len(agent_ids),
            "prompt_tokens": prompt_parts[index],
            "completion_tokens": completion_parts[index],
            "tokens_used": total_parts[index],
            "cost_nanos_usd": cost_parts[index],
            "cost_usd": nanos_to_usd(cost_parts[index]),
            "estimated_cost_nanos_usd": estimated_cost_parts[index],
            "estimated_cost_usd": nanos_to_usd(estimated_cost_parts[index]),
            # Exact fractional-call allocation in billionths. This lets rows
            # grouped by agent reconcile to the physical provider-call count
            # without pretending a shared call happened once per participant.
            "call_nanos": call_parts[index],
        }
        for index, agent_id in enumerate(agent_ids)
    ]


def stable_usage_event_id(document: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Resolve (event id, idempotency key, reconciliation-required).

    Provider request IDs are the strongest identity.  Otherwise an explicit
    idempotency key or activity/call sequence is required for deterministic
    replay.  Legacy callers still receive a unique ID, but the event is marked
    for reconciliation instead of pretending it can be replayed safely.
    """
    explicit_id = str(document.get("_id") or document.get("event_id") or "").strip()
    explicit_key = str(document.get("idempotency_key") or "").strip()
    provider = str(document.get("provider") or "").strip().lower()
    provider_request_id = str(document.get("provider_request_id") or "").strip()
    if provider_request_id:
        # Provider identity is authoritative. Never let a producer-supplied
        # event ID or idempotency key fork one physical provider call into
        # multiple billable ledger rows.
        key = f"provider:{provider or 'unknown'}:{provider_request_id}"
        return f"{PREFIX_TOKEN_USAGE}-{_digest({'key': key})[:32]}", key, False
    if explicit_key:
        # The idempotency key, rather than a mutable transport event ID, owns
        # replay identity when the provider exposes no request ID.
        return f"{PREFIX_TOKEN_USAGE}-{_digest({'key': explicit_key})[:32]}", explicit_key, False
    activity_id = str(document.get("activity_id") or "").strip()
    sequence = document.get("call_sequence")
    if activity_id and sequence is not None and int(sequence) > 0:
        key = ":".join(
            (
                "activity",
                str(document.get("account_id") or ""),
                activity_id,
                str(int(sequence)),
                str(document.get("operation") or "llm.call"),
            )
        )
        return f"{PREFIX_TOKEN_USAGE}-{_digest({'key': key})[:32]}", key, False
    fallback = explicit_id or f"{PREFIX_TOKEN_USAGE}-{secrets.token_hex(16)}"
    return fallback, f"legacy:{fallback}", True


def canonicalize_usage_event(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one immutable usage/billing event."""
    doc = dict(document)
    prompt = doc.get("prompt_tokens")
    completion = doc.get("completion_tokens")
    total = doc.get("tokens_used")
    for label, value in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("tokens_used", total),
    ):
        if value is not None and int(value) < 0:
            raise ValueError(f"{label} must be non-negative")
    prompt_i = int(prompt) if prompt is not None else None
    completion_i = int(completion) if completion is not None else None
    if total is None:
        total_i = int(prompt_i or 0) + int(completion_i or 0)
    else:
        total_i = int(total)
    if prompt_i is not None and completion_i is not None and total_i != prompt_i + completion_i:
        raise ValueError("tokens_used must equal prompt_tokens + completion_tokens")
    doc["prompt_tokens"] = prompt_i
    doc["completion_tokens"] = completion_i
    doc["tokens_used"] = total_i

    billable_units: dict[str, int | float] = {}
    for raw_name, raw_value in dict(doc.get("billable_units") or {}).items():
        name = str(raw_name or "").strip()
        if not name:
            raise ValueError("billable unit names must be non-empty")
        if isinstance(raw_value, bool):
            raise ValueError(f"billable unit {name} must be numeric")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"billable unit {name} must be finite and non-negative")
        billable_units[name] = int(value) if value.is_integer() else value
    doc["billable_units"] = billable_units or None

    # Provider token-detail fields (cached input, reasoning, audio, etc.) are
    # audit facts, not alternative totals. Preserve them without allowing
    # negative, fractional, boolean, or non-finite values into the ledger.
    token_details: dict[str, int] = {}
    for raw_name, raw_value in dict(doc.get("token_details") or {}).items():
        name = str(raw_name or "").strip()
        if not name:
            raise ValueError("token detail names must be non-empty")
        if isinstance(raw_value, bool):
            raise ValueError(f"token detail {name} must be a non-negative integer")
        try:
            numeric = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                f"token detail {name} must be a non-negative integer"
            ) from exc
        if not numeric.is_finite() or numeric < 0 or numeric != numeric.to_integral_value():
            raise ValueError(f"token detail {name} must be a non-negative integer")
        token_details[name] = int(numeric)
    doc["token_details"] = token_details or None

    metadata = dict(doc.get("metadata") or {})
    refs = merge_resource_refs(
        build_resource_refs(
            user_id=doc.get("user_id"),
            user_email=doc.get("user_email"),
            conversation_id=(
                doc.get("resource_id")
                if doc.get("resource_type") in {"chat", "intent_detection"}
                else None
            ),
            agent_id=metadata.get("agent_id"),
            agent_ids=metadata.get("agent_ids"),
            trigger_id=metadata.get("trigger_id"),
            schedule_id=metadata.get("schedule_id"),
            execution_id=metadata.get("execution_id"),
            workforce_id=metadata.get("workforce_id"),
            endpoint_id=metadata.get("endpoint_id"),
            message_id=doc.get("sub_resource_id") or metadata.get("message_id"),
        ),
        metadata.get("resource_refs"),
        doc.get("resource_refs"),
    )
    doc["resource_refs"] = refs or None
    resource_type = str(doc.get("resource_type") or "chat")
    doc["resource_type"] = resource_type
    doc.setdefault("entity_type", entity_type_from_resource_type(resource_type))
    doc.setdefault("entity_id", doc.get("resource_id"))
    doc["provider"] = str(doc.get("provider") or metadata.get("provider") or "unknown").lower()
    doc["provider_request_id"] = doc.get("provider_request_id") or metadata.get(
        "provider_request_id"
    )
    doc["service"] = doc.get("service") or metadata.get("service")

    credential_source = str(
        doc.get("credential_source") or metadata.get("credential_source") or "platform"
    ).strip().lower()
    if credential_source not in {"platform", "customer"}:
        raise ValueError("credential_source must be platform or customer")
    doc["credential_source"] = credential_source
    # Producer booleans are not authoritative. Only the credentials selected
    # by the universal runtime decide whether the call is chargeable.
    doc["billable_to_customer"] = credential_source == "platform"
    source_scope = str(
        doc.get("llm_source_scope") or metadata.get("llm_source_scope") or "platform"
    ).strip().lower()
    if source_scope not in {"platform", "account", "organization", "project"}:
        raise ValueError("llm_source_scope is invalid")
    if credential_source == "customer" and source_scope == "platform":
        raise ValueError("customer credentials require a tenant LLM source scope")
    if credential_source == "platform" and source_scope != "platform":
        raise ValueError("platform credentials require platform LLM source scope")
    doc["llm_source_scope"] = source_scope

    pricing = doc.get("pricing")
    if pricing is not None and not isinstance(pricing, Mapping):
        raise ValueError("pricing must be an object")
    # A producer cannot make an arbitrary number invoice eligible by merely
    # setting ``billing_eligible=true``. Invoice cost requires a versioned USD
    # snapshot from one of the deliberately narrow pricing authorities. The
    # immutable snapshot remains attached to the event for later audit.
    pricing_contract_valid = pricing_contract_is_invoice_eligible(pricing)
    pricing_is_estimate = bool(
        isinstance(pricing, Mapping)
        and (
            pricing.get("billing_eligible") is False
            or pricing.get("source") == "platform_estimate"
        )
    )
    estimated = bool(doc.get("estimated") or pricing_is_estimate)
    doc["estimated"] = estimated
    if doc.get("usage_status") not in {"observed", "estimated", "unavailable", "reconciled"}:
        doc["usage_status"] = (
            "estimated" if estimated else ("observed" if total_i > 0 else "unavailable")
        )
    if "cost_nanos_usd" not in doc:
        doc["cost_nanos_usd"] = usd_to_nanos(doc.get("cost_usd"))
    elif doc["cost_nanos_usd"] is not None:
        doc["cost_nanos_usd"] = int(doc["cost_nanos_usd"])
        if doc["cost_nanos_usd"] < 0:
            raise ValueError("cost_nanos_usd must be non-negative")
    if doc.get("cost_usd") is None and doc.get("cost_nanos_usd") is not None:
        doc["cost_usd"] = nanos_to_usd(doc["cost_nanos_usd"])
    if credential_source == "customer":
        # Preserve the provider-equivalent estimate for analytics while the
        # Viksa invoice amount is canonically zero.
        estimated_nanos = doc.get("estimated_cost_nanos_usd")
        if estimated_nanos is None:
            estimated_nanos = doc.get("cost_nanos_usd")
        if estimated_nanos is None:
            estimated_nanos = usd_to_nanos(
                doc.get("estimated_cost_usd") or doc.get("cost_usd")
            )
        if estimated_nanos is not None:
            estimated_nanos = int(estimated_nanos)
            if estimated_nanos < 0:
                raise ValueError("estimated_cost_nanos_usd must be non-negative")
        doc["estimated_cost_nanos_usd"] = estimated_nanos
        doc["estimated_cost_usd"] = nanos_to_usd(estimated_nanos)
        doc["provider_equivalent_cost_status"] = (
            "priced" if estimated_nanos is not None else "unpriced"
        )
        doc["cost_nanos_usd"] = 0
        doc["cost_usd"] = "0"
    else:
        estimated_nanos = doc.get("estimated_cost_nanos_usd")
        if estimated_nanos is None:
            estimated_nanos = usd_to_nanos(doc.get("estimated_cost_usd"))
        if estimated_nanos is not None:
            estimated_nanos = int(estimated_nanos)
            if estimated_nanos < 0:
                raise ValueError("estimated_cost_nanos_usd must be non-negative")
        doc["estimated_cost_nanos_usd"] = estimated_nanos
        doc["estimated_cost_usd"] = nanos_to_usd(estimated_nanos)
        doc["provider_equivalent_cost_status"] = "not_applicable"
    doc["cost_status"] = "priced" if doc.get("cost_nanos_usd") is not None else "unpriced"
    doc["agent_allocations"] = build_agent_allocations(doc)
    doc["attribution_version"] = "agent-equal-v1"

    event_id, idempotency_key, needs_reconciliation = stable_usage_event_id(doc)
    doc["_id"] = event_id
    doc["idempotency_key"] = idempotency_key
    doc["reconciliation_required"] = bool(
        doc.get("reconciliation_required")
        or needs_reconciliation
        or doc["usage_status"] == "unavailable"
        or (credential_source == "platform" and doc["cost_status"] == "unpriced")
        or (
            credential_source == "platform"
            and doc["cost_status"] == "priced"
            and not estimated
            and not pricing_contract_valid
        )
    )
    doc["billing_status"] = effective_usage_billing_status(doc)
    doc["payload_fingerprint"] = usage_event_fingerprint(doc)
    return doc


__all__ = [
    "UsageDeliveryRejected",
    "UsageEventCollisionError",
    "billing_ledger_descriptor",
    "build_agent_allocations",
    "canonicalize_usage_event",
    "effective_usage_billing_status",
    "nanos_to_usd",
    "pricing_contract_is_invoice_eligible",
    "stable_usage_event_id",
    "usd_to_nanos",
    "usage_event_fingerprint",
]
