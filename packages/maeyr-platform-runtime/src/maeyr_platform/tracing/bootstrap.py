"""Service-neutral startup wiring for the shared tracing runtime."""

from __future__ import annotations

from maeyr_platform.tracing.remote_recorder import configure_remote_sink
from maeyr_platform.tracing.sampling import configure_sampling


def bootstrap_remote_traces(service: str) -> None:
    """Configure sampling and the remote sink for one named service."""

    configure_sampling()
    configure_remote_sink(service)


__all__ = ["bootstrap_remote_traces"]
