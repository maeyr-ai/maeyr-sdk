from __future__ import annotations

import inspect
import re
from collections import defaultdict

import pytest
from pydantic import ValidationError
from viksa_platform.metrics.constants import (
    REDIS_PROCESSING_QUEUE_KEY,
    REDIS_QUEUE_KEY,
    EntityType,
    entity_type_from_resource_type,
)
from viksa_platform.metrics.context import (
    UsageContext,
    get_usage_context,
    start_activity,
    token_metadata_patch,
)
from viksa_platform.metrics.recorder import configure_recorder, record_usage
from viksa_platform.metrics.resource_refs import (
    build_channel_turn_resource_refs,
    build_resource_refs,
    merge_resource_refs,
)
from viksa_platform.metrics.schema import TokenUsageBatchRequest, TokenUsageEvent
from viksa_platform.metrics.transport import (
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
        from viksa_platform.metrics.context import clear_usage_context

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
        {"_id": "TU-1", "account_id": "a", "org_id": "o", "project_id": "p"}
    )
    assert event.event_id == "TU-1"
    assert event.model_dump(by_alias=True)["_id"] == "TU-1"
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


def test_usage_context_keyword_contract() -> None:
    context = UsageContext(account_id="a", org_id="o", project_id="p")
    assert context.with_operation(operation="generate").operation == "generate"
