"""Durable worker ownership, scoped placement, and physical attempt accounting.

Placement examines at most the configured worker bound, never tenant databases.
Lease expiry fences new work; it is not proof an admitted sandbox terminated.
Only a matching durable termination receipt releases a physical reservation.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis

from .errors import ServiceError
from .financial import StartCommand, StartReceipt
from .models import Scope
from .names import worker_name
from .security import encode
from .telemetry import UsageReceipt

_WORKER = re.compile(r"worker_[a-f0-9]{40}")
_TOKEN = re.compile(r"[a-f0-9]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CREATION = re.compile(r"ACO-[a-f0-9]{64}")
_SCOPE_FIELDS = ("account_id", "org_id", "project_id")
_AGENT_FIELDS = (*_SCOPE_FIELDS, "agent_id")
_KEY_SUFFIXES = (
    "tombstones",
    "tombstone_expiry",
    "workers",
    "live",
    "counts",
    "expiry",
    "assignments",
    "sequence",
    "physical",
    "usage",
    "usage_pending",
    "usage_by_worker",
    "usage_attempts",
    "usage_by_attempt",
)


def _abi(value: dict[str, str]) -> dict[str, str]:
    if value not in (
        {"python": "3.11", "architecture": "amd64", "platform": "linux"},
        {"python": "3.11", "architecture": "arm64", "platform": "linux"},
    ):
        raise ValueError("Unsupported worker ABI")
    return value


class WorkerDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    worker_id: str = Field(pattern=r"^worker_[a-f0-9]{40}$")
    worker_name: str = Field(min_length=1, max_length=96)
    incarnation: str
    task_queue: str = Field(pattern=r"^maeyr-serverless-agent-worker_[a-f0-9]{40}$")
    artifact_store_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,95}$")
    image_ref: str = Field(pattern=r"^[^\s]+@sha256:[a-f0-9]{64}$", max_length=512)
    runtime_abi: dict[str, str]
    slots: int = Field(ge=1, le=256)
    cpu_millis: int = Field(ge=100, le=8000)
    memory_bytes: int = Field(ge=67108864, le=8589934592)

    @field_validator("incarnation")
    @classmethod
    def uid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Worker incarnation must be a canonical boot UUID")
        return value

    @field_validator("runtime_abi")
    @classmethod
    def abi(cls, value: dict[str, str]) -> dict[str, str]:
        return _abi(value)

    @model_validator(mode="after")
    def identity(self) -> WorkerDescriptor:
        identity = "worker_" + hashlib.sha256(self.incarnation.encode()).hexdigest()[:40]
        if self.worker_id != identity or self.task_queue != "maeyr-serverless-agent-" + identity:
            raise ValueError("Worker queue must bind its boot incarnation")
        return self

    @classmethod
    def create(cls, *, incarnation: str, **values: Any) -> WorkerDescriptor:
        identity = "worker_" + hashlib.sha256(incarnation.encode()).hexdigest()[:40]
        return cls(
            worker_id=identity,
            worker_name=worker_name(incarnation),
            incarnation=incarnation,
            task_queue="maeyr-serverless-agent-" + identity,
            **values,
        )


class WorkerLease(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    worker: WorkerDescriptor
    expires_at_ms: int = Field(ge=0)
    draining: bool
    healthy: bool
    active_runs: int = Field(ge=0, le=256)
    live: bool


class _Publication(Scope):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    image_ref: str = Field(pattern=r"^[^\s]+@sha256:[a-f0-9]{64}$", max_length=512)
    runtime_abi: dict[str, str]
    creation_operation_id: str = Field(pattern=r"^ACO-[a-f0-9]{64}$")
    build_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    publication_generation: int = Field(ge=1, le=9_007_199_254_740_991)

    @field_validator("runtime_abi")
    @classmethod
    def abi(cls, value: dict[str, str]) -> dict[str, str]:
        return _abi(value)


class _AgentScope(Scope):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class _Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    worker_id: str = Field(pattern=r"^worker_[a-f0-9]{40}$")
    worker_name: str = Field(min_length=1, max_length=96)
    namespace: str = Field(pattern=r"^serverless$")
    task_queue: str = Field(pattern=r"^maeyr-serverless-agent-worker_[a-f0-9]{40}$")
    generation: int = Field(ge=1, le=9_007_199_254_740_991)
    assigned_at: str
    scope: _AgentScope
    creation_operation_id: str = Field(pattern=r"^ACO-[a-f0-9]{64}$")
    build_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    publication_generation: int = Field(ge=1, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def queue(self) -> _Assignment:
        if self.task_queue != "maeyr-serverless-agent-" + self.worker_id:
            raise ValueError("Assignment queue differs from worker identity")
        stamp = datetime.fromisoformat(self.assigned_at)
        if stamp.tzinfo is None:
            raise ValueError("Assignment timestamp must be timezone aware")
        return self


# Every mutation uses one hash-tagged key layout. Pruning is bounded, independent
# of accounts and workers. Pending receipts and unresolved starts never expire.
_LUA_BASE = """
local clock = redis.call('TIME')
local now = clock[1]*1000 + math.floor(clock[2]/1000)
local maximum_tombstones = tonumber(ARGV[1])
local retention = tonumber(ARGV[2])
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now, 'LIMIT', 0, 256)
for _, id in ipairs(expired) do
  redis.call('HDEL', KEYS[1], id); redis.call('ZREM', KEYS[2], id)
