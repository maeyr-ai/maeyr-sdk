from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from maeyr.client.pagination import iter_pages

if TYPE_CHECKING:
    from maeyr.client.base import MaeyrClient


class _ExecutionsClient:
    def __init__(self, workflow: WorkflowClient) -> None:
        self._workflow = workflow

    async def create(
        self, workflow_id: str, *, schedule_id: Optional[str] = None
    ) -> Dict[str, Any]:
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

    async def list_for_workflow(self, workflow_id: str, **params: Any) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "GET", "/workflow", f"/execution/workflow/{workflow_id}/list", params=params
        )

    async def start(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/start"
        )

    async def retry(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/retry"
        )

    async def cancel(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/cancel"
        )

    async def stop(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "POST", "/workflow", f"/execution/{execution_id}/stop"
        )

    async def delete(self, execution_id: str) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "DELETE", "/workflow", f"/execution/{execution_id}"
        )

    async def patch_tasks(self, execution_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._workflow._client._arequest(
            "PATCH", "/workflow", f"/execution/{execution_id}/tasks", json=body
        )


class WorkflowClient:
    def __init__(self, client: MaeyrClient) -> None:
        self._client = client
        self.executions = _ExecutionsClient(self)

    async def start(
        self, workflow_id: str, *, trigger_source: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"workflow_id": workflow_id}
        if trigger_source:
            body["trigger_source"] = trigger_source
        return await self._client._arequest("POST", "/workflow", "/start", json=body)

    async def get(self, workflow_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/workflow", f"/id/{workflow_id}")

    async def patch(self, workflow_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("PATCH", "/workflow", f"/id/{workflow_id}", json=body)

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/workflow", "/list", params={"skip": skip, "limit": limit}
        )

    def iter_all(self, *, limit: int = 50) -> Any:
        return iter_pages(lambda **kw: self.list(**kw), limit=limit, items_key="workflows")

    async def related(self, workflow_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/workflow", f"/related/{workflow_id}")

    async def clone(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/workflow", "/clone", json=body)

    async def rerun(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/workflow", "/rerun", json=body)

    async def execute(
        self, workflow_id: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST",
            "/workflow",
            f"/{workflow_id}/execute",
            json=body or {},
        )

    async def delete(self, workflow_id: str) -> Dict[str, Any]:
        return await self._client._arequest("DELETE", "/workflow", f"/{workflow_id}")
