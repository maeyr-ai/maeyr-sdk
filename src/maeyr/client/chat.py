from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

from maeyr._constants import SERVICE_PATHS
from maeyr.client.pagination import iter_pages

if TYPE_CHECKING:
    from maeyr.client.base import MaeyrClient


class _TriggersClient:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    async def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._chat._client._arequest("POST", "/chat", "/trigger", json=body)

    async def list(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._chat._client._arequest(
            "GET", "/chat", "/trigger", params={"skip": skip, "limit": limit}
        )

    async def get(self, trigger_id: str) -> Dict[str, Any]:
        return await self._chat._client._arequest("GET", "/chat", f"/trigger/{trigger_id}")

    async def update(self, trigger_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._chat._client._arequest(
            "PATCH", "/chat", f"/trigger/{trigger_id}", json=body
        )

    async def delete(self, trigger_id: str) -> Dict[str, Any]:
        return await self._chat._client._arequest("DELETE", "/chat", f"/trigger/{trigger_id}")

    async def test(self, trigger_id: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._chat._client._astream(
            "POST",
            SERVICE_PATHS["chat"],
            f"/trigger/{trigger_id}/test",
            json_body=body or {},
        )

    async def list_executions(self, trigger_id: str, **params: Any) -> Dict[str, Any]:
        return await self._chat._client._arequest(
            "GET", "/chat", f"/trigger/{trigger_id}/executions", params=params or None
        )


class _ApprovalsClient:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    async def list(self, **params: Any) -> Dict[str, Any]:
        return await self._chat._client._arequest("GET", "/chat", "/approvals", params=params)

    async def get(self, approval_id: str) -> Dict[str, Any]:
        return await self._chat._client._arequest("GET", "/chat", f"/approvals/{approval_id}")

    async def decide(self, approval_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._chat._client._arequest(
            "POST", "/chat", f"/approvals/{approval_id}/decision", json=body
        )


class ChatClient:
    def __init__(self, client: MaeyrClient) -> None:
        self._client = client
        self.triggers = _TriggersClient(self)
        self.approvals = _ApprovalsClient(self)

    async def message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/chat", "/message", json=body)

    async def indent_finder(
        self,
        message: str,
        *,
        conversation_id: Optional[str] = None,
        workforce_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        schedule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "message": message,
            # A chat turn may decide to create a schedule. Allocate its identity
            # before the retrying transport so an ambiguous POST cannot create
            # a second schedule. Persist/pass this value for a manual retry.
            "schedule_id": schedule_id or f"SC-{uuid.uuid4().hex.upper()}",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if workforce_id:
            body["workforce_id"] = workforce_id
        if metadata:
            body["metadata"] = metadata
        return await self._client._arequest("POST", "/chat", "/indent_finder", json=body)

    async def stream_indent_finder(
        self,
        message: str,
        *,
        conversation_id: Optional[str] = None,
        workforce_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "message": message,
            "schedule_id": schedule_id or f"SC-{uuid.uuid4().hex.upper()}",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if workforce_id:
            body["workforce_id"] = workforce_id
        async for event in self._client._astream(
            "POST",
            SERVICE_PATHS["chat"],
            "/indent_finder/stream",
            json_body=body,
        ):
            yield event

    async def cancel_execution(self, conversation_id: str) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/chat", f"/execution/{conversation_id}/cancel")

    async def active_execution(self, conversation_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/chat", f"/execution/{conversation_id}/active")

    async def stream_execution(self, conversation_id: str) -> AsyncIterator[Dict[str, Any]]:
        async for event in self._client._astream(
            "GET", SERVICE_PATHS["chat"], f"/execution/{conversation_id}/stream"
        ):
            yield event

    async def list_conversations(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET",
            "/chat",
            "/conversations",
            params={"limit": limit, "skip": skip},
        )

    def iter_conversations(self, *, limit: int = 50) -> AsyncIterator[Dict[str, Any]]:
        return iter_pages(
            lambda **kw: self.list_conversations(**kw),
            limit=limit,
            items_key="conversations",
        )

    async def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/chat", f"/conversations/{conversation_id}")

    async def patch_conversation(
        self, conversation_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._client._arequest(
            "PATCH", "/chat", f"/conversations/{conversation_id}", json=body
        )

    async def delete_conversation(self, conversation_id: str) -> Dict[str, Any]:
        return await self._client._arequest("DELETE", "/chat", f"/conversations/{conversation_id}")

    async def delete_message(self, message_id: str) -> Dict[str, Any]:
        return await self._client._arequest("DELETE", "/chat", f"/messages/{message_id}")

    async def search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/chat", "/search", json=body)

    async def token_stats(self, **params: Any) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/chat", "/stats/tokens", params=params or None)

    async def generate_agent(self, prompt: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", "/generate/agent", json={"prompt": prompt}
        )

    async def fix_agent(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest("POST", "/chat", "/fix/agent", json=body)

    async def execution_events(self, execution_id: str) -> Dict[str, Any]:
        return await self._client._arequest("GET", "/chat", f"/executions/{execution_id}/events")

    async def debug_start(self, execution_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", f"/executions/{execution_id}/debug/start", json=body
        )

    async def debug_resume(self, execution_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", f"/executions/{execution_id}/debug/resume", json=body
        )

    async def debug_stop(self, execution_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", f"/executions/{execution_id}/debug/stop"
        )

    async def debug_inspect(self, execution_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", f"/executions/{execution_id}/debug/inspect", json=body
        )
