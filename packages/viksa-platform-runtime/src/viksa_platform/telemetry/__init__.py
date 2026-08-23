"""Shared cost and execution telemetry contracts.

This is a code module, not a service: services keep their own APIs and
storage, and import these helpers the same way they import ``viksa_platform.tracing``.
"""

from viksa_platform.execution.cost_rollup_match import build_cost_rollup_match_filter
from viksa_platform.metrics.resource_refs import (
    RESOURCE_REF_KEYS,
    build_resource_refs,
    merge_resource_refs,
    resource_ref_match,
)
from viksa_platform.telemetry.attribution import (
    agent_id_from_document,
    catalog_agent_ids,
    stamp_catalog_agent_ids,
)

__all__ = [
    "RESOURCE_REF_KEYS",
    "agent_id_from_document",
    "build_cost_rollup_match_filter",
    "build_resource_refs",
    "catalog_agent_ids",
    "merge_resource_refs",
    "resource_ref_match",
    "stamp_catalog_agent_ids",
]