end
local function room(extra)
  return redis.call('HLEN', KEYS[1]) + extra +
    2*(redis.call('HLEN', KEYS[9]) + redis.call('ZCARD', KEYS[11])) <= maximum_tombstones
end
local function remember(id, value)
  redis.call('HSET', KEYS[1], id, value)
  redis.call('ZADD', KEYS[2], now + retention, id)
end
local function retire(id, value)
  remember('worker:'..id, '1')
  redis.call('HDEL', KEYS[3], id)
  redis.call('ZREM', KEYS[4], id); redis.call('ZREM', KEYS[5], id)
  redis.call('ZREM', KEYS[6], id)
end
local function retire_expired(bound)
  local due = redis.call('ZRANGEBYSCORE', KEYS[6], '-inf', now, 'LIMIT', 0, bound)
  local retired = 0
  for _, id in ipairs(due) do
    local raw = redis.call('HGET', KEYS[3], id)
    if not raw then redis.call('ZREM', KEYS[6], id)
    else
      local value = cjson.decode(raw)
      if value.expires_at_ms <= now and value.active_runs == 0 and
         value.observed_runs == 0 and
         tonumber(redis.call('HGET', KEYS[12], id) or '0') == 0 and room(1) then
        retire(id, value); retired = retired + 1
      else
        -- Fair retry keeps unresolved old workers from starving safe cleanup.
        redis.call('ZADD', KEYS[6], math.max(now + 10000, value.expires_at_ms), id)
      end
    end
  end
  return retired
end
"""

_ASSIGN = """
local prior = redis.call('HGET', KEYS[7], ARGV[3])
local previous = prior and cjson.decode(prior) or nil
if redis.call('HEXISTS', KEYS[1], 'assignment:'..ARGV[3]..':'..ARGV[11]) == 1 then
  return {-3}
end
local requested_generation = tonumber(ARGV[13])
if previous then
  if previous.creation_operation_id ~= ARGV[11] then return {-2} end
  if requested_generation < previous.publication_generation or
     (requested_generation == previous.publication_generation and previous.build_id ~= ARGV[12]) then
    return {-3}
  end
end
local abi = cjson.decode(ARGV[6])
local function eligible(id)
  local raw = redis.call('HGET', KEYS[3], id)
  if not raw then return nil end
  local w = cjson.decode(raw)
  if w.expires_at_ms <= now or w.draining or not w.healthy then return nil end
  if w.worker.image_ref ~= ARGV[5] or w.worker.runtime_abi.python ~= abi.python or
     w.worker.runtime_abi.architecture ~= abi.architecture or
     w.worker.runtime_abi.platform ~= abi.platform or w.worker.artifact_store_id ~= ARGV[7] or
     w.worker.cpu_millis < tonumber(ARGV[8]) or w.worker.memory_bytes < tonumber(ARGV[9]) then
    return nil
  end
  return w.worker
end
if previous then
  local current = eligible(previous.worker_id)
  if current then
    if requested_generation > previous.publication_generation then
      if previous.generation >= 9007199254740991 then return {-4} end
      previous.generation = previous.generation + 1
      previous.publication_generation = requested_generation
      previous.build_id = ARGV[12]
      previous.assigned_at = ARGV[10]
      redis.call('HSET', KEYS[7], ARGV[3], cjson.encode(previous))
    end
    return {1, cjson.encode(previous)}
  end
