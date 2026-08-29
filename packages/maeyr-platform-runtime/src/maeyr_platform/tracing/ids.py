"""OpenTelemetry-compatible 128/64-bit trace and span identifiers."""

from __future__ import annotations

import re
import secrets

_TRACE_HEX_RE = re.compile(r"^[\da-f]{32}$", re.IGNORECASE)
_SPAN_HEX_RE = re.compile(r"^[\da-f]{16}$", re.IGNORECASE)


def generate_trace_id() -> str:
    """Return a 128-bit trace ID as 32 lowercase hexadecimal characters."""
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """Return a 64-bit span ID as 16 lowercase hexadecimal characters."""
    return secrets.token_hex(8)


def normalize_trace_id(raw: str) -> str:
    """Coerce legacy ``TR-*``, UUID, or hexadecimal input to a 32-character ID."""
    value = (raw or "").strip()
    if not value:
        return generate_trace_id()
    if _TRACE_HEX_RE.match(value):
        return value.lower()
    hex_only = value.replace("-", "")
    if value.upper().startswith("TR") and len(hex_only) >= 8:
        legacy_hex = value[2:].lstrip("-").replace("-", "")
        if re.fullmatch(r"[\da-f]+", legacy_hex, re.IGNORECASE):
            return legacy_hex[-32:].lower().zfill(32)
    if len(hex_only) == 32 and re.fullmatch(r"[\da-f]+", hex_only, re.IGNORECASE):
        return hex_only.lower()
    if len(hex_only) >= 32:
        return hex_only[-32:].lower()
    return hex_only.lower().zfill(32)[-32:]


def normalize_parent_span_id(raw: str | None) -> str | None:
    """Normalize a parent span ID, preserving an absent value as ``None``."""
    if raw is None or str(raw).strip() == "":
        return None
    return normalize_span_id(str(raw))


def normalize_span_id(raw: str) -> str:
    """Coerce legacy ``SP-*`` or hexadecimal input to a 16-character ID."""
    value = (raw or "").strip()
    if not value:
        return generate_span_id()
    if _SPAN_HEX_RE.match(value):
        return value.lower()
    hex_only = value.replace("-", "")
    if value.upper().startswith("SP") and len(hex_only) >= 8:
        legacy_hex = value[2:].lstrip("-").replace("-", "")
        if re.fullmatch(r"[\da-f]+", legacy_hex, re.IGNORECASE):
            return legacy_hex[-16:].lower().zfill(16)
    if len(hex_only) >= 16:
        return hex_only[-16:].lower()
    return hex_only.lower().zfill(16)[-16:]


__all__ = [
    "generate_span_id",
    "generate_trace_id",
    "normalize_parent_span_id",
    "normalize_span_id",
    "normalize_trace_id",
]
