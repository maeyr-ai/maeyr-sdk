"""Lifecycle-compatible tracing facade; span conversion remains explicit."""

from maeyr_platform.tracing import (
    configure_recorder,
    configure_transport,
    drain_recorder,
    get_recorder_stats,
    record_span,
    start_recorder,
    stop_recorder,
)

__all__ = [
    "configure_recorder",
    "configure_transport",
    "drain_recorder",
    "get_recorder_stats",
    "record_span",
    "start_recorder",
    "stop_recorder",
]
