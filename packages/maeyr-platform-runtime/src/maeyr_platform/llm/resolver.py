"""Pure hierarchical selection used by API and adversarial tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maeyr_platform.llm.models import LLMScope, LLMScopeType


def select_effective_configuration(
    scope: LLMScope,
    configurations: Mapping[LLMScopeType | str, Mapping[str, Any] | None],
) -> tuple[LLMScopeType, Mapping[str, Any] | None]:
    """Select project > organization > account > platform.

    Disabled tenant documents are intentionally skipped. A selected enabled
    document is authoritative even when incomplete; capability validation must
    fail closed later rather than falling through to a chargeable provider.
    """

    candidates: list[LLMScopeType] = []
    if scope.project_id:
        candidates.append(LLMScopeType.PROJECT)
    if scope.org_id:
        candidates.append(LLMScopeType.ORGANIZATION)
    candidates.append(LLMScopeType.ACCOUNT)
    for candidate in candidates:
        raw = configurations.get(candidate) or configurations.get(candidate.value)
        if raw is not None and raw.get("enabled") is True:
            return candidate, raw
    return LLMScopeType.PLATFORM, None
