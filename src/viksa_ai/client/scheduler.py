from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class SchedulerClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client

    async def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/scheduler", "/schedule/create", json=body)

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/scheduler", "/schedule/list", params={"skip": skip, "limit": limit}
        )

    async def get(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/scheduler", f"/schedule/{schedule_id}"
        )

    async def update(self, schedule_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "PATCH", "/scheduler", f"/schedule/{schedule_id}", json=body
        )

    async def delete(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "DELETE", "/scheduler", f"/schedule/{schedule_id}"
        )

    async def pause(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/scheduler", f"/schedule/{schedule_id}/pause"
        )

    async def resume(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/scheduler", f"/schedule/{schedule_id}/resume"
        )

    async def run_now(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/scheduler", f"/schedule/{schedule_id}/run-now"
        )