end
local live = redis.call('ZRANGEBYSCORE', KEYS[4], now+1, '+inf', 'LIMIT', 0, ARGV[14])
local best = {}; local minimum = nil
for _, id in ipairs(live) do
  local w = eligible(id)
  if w then
    local count = tonumber(redis.call('ZSCORE', KEYS[5], id) or '0')
    if not minimum or count < minimum then minimum = count; best = {w}
    elseif count == minimum then table.insert(best, w) end
  end
end
if #best == 0 then return {0} end
if previous and previous.generation >= 9007199254740991 then return {-4} end
local sequence = redis.call('INCR', KEYS[8])
local selected = best[(sequence-1)%#best+1]
if previous and redis.call('ZSCORE', KEYS[5], previous.worker_id) then
  local count = tonumber(redis.call('ZSCORE', KEYS[5], previous.worker_id))
  if count > 0 then redis.call('ZINCRBY', KEYS[5], -1, previous.worker_id) end
end
redis.call('ZINCRBY', KEYS[5], 1, selected.worker_id)
local assignment = {worker_id=selected.worker_id, worker_name=selected.worker_name,
 namespace='serverless', task_queue=selected.task_queue, generation=previous and previous.generation+1 or 1,
 assigned_at=ARGV[10], scope=cjson.decode(ARGV[4]), creation_operation_id=ARGV[11],
 build_id=ARGV[12], publication_generation=requested_generation}
local value = cjson.encode(assignment)
redis.call('HSET', KEYS[7], ARGV[3], value)
return {1, value}
"""

_RESERVE = """
local raw = redis.call('HGET', KEYS[3], ARGV[3]); if not raw then return 0 end
local value = cjson.decode(raw)
if value.token_hash ~= ARGV[4] or value.draining or not value.healthy or
   value.expires_at_ms <= now or value.worker.incarnation ~= ARGV[7] then return 0 end
if redis.call('HEXISTS', KEYS[1], 'attempt:'..ARGV[5]) == 1 then return 0 end
local existing = redis.call('HGET', KEYS[9], ARGV[5])
if existing then return existing == ARGV[6] and 1 or 0 end
if value.active_runs >= value.worker.slots or not room(2) or
   redis.call('ZCARD', KEYS[11]) + redis.call('HLEN', KEYS[9]) >= tonumber(ARGV[8]) then
  return 0
end
value.active_runs = value.active_runs + 1
redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(value))
redis.call('HSET', KEYS[9], ARGV[5], ARGV[6])
return 1
"""

_FINISH = """
local completed = redis.call('HGET', KEYS[1], 'attempt:'..ARGV[4])
if completed then return completed == ARGV[7] and 1 or -1 end
local durable = redis.call('HGET', KEYS[10], ARGV[6])
if durable ~= ARGV[8] then return -2 end
local raw = redis.call('HGET', KEYS[3], ARGV[3]); if not raw then return -1 end
local worker = cjson.decode(raw)
if worker.worker.incarnation ~= ARGV[9] then return -1 end
local prior = redis.call('HGET', KEYS[9], ARGV[4])
if prior then
  if prior ~= ARGV[5] or worker.active_runs <= 0 then return -1 end
else
  -- A failed authority/admission check can prove no guest ever launched.
  if ARGV[10] ~= 'not_started' then return -1 end
end
-- Completion removes one physical reservation while adding one stop proof.
if not room(prior and -1 or 1) then return -3 end
if prior then
  worker.active_runs = worker.active_runs - 1
  redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(worker))
  redis.call('HDEL', KEYS[9], ARGV[4])
end
remember('attempt:'..ARGV[4], ARGV[7])
return 1
"""

_RECEIPT = """
local acknowledged = redis.call('HGET', KEYS[1], 'usage:'..ARGV[3])
if acknowledged then return acknowledged == ARGV[5] and 0 or -1 end
local existing = redis.call('HGET', KEYS[10], ARGV[3])
if existing then return existing == ARGV[4] and 0 or -1 end
local completed = redis.call('HGET', KEYS[1], 'attempt:'..ARGV[6])
if completed and completed ~= ARGV[5] then return -1 end
local pending_attempt = redis.call('HGET', KEYS[14], ARGV[6])
if pending_attempt and pending_attempt ~= ARGV[3] then return -1 end
local reserved = redis.call('HGET', KEYS[9], ARGV[6])
if reserved and reserved ~= ARGV[7] then return -1 end
-- An admitted physical attempt has already reserved its eventual outbox entry.
local pending = redis.call('ZCARD', KEYS[11])
local physical = redis.call('HLEN', KEYS[9])
if pending >= tonumber(ARGV[8]) or
   (not reserved and (pending + physical >= tonumber(ARGV[8]) or not room(2))) then
  return -2
