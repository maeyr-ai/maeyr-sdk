"""Small immutable references suitable for Temporal's durable history."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

WORKFLOW_NAME = "maeyr.serverless.invoke.v1"
ACTIVITY_NAME = "maeyr.serverless.execute.v1"
BUILD_WORKFLOW_NAME = "maeyr.serverless.build.v1"
BUILD_ACTIVITY_NAME = "maeyr.serverless.prepare.v1"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
STATUSES = TERMINAL_STATUSES | {"queued", "running"}
ReferencePayload = dict[str, str | int]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_REFERENCE_KEYS = frozenset(
    {"execution_id", "account_id", "org_id", "project_id", "deadline", "fence"}
)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid serverless execution identity")
    return value


@dataclass(frozen=True)
class InvocationReference:
    execution_id: str
    account_id: str
    org_id: str
    project_id: str
    deadline: datetime
    fence: int
    worker_id: str | None = None
    assignment_generation: int | None = None
    activity_task_queue: str | None = None

    def __post_init__(self) -> None:
        for value in (self.execution_id, self.account_id, self.org_id, self.project_id):
            _identifier(value)
        if not self.account_id.startswith("AC-") or len(self.account_id) > 63:
            raise ValueError("Invalid serverless account identity")
        if (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ValueError("Execution deadline must include a timezone")
        if type(self.fence) is not int or not 1 <= self.fence <= 2**53 - 1:
            raise ValueError("Invalid serverless execution fence")
        placement = (self.worker_id, self.assignment_generation, self.activity_task_queue)
        if any(value is not None for value in placement):
            _worker_id(self.worker_id)
            _generation(self.assignment_generation)
            _worker_queue(self.activity_task_queue, self.worker_id)
        object.__setattr__(self, "deadline", self.deadline.astimezone(timezone.utc))

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> InvocationReference:
        # Do not silently record caller inputs or credentials in history.
        if set(payload) != _REFERENCE_KEYS | {
            "worker_id",
            "assignment_generation",
            "activity_task_queue",
        }:
            raise ValueError("Invalid serverless workflow reference fields")
        deadline = payload["deadline"]
        if not isinstance(deadline, str):
            raise ValueError("Execution deadline must be an ISO timestamp")
        stamp = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        fence = payload["fence"]
        if type(fence) is not int:
            raise ValueError("Invalid serverless execution fence")
        return cls(
            _identifier(payload["execution_id"]),
            _identifier(payload["account_id"]),
            _identifier(payload["org_id"]),
            _identifier(payload["project_id"]),
            stamp,
            fence,
            _worker_id(payload["worker_id"]),
            _generation(payload["assignment_generation"]),
            _worker_queue(payload["activity_task_queue"], payload["worker_id"]),
        )

    def payload(self) -> ReferencePayload:
        value: ReferencePayload = {
            "execution_id": self.execution_id,
            "account_id": self.account_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "deadline": self.deadline.isoformat(),
            "fence": self.fence,
        }

        if self.worker_id is not None:
            assert self.assignment_generation is not None and self.activity_task_queue is not None
            value.update(
                worker_id=self.worker_id,
                assignment_generation=self.assignment_generation,
                activity_task_queue=self.activity_task_queue,
            )
        return value

    def bridge_scope(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "fence": self.fence,
        }

    def tenant_scope(self) -> dict[str, str]:
        return {
            "account_id": self.account_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
        }


def workflow_id(reference: InvocationReference) -> str:
    identity = json.dumps(
        [reference.account_id, reference.org_id, reference.project_id, reference.execution_id],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "maeyr-serverless-" + hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class BuildReference(InvocationReference):
    """The build store may use BUILD_<id>; the durable API exposes only build_id."""

    @property
    def build_id(self) -> str:
        if not self.execution_id.startswith("BUILD_"):
            raise ValueError("Invalid serverless build identity")
        return _identifier(self.execution_id[6:])

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> BuildReference:
        if set(payload) != (_REFERENCE_KEYS - {"execution_id"}) | {"build_id"}:
            raise ValueError("Invalid serverless build reference fields")
        build_id = _identifier(payload["build_id"])
        mapped = dict(payload)
        mapped.pop("build_id")
        mapped["execution_id"] = "BUILD_" + build_id
        deadline = mapped["deadline"]
        if not isinstance(deadline, str):
            raise ValueError("Invalid build deadline")
        fence = mapped["fence"]
        if type(fence) is not int:
            raise ValueError("Invalid build attempt fence")
        reference = InvocationReference(
            _identifier(mapped["execution_id"]),
            _identifier(mapped["account_id"]),
            _identifier(mapped["org_id"]),
            _identifier(mapped["project_id"]),
            datetime.fromisoformat(deadline.replace("Z", "+00:00")),
            fence,
        )
        return cls(
            reference.execution_id,
            reference.account_id,
            reference.org_id,
            reference.project_id,
            reference.deadline,
            reference.fence,
        )

    def payload(self) -> ReferencePayload:
        payload = super().payload()
        payload.pop("execution_id")
        payload["build_id"] = self.build_id
        return payload


def build_workflow_id(reference: InvocationReference) -> str:
    return workflow_id(reference).replace("maeyr-serverless-", "maeyr-serverless-build-", 1)


def _worker_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"worker_[a-f0-9]{40}", value):
        raise ValueError("Invalid worker identity")
    return value


def _generation(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise ValueError("Invalid assignment generation")
    return value


def _worker_queue(value: object, identity: object) -> str:
    if value != "maeyr-serverless-agent-" + _worker_id(identity):
        raise ValueError("Queue does not bind worker identity")
    return str(value)
