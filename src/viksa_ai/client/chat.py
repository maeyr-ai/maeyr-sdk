from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import httpx

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient


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
        return await self._chat._client._arequest(
            "DELETE", "/chat", f"/trigger/{trigger_id}"
        )


class ChatClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self.triggers = _TriggersClient(self)

    async def indent_finder(
        self,
        message: str,
        *,
        conversation_id: Optional[str] = None,
        workforce_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if workforce_id:
            body["workforce_id"] = workforce_id
        if metadata:
            body["metadata"] = metadata
        return await self._client._arequest(
            "POST", "/chat", "/indent_finder", json=body
        )

    async def stream_indent_finder(
        self,
        message: str,
        *,
        conversation_id: Optional[str] = None,
        workforce_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        body: Dict[str, Any] = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if workforce_id:
            body["workforce_id"] = workforce_id
        url = f"{self._client.base_url}/chat/indent_finder/stream"
        client = self._client._get_async_client()
        async with client.stream("POST", url, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                import json

                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue

    async def list_conversations(self, *, skip: int = 0, limit: int = 50) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET",
            "/chat",
            "/conversations",
            params={"limit": limit, "skip": skip},
        )

    async def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "GET", "/chat", f"/conversations/{conversation_id}"
        )

    async def generate_agent(self, prompt: str) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", "/generate/agent", json={"prompt": prompt}
        )

    async def fix_agent(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._client._arequest(
            "POST", "/chat", "/fix/agent", json=body
        )