end
redis.call('HSET', KEYS[10], ARGV[3], ARGV[4])
redis.call('HSET', KEYS[13], ARGV[3], cjson.encode({logical=ARGV[6], hash=ARGV[5]}))
redis.call('HSET', KEYS[14], ARGV[6], ARGV[3])
redis.call('HINCRBY', KEYS[12], ARGV[9], 1)
redis.call('ZADD', KEYS[11], now, ARGV[3])
return 1
"""


class FleetRegistry:
    def __init__(
        self,
        redis: Redis,
        deployment_id: str,
        *,
        lease_seconds: int = 30,
        max_workers: int = 10000,
        max_pending_receipts: int = 100000,
        max_tombstones: int = 4000000,
        tombstone_seconds: int = 960,
    ) -> None:
        if not isinstance(deployment_id, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]{0,95}", deployment_id
        ):
            raise ValueError("Invalid fleet authority")
        for value, lower, upper in (
            (lease_seconds, 6, 120),
            (max_workers, 1, 10000),
            (max_pending_receipts, 1, 1000000),
            (max_tombstones, 4, 20000000),
            (tombstone_seconds, 960, 3600),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Invalid fleet bound")
        if max_tombstones < 2 * max_pending_receipts + max_workers:
            raise ValueError(
                "Completion capacity must cover admitted attempts and worker retirement"
            )
        self.redis = redis
        self.command = cast(Callable[..., Awaitable[Any]], redis.execute_command)
        self.prefix = "{maeyr-serverless:" + deployment_id + "}:executing-fleet-v2"
        self.lease_seconds = lease_seconds
        self.max_workers = max_workers
        self.max_pending_receipts = max_pending_receipts
        self.max_tombstones = max_tombstones
        self.tombstone_seconds = tombstone_seconds
        self._preflight_until = 0.0

    async def _eval(self, script: str, *arguments: Any) -> Any:
        await self.preflight()
        return await self.command(
            "EVAL",
            _LUA_BASE + script,
            len(_KEY_SUFFIXES),
            *(self.prefix + ":" + suffix for suffix in _KEY_SUFFIXES),
            self.max_tombstones,
            self.tombstone_seconds * 1000,
            *arguments,
        )

    async def preflight(self) -> None:
        if self._preflight_until > time.monotonic():
            return
        persistence = await self.redis.info("persistence")
        memory = await self.redis.info("memory")
        config = await self.redis.config_get("appendfsync")
        fsync = config.get("appendfsync", config.get(b"appendfsync"))
        if isinstance(fsync, bytes):
            fsync = fsync.decode()
        if (
            persistence.get("aof_enabled") != 1
            or persistence.get("aof_last_write_status") != "ok"
            or memory.get("maxmemory_policy") != "noeviction"
            or fsync != "always"
        ):
            raise ServiceError(
                "durable_authority_unavailable", "Worker ownership requires durable Redis"
            )
        self._preflight_until = time.monotonic() + 5

    @staticmethod
    def _owner(worker_id: str, token: str) -> str:
        if not isinstance(worker_id, str) or not _WORKER.fullmatch(worker_id):
            raise ValueError("Invalid worker identity")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid worker ownership token")
        return hashlib.sha256(token.encode()).hexdigest()

    async def register(self, worker: WorkerDescriptor) -> str:
        worker = WorkerDescriptor.model_validate(worker)
        await self.preflight()
        token = secrets.token_hex(32)
        value = encode(
            {
                "worker": worker.model_dump(),
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "draining": False,
                "healthy": True,
                "active_runs": 0,
                "observed_runs": 0,
            }
        )
        result = await self._eval(
            """
retire_expired(128)
if redis.call('HEXISTS', KEYS[3], ARGV[3]) == 1 or
   redis.call('HEXISTS', KEYS[1], 'worker:'..ARGV[3]) == 1 then return 0 end
