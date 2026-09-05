"""Typed environment lookup shared by platform services."""

import os
from typing import Literal, TypeVar, overload

_Default = TypeVar("_Default")


@overload
def ENVIRON(key: str, default: _Default, optional: bool = False) -> str | _Default: ...


@overload
def ENVIRON(
    key: str,
    default: None = None,
    optional: Literal[False] = False,
) -> str: ...


@overload
def ENVIRON(
    key: str,
    default: None = None,
    optional: Literal[True] = True,
) -> str | None: ...


def ENVIRON(
    key: str,
    default: object | None = None,
    optional: bool = False,
) -> object | None:
    """Read a required variable, or return the supplied optional/default value."""
    return os.environ[key] if not optional and default is None else os.environ.get(key, default)


__all__ = ["ENVIRON"]
