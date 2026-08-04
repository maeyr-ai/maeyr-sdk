"""Canonical orchestration budgets and context compaction.

The estimator is deliberately dependency-free and conservative.  It is not a
billing tokenizer: provider-reported usage remains the source of truth for the
cumulative run budget.  Estimates are used before a request for context-window
admission, tool-result limits, and host-side rate limiting.
"""

from __future__ import annotations

import copy
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_BYTES_PER_ESTIMATED_TOKEN = 3
_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_OVERHEAD_TOKENS = 8
_MIN_COMPACTED_TOOL_TOKENS = 64
_TRUNCATION_MARKER = "\n... (truncated by orchestration budget)"


class BudgetReason:
    """Stable terminal reason values emitted by the shared harness."""

    CONTEXT_WINDOW = "context_window"
    CUMULATIVE_TOKENS = "cumulative_tokens"
    WALL_TIME = "wall_time"
    INVALID_TOOL_PAIRING = "invalid_tool_pairing"


def _serialized(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(value)


def estimate_text_tokens(value: Any) -> int:
    """Conservatively estimate tokens from UTF-8 bytes."""

    text = _serialized(value)
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / _BYTES_PER_ESTIMATED_TOKEN))


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate a provider message array, including tool-call metadata."""

    if not messages:
        return 0
    return 3 + sum(
        estimate_text_tokens(dict(message)) + _MESSAGE_OVERHEAD_TOKENS
        for message in messages
    )


def estimate_tools_tokens(tools: Sequence[Mapping[str, Any]]) -> int:
    """Estimate native tool schemas sent with a completion request."""

    return sum(
        estimate_text_tokens(dict(tool)) + _TOOL_OVERHEAD_TOKENS
        for tool in (tools or [])
    )


def estimate_request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
) -> int:
    """Estimate prompt tokens for messages plus native tool schemas."""

    return estimate_messages_tokens(messages) + estimate_tools_tokens(tools or [])


def _truncate_utf8(text: str, max_bytes: int, marker: str = _TRUNCATION_MARKER) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _compact_json_value(value: Any, max_bytes: int, depth: int = 0) -> Any:
    """Return a valid, structure-preserving JSON value within ``max_bytes``.

    Lists retain complete leading items and dictionaries retain complete keys.
    A single oversized nested value is recursively compacted rather than
    slicing serialized JSON in the middle of a record.
    """

    if max_bytes <= 8:
        return None
    try:
        if _json_bytes(value) <= max_bytes:
            return value
    except (TypeError, ValueError):
        value = str(value)

    if depth >= 8:
        return _truncate_utf8(str(value), max_bytes)

    if isinstance(value, str):
        # JSON quotes and escaping need a small safety margin.
        return _truncate_utf8(value, max(0, max_bytes - 8))

    if isinstance(value, list):
        marker: Dict[str, Any] = {
            "_truncated": True,
            "omitted_items": len(value),
        }
        list_result: List[Any] = []
        for index, item in enumerate(value):
            marker["omitted_items"] = len(value) - index - 1
            marker_bytes = _json_bytes(marker) + 2
            remaining = max_bytes - _json_bytes(list_result) - marker_bytes
            if remaining <= 8:
                break
            compacted = _compact_json_value(item, remaining, depth + 1)
            candidate_list = list_result + [compacted]
            candidate_with_marker = candidate_list + [marker]
            if _json_bytes(candidate_with_marker) <= max_bytes:
                list_result = candidate_list
                continue
            break
        marker["omitted_items"] = max(0, len(value) - len(list_result))
        candidate_list = list_result + [marker]
        if _json_bytes(candidate_list) <= max_bytes:
            return candidate_list
        return [{"_truncated": True}]

    if isinstance(value, dict):
        dict_result: Dict[str, Any] = {"_truncated": True}
        omitted = 0
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            key = str(key)
            remaining = max_bytes - _json_bytes(dict_result) - len(key.encode("utf-8")) - 8
            if remaining <= 8:
                omitted = len(items) - index
                break
            compacted = _compact_json_value(item, remaining, depth + 1)
            candidate_dict = dict(dict_result)
            candidate_dict[key] = compacted
            if _json_bytes(candidate_dict) <= max_bytes:
                dict_result = candidate_dict
            else:
                omitted = len(items) - index
                break
        if omitted:
            dict_result["_omitted_keys"] = omitted
            if _json_bytes(dict_result) > max_bytes:
                dict_result.pop("_omitted_keys", None)
        if _json_bytes(dict_result) <= max_bytes:
            return dict_result
        return {"_truncated": True}

    return _truncate_utf8(str(value), max_bytes)


def truncate_tool_content(content: Any, max_tokens: int) -> Tuple[str, bool, int, int]:
    """Bound a tool result without producing malformed JSON.

    Returns ``(content, truncated, before_tokens, after_tokens)``.
    """

    max_tokens = max(1, int(max_tokens))
    text = _serialized(content)
    before = estimate_text_tokens(text)
    if before <= max_tokens:
        return text, False, before, before

    max_bytes = max_tokens * _BYTES_PER_ESTIMATED_TOKEN
    compacted_text: str
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        compacted_text = _truncate_utf8(text, max_bytes)
    else:
        compacted = _compact_json_value(parsed, max_bytes)
        compacted_text = json.dumps(
            compacted,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(compacted_text.encode("utf-8")) > max_bytes:
            compacted_text = (
                '{"_truncated":true}'
                if max_bytes >= len('{"_truncated":true}'.encode("utf-8"))
                else '""'
            )

    # Escaping and conservative estimator overhead can leave a tiny overrun.
    if estimate_text_tokens(compacted_text) > max_tokens:
        if "parsed" in locals():
            compacted_text = '""'
        else:
            while estimate_text_tokens(compacted_text) > max_tokens and max_bytes > 0:
                max_bytes = max(0, max_bytes - _BYTES_PER_ESTIMATED_TOKEN)
                compacted_text = _truncate_utf8(compacted_text, max_bytes)
    after = estimate_text_tokens(compacted_text)
    return compacted_text, True, before, after


def _tool_call_ids(message: Mapping[str, Any]) -> List[str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    out: List[str] = []
    for call in calls:
        if isinstance(call, Mapping) and call.get("id") is not None:
            out.append(str(call["id"]))
    return out


def tool_pairs_are_valid(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Validate OpenAI-style assistant tool-call/tool-result adjacency."""

    pending: Optional[set[str]] = None
    for message in messages:
        role = message.get("role")
        call_ids = _tool_call_ids(message) if role == "assistant" else []
        if call_ids:
            if pending:
                return False
            pending = set(call_ids)
            if len(pending) != len(call_ids):
                return False
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not pending or call_id is None or str(call_id) not in pending:
                return False
            pending.remove(str(call_id))
            continue
        if pending:
            return False
    return not pending