if redis.call('HLEN', KEYS[3]) >= tonumber(ARGV[6]) or not room(1) then return -1 end
local value = cjson.decode(ARGV[4]); value.expires_at_ms = now + tonumber(ARGV[5])
redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(value))
redis.call('ZADD', KEYS[4], value.expires_at_ms, ARGV[3])
redis.call('ZADD', KEYS[5], 0, ARGV[3]); redis.call('ZADD', KEYS[6], value.expires_at_ms, ARGV[3])
return 1
""",
            worker.worker_id,
            value,
            self.lease_seconds * 1000,
            self.max_workers,
        )
        if result != 1:
            code = "worker_registration_conflict" if result == 0 else "worker_registry_capacity"
            raise ServiceError(
                code, "Worker incarnation already exists or fleet capacity is unavailable", 409
            )
        return token

    async def heartbeat(
        self,
        worker_id: str,
        token: str,
        *,
        active_runs: int = 0,
        healthy: bool = True,
        draining: bool = False,
    ) -> bool:
        token_hash = self._owner(worker_id, token)
        if type(active_runs) is not int or not 0 <= active_runs <= 256:
            raise ValueError("Invalid observed occupancy")
        if type(healthy) is not bool or type(draining) is not bool:
            raise ValueError("Invalid worker health")
        return bool(
            await self._eval(
                """
local raw = redis.call('HGET', KEYS[3], ARGV[3]); if not raw then return 0 end
local value = cjson.decode(raw)
if value.token_hash ~= ARGV[4] or value.expires_at_ms <= now or
   tonumber(ARGV[5]) > value.worker.slots then return 0 end
value.observed_runs = tonumber(ARGV[5]); value.healthy = ARGV[6] == '1'
value.draining = value.draining or ARGV[7] == '1'; value.expires_at_ms = now + tonumber(ARGV[8])
redis.call('HSET', KEYS[3], ARGV[3], cjson.encode(value))
redis.call('ZADD', KEYS[6], value.expires_at_ms, ARGV[3])
if value.draining or not value.healthy then redis.call('ZREM', KEYS[4], ARGV[3])
else redis.call('ZADD', KEYS[4], value.expires_at_ms, ARGV[3]) end
return 1
""",
                worker_id,
                token_hash,
                active_runs,
                "1" if healthy else "0",
                "1" if draining else "0",
                self.lease_seconds * 1000,
            )
        )

    async def get(self, worker_id: str) -> WorkerLease | None:
        if not isinstance(worker_id, str) or not _WORKER.fullmatch(worker_id):
            raise ValueError("Invalid worker identity")
        raw = await self.command(
            "EVAL",
            """
local raw = redis.call('HGET', KEYS[1], ARGV[1]); if not raw then return {} end
local clock = redis.call('TIME')
return {raw, clock[1]*1000+math.floor(clock[2]/1000)}
""",
            1,
            self.prefix + ":workers",
            worker_id,
        )
        if not raw:
            return None
        value = json.loads(raw[0])
        worker = WorkerDescriptor.model_validate(value["worker"])
        if worker.worker_id != worker_id:
            raise ValueError("Worker identity mismatch")
        return WorkerLease(
            worker=worker,
            expires_at_ms=value["expires_at_ms"],
            draining=value["draining"],
            healthy=value["healthy"],
            active_runs=value["active_runs"],
            live=value["expires_at_ms"] > raw[1],
        )

    async def owns(self, worker_id: str, token: str, *, admit: bool = True) -> bool:
        token_hash = self._owner(worker_id, token)
        if type(admit) is not bool:
            raise ValueError("Invalid admission mode")
        return bool(
            await self._eval(
                """
local raw = redis.call('HGET', KEYS[3], ARGV[3]); if not raw then return 0 end
local value = cjson.decode(raw)
if value.token_hash ~= ARGV[4] then return 0 end
if ARGV[5] == '1' and (value.expires_at_ms <= now or value.draining or not value.healthy) then
  return 0
