from __future__ import annotations

import inspect
import re
from collections import defaultdict

import pytest
from pydantic import ValidationError

from maeyr_platform.metrics.billing import UsageDeliveryRejected
from maeyr_platform.metrics.constants import (
    REDIS_PROCESSING_QUEUE_KEY,
    REDIS_QUEUE_KEY,
    EntityType,
    entity_type_from_resource_type,
)
from maeyr_platform.metrics.context import (
    UsageContext,
    get_usage_context,
    start_activity,
    token_metadata_patch,
)
from maeyr_platform.metrics.recorder import (
    configure_delivery_policy,
    configure_recorder,
    record_usage,
)
from maeyr_platform.metrics.resource_refs import (
    build_channel_turn_resource_refs,
    build_resource_refs,
    merge_resource_refs,
)
from maeyr_platform.metrics.schema import TokenUsageBatchRequest, TokenUsageEvent
from maeyr_platform.metrics.transport import (
    acknowledge_events,
    configure_transport,
    drain_queue,
    enqueue_event,
    queue_length,
)


class FakeRedis:
    def __init__(self) -> None:
        self.lists: defaultdict[str, list[str]] = defaultdict(list)

    async def eval(
        self,
        _script: str,
        _key_count: int,
        key: str,
        payload: str,
        maximum: int,
    ) -> int:
        if len(self.lists[key]) >= int(maximum):
            return 0
        self.lists[key].insert(0, payload)
        return 1

    async def rpoplpush(self, source: str, destination: str) -> str | None:
        if not self.lists[source]:
            return None
        value = self.lists[source].pop()
        self.lists[destination].insert(0, value)
        return value

    async def lrem(self, key: str, _count: int, value: str) -> int:
        try:
            self.lists[key].remove(value)
        except ValueError:
            return 0
        return 1

    async def llen(self, key: str) -> int:
        return len(self.lists[key])


def test_legacy_metrics_call_shapes_remain_keyword_compatible() -> None:
    assert list(inspect.signature(start_activity).parameters) == [
        "account_id",
        "org_id",
        "project_id",
        "entity_type",
        "entity_id",
        "user_id",
        "user_email",
        "trace_id",
        "resource_type",
        "metadata",
        "resource_refs",
        "service",
        "activity_id",
    ]
    assert list(inspect.signature(record_usage).parameters) == [
        "tokens_used",
        "prompt_tokens",
        "completion_tokens",
        "estimated",
        "model",
        "operation",
        "sub_resource_id",
        "metadata",
        "override_kwargs",
    ]
    assert list(inspect.signature(configure_recorder).parameters) == ["flush_handler"]
    assert "channel_type" in inspect.signature(build_resource_refs).parameters
    assert "extra" in inspect.signature(build_resource_refs).parameters


def test_context_and_additive_resource_references_preserve_fleet_contract() -> None:
    activity_id, token = start_activity(
        "account",
        "org",
        "project",
        "slack_chat",
        "thread",
        user_id="user",
    )
    try:
        assert re.fullmatch(r"UA-[0-9a-f]{32}", activity_id)
        assert get_usage_context() is not None
        with token_metadata_patch(agent_ids=["agent"]):
            current = get_usage_context()
            assert current is not None
            assert current.resource_refs == {
                "user_id": "user",
                "slack_thread_ts": "thread",
                "agent_ids": ["agent"],
                "agent_id": "agent",
            }
    finally:
        from maeyr_platform.metrics.context import clear_usage_context

        clear_usage_context(token)

    refs = build_channel_turn_resource_refs(
        channel_type=" Slack ",
        user_id="user",
        channel_id="channel",
        thread_id="thread",
    )
    assert refs["channel_type"] == "slack"
    assert refs["slack_channel_id"] == "channel"
    assert refs["slack_thread_ts"] == "thread"
    assert merge_resource_refs(
        build_resource_refs(agent_ids=["a"]),
        build_resource_refs(agent_ids=["b"]),
    )["agent_ids"] == ["a", "b"]
    assert entity_type_from_resource_type("agent_fix") == EntityType.AGENT_GENERATE.value


def test_schema_preserves_id_alias_and_bounds_internal_batches() -> None:
    event = TokenUsageEvent.model_validate(
        {
            "_id": "TU-1",
            "account_id": "a",
            "org_id": "o",
            "project_id": "p",
            "credential_source": "customer",
            "llm_source_scope": "project",
            "billable_to_customer": False,
            "cost_nanos_usd": 0,
            "estimated_cost_nanos_usd": 12_345,
            "estimated_cost_usd": 0.000012345,
            "provider_equivalent_cost_status": "priced",
        }
    )
    assert event.event_id == "TU-1"
    serialized = event.model_dump(by_alias=True)
    assert serialized["_id"] == "TU-1"
    assert serialized["billable_to_customer"] is False
    assert serialized["cost_nanos_usd"] == 0
    assert serialized["estimated_cost_nanos_usd"] == 12_345
    assert serialized["provider_equivalent_cost_status"] == "priced"
    with pytest.raises(ValidationError):
        TokenUsageBatchRequest(events=[event] * 501)


@pytest.mark.asyncio
async def test_transport_reserves_then_acknowledges_without_delete_before_commit() -> None:
    redis = FakeRedis()
    configure_transport(redis)
    try:
        assert await enqueue_event({"_id": "TU-1", "created_at": None})
        assert len(redis.lists[REDIS_QUEUE_KEY]) == 1
        batch = await drain_queue(1)
        assert batch == [{"_id": "TU-1", "created_at": None}]
        assert len(redis.lists[REDIS_QUEUE_KEY]) == 0
        assert len(redis.lists[REDIS_PROCESSING_QUEUE_KEY]) == 1
        assert await queue_length() == 1
        assert await acknowledge_events(batch) == 1
        assert await queue_length() == 0
    finally:
        configure_transport(None)


@pytest.mark.asyncio
async def test_transport_uses_durable_fallback_when_redis_is_unavailable() -> None:
    accepted: list[dict[str, object]] = []

    async def persist(doc: dict[str, object]) -> bool:
        accepted.append(doc)
        return True

    configure_transport(None, durable_fallback=persist)
    try:
        assert await enqueue_event({"_id": "TU-fallback"}) is True
        assert accepted == [{"_id": "TU-fallback"}]
    finally:
        configure_transport(None)


@pytest.mark.asyncio
async def test_strict_delivery_rejects_process_local_memory_as_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(_doc: dict[str, object]) -> bool:
        return False

    monkeypatch.setattr("maeyr_platform.metrics.recorder.enqueue_event", reject)
    configure_delivery_policy(allow_memory_fallback=False)
    try:
        with pytest.raises(UsageDeliveryRejected, match="not durably accepted"):
            await record_usage(tokens_used=1, account_id="account")
    finally:
        configure_delivery_policy(allow_memory_fallback=True)


def test_usage_context_keyword_contract() -> None:
    context = UsageContext(account_id="a", org_id="o", project_id="p")
    assert context.with_operation(operation="generate").operation == "generate"
