"""Canonical resource references for token and cost attribution."""

from __future__ import annotations

from typing import Any

RESOURCE_REF_KEYS = (
    "agent_id",
    "agent_ids",
    "trigger_id",
    "schedule_id",
    "conversation_id",
    "execution_id",
    "workforce_id",
    "endpoint_id",
    "message_id",
    "user_id",
    "user_email",
    "channel_type",
    "channel_id",
    "thread_id",
    "slack_user_id",
    "slack_user_email",
    "slack_channel_id",
    "slack_thread_ts",
)


def build_resource_refs(
    *,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
    trigger_id: str | None = None,
    schedule_id: str | None = None,
    conversation_id: str | None = None,
    execution_id: str | None = None,
    workforce_id: str | None = None,
    endpoint_id: str | None = None,
    message_id: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    channel_type: str | None = None,
    channel_id: str | None = None,
    thread_id: str | None = None,
    slack_user_id: str | None = None,
    slack_user_email: str | None = None,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized references containing only non-empty values."""
    refs: dict[str, Any] = {}
    scalar_values = {
        "agent_id": agent_id,
        "trigger_id": trigger_id,
        "schedule_id": schedule_id,
        "conversation_id": conversation_id,
        "execution_id": execution_id,
        "workforce_id": workforce_id,
        "endpoint_id": endpoint_id,
        "message_id": message_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "slack_user_id": slack_user_id,
        "slack_channel_id": slack_channel_id,
        "slack_thread_ts": slack_thread_ts,
    }
    for key, value in scalar_values.items():
        if value:
            refs[key] = str(value)
    if agent_ids:
        cleaned = [str(value) for value in agent_ids if value]
        if cleaned:
            refs["agent_ids"] = cleaned
            if not refs.get("agent_id") and len(cleaned) == 1:
                refs["agent_id"] = cleaned[0]
    if user_email:
        refs["user_email"] = str(user_email).strip()
    if channel_type:
        refs["channel_type"] = str(channel_type).strip().lower()
    if slack_user_email:
        refs["slack_user_email"] = str(slack_user_email).strip()
    for key, value in extra.items():
        if key in RESOURCE_REF_KEYS and value is not None and value != "":
            refs[key] = value
    return refs


def build_channel_turn_resource_refs(
    *,
    channel_type: str,
    user_id: str | None = None,
    user_email: str | None = None,
    channel_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Build normalized Tier-1 channel-turn references."""
    normalized_type = (channel_type or "slack").strip().lower()
    refs = build_resource_refs(
        user_id=user_id,
        user_email=user_email,
        channel_type=normalized_type,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if normalized_type == "slack":
        refs.update(
            build_resource_refs(
                slack_user_id=user_id,
                slack_user_email=user_email,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_id,
            )
        )
    return refs


def merge_resource_refs(*parts: dict[str, Any] | None) -> dict[str, Any]:
    """Merge reference mappings, with later non-empty values winning."""
    output: dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        for key, value in part.items():
            if value is None or value == "" or value == []:
                continue
            if key == "agent_ids" and key in output:
                existing = set(output.get("agent_ids") or [])
                existing.update(value if isinstance(value, list) else [value])
                output["agent_ids"] = sorted(existing)
            else:
                output[key] = value
    agent_ids = output.get("agent_ids")
    if agent_ids and not output.get("agent_id") and len(agent_ids) == 1:
        output["agent_id"] = agent_ids[0]
    return output


def resource_ref_match(ref_key: str, ref_id: str) -> dict[str, Any]:
    """Build the MongoDB match for one resource reference."""
    if ref_key in {"agent_ids", "agent_id"}:
        return {"resource_refs.agent_ids": ref_id}
    return {f"resource_refs.{ref_key}": ref_id}


__all__ = [
    "RESOURCE_REF_KEYS",
    "build_channel_turn_resource_refs",
    "build_resource_refs",
    "merge_resource_refs",
    "resource_ref_match",
]
