"""Canonical optional OTLP/HTTP JSON trace export."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("platform_traces.otlp_export")

_otlp_endpoint: Optional[str] = None
_otlp_headers: Dict[str, str] = {}


def configure_otlp_export(
    endpoint: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    """Configure OTLP HTTP endpoint, e.g. http://jaeger:4318/v1/traces."""
    global _otlp_endpoint, _otlp_headers
    _otlp_endpoint = (endpoint or os.getenv("OTLP_TRACES_ENDPOINT") or "").strip() or None
    _otlp_headers = dict(headers or {})


def otlp_export_enabled() -> bool:
    return bool(_otlp_endpoint)


def _to_unix_nano(value: Any) -> int:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _to_otlp_hex_id(raw: str, length: int) -> str:
    cleaned = (raw or "").replace("-", "")
    if cleaned.startswith("TR") or cleaned.startswith("SP"):
        cleaned = cleaned[2:]
    return cleaned[-length:].lower().zfill(length)


def _attr_string(key: str, value: Any) -> Dict[str, Any]:
    return {"key": key, "value": {"stringValue": str(value)}}


def _attr_int(key: str, value: int) -> Dict[str, Any]:
    return {"key": key, "value": {"intValue": str(value)}}


def spans_to_otlp_payload(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert internal span docs to OTLP JSON trace export body."""
    otlp_spans: List[Dict[str, Any]] = []
    for doc in spans:
        started = _to_unix_nano(doc.get("started_at"))
        ended = _to_unix_nano(doc.get("ended_at") or doc.get("started_at"))
        attrs: List[Dict[str, Any]] = [
            _attr_string("service.name", doc.get("service") or "unknown"),
        ]
        if doc.get("operation"):
            attrs.append(_attr_string("platform.operation", doc["operation"]))
        if doc.get("org_id"):
            attrs.append(_attr_string("tenant.org_id", doc["org_id"]))
        if doc.get("project_id"):
            attrs.append(_attr_string("tenant.project_id", doc["project_id"]))
        inner = doc.get("attributes") or {}
        if isinstance(inner, dict):
            for k, v in inner.items():
                if v is not None:
                    attrs.append(_attr_string(k, v))

        otlp_spans.append(
            {
                "traceId": _to_otlp_hex_id(str(doc.get("trace_id") or ""), 32),
                "spanId": _to_otlp_hex_id(str(doc.get("span_id") or doc.get("_id") or ""), 16),
                "parentSpanId": _to_otlp_hex_id(str(doc["parent_span_id"]), 16)
                if doc.get("parent_span_id")
                else None,
                "name": doc.get("span_name") or "internal",
                "kind": 2 if doc.get("span_kind") == "server" else 1,
                "startTimeUnixNano": str(started),
                "endTimeUnixNano": str(ended),
                "attributes": attrs,
                "status": {
                    "code": 2 if doc.get("status") == "error" else 1,
                },
            }
        )
        if otlp_spans[-1]["parentSpanId"] is None:
            del otlp_spans[-1]["parentSpanId"]

    service_name = spans[0].get("service") if spans else "unknown"
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_attr_string("service.name", service_name)],
                },
                "scopeSpans": [{"spans": otlp_spans}],
            }
        ]
    }


async def export_spans_otlp(spans: List[Dict[str, Any]]) -> None:
    """POST spans to configured OTLP endpoint (best-effort)."""
    if not _otlp_endpoint or not spans:
        return
    try:
        import httpx

        payload = spans_to_otlp_payload(spans)
        headers = {"Content-Type": "application/json", **_otlp_headers}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(_otlp_endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning("OTLP export failed %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.debug("OTLP export error (non-fatal): %s", exc)


def schedule_otlp_export(spans: List[Dict[str, Any]]) -> None:
    """Non-blocking OTLP export."""
    if not _otlp_endpoint or not spans:
        return
    asyncio.create_task(export_spans_otlp(spans), name="otlp_trace_export")
