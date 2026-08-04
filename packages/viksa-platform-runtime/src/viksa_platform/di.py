"""Small dependency-ownership primitives shared by service composition roots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar, cast

T = TypeVar("T")
_UNRESOLVED = object()


class LazyOwned(Generic[T]):
    """Defer construction while retaining a stable, monkeypatch-compatible handle."""

    __slots__ = ("_factory", "_resolved")
    _factory: Callable[[], T]
    _resolved: object

    def __init__(self, factory: Callable[[], T]) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_resolved", _UNRESOLVED)

    def resolve(self) -> T:
        current = self._resolved
        if current is _UNRESOLVED:
            current = self._factory()
            object.__setattr__(self, "_resolved", current)
        return cast(T, current)

    def replace(self, value: T | None) -> None:
        object.__setattr__(self, "_resolved", _UNRESOLVED if value is None else value)

    def __getattr__(self, name: str) -> object:
        return getattr(self.resolve(), name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        setattr(self.resolve(), name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            object.__delattr__(self, name)
            return
        delattr(self.resolve(), name)


def lazy_owned(factory: Callable[[], T]) -> T:
    """Expose a lazy handle with the owned dependency's concrete static type."""
    return cast(T, LazyOwned(factory))


__all__ = ["LazyOwned", "lazy_owned"]
