from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


class WorkflowClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self.executions = _ExecutionsClient(self)

    async def start(self, workflow_id: str, *, trigger_source: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"workflow_id": workflow_id}
        if trigger_source:
            body["trigger_source"] = trigger_source
        return await self._client._arequest("POST", "/workflow", "/start", json=body)

    async def get(self, workflow_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/workflow", f"/id/{workflow_id}")

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/workflow", "/list", params={"skip": skip, "limit": limit}
        )

    async def delete(self, workflow_id: str) -> Dict[str, Any]:
        return await self._client._arequest("DELETE", "/workflow", f"/{workflow_id}")


class _ExecutionsClient:
    def __init__(self, workflow: WorkflowClient) -> None:
        self._workflow = workflow

    async def create(self, workflow_id: str, *, schedule_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"workflow_id": workflow_id}
        if schedule_id:
            body["schedule_id"] = schedule_id
        return await self._workflow._client._arequest(
            "POST", "/workflow", "/execution/create", json=body
        )

    async def get(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "GET", "/workflow", f"/execution/{execution_id}"
        )

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "GET", "/workflow", "/execution/list", params={"skip": skip, "limit": limit}
        )

    async def start(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/start"
        )

    async def cancel(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/cancel"
        )