end
return 1
""",
                worker_id,
                token_hash,
                "1" if admit else "0",
            )
        )

    @staticmethod
    def _attempt(
        scope: dict[str, Any],
        execution_id: str,
        attempt: int,
        receipt: StartReceipt,
    ) -> tuple[str, bytes]:
        selected = Scope.model_validate({k: scope[k] for k in _SCOPE_FIELDS})
        command = StartCommand(
            **selected.model_dump(), execution_id=execution_id, attempt=attempt, receipt=receipt
        )
        logical = hashlib.sha256(
            encode(
                [
                    selected.account_id,
                    selected.org_id,
                    selected.project_id,
                    execution_id,
                    attempt,
                ]
            )
        ).hexdigest()
        return logical, encode(command.model_dump())

    async def reserve_attempt(
        self,
        worker_id: str,
        token: str,
        receipt: dict[str, Any],
        *,
        scope: dict[str, Any],
        execution_id: str,
        attempt: int,
    ) -> bool:
        token_hash = self._owner(worker_id, token)
        checked = StartReceipt.model_validate(receipt)
        if checked.broker_id != worker_id or not re.fullmatch(
            r"maeyr-[a-f0-9]{48}", checked.sandbox_id
        ):
            raise ValueError("Physical attempt identity differs from its worker")
        logical, binding = self._attempt(scope, execution_id, attempt, checked)
        return bool(
            await self._eval(
                _RESERVE,
                worker_id,
                token_hash,
                logical,
                binding,
                checked.boot_id,
                self.max_pending_receipts,
            )
        )

    @staticmethod
    def _usage(receipt: dict[str, Any]) -> tuple[UsageReceipt, bytes, str, bytes]:
        checked = UsageReceipt.model_validate(receipt)
        if (
            not _WORKER.fullmatch(checked.broker_id)
            or str(UUID(checked.boot_id)) != checked.boot_id
            or checked.broker_id
            != "worker_" + hashlib.sha256(checked.boot_id.encode()).hexdigest()[:40]
        ):
            raise ValueError("Usage requires a bound executing worker incarnation")
        logical, binding = FleetRegistry._attempt(
            checked.model_dump(),
            checked.execution_id,
            checked.attempt,
            StartReceipt(
                **{k: receipt[k] for k in ("broker_id", "boot_id", "sandbox_id", "started_at")}
            ),
        )
        return checked, encode(checked.model_dump(by_alias=True)), logical, binding

    async def finish_attempt(self, worker_id: str, receipt: dict[str, Any]) -> bool:
        checked, raw, logical, binding = self._usage(receipt)
        if checked.broker_id != worker_id or not checked.termination_confirmed:
            raise ServiceError("execution_fence_conflict", "Sandbox termination is not proven", 409)
        result = await self._eval(
            _FINISH,
            worker_id,
            logical,
            binding,
            checked.receipt_id,
            hashlib.sha256(raw).hexdigest(),
            raw,
            checked.boot_id,
            checked.outcome,
        )
        if result < 0:
            raise ServiceError(
                "execution_fence_conflict", "Physical termination receipt is not durably bound", 409
            )
        return bool(result)

    async def retire_expired(self, *, limit: int = 128) -> int:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("Invalid retirement page bound")
        return int(await self._eval("return retire_expired(tonumber(ARGV[3]))", limit))

    async def retire(self, worker_id: str, token: str, *, journals_drained: bool) -> bool:
        token_hash = self._owner(worker_id, token)
        if type(journals_drained) is not bool:
            raise ValueError("Invalid journal proof")
        return bool(
            await self._eval(
                """
local raw = redis.call('HGET', KEYS[3], ARGV[3]); if not raw then return 0 end
local value = cjson.decode(raw)
if value.token_hash ~= ARGV[4] or not value.draining or value.active_runs ~= 0 or
   value.observed_runs ~= 0 or ARGV[5] ~= '1' or
   tonumber(redis.call('HGET', KEYS[12], ARGV[3]) or '0') ~= 0 or not room(1) then return 0 end
retire(ARGV[3], value); return 1
""",
                worker_id,
                token_hash,
                "1" if journals_drained else "0",
            )
        )

    async def list_all(self, *, limit: int | None = None) -> builtins.list[WorkerDescriptor]:
        if limit is None:
            limit = self.max_workers
        if type(limit) is not int or not 1 <= limit <= self.max_workers:
            raise ValueError("Invalid worker page bound")
        rows = await self._eval(
            """
retire_expired(128)
local ids = redis.call('ZRANGEBYSCORE', KEYS[4], now+1, '+inf', 'LIMIT', 0, ARGV[3])
local result = {}
for _, id in ipairs(ids) do
  local raw = redis.call('HGET', KEYS[3], id)
  if raw then
    local value = cjson.decode(raw)
    if value.expires_at_ms > now and not value.draining and value.healthy then
      table.insert(result, cjson.encode(value.worker))
    end
  end
end
return result
""",
            limit,
        )
        return [WorkerDescriptor.model_validate(json.loads(row)) for row in rows]

    async def stats(self) -> dict[str, int]:
        rows = await self._eval(
            """
