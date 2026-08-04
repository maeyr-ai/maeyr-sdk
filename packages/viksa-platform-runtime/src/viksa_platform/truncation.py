"""Structure-aware truncation for bounded LLM context payloads.

The helper keeps whole structured list items whenever possible and annotates
intentional downsampling so callers do not mistake a bounded payload for an
empty upstream result.
"""

from __future__ import annotations

import copy
import json
from typing import Any, List, Optional, Tuple

DEFAULT_SYNTHESIS_BUDGET = 60_000

_MIN_ITEMS_KEPT = 3


def smart_truncate(
    value: Any,
    max_chars: int = DEFAULT_SYNTHESIS_BUDGET,
) -> str:
    """Serialize ``value`` and bound it without slicing structured list items."""
    if value is None:
        return ""

    if isinstance(value, str):
        return _plain_truncate(value, max_chars)

    try:
        serialized = json.dumps(value, default=str)
    except (TypeError, ValueError):
        serialized = str(value)

    if len(serialized) <= max_chars:
        return serialized

    if isinstance(value, (dict, list)):
        shrunk = _shrink_largest_list(value, max_chars)
        if shrunk is not None:
            return shrunk

    return _plain_truncate(serialized, max_chars)


def _plain_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated to fit context]"


def _shrink_largest_list(value: Any, max_chars: int) -> Optional[str]:
    """Shrink the heaviest list-of-dicts until the full JSON fits the budget."""
    candidates: List[Tuple[List[Any], List[Any]]] = []
    _collect_lists(value, [], candidates)
    if not candidates:
        return None

    def weight(candidate: Tuple[List[Any], List[Any]]) -> int:
        try:
            return len(json.dumps(candidate[1], default=str))
        except (TypeError, ValueError):
            return 0

    path, original_list = max(candidates, key=weight)
    if len(original_list) < _MIN_ITEMS_KEPT:
        return None

    low, high = _MIN_ITEMS_KEPT, len(original_list)
    best_serialized: Optional[str] = None
    best_kept = 0
    while low <= high:
        midpoint = (low + high) // 2
        candidate = _replace_at_path(value, path, original_list[:midpoint])
        _annotate_truncation(
            candidate,
            path,
            kept=midpoint,
            total=len(original_list),
        )
        try:
            serialized = json.dumps(candidate, default=str)
        except (TypeError, ValueError):
            return None
        if len(serialized) <= max_chars:
            best_serialized = serialized
            best_kept = midpoint
            low = midpoint + 1
        else:
            high = midpoint - 1

    if best_serialized is not None and best_kept >= _MIN_ITEMS_KEPT:
        return best_serialized
    return None


def _collect_lists(
    node: Any,
    path: List[Any],
    out: List[Tuple[List[Any], List[Any]]],
) -> None:
    """Collect nested list-of-dict candidates and their traversal paths."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list):
                if any(isinstance(item, dict) for item in value):
                    out.append((path + [key], value))
                for index, item in enumerate(value):
                    if isinstance(item, (dict, list)):
                        _collect_lists(item, path + [key, index], out)
            elif isinstance(value, (dict, list)):
                _collect_lists(value, path + [key], out)
    elif isinstance(node, list):
        if not path and any(isinstance(item, dict) for item in node):
            out.append((path[:], node))
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                _collect_lists(value, path + [index], out)


def _replace_at_path(root: Any, path: List[Any], new_value: Any) -> Any:
    """Return a deep copy of ``root`` with the value at ``path`` replaced."""
    new_root = copy.deepcopy(root)
    if not path:
        return new_value
    cursor = new_root
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = new_value
    return new_root


def _annotate_truncation(
    root: Any,
    path: List[Any],
    *,
    kept: int,
    total: int,
) -> None:
    """Add a sibling note explaining how many retrieved items were omitted."""
    if not path:
        return
    parent_path = path[:-1]
    last_key = path[-1]
    cursor = root
    for step in parent_path:
        cursor = cursor[step]
    if isinstance(cursor, dict):
        cursor[f"_{last_key}_truncation_note"] = (
            f"Showing {kept} of {total} items. "
            f"{total - kept} additional items were omitted ONLY to fit the "
            f"LLM context window - they were retrieved successfully and the "
            f"data is real. Do not claim no data was found."
        )


__all__ = ["DEFAULT_SYNTHESIS_BUDGET", "smart_truncate"]
