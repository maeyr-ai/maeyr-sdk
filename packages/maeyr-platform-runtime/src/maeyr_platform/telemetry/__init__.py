"""Shared cost and execution telemetry contracts.

This is a code module, not a service: services keep their own APIs and
storage, and import these helpers the same way they import ``maeyr_platform.tracing``.
"""

from maeyr_platform.execution.cost_rollup_match import build_cost_rollup_match_filter
from maeyr_platform.metrics.resource_refs import (
    RESOURCE_REF_KEYS,
    build_resource_refs,
    merge_resource_refs,
    resource_ref_match,
)
from maeyr_platform.telemetry.attribution import (
    agent_id_from_document,
    catalog_agent_ids,
    stamp_catalog_agent_ids,
)
from maeyr_platform.telemetry.execution import (
    EXECUTION_FAILURE_STATUSES,
    EXECUTION_ROLLUP_GROUP_BY,
    EXECUTION_SUCCESS_STATUSES,
    execution_failure_cond,
    execution_rollup_group_id,
    execution_status_label,
    execution_success_cond,
    normalize_execution_group_by,
    shape_execution_rollup,
    shape_execution_rollup_row,
)

__all__ = [
    "EXECUTION_FAILURE_STATUSES",
    "EXECUTION_ROLLUP_GROUP_BY",
    "EXECUTION_SUCCESS_STATUSES",
    "RESOURCE_REF_KEYS",
    "agent_id_from_document",
    "build_cost_rollup_match_filter",
    "build_resource_refs",
    "catalog_agent_ids",
    "execution_failure_cond",
    "execution_rollup_group_id",
    "execution_status_label",
    "execution_success_cond",
    "merge_resource_refs",
    "normalize_execution_group_by",
    "resource_ref_match",
    "shape_execution_rollup",
    "shape_execution_rollup_row",
    "stamp_catalog_agent_ids",
]
