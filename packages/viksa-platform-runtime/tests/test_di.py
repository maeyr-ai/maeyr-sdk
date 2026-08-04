from __future__ import annotations

from dataclasses import dataclass

from viksa_platform.di import LazyOwned, lazy_owned


@dataclass
class _Resource:
    value: int


def test_lazy_owned_constructs_once_and_forwards_attributes() -> None:
    calls = 0

    def build() -> _Resource:
        nonlocal calls
        calls += 1
        return _Resource(1)

    resource = lazy_owned(build)
    assert calls == 0
    assert resource.value == 1
    assert resource.value == 1
    assert calls == 1


def test_lazy_owned_replace_can_reset_resolution() -> None:
    proxy = LazyOwned(lambda: _Resource(1))
    proxy.replace(_Resource(2))
    assert proxy.value == 2
    proxy.replace(None)
    assert int(proxy.value) == 1