retire_expired(128)
local ids = redis.call('ZRANGEBYSCORE', KEYS[4], now+1, '+inf', 'LIMIT', 0, ARGV[3])
local workers = 0; local slots = 0; local active = 0
for _, id in ipairs(ids) do
  local raw = redis.call('HGET', KEYS[3], id)
  if raw then
    local value = cjson.decode(raw)
    if value.expires_at_ms > now and not value.draining and value.healthy then
      workers = workers + 1; slots = slots + value.worker.slots; active = active + value.active_runs
    end
  end
end
return {workers, slots, active, redis.call('HLEN', KEYS[9]), redis.call('ZCARD', KEYS[11])}
""",
            self.max_workers,
        )
        return {
            "ready_workers": rows[0],
            "total_slots": rows[1],
            "active_slots": rows[2],
            "available_slots": rows[1] - rows[2],
            "physical_attempts": rows[3],
            "pending_receipts": rows[4],
        }

    @staticmethod
    def _scope(scope: dict[str, Any]) -> _AgentScope:
        return _AgentScope.model_validate({k: scope[k] for k in _AGENT_FIELDS})

    @staticmethod
    def agent_key(scope: dict[str, Any]) -> str:
        checked = FleetRegistry._scope(scope)
        return hashlib.sha256(encode([getattr(checked, k) for k in _AGENT_FIELDS])).hexdigest()

    @staticmethod
    def _assignment(raw: Any, selected: _AgentScope) -> dict[str, Any]:
        checked = _Assignment.model_validate(json.loads(raw))
        if checked.scope != selected:
            raise ValueError("Assignment scope mismatch")
        return checked.model_dump(
            exclude={"scope", "creation_operation_id", "build_id", "publication_generation"}
        )

    async def assign(
        self,
        scope: dict[str, Any],
        publication: dict[str, Any],
        *,
        cpu_millis: int = 1000,
        memory_bytes: int = 268435456,
        artifact_store_id: str,
    ) -> dict[str, Any]:
        selected = self._scope(scope)
        checked = _Publication.model_validate(
            {k: publication[k] for k in _Publication.model_fields}
        )
        if any(getattr(checked, key) != getattr(selected, key) for key in _AGENT_FIELDS):
            raise ServiceError(
                "assignment_fence_conflict", "Publication belongs to a different tenant agent", 409
            )
        if type(cpu_millis) is not int or not 100 <= cpu_millis <= 8000:
            raise ValueError("Invalid invocation CPU profile")
        if type(memory_bytes) is not int or not 67108864 <= memory_bytes <= 8589934592:
            raise ValueError("Invalid invocation memory profile")
        if not isinstance(artifact_store_id, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]{0,95}", artifact_store_id
        ):
            raise ValueError("Invalid artifact store authority")
        result = await self._eval(
            _ASSIGN,
            self.agent_key(scope),
            encode(selected.model_dump()),
            checked.image_ref,
            encode(checked.runtime_abi),
            artifact_store_id,
            cpu_millis,
            memory_bytes,
            datetime.now(UTC).isoformat(),
            checked.creation_operation_id,
            checked.build_id,
            checked.publication_generation,
            self.max_workers,
        )
        if result[0] <= 0:
            if result[0] == 0:
                raise ServiceError(
                    "worker_unavailable", "No compatible shared executing worker is ready", 503
                )
            raise ServiceError(
                "assignment_fence_conflict", "Agent publication or creation ownership changed", 409
            )
        return self._assignment(result[1], selected)

    async def assignment(self, scope: dict[str, Any]) -> dict[str, Any] | None:
        selected = self._scope(scope)
        raw = await self.command("HGET", self.prefix + ":assignments", self.agent_key(scope))
        if raw is None:
            return None
        value = self._assignment(raw, selected)
        lease = await self.get(value["worker_id"])
        ready = lease is not None and lease.live and lease.healthy and not lease.draining
        return {**value, "state": "assigned" if ready else "unavailable"}

    async def release_assignment(
        self,
        scope: dict[str, Any],
        *,
        creation_operation_id: str,
        build_id: str | None,
    ) -> bool:
        if not isinstance(creation_operation_id, str) or not _CREATION.fullmatch(
            creation_operation_id
        ):
            raise ValueError("Invalid creation fence")
        if build_id is not None and (
            not isinstance(build_id, str) or not _IDENTIFIER.fullmatch(build_id)
        ):
            raise ValueError("Invalid build fence")
        result = await self._eval(
            """
