"""Canonical tenant isolation for Cloud Workers in shared tier namespaces."""

from __future__ import annotations

import hashlib
import json


def cloud_worker_task_queue(account_id: str, org_id: str, project_id: str) -> str:
    """Bounded immutable queue identity, independent of tier and customer input.

    Org/project IDs need only be unique inside their account. Including the exact
    account tuple is mandatory now that Cloud Temporal namespaces are shared.
    JSON array encoding prevents ambiguous concatenation; the version fixes the
    derivation for the lifetime of persisted worker and execution records.
    """
    scope = (account_id, org_id, project_id)
    if any(type(value) is not str or not value or len(value) > 256
           or value != value.strip() or any(ord(char) < 32 for char in value)
           for value in scope):
        raise ValueError(
            "Cloud worker task queue requires exact account, org, and project identities"
        )
    material = json.dumps(
        ["cloud-worker-queue.v1", *scope], ensure_ascii=True, separators=(",", ":")
    )
    return "cloud-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def secure_worker_task_queue(
    account_id: str, org_id: str, project_id: str, worker_id: str
) -> str:
    """Exact tenant and logical worker isolation inside shared namespaces."""
    scope = (account_id, org_id, project_id, worker_id)
    if any(type(value) is not str or not value or len(value) > 256
           or value != value.strip() or any(ord(char) < 32 for char in value)
           for value in scope):
        raise ValueError("Secure worker queue requires exact tenant and worker identities")
    material = json.dumps(
        ["secure-worker-queue.v1", *scope], ensure_ascii=True, separators=(",", ":")
    )
    return "secure-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = ["cloud_worker_task_queue", "secure_worker_task_queue"]
