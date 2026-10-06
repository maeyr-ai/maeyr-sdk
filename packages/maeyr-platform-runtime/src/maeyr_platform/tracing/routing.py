"""Route diagnostics without passing through tenant trace transport or metering."""

from __future__ import annotations

import logging
from typing import Any

from .otlp_export import schedule_otlp_export
from .policy import tenant_trace_category
from .sampling import should_sample

logger = logging.getLogger("platform_traces.routing")


def route_span_for_recording(document: dict[str, Any]) -> bool:
    """True means retain in tenant transport; False means optional platform sink.

    Important semantic activity is never head sampled. Diagnostic export is
    completed-only, sampled, nonblocking, and never enqueued back into Trace.
    """
    if tenant_trace_category(document) is not None:
        return True
    if document.get("status") == "running" and not document.get("_is_completion"):
        return False
    try:
        if not should_sample(str(document.get("trace_id") or "")):
            return False
        # Header tenant metadata is correlation, not verified identity.
        exported = dict(document)
        if exported.get("_tenant_verified") is False:
            for key in (
                "account_id",
                "org_id",
                "project_id",
                "user_id",
                "user_email",
                "resource_refs",
            ):
                exported.pop(key, None)
        schedule_otlp_export([exported])
    except Exception:
        # Invalid diagnostic IDs or an unavailable exporter must not abort a
        # mixed batch containing important activity or change API/ledger results.
        logger.debug("Platform diagnostic routing failed", exc_info=True)
    return False