def _message_units(messages: Sequence[Mapping[str, Any]]) -> List[List[int]]:
    """Group assistant tool calls with all immediately following tool results."""

    units: List[List[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and _tool_call_ids(message):
            unit = [index]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                unit.append(index)
                index += 1
            units.append(unit)
            continue
        units.append([index])
        index += 1
    return units


@dataclass(frozen=True)
class TrimResult:
    messages: List[Dict[str, Any]]
    tokens_before: int
    tokens_after: int
    dropped_messages: int = 0
    compacted_tool_results: int = 0
    fits: bool = True
    pairing_valid: bool = True


def trim_messages_for_budget(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_message_tokens: int,
    max_tool_result_tokens: int,
) -> TrimResult:
    """Compact old tool output, then atomically drop oldest history units.

    Every system message and the most recent user message are immutable.  An
    assistant message containing tool calls is always kept or removed together
    with all of its corresponding tool messages.
    """

    copied: List[Dict[str, Any]] = [
        copy.deepcopy(dict(message)) for message in (messages or [])
    ]
    before = estimate_messages_tokens(copied)
    compacted_count = 0

    # Enforce the per-result ceiling first, oldest tool output first.
    for message in copied:
        if message.get("role") != "tool":
            continue
        compacted, changed, _old, _new = truncate_tool_content(
            message.get("content", ""),
            max_tool_result_tokens,
        )
        if changed:
            message["content"] = compacted
            compacted_count += 1

    current = estimate_messages_tokens(copied)
    # If the whole request is still too large, progressively compact the oldest
    # tool outputs before removing any conversational history.
    if current > max_message_tokens:
        for message in copied:
            if message.get("role") != "tool" or current <= max_message_tokens:
                continue
            current_tool_tokens = estimate_text_tokens(message.get("content", ""))
            if current_tool_tokens <= _MIN_COMPACTED_TOOL_TOKENS:
                continue
            over = current - max_message_tokens
            target = max(
                _MIN_COMPACTED_TOOL_TOKENS,
                current_tool_tokens - over - _MESSAGE_OVERHEAD_TOKENS,
            )
            if target >= current_tool_tokens:
                target = max(_MIN_COMPACTED_TOOL_TOKENS, current_tool_tokens // 2)
            compacted, changed, _old, _new = truncate_tool_content(
                message.get("content", ""),
                target,
            )
            if changed:
                message["content"] = compacted
                compacted_count += 1
                current = estimate_messages_tokens(copied)

    latest_user_index: Optional[int] = None
    for index, message in enumerate(copied):
        if message.get("role") == "user":
            latest_user_index = index
    protected = {
        index
        for index, message in enumerate(copied)
        if message.get("role") == "system"
    }
    if latest_user_index is not None:
        protected.add(latest_user_index)

    units = _message_units(copied)
    # Preserve the newest completed tool exchange after the current user. This
    # commonly carries a just-approved action or ask_user response; dropping it
    # would silently discard the user's latest continuation. Older exchanges
    # remain eligible for atomic removal.
    if latest_user_index is not None:
        for unit in reversed(units):
            if (
                unit[0] > latest_user_index
                and copied[unit[0]].get("role") == "assistant"
                and _tool_call_ids(copied[unit[0]])
            ):
                protected.update(unit)
                break

    removed: set[int] = set()
    if current > max_message_tokens:
        for unit in units:
            if current <= max_message_tokens:
                break
            if protected.intersection(unit):
                continue
            removed.update(unit)
            candidate = [
                message
                for index, message in enumerate(copied)
                if index not in removed
            ]
            current = estimate_messages_tokens(candidate)

    trimmed = [
        message for index, message in enumerate(copied) if index not in removed
    ]
    pairing_valid = tool_pairs_are_valid(trimmed)
    after = estimate_messages_tokens(trimmed)
    return TrimResult(
        messages=trimmed,
        tokens_before=before,
        tokens_after=after,
        dropped_messages=len(removed),
        compacted_tool_results=compacted_count,
        fits=after <= max_message_tokens and pairing_valid,
        pairing_valid=pairing_valid,
    )


def _bounded_env_number(
    environ: Mapping[str, str],
    names: Sequence[str],
    default: float,
    minimum: float,
    maximum: float,
    *,
    integer: bool,
) -> float:
    raw: Any = None
    for name in names:
        if name and environ.get(name) not in (None, ""):
            raw = environ.get(name)
            break
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    value = max(minimum, min(value, maximum))
    return int(value) if integer else value


@dataclass(frozen=True)
class BudgetConfig:
    """Validated limits for one orchestration host."""

    context_window_tokens: int = 128_000
    completion_reserve_tokens: int = 16_384
    max_tool_result_tokens: int = 12_000
    max_run_tokens: int = 300_000
    wall_time_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not 1_024 <= int(self.context_window_tokens) <= 2_000_000:
            raise ValueError("context_window_tokens must be between 1024 and 2000000")
        if not 1 <= int(self.completion_reserve_tokens) < int(
            self.context_window_tokens
        ):
            raise ValueError("completion_reserve_tokens must be below context window")
        if not 1 <= int(self.max_tool_result_tokens) <= int(
            self.context_window_tokens
        ):
            raise ValueError("max_tool_result_tokens is outside the context window")
        if not 1 <= int(self.max_run_tokens) <= 10_000_000:
            raise ValueError("max_run_tokens must be between 1 and 10000000")
        if not 0.1 <= float(self.wall_time_seconds) <= 86_400:
            raise ValueError("wall_time_seconds must be between 0.1 and 86400")

    @classmethod
    def from_env(
        cls,
        *,
        prefix: str,
        completion_reserve_default: int,
        max_run_default: int,
        wall_time_default: float,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "BudgetConfig":
        """Read host-specific values with global ``ORCHESTRATION_*`` fallback."""

        env = os.environ if environ is None else environ

        def names(suffix: str) -> List[str]:
            host = f"{prefix}_{suffix}" if prefix else ""
            global_name = f"ORCHESTRATION_{suffix}"
            return [host, global_name] if host != global_name else [host]

        context = int(
            _bounded_env_number(
                env,
                names("CONTEXT_WINDOW_TOKENS"),
                128_000,
                1_024,
                2_000_000,
                integer=True,
            )
        )
        completion = int(
            _bounded_env_number(
                env,
                names("COMPLETION_RESERVE_TOKENS"),
                completion_reserve_default,
                1,
                max(1, context - 1),
                integer=True,
            )
        )
        tool_result = int(
            _bounded_env_number(
                env,
                names("MAX_TOOL_RESULT_TOKENS"),
                12_000,
                1,
                context,
                integer=True,
            )
        )
        max_run = int(
            _bounded_env_number(
                env,
                names("MAX_RUN_TOKENS"),
                max_run_default,
                1,
                10_000_000,
                integer=True,
            )
        )
        wall_time = float(
            _bounded_env_number(
                env,
                names("RUN_TIMEOUT_SECONDS"),
                wall_time_default,
                0.1,
                86_400,
                integer=False,
            )
        )
        return cls(
            context_window_tokens=context,
            completion_reserve_tokens=completion,
            max_tool_result_tokens=tool_result,
            max_run_tokens=max_run,
            wall_time_seconds=wall_time,
        )


@dataclass
class BudgetState:
    """Resume-safe cumulative counters for one logical orchestration run."""

    cumulative_tokens: int = 0
    elapsed_seconds: float = 0.0
    context_trim_events: int = 0
    compacted_tool_results: int = 0
    dropped_messages: int = 0
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def from_dict(
        cls,
        value: Optional[Mapping[str, Any]],
        *,
        fallback_tokens: int = 0,
    ) -> "BudgetState":
        raw = value if isinstance(value, Mapping) else {}

        def non_negative_int(name: str, fallback: int = 0) -> int:
            try:
                return max(0, int(raw.get(name, fallback)))
            except (TypeError, ValueError):
                return max(0, fallback)

        try:
            elapsed = max(0.0, float(raw.get("elapsed_seconds", 0.0)))
        except (TypeError, ValueError):
            elapsed = 0.0
        return cls(
            cumulative_tokens=non_negative_int(
                "cumulative_tokens",
                max(0, int(fallback_tokens or 0)),
            ),
            elapsed_seconds=elapsed,
            context_trim_events=non_negative_int("context_trim_events"),
            compacted_tool_results=non_negative_int("compacted_tool_results"),
            dropped_messages=non_negative_int("dropped_messages"),
        )

    def active_elapsed_seconds(self) -> float:
        return self.elapsed_seconds + max(0.0, time.monotonic() - self._started_at)

    def add_usage(self, actual_tokens: Any) -> int:
        try:
            amount = max(0, int(actual_tokens or 0))
        except (TypeError, ValueError):
            amount = 0
        self.cumulative_tokens += amount
        return self.cumulative_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cumulative_tokens": self.cumulative_tokens,
            "elapsed_seconds": round(self.active_elapsed_seconds(), 6),
            "context_trim_events": self.context_trim_events,
            "compacted_tool_results": self.compacted_tool_results,
            "dropped_messages": self.dropped_messages,
        }


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    messages: List[Dict[str, Any]]
    estimated_prompt_tokens: int
    reason: Optional[str] = None
    events: Tuple[Dict[str, Any], ...] = ()


class BudgetPolicy:
    """Canonical admission, compaction, and accounting policy."""

    def __init__(self, config: BudgetConfig) -> None:
        self.config = config

    def remaining_seconds(self, state: BudgetState) -> float:
        return max(
            0.0,
            float(self.config.wall_time_seconds) - state.active_elapsed_seconds(),
        )

    def prepare_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        state: BudgetState,
    ) -> BudgetDecision:
        if self.remaining_seconds(state) <= 0:
            return self._denied(
                messages,
                tools,
                BudgetReason.WALL_TIME,
            )

        max_prompt_tokens = (
            self.config.context_window_tokens
            - self.config.completion_reserve_tokens
        )
        tool_tokens = estimate_tools_tokens(tools)
        max_message_tokens = max_prompt_tokens - tool_tokens
        if max_message_tokens <= 0:
            return self._denied(
                messages,
                tools,
                BudgetReason.CONTEXT_WINDOW,
            )

        trimmed = trim_messages_for_budget(
            messages,
            max_message_tokens=max_message_tokens,
            max_tool_result_tokens=self.config.max_tool_result_tokens,
        )
        events: List[Dict[str, Any]] = []
        if (
            trimmed.compacted_tool_results
            or trimmed.dropped_messages
            or trimmed.tokens_after < trimmed.tokens_before
        ):
            state.context_trim_events += 1
            state.compacted_tool_results += trimmed.compacted_tool_results
            state.dropped_messages += trimmed.dropped_messages
            events.append(
                {
                    "event": "context_trimmed",
                    "tokens_before": trimmed.tokens_before + tool_tokens,
                    "tokens_after": trimmed.tokens_after + tool_tokens,
                    "compacted_tool_results": trimmed.compacted_tool_results,
                    "dropped_messages": trimmed.dropped_messages,
                }
            )

        estimated = trimmed.tokens_after + tool_tokens
        if not trimmed.pairing_valid:
            return BudgetDecision(
                allowed=False,
                messages=trimmed.messages,
                estimated_prompt_tokens=estimated,
                reason=BudgetReason.INVALID_TOOL_PAIRING,
                events=tuple(events),
            )
        if not trimmed.fits or estimated > max_prompt_tokens:
            return BudgetDecision(
                allowed=False,
                messages=trimmed.messages,
                estimated_prompt_tokens=estimated,
                reason=BudgetReason.CONTEXT_WINDOW,
                events=tuple(events),
            )
        projected_run_tokens = (
            state.cumulative_tokens
            + estimated
            + self.config.completion_reserve_tokens
        )
        if projected_run_tokens > self.config.max_run_tokens:
            return BudgetDecision(
                allowed=False,
                messages=trimmed.messages,
                estimated_prompt_tokens=estimated,
                reason=BudgetReason.CUMULATIVE_TOKENS,
                events=tuple(events),
            )
        return BudgetDecision(
            allowed=True,
            messages=trimmed.messages,
            estimated_prompt_tokens=estimated,
            events=tuple(events),
        )

    def limit_tool_result(self, content: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
        bounded, changed, before, after = truncate_tool_content(
            content,
            self.config.max_tool_result_tokens,
        )
        if not changed:
            return bounded, None
        return bounded, {
            "event": "tool_result_truncated",
            "tokens_before": before,
            "tokens_after": after,
            "limit_tokens": self.config.max_tool_result_tokens,
        }

    def token_limit_reached(self, state: BudgetState) -> bool:
        return state.cumulative_tokens >= self.config.max_run_tokens

    def token_limit_exceeded(self, state: BudgetState) -> bool:
        return state.cumulative_tokens > self.config.max_run_tokens

    def terminal_summary(
        self,
        *,
        reason: str,
        state: BudgetState,
        estimated_prompt_tokens: int = 0,
    ) -> Dict[str, Any]:
        messages = {
            BudgetReason.WALL_TIME: (
                "The orchestration run reached its time budget. "
                "The results below are partial and the request is incomplete."
            ),
            BudgetReason.CUMULATIVE_TOKENS: (
                "The orchestration run reached its cumulative token budget. "
                "The results below are partial and the request is incomplete."
            ),
            BudgetReason.CONTEXT_WINDOW: (
                "The request cannot safely fit in the configured model context window "
                "without dropping the current request or system instructions. "
                "The results below are partial and the request is incomplete."
            ),
            BudgetReason.INVALID_TOOL_PAIRING: (
                "The orchestration transcript could not be compacted into a "
                "provider-valid tool-call sequence. The request is incomplete."
            ),
        }
        return {
            "reason": reason,
            "message": messages.get(
                reason,
                "The orchestration run reached a configured budget and is incomplete.",
            ),
            "incomplete": True,
            "partial": True,
            "cumulative_tokens": state.cumulative_tokens,
            "max_run_tokens": self.config.max_run_tokens,
            "estimated_prompt_tokens": max(0, int(estimated_prompt_tokens or 0)),
            "context_window_tokens": self.config.context_window_tokens,
            "completion_reserve_tokens": self.config.completion_reserve_tokens,
            "elapsed_seconds": round(state.active_elapsed_seconds(), 3),
            "wall_time_seconds": self.config.wall_time_seconds,
            "budget_state": state.to_dict(),
        }

    @staticmethod
    def _denied(
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> BudgetDecision:
        copied = [copy.deepcopy(dict(message)) for message in (messages or [])]
        return BudgetDecision(
            allowed=False,
            messages=copied,
            estimated_prompt_tokens=estimate_request_tokens(copied, tools),
            reason=reason,
        )
