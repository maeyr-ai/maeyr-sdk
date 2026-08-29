"""Deterministic OTel-style head sampling keyed by trace ID."""

from __future__ import annotations

import os

_SAMPLE_RATE = 1.0


def configure_sampling(rate: float | None = None) -> None:
    global _SAMPLE_RATE
    if rate is not None:
        _SAMPLE_RATE = max(0.0, min(1.0, rate))
        return
    environment_rate = os.getenv("TRACE_SAMPLE_RATE", "1.0").strip()
    try:
        _SAMPLE_RATE = max(0.0, min(1.0, float(environment_rate)))
    except ValueError:
        _SAMPLE_RATE = 1.0


def sample_rate() -> float:
    return _SAMPLE_RATE


def should_sample(trace_id: str, *, force: bool = False) -> bool:
    if force or _SAMPLE_RATE >= 1.0:
        return True
    if _SAMPLE_RATE <= 0.0:
        return False
    key = (trace_id or "").replace("-", "")
    if not key:
        return True
    bucket = int(key[-8:], 16) if len(key) >= 8 else int(key or "0", 16)
    return (bucket % 10_000) < int(_SAMPLE_RATE * 10_000)


def traceparent_sampled(sampled: bool = True) -> str:
    return "01" if sampled else "00"


__all__ = [
    "configure_sampling",
    "sample_rate",
    "should_sample",
    "traceparent_sampled",
]
