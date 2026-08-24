from __future__ import annotations

import pytest

from viksa_platform.telemetry import (
    EXECUTION_FAILURE_STATUSES,
    EXECUTION_SUCCESS_STATUSES,
    execution_failure_cond,
    execution_rollup_group_id,
    execution_status_label,
    execution_success_cond,
    normalize_execution_group_by,
    shape_execution_rollup,
)


def test_normalize_execution_group_by_aliases() -> None:
    assert normalize_execution_group_by("person") == "user"
    assert normalize_execution_group_by("date") == "day"
    assert normalize_execution_group_by("STATUS") == "status"
    with pytest.raises(ValueError, match="unsupported execution rollup group"):
        normalize_execution_group_by("model")


def test_execution_status_labels_and_mongo_group_ids() -> None:
    assert execution_status_label("ok") == "Succeeded"
    assert execution_status_label("error") == "Failed"
    assert execution_status_label("waiting_approval") == "Waiting for approval"
    assert execution_rollup_group_id("user")["$ifNull"][0] == "$user_email"
    assert execution_rollup_group_id("day")["$dateToString"]["format"] == "%Y-%m-%d"
    assert EXECUTION_SUCCESS_STATUSES == ("ok", "completed")
    assert EXECUTION_FAILURE_STATUSES == ("error", "timeout")
    assert execution_success_cond()["$in"][1] == list(EXECUTION_SUCCESS_STATUSES)
    assert execution_failure_cond()["$in"][1] == list(EXECUTION_FAILURE_STATUSES)


def test_shape_execution_rollup_totals_and_rows() -> None:
    payload = shape_execution_rollup(
        group_by="person",
        totals={
            "run_count": 4,
            "succeeded": 3,
            "failed": 1,
            "avg_duration_ms": 7930.4,
            "max_duration_ms": 8100,
            "tokens": 3242,
        },
        rows=[
            {
                "_id": "ada@example.com",
                "run_count": 3,
                "succeeded": 3,
                "failed": 0,
                "avg_duration_ms": 7000,
                "tokens": 2000,
            },
            {
                "_id": None,
                "run_count": 1,
                "succeeded": 0,
                "failed": 1,
                "avg_duration_ms": 1000,
                "tokens": 1242,
            },
        ],
    )
    assert payload["group_by"] == "user"
    assert payload["run_count"] == 4
    assert payload["succeeded"] == 3
    assert payload["failed"] == 1
    assert payload["avg_duration_ms"] == 7930
    assert payload["p95_duration_ms"] == 8100
    assert payload["rows"][0]["group_label"] == "ada@example.com"
    assert payload["rows"][1]["group_id"] == "unknown"
    status_payload = shape_execution_rollup(
        group_by="status",
        totals=None,
        rows=[{"_id": "ok", "run_count": 2, "succeeded": 2, "failed": 0}],
    )
    assert status_payload["run_count"] == 0
    assert status_payload["rows"][0]["group_label"] == "Succeeded"
