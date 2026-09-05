"""Contract-aware pricing for immutable provider-call usage events.

Bundled prices are estimates for product telemetry. Only rates supplied via
``MODEL_PRICING_OVERRIDES`` are invoice eligible. Pricing is deliberately
fail-closed: if an observed token detail or provider-specific unit has no
explicit policy, no partial price is returned.

The deployment contract remains backward compatible with the original shape::

    {"gpt-4o": {"prompt_per_1k": "0.003", "completion_per_1k": "0.012"}}

It may additionally define special token and non-token rates::

    {
      "gpt-4o": {
        "prompt_per_1k": "0.003",
        "completion_per_1k": "0.012",
        "token_details": {
          "prompt_cached_tokens": {"per_1k": "0.0015", "deduct_from": "prompt"},
          "completion_reasoning_tokens": {"included_in": "completion"}
        }
      },
      "tts-1": {
        "billable_units": {
          "input_characters": {"price": "15", "unit_size": "1000000"}
        }
      }
    }

Every price snapshot is copied into the ledger event, so later configuration
changes never rewrite historical cost.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from logging import getLogger
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

logger = getLogger("[maeyr_platform.execution.token_cost]")

PRICING_VERSION = "2026-08-19.v2"
_THOUSAND = Decimal("1000")
_USD_QUANTUM = Decimal("0.000000001")


def _decimal(value: Any, *, label: str, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be finite and {qualifier}")
    return result


@dataclass(frozen=True)
class TokenDetailPrice:
    """Explicit treatment of one provider token-detail counter."""

    per_1k: Decimal | None = None
    deduct_from: str | None = None
    included_in: str | None = None

    def __post_init__(self) -> None:
        if self.deduct_from not in {None, "prompt", "completion"}:
            raise ValueError("token detail deduct_from must be prompt or completion")
        if self.included_in not in {None, "prompt", "completion"}:
            raise ValueError("token detail included_in must be prompt or completion")
        if self.per_1k is not None:
            object.__setattr__(self, "per_1k", _decimal(self.per_1k, label="token detail per_1k"))
        if self.included_in and (self.per_1k is not None or self.deduct_from):
            raise ValueError("included token details cannot also have a separate rate")
        if not self.included_in and self.per_1k is None:
            raise ValueError("token detail policy needs included_in or per_1k")


@dataclass(frozen=True)
class UnitPrice:
    """Price for a provider-specific quantity such as characters or seconds."""

    price: Decimal
    unit_size: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, label="unit price"))
        object.__setattr__(
            self,
            "unit_size",
            _decimal(self.unit_size, label="unit size", positive=True),
        )


@dataclass(frozen=True)
class ModelPrice:
    prompt_per_1k: Decimal | None = None
    completion_per_1k: Decimal | None = None
    token_details: Mapping[str, TokenDetailPrice] = field(default_factory=dict)
    billable_units: Mapping[str, UnitPrice] = field(default_factory=dict)

    def __init__(
        self,
        prompt_per_1k: float | str | Decimal | None = None,
        completion_per_1k: float | str | Decimal | None = None,
        *,
        token_details: Mapping[str, TokenDetailPrice] | None = None,
        billable_units: Mapping[str, UnitPrice] | None = None,
    ) -> None:
        prompt = (
            _decimal(prompt_per_1k, label="prompt_per_1k") if prompt_per_1k is not None else None
        )
        completion = (
            _decimal(completion_per_1k, label="completion_per_1k")
            if completion_per_1k is not None
            else None
        )
        details = dict(token_details or {})
        units = dict(billable_units or {})
        if prompt is None and completion is None and not details and not units:
            raise ValueError("model pricing contract is empty")
        object.__setattr__(self, "prompt_per_1k", prompt)
        object.__setattr__(self, "completion_per_1k", completion)
        object.__setattr__(self, "token_details", MappingProxyType(details))
        object.__setattr__(self, "billable_units", MappingProxyType(units))


@dataclass(frozen=True)
class UsagePriceResult:
    """Complete outcome of pricing one physical provider call."""

    cost_usd: float | None
    pricing_version: str
    pricing: dict[str, Any] | None
    uncovered_dimensions: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.cost_usd is not None and not self.uncovered_dimensions


_DEFAULT_PRICING: Dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(prompt_per_1k="0.0025", completion_per_1k="0.01"),
    "gpt-4o-mini": ModelPrice(prompt_per_1k="0.00015", completion_per_1k="0.0006"),
    "gpt-4.1": ModelPrice(prompt_per_1k="0.002", completion_per_1k="0.008"),
    "gpt-4.1-mini": ModelPrice(prompt_per_1k="0.0004", completion_per_1k="0.0016"),
    "claude-3-5-sonnet": ModelPrice(prompt_per_1k="0.003", completion_per_1k="0.015"),
    "claude-3-5-haiku": ModelPrice(prompt_per_1k="0.0008", completion_per_1k="0.004"),
    "azure-openai": ModelPrice(prompt_per_1k="0.0025", completion_per_1k="0.01"),
}


def _parse_token_detail_policy(name: str, raw: Any) -> TokenDetailPrice:
    if not isinstance(raw, Mapping):
        raise ValueError(f"token_details.{name} must be an object")
    return TokenDetailPrice(
        per_1k=(raw.get("per_1k") if "per_1k" in raw else None),
        deduct_from=(str(raw.get("deduct_from")) if raw.get("deduct_from") else None),
        included_in=(str(raw.get("included_in")) if raw.get("included_in") else None),
    )


def _parse_unit_price(name: str, raw: Any) -> UnitPrice:
    if not isinstance(raw, Mapping):
        raise ValueError(f"billable_units.{name} must be an object")
    if "per_unit" in raw:
        return UnitPrice(price=raw["per_unit"], unit_size=1)
    return UnitPrice(price=raw["price"], unit_size=raw.get("unit_size", 1))


def _parse_model_price(raw: Any) -> ModelPrice:
    if not isinstance(raw, Mapping):
        raise ValueError("model pricing must be an object")
    details = {
        str(name): _parse_token_detail_policy(str(name), policy)
        for name, policy in dict(raw.get("token_details") or {}).items()
    }
    units = {
        str(name): _parse_unit_price(str(name), policy)
        for name, policy in dict(raw.get("billable_units") or {}).items()
    }
    return ModelPrice(
        prompt_per_1k=raw.get("prompt_per_1k"),
        completion_per_1k=raw.get("completion_per_1k"),
        token_details=details,
        billable_units=units,
    )


def _normalise(model: Optional[str]) -> str:
    if not model:
        return ""
    name = model.strip().lower()
    for prefix in (
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-4.1",
        "claude-3-5-sonnet",
        "claude-3-5-haiku",
    ):
        if name.startswith(prefix):
            return prefix
    return name


def _load_overrides_from_env() -> Dict[str, ModelPrice]:
    raw = os.getenv("MODEL_PRICING_OVERRIDES")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - invalid config must not crash usage capture
        logger.error("MODEL_PRICING_OVERRIDES is not valid JSON: %s", exc)
        return {}
    if not isinstance(parsed, Mapping):
        logger.error("MODEL_PRICING_OVERRIDES must be a JSON object")
        return {}
    out: Dict[str, ModelPrice] = {}
    for name, value in parsed.items():
        try:
            normalized = _normalise(str(name))
            if not normalized:
                raise ValueError("model name is empty")
            out[normalized] = _parse_model_price(value)
        except Exception as exc:  # noqa: BLE001 - isolate one malformed model
            logger.error("invalid pricing contract for model=%s: %s", name, exc)
    return out


_OVERRIDES = _load_overrides_from_env()


def get_price(model: Optional[str]) -> Optional[ModelPrice]:
    name = _normalise(model)
    if not name:
        return None
    return _OVERRIDES.get(name) or _DEFAULT_PRICING.get(name)


def pricing_source(model: Optional[str]) -> str | None:
    name = _normalise(model)
    if not name:
        return None
    if name in _OVERRIDES:
        return "deployment_contract_override"
    if name in _DEFAULT_PRICING:
        return "platform_estimate"
    return None


def pricing_snapshot(model: Optional[str]) -> dict[str, Any] | None:
    """Return the immutable contract selected for an event."""
    price = get_price(model)
    if price is None:
        return None
    source = pricing_source(model)
    snapshot: dict[str, Any] = {
        "currency": "USD",
        "source": source,
        "billing_eligible": source == "deployment_contract_override",
    }
    if price.prompt_per_1k is not None:
        snapshot["prompt_per_1k"] = format(price.prompt_per_1k, "f")
    if price.completion_per_1k is not None:
        snapshot["completion_per_1k"] = format(price.completion_per_1k, "f")
    if price.token_details:
        snapshot["token_details"] = {
            name: {
                **({"per_1k": format(policy.per_1k, "f")} if policy.per_1k is not None else {}),
                **({"deduct_from": policy.deduct_from} if policy.deduct_from else {}),
                **({"included_in": policy.included_in} if policy.included_in else {}),
            }
            for name, policy in sorted(price.token_details.items())
        }
    if price.billable_units:
        snapshot["billable_units"] = {
            name: {
                "price": format(policy.price, "f"),
                "unit_size": format(policy.unit_size, "f"),
            }
            for name, policy in sorted(price.billable_units.items())
        }
    if source == "deployment_contract_override":
        configured_version = str(os.getenv("MODEL_PRICING_VERSION") or "").strip()
        contract_digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        snapshot["version"] = configured_version or f"{PRICING_VERSION}+{contract_digest}"
    else:
        snapshot["version"] = PRICING_VERSION
    return snapshot


def _count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("token counts must be non-negative integers")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("token counts must be non-negative integers")
    return int(parsed)


def compute_usage_cost_usd(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    *,
    token_details: Mapping[str, int] | None = None,
    billable_units: Mapping[str, int | float | Decimal] | None = None,
) -> UsagePriceResult:
    """Price every observed dimension or return an explicitly incomplete result."""
    price = get_price(model)
    snapshot = pricing_snapshot(model)
    prompt = _count(prompt_tokens)
    completion = _count(completion_tokens)
    details: dict[str, int] = {}
    for raw_name, raw_value in dict(token_details or {}).items():
        count = _count(raw_value)
        if count > 0:
            details[str(raw_name)] = count
    units: dict[str, Decimal] = {}
    for raw_name, raw_value in dict(billable_units or {}).items():
        value = _decimal(raw_value, label=f"billable unit {raw_name}")
        if value > 0:
            units[str(raw_name)] = value
    uncovered: list[str] = []
    if price is None:
        if prompt:
            uncovered.append("prompt_tokens")
        if completion:
            uncovered.append("completion_tokens")
        uncovered.extend(f"token_details.{name}" for name in sorted(details))
        uncovered.extend(f"billable_units.{name}" for name in sorted(units))
        if not uncovered:
            uncovered.append("model_rate")
        return UsagePriceResult(None, PRICING_VERSION, None, tuple(uncovered))

    prompt_base = prompt
    completion_base = completion
    detail_cost = Decimal("0")
    for name, count in sorted(details.items()):
        policy = price.token_details.get(name)
        if policy is None:
            uncovered.append(f"token_details.{name}")
            continue
        bucket_name = policy.included_in or policy.deduct_from
        if bucket_name:
            bucket = prompt if bucket_name == "prompt" else completion
            if count > bucket:
                uncovered.append(f"token_details.{name}:exceeds_{bucket_name}")
                continue
        if policy.included_in:
            continue
        if policy.deduct_from == "prompt":
            prompt_base -= count
        elif policy.deduct_from == "completion":
            completion_base -= count
        detail_cost += Decimal(count) / _THOUSAND * (policy.per_1k or Decimal("0"))

    cost = detail_cost
    if prompt_base:
        if price.prompt_per_1k is None:
            uncovered.append("prompt_tokens")
        else:
            cost += Decimal(prompt_base) / _THOUSAND * price.prompt_per_1k
    if completion_base:
        if price.completion_per_1k is None:
            uncovered.append("completion_tokens")
        else:
            cost += Decimal(completion_base) / _THOUSAND * price.completion_per_1k
    for name, value in sorted(units.items()):
        unit_price = price.billable_units.get(name)
        if unit_price is None:
            uncovered.append(f"billable_units.{name}")
            continue
        cost += value / unit_price.unit_size * unit_price.price

    if uncovered:
        return UsagePriceResult(
            None,
            str((snapshot or {}).get("version") or PRICING_VERSION),
            snapshot,
            tuple(sorted(set(uncovered))),
        )
    rounded = cost.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    return UsagePriceResult(
        float(rounded),
        str((snapshot or {}).get("version") or PRICING_VERSION),
        snapshot,
    )


def compute_cost_usd(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Tuple[Optional[float], str]:
    """Backward-compatible token-only pricing surface."""
    result = compute_usage_cost_usd(model, prompt_tokens, completion_tokens)
    return result.cost_usd, result.pricing_version


__all__ = [
    "ModelPrice",
    "PRICING_VERSION",
    "TokenDetailPrice",
    "UnitPrice",
    "UsagePriceResult",
    "compute_cost_usd",
    "compute_usage_cost_usd",
    "get_price",
    "pricing_snapshot",
    "pricing_source",
]
