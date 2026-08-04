from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class SchedulerClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client

    async def create(
        self,
        body: Dict[str, Any],
        *,
        schedule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a schedule with a caller-stable identifier.

        ``schedule_id`` is generated once before the HTTP transport starts, so
        automatic retries reuse the same creation identity.  Callers recovering
        from an ambiguous response should persist the supplied/generated ID and
        pass it again on their next invocation.
        """
        request_body = dict(body)
        body_schedule_id = request_body.get("schedule_id")
        if schedule_id is not None and body_schedule_id not in (None, schedule_id):
            raise ValueError("schedule_id conflicts with body['schedule_id']")
        request_body["schedule_id"] = (
            schedule_id
            or (str(body_schedule_id) if body_schedule_id is not None else None)
            or f"SC-{uuid.uuid4().hex.upper()}"
        )
        return await self._client._arequest(
            "POST", "/scheduler", "/schedule/create", json=request_body
        )

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/scheduler", "/schedule/list", params={"skip": skip, "limit": limit}
        )

    async def get(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/scheduler", f"/schedule/{schedule_id}")

    async def update(self, schedule_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "PATCH", "/scheduler", f"/schedule/{schedule_id}", json=body
        )

    async def delete(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest("DELETE", "/scheduler", f"/schedule/{schedule_id}")

    async def pause(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/scheduler", f"/schedule/{schedule_id}/pause")

    async def resume(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/scheduler", f"/schedule/{schedule_id}/resume")

    async def run_now(self, schedule_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/scheduler", f"/schedule/{schedule_id}/run-now"
        )
