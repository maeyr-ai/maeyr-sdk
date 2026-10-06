"""Trusted, bounded sandbox telemetry; customer bytes never choose attribution."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UsageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["maeyr-runtime-usage-v1"] = Field(
        default="maeyr-runtime-usage-v1", alias="schema"
    )
    receipt_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_id: str = Field(pattern=r"^AC-[A-Za-z0-9_-]{1,60}$")
    org_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    execution_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    attempt: int = Field(ge=1, le=9_007_199_254_740_991)
    broker_id: str = Field(min_length=1, max_length=253)
    boot_id: str = Field(min_length=1, max_length=96)
    sandbox_id: str = Field(pattern=r"^maeyr-[a-f0-9]{48}$")
    started_at: str = Field(min_length=20, max_length=64)
    finished_at: str = Field(min_length=20, max_length=64)
    occupied_micros: int = Field(ge=0, le=900_000_000)
    cpu_usage_micros: int | None = Field(default=None, ge=0)
    memory_peak_bytes: int | None = Field(default=None, ge=0)
    cpu_millis: int = Field(ge=1, le=1_024_000)
    memory_bytes: int = Field(ge=33_554_432, le=2**50)
    termination_confirmed: bool
    outcome: Literal["succeeded", "failed", "cancelled", "timeout", "not_started"]

    @model_validator(mode="after")
    def bound_receipt(self) -> UsageReceipt:
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
        if started.tzinfo is None or finished.tzinfo is None or finished < started:
            raise ValueError("Usage timestamps require an ordered timezone-aware interval")
        if self.outcome == "not_started" and (
            self.occupied_micros != 0 or self.finished_at != self.started_at
        ):
            raise ValueError("An unstarted invocation must retain the exact zero-duration binding")
        if self.receipt_id != receipt_identity(self.model_dump(by_alias=True)):
            raise ValueError("Usage receipt identity does not bind its invocation and runtime")
        return self


def receipt_identity(receipt: Mapping[str, Any]) -> str:
    identity = [
        receipt[k]
        for k in (
            "account_id",
            "org_id",
            "project_id",
            "execution_id",
            "attempt",
            "broker_id",
            "boot_id",
            "sandbox_id",
        )
    ]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def scrub_logs(raw: bytes, credentials: Mapping[str, str], *, maximum: int) -> str:
    """Redact supplied secret values and common credential assignments.

    This is best-effort output redaction, not protection from malicious agents
    encoding a secret. Logs remain private to their trusted execution scope.
    """
    text = raw.decode("utf-8", errors="replace")
    variants = set()
    for value in credentials.values():
        if value:
            variants.add(value)
            variants.add(json.dumps(value, ensure_ascii=True)[1:-1])
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    # Strip terminal control sequences and non-printing controls from customer bytes.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
    return text.encode()[:maximum].decode("utf-8", errors="ignore")


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def occupied_micros(start_ns: int) -> int:
    return max(0, (time.monotonic_ns() - start_ns) // 1000)
