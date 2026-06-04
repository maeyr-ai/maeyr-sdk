"""Pagination helpers for list endpoints."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Protocol


class _ListPage(Protocol):
    async def __call__(
        self, *, skip: int, limit: int, **kwargs: Any
    ) -> Dict[str, Any]: ...


async def iter_pages(
    fetch_page: _ListPage,
    *,
    limit: int = 50,
    items_key: str = "items",
    total_key: Optional[str] = "total",
    **kwargs: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Async iterator over all items from a paginated list endpoint.

    Supports responses shaped as ``{items: [...], total: N}`` or bare lists
    under common keys (``agents``, ``conversations``, ``secrets``, etc.).
    """
    skip = 0
    seen = 0
    total: Optional[int] = None

    while True:
        page = await fetch_page(skip=skip, limit=limit, **kwargs)
        if isinstance(page, list):
            items = page
            total = len(page)
        else:
            items = (
                page.get(items_key)
                or page.get("agents")
                or page.get("conversations")
                or page.get("secrets")
                or page.get("servers")
                or page.get("listings")
                or page.get("workflows")
                or page.get("executions")
                or page.get("schedules")
                or page.get("triggers")
                or []
            )
            if total_key and total is None:
                total = page.get(total_key)

        if not items:
            break

        for item in items:
            yield item
            seen += 1

        if len(items) < limit:
            break
        if total is not None and seen >= total:
            break
        skip += limit


def extract_items(
    page: Dict[str, Any],
    *,
    items_key: str = "items",
) -> List[Any]:
    """Extract list payload from a paginated response dict."""
    if items_key in page and isinstance(page[items_key], list):
        return page[items_key]
    for key in (
        "agents",
        "conversations",
        "secrets",
        "servers",
        "listings",
        "workflows",
        "executions",
        "schedules",
        "triggers",
        "keys",
    ):
        if key in page and isinstance(page[key], list):
            return page[key]
    return []
