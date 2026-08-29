from __future__ import annotations

from collections.abc import Callable

import pytest

from maeyr_platform.lifecycle import BufferConfig


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BufferConfig(max_queue_size=0),
        lambda: BufferConfig(max_queue_size=2, max_batch_size=3),
        lambda: BufferConfig(max_batch_size=0),
        lambda: BufferConfig(flush_interval_seconds=0.0),
    ],
)
def test_buffer_config_rejects_unbounded_or_invalid_values(
    factory: Callable[[], BufferConfig],
) -> None:
    with pytest.raises(ValueError):
        factory()
