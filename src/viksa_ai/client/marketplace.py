from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from viksa_ai._constants import SERVICE_PATHS
from viksa_ai.client.pagination import iter_pages

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class _ListingsClient:
    def __init__(self, marketplace: MarketplaceClient) -> None:
        self._mp = marketplace

    async def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest("POST", self._mp._prefix, "/listings", json=body)

    async def search(self, **params: Any) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "GET", self._mp._prefix, "/listings/search", params=params or None
        )

    async def list(self, **params: Any) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "GET", self._mp._prefix, "/listings", params=params or None
        )

    def iter_all(self, **params: Any) -> Any:
        return iter_pages(
            lambda **kw: self.list(**{**params, **kw}),
            limit=int(params.get("limit", 50)),
            items_key="listings",
        )

    async def get(self, listing_id: str) -> Dict[str, Any]:
        return await self._mp._client._arequest("GET", self._mp._prefix, f"/listings/{listing_id}")

    async def publish(
        self, listing_id: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "POST",
            self._mp._prefix,
            f"/listings/{listing_id}/publish",
            json=body or {},
        )

    async def install(self, listing_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "POST",
            self._mp._prefix,
            f"/listings/{listing_id}/install",
            json=body,
        )


class _WorkforceClient:
    def __init__(self, marketplace: MarketplaceClient) -> None:
        self._mp = marketplace

    async def publish(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "POST", self._mp._prefix, "/workforce/publish", json=body
        )

    async def search(self, **params: Any) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "GET", self._mp._prefix, "/workforce/search", params=params or None
        )

    async def get(self, listing_id: str) -> Dict[str, Any]:
        return await self._mp._client._arequest("GET", self._mp._prefix, f"/workforce/{listing_id}")

    async def install(self, listing_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "POST",
            self._mp._prefix,
            f"/workforce/{listing_id}/install",
            json=body,
        )


class _PublishersClient:
    def __init__(self, marketplace: MarketplaceClient) -> None:
        self._mp = marketplace

    async def create_profile(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "POST", self._mp._prefix, "/publishers/profile", json=body
        )

    async def me(self) -> Dict[str, Any]:
        return await self._mp._client._arequest("GET", self._mp._prefix, "/publishers/me")

    async def update_profile(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mp._client._arequest(
            "PUT", self._mp._prefix, "/publishers/profile", json=body
        )


class MarketplaceClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self._prefix = SERVICE_PATHS["marketplace"]
        self.listings = _ListingsClient(self)
        self.workforce = _WorkforceClient(self)
        self.publishers = _PublishersClient(self)

    async def categories(self) -> Dict[str, Any]:
        return await self._client._arequest("GET", self._prefix, "/categories")

    async def installations(self, **params: Any) -> Dict[str, Any]:
        return await self._client._arequest("GET", self._prefix, "/install", params=params or None)
