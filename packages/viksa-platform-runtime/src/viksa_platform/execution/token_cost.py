"""
Canonical token-cost helper.

Translates LLM token counts into USD using a small in-process pricing table.
The table is intentionally illustrative; production deployments are expected
to override it via the `model_pricing` collection that marketplace-service
seeds at startup, or via `MODEL_PRICING_OVERRIDES` environment JSON.

Pricing convention: prices are USD per 1,000 tokens, split into prompt and
completion (a.k.a. input/output) per OpenAI/Anthropic standard. Unknown
models return cost_usd=None so callers can still record the row without
distorting downstream rollups.

Example:
    cost = compute_cost_usd("gpt-4o-mini", prompt_tokens=120, completion_tokens=300)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from logging import getLogger
from typing import Dict, Optional, Tuple

logger = getLogger("[viksa_chat.domain.token_cost]")


PRICING_VERSION = "2026-04-25"


@dataclass(frozen=True)
class ModelPrice:
    prompt_per_1k: float
    completion_per_1k: float


# Illustrative defaults. Override at deploy time via env or via the
# marketplace-service `model_pricing` collection.
_DEFAULT_PRICING: Dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(prompt_per_1k=0.0025, completion_per_1k=0.01),
    "gpt-4o-mini": ModelPrice(prompt_per_1k=0.00015, completion_per_1k=0.0006),
    "gpt-4.1": ModelPrice(prompt_per_1k=0.002, completion_per_1k=0.008),
    "gpt-4.1-mini": ModelPrice(prompt_per_1k=0.0004, completion_per_1k=0.0016),
    "claude-3-5-sonnet": ModelPrice(prompt_per_1k=0.003, completion_per_1k=0.015),
    "claude-3-5-haiku": ModelPrice(prompt_per_1k=0.0008, completion_per_1k=0.004),
    # Aliases used by chat-service today
    "azure-openai": ModelPrice(prompt_per_1k=0.0025, completion_per_1k=0.01),
}


def _load_overrides_from_env() -> Dict[str, ModelPrice]:
    """
    MODEL_PRICING_OVERRIDES is JSON of the form:
        {"gpt-4o": {"prompt_per_1k": 0.003, "completion_per_1k": 0.012}, ...}
    """
    raw = os.getenv("MODEL_PRICING_OVERRIDES")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning("MODEL_PRICING_OVERRIDES is not valid JSON: %s", exc)
        return {}
    out: Dict[str, ModelPrice] = {}
    for k, v in (parsed or {}).items():
        try:
            out[str(k).lower()] = ModelPrice(
                prompt_per_1k=float(v["prompt_per_1k"]),
                completion_per_1k=float(v["completion_per_1k"]),
            )
        except Exception:
            continue
    return out


_OVERRIDES = _load_overrides_from_env()


def _normalise(model: Optional[str]) -> str:
    if not model:
        return ""
    name = model.strip().lower()
    # Strip common deployment-name suffixes (e.g. "gpt-4o-2024-08-06")
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


def get_price(model: Optional[str]) -> Optional[ModelPrice]:
    name = _normalise(model)
    if not name:
        return None
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    return _DEFAULT_PRICING.get(name)


def compute_cost_usd(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Tuple[Optional[float], str]:
    """
    Returns (cost_usd, pricing_version). cost_usd is None for unknown models.
    """
    price = get_price(model)
    if price is None:
        return None, PRICING_VERSION
    p = max(0, int(prompt_tokens or 0))
    c = max(0, int(completion_tokens or 0))
    cost = (p / 1000.0) * price.prompt_per_1k + (c / 1000.0) * price.completion_per_1k
    # Round to 6 decimal places to keep Mongo doc compact and predictable.
    return round(cost, 6), PRICING_VERSION