local raw = redis.call('HGET', KEYS[7], ARGV[3]); if not raw then return 0 end
local value = cjson.decode(raw)
if value.creation_operation_id ~= ARGV[4] or value.build_id ~= ARGV[5] then return 0 end
if not room(1) then return -1 end
local count = tonumber(redis.call('ZSCORE', KEYS[5], value.worker_id) or '0')
if count > 0 then redis.call('ZINCRBY', KEYS[5], -1, value.worker_id) end
remember('assignment:'..ARGV[3]..':'..ARGV[4], '1')
return redis.call('HDEL', KEYS[7], ARGV[3])
""",
            self.agent_key(scope),
            creation_operation_id,
            build_id or "",
        )
        if result < 0:
            raise ServiceError(
                "worker_registry_capacity", "Placement deletion proof capacity is unavailable"
            )
        return bool(result)

    async def persist_usage(self, receipt: dict[str, Any]) -> None:
        """Persist trusted billing bytes before terminal acknowledgement.

        Pending receipts never expire. ACK follows an independent idempotent
        billing commit. Queue capacity is reserved before physical admission.
        """
        checked, raw, logical, binding = self._usage(receipt)
        result = await self._eval(
            _RECEIPT,
            checked.receipt_id,
            raw,
            hashlib.sha256(raw).hexdigest(),
            logical,
            binding,
            self.max_pending_receipts,
            checked.broker_id,
        )
        if result < 0:
            code = "usage_receipt_conflict" if result == -1 else "usage_receipt_capacity"
            raise ServiceError(code, "Runtime usage could not be durably recorded")

    async def pending_usage(
        self,
        *,
        limit: int = 100,
        claim: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid receipt page bound")
        if type(claim) is not bool:
            raise ValueError("Invalid receipt claim mode")
        values = await self._eval(
            """
local ids
if ARGV[4] == '1' then
  ids = redis.call('ZRANGEBYSCORE', KEYS[11], '-inf', now, 'LIMIT', 0, ARGV[3])
  for _, id in ipairs(ids) do redis.call('ZADD', KEYS[11], 'XX', now + 10000, id) end
else ids = redis.call('ZRANGE', KEYS[11], 0, tonumber(ARGV[3])-1) end
local result = {}
for _, id in ipairs(ids) do
  local raw = redis.call('HGET', KEYS[10], id)
  if raw then table.insert(result, raw) end
end
return result
""",
            limit,
            "1" if claim else "0",
        )
        return [
            UsageReceipt.model_validate(json.loads(value)).model_dump(by_alias=True)
            for value in values
        ]

    async def acknowledge_usage(self, receipt_id: str) -> bool:
        if not isinstance(receipt_id, str) or not _TOKEN.fullmatch(receipt_id):
            raise ValueError("Invalid usage receipt identity")
        result = await self._eval(
            """
local raw = redis.call('HGET', KEYS[10], ARGV[3]); if not raw then return 0 end
local receipt = cjson.decode(raw)
local metadata = redis.call('HGET', KEYS[13], ARGV[3])
if not metadata then return -1 end
metadata = cjson.decode(metadata)
local completed = redis.call('HGET', KEYS[1], 'attempt:'..metadata.logical)
if not receipt.termination_confirmed or
   redis.call('HEXISTS', KEYS[9], metadata.logical) == 1 or
   (completed and completed ~= metadata.hash) or
   not room(completed and -1 or 0) then return -1 end
-- Atomic completion already moved any physical reservation to a tombstone.
-- The independent control/billing ledger remains the permanent retry fence.
local broker = receipt.broker_id
local remaining = tonumber(redis.call('HGET', KEYS[12], broker) or '0')
if remaining <= 0 then return -1 end
if remaining == 1 then redis.call('HDEL', KEYS[12], broker)
else redis.call('HINCRBY', KEYS[12], broker, -1) end
if not completed then remember('attempt:'..metadata.logical, metadata.hash) end
remember('usage:'..ARGV[3], metadata.hash)
redis.call('ZREM', KEYS[11], ARGV[3]); redis.call('HDEL', KEYS[10], ARGV[3])
redis.call('HDEL', KEYS[13], ARGV[3]); redis.call('HDEL', KEYS[14], metadata.logical)
return 1
""",
            receipt_id,
        )
        if result < 0:
            raise ServiceError(
                "usage_receipt_conflict",
                "Usage acknowledgement lacks termination or durable capacity",
            )
        return bool(result)
