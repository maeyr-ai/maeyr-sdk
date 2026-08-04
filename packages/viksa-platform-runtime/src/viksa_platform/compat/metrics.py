"""Lifecycle-compatible metrics facade; event conversion remains explicit."""

from viksa_platform.metrics import (
    configure_recorder,
    configure_transport,
    drain_recorder,
    get_recorder_stats,
    record_usage,
    start_recorder,
    stop_recorder,
)

__all__ = [
    "configure_recorder",
    "configure_transport",
    "drain_recorder",
    "get_recorder_stats",
    "record_usage",
    "start_recorder",
    "stop_recorder",
]
