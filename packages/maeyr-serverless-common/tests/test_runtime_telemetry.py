"""Trusted scoped receipt and bounded log privacy boundaries."""

import pytest
from pydantic import ValidationError

from maeyr_serverless_common.telemetry import UsageReceipt, receipt_identity, scrub_logs


def receipt():
    value = {
        "schema": "maeyr-runtime-usage-v1",
        "account_id": "AC-one",
        "org_id": "OI-one",
        "project_id": "PI-one",
        "execution_id": "EX-one",
        "attempt": 1,
        "broker_id": "native-worker",
        "boot_id": "native-pod",
        "sandbox_id": "maeyr-" + "a" * 48,
        "started_at": "2026-10-06T00:00:00+00:00",
        "finished_at": "2026-10-06T00:00:01+00:00",
        "occupied_micros": 1000000,
        "cpu_millis": 1000,
        "memory_bytes": 128 * 1024**2,
        "termination_confirmed": True,
        "outcome": "succeeded",
    }
    value["receipt_id"] = receipt_identity(value)
    return value


def test_supplied_secrets_and_terminal_sequences_are_scrubbed():
    raw = (
        b"hello\x1b[31m API_KEY=private\x1b[0m Authorization: Bearer other\n"
        + b"customer-secret" * 100
    )
    logs = scrub_logs(raw, {"TOKEN": "customer-secret"}, maximum=128)
    assert len(logs.encode()) <= 128
    assert "customer-secret" not in logs and "private" not in logs and "other" not in logs
    assert "\x1b" not in logs and "[REDACTED]" in logs


@pytest.mark.parametrize(
    "updates",
    [
        {"started_at": "2026-10-06T00:00:00"},
        {"finished_at": "2026-10-05T00:00:00+00:00"},
        {"outcome": "not_started", "occupied_micros": 0},
        {"execution_id": "EX-different"},
        {"occupied_micros": 900_000_001},
    ],
)
def test_receipt_rejects_unbound_scope_invalid_interval_and_fake_zero(updates):
    with pytest.raises(ValidationError):
        UsageReceipt.model_validate({**receipt(), **updates})


def test_unstarted_receipt_keeps_exact_binding_and_zero_duration():
    value = receipt()
    value.update(outcome="not_started", occupied_micros=0, finished_at=value["started_at"])
    value["receipt_id"] = receipt_identity(value)
    assert UsageReceipt.model_validate(value).occupied_micros == 0
