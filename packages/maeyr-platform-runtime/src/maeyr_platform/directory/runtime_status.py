from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Dict

from maeyr_platform.di import lazy_owned


class RuntimeStatusService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._timers: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0}
        )

    async def record_operation(
        self,
        operation: str,
        *,
        success: bool,
        duration_ms: float | None = None,
    ) -> None:
        async with self._lock:
            status = "success" if success else "error"
            self._counters[f"{operation}_{status}_total"] += 1
            if duration_ms is not None:
                timer = self._timers[operation]
                timer["count"] += 1
                timer["total_ms"] += float(duration_ms)
                timer["max_ms"] = max(timer["max_ms"], float(duration_ms))

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            timers: Dict[str, Dict[str, float]] = {}
            for operation, values in self._timers.items():
                count = values["count"] or 0.0
                avg_ms = values["total_ms"] / count if count else 0.0
                timers[operation] = {
                    "count": int(count),
                    "total_ms": round(values["total_ms"], 3),
                    "max_ms": round(values["max_ms"], 3),
                    "avg_ms": round(avg_ms, 3),
                }
            return {
                "counters": dict(self._counters),
                "timers": timers,
            }


def build_runtime_status_service() -> RuntimeStatusService:
    return RuntimeStatusService()


runtime_status = lazy_owned(build_runtime_status_service)
