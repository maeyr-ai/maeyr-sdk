"""Minimal typed lifecycle helper for shared aiohttp sessions."""

from __future__ import annotations

from typing import Protocol


class ClosableSession(Protocol):
    @property
    def closed(self) -> bool: ...

    async def close(self) -> None: ...


async def close_session(session: ClosableSession | None) -> None:
    """Close an optional live session exactly once."""
    if session is not None and not session.closed:
        await session.close()


__all__ = ["ClosableSession", "close_session"]
