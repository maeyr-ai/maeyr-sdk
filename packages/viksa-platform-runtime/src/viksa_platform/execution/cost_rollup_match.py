"""Canonical pure helpers for token-usage cost rollup matching."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_cost_rollup_match_filter(
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    agent_id: Optional[str] = None,
    workforce_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    ref_key: Optional[str] = None,
    ref_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the match dict passed to aggregate_summary for cost_rollup."""
    match: Dict[str, Any] = {}
    if org_id:
        match["org_id"] = org_id
    if project_id:
        match["project_id"] = project_id
    if user_id:
        match["user_id"] = user_id
    if user_email and str(user_email).strip():
        match["$or"] = match.get("$or", []) + [
            {"user_email": str(user_email).strip()},
            {"resource_refs.user_email": str(user_email).strip()},
            {"resource_refs.slack_user_email": str(user_email).strip()},
        ]
    aid = str(agent_id).strip() if agent_id else ""
    if aid:
        match["$or"] = match.get("$or", []) + [
            {"metadata.agent_ids": aid},
            {"resource_refs.agent_ids": aid},
            {"resource_refs.agent_id": aid},
        ]
    if workforce_id and str(workforce_id).strip():
        match["$or"] = match.get("$or", []) + [
            {"metadata.workforce_id": str(workforce_id).strip()},
            {"resource_refs.workforce_id": str(workforce_id).strip()},
        ]
    if trigger_id and str(trigger_id).strip():
        match["$or"] = match.get("$or", []) + [
            {"resource_refs.trigger_id": str(trigger_id).strip()},
            {"resource_id": str(trigger_id).strip(), "resource_type": "trigger_execution"},
        ]
    if schedule_id and str(schedule_id).strip():
        match["$or"] = match.get("$or", []) + [
            {"resource_refs.schedule_id": str(schedule_id).strip()},
            {"resource_id": str(schedule_id).strip(), "resource_type": "schedule_execution"},
        ]
    if conversation_id and str(conversation_id).strip():
        match["$or"] = match.get("$or", []) + [
            {"resource_refs.conversation_id": str(conversation_id).strip()},
            {"resource_id": str(conversation_id).strip(), "resource_type": "chat"},
        ]
    if execution_id and str(execution_id).strip():
        match["$or"] = match.get("$or", []) + [
            {"resource_refs.execution_id": str(execution_id).strip()},
            {"resource_id": str(execution_id).strip()},
        ]
    if ref_key and ref_id and str(ref_id).strip():
        rk = str(ref_key).strip()
        rid = str(ref_id).strip()
        if rk == "agent_id" or rk == "agent_ids":
            match["$or"] = match.get("$or", []) + [
                {"resource_refs.agent_ids": rid},
                {"resource_refs.agent_id": rid},
            ]
        else:
            match[f"resource_refs.{rk}"] = rid
    # Flatten single $or into direct match when only one clause
    if match.get("$or") and len(match["$or"]) == 1:
        clause = match.pop("$or")[0]
        match.update(clause)
    return match
