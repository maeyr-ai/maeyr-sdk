from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest

from maeyr_platform.tracing import lifecycle


def _install_recorder(
    monkeypatch: pytest.MonkeyPatch,
    scheduled: list[tuple[str, dict[str, Any]]],
    emitted: list[tuple[str, dict[str, Any]]],
) -> None:
    recorder = ModuleType("common.platform_traces.recorder")

    def schedule_span_start(**kwargs: Any) -> None:
        scheduled.append(("start", kwargs))

    def schedule_span_end(**kwargs: Any) -> None:
        scheduled.append(("end", kwargs))

    async def record_span_start(**kwargs: Any) -> None:
        emitted.append(("start", kwargs))

    async def record_span_end(**kwargs: Any) -> None:
        emitted.append(("end", kwargs))

    setattr(recorder, "schedule_span_start", schedule_span_start)
    setattr(recorder, "schedule_span_end", schedule_span_end)
    setattr(recorder, "record_span_start", record_span_start)
    setattr(recorder, "record_span_end", record_span_end)
    monkeypatch.setitem(sys.modules, recorder.__name__, recorder)


def test_span_handle_and_duration_resolution_preserve_boundary_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(milliseconds=124.6)
    monkeypatch.setattr(
        "maeyr_platform.tracing.lifecycle.time.perf_counter",
        lambda: 8.125,
    )

    generated = lifecycle.make_span_handle("span", started_at=started_at)
    assert generated == lifecycle.SpanHandle("span", started_at, 8.125)
    assert lifecycle.resolve_duration_ms(generated, ended_at=ended_at, duration_ms=-1) == (
        ended_at,
        0,
    )

    perf_handle = lifecycle.SpanHandle("perf", started_at, 8.0)
    assert lifecycle.resolve_duration_ms(perf_handle, ended_at=ended_at) == (ended_at, 125)

    wall_handle = lifecycle.SpanHandle("wall", started_at, None)
    assert lifecycle.resolve_duration_ms(wall_handle, ended_at=ended_at) == (ended_at, 125)
    assert (
        lifecycle.resolve_duration_ms(
            wall_handle,
            ended_at=started_at - timedelta(seconds=1),
        )[1]
        == 0
    )


def test_scheduled_root_lifecycle_delegates_with_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[tuple[str, dict[str, Any]]] = []
    emitted: list[tuple[str, dict[str, Any]]] = []
    _install_recorder(monkeypatch, scheduled, emitted)
    started_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(milliseconds=9)
    monkeypatch.setattr(lifecycle, "generate_span_id", lambda: "a" * 16)

    handle = lifecycle.schedule_root_start(
        is_root=False,
        started_at=started_at,
        perf_started=10.0,
        span_name="root",
    )
    lifecycle.schedule_root_end(
        handle,
        is_root=False,
        ended_at=ended_at,
        duration_ms=9,
        status="ok",
    )

    assert handle == lifecycle.SpanHandle("a" * 16, started_at, 10.0)
    assert scheduled == [
        (
            "start",
            {
                "span_id": "a" * 16,
                "started_at": started_at,
                "status": "running",
                "duration_ms": 0,
                "is_root": True,
                "span_name": "root",
            },
        ),
        (
            "end",
            {
                "span_id": "a" * 16,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": 9,
                "_is_completion": True,
                "is_root": True,
                "status": "ok",
            },
        ),
    ]
    assert emitted == []


@pytest.mark.asyncio
async def test_awaited_root_lifecycle_delegates_with_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[tuple[str, dict[str, Any]]] = []
    emitted: list[tuple[str, dict[str, Any]]] = []
    _install_recorder(monkeypatch, scheduled, emitted)
    started_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(milliseconds=13)

    handle = await lifecycle.emit_root_start(
        span_id="b" * 16,
        started_at=started_at,
        perf_started=20.0,
        span_name="root",
    )
    await lifecycle.emit_root_end(
        handle,
        ended_at=ended_at,
        duration_ms=13,
        status="ok",
    )

    assert scheduled == []
    assert emitted[0] == (
        "start",
        {
            "span_id": "b" * 16,
            "started_at": started_at,
            "status": "running",
            "duration_ms": 0,
            "is_root": True,
            "span_name": "root",
        },
    )
    assert emitted[1] == (
        "end",
        {
            "span_id": "b" * 16,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": 13,
            "_is_completion": True,
            "is_root": True,
            "status": "ok",
        },
    )
