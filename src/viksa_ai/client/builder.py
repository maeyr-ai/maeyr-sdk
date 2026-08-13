from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from viksa_ai._constants import SERVICE_PATHS
from viksa_ai.client.pagination import iter_pages
from viksa_ai.models.agent import (
    AgentCreationRequest,
    AgentDeletionResult,
    AgentUpdateRequest,
)

if TYPE_CHECKING:
    from viksa_ai.client.base import ViksaClient

_BUILDER = SERVICE_PATHS["builder"]


class _AgentsClient:
    def __init__(self, builder: BuilderClient) -> None:
        self._builder = builder

    async def create(self, request: AgentCreationRequest) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST",
            _BUILDER,
            "/agent/create",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": request.idempotency_key},
        )

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"skip": skip, "limit": limit}
        if search:
            params["search"] = search
        return await self._builder._client._arequest("GET", _BUILDER, "/agent/list", params=params)

    def iter_all(
        self, *, limit: int = 50, search: Optional[str] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        return iter_pages(
            lambda **kw: self.list(search=search, **kw),
            limit=limit,
            items_key="agents",
        )

    async def get(self, agent_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest("GET", _BUILDER, f"/agent/{agent_id}")

    async def update(self, agent_id: str, request: AgentUpdateRequest) -> Dict[str, Any]:
        body = request.model_dump(mode="json", exclude_none=True)
        return await self._builder._client._arequest(
            "PUT", _BUILDER, f"/agent/{agent_id}", json=body
        )

    async def delete(self, agent_id: str) -> AgentDeletionResult:
        payload = await self._builder._client._arequest(
            "DELETE", _BUILDER, f"/agent/{agent_id}"
        )
        return AgentDeletionResult.model_validate(payload)

    async def set_status(self, agent_id: str, *, enabled: bool) -> Dict[str, Any]:
        status = "enabled" if enabled else "disabled"
        return await self._builder._client._arequest(
            "PATCH", _BUILDER, f"/agent/{agent_id}/status", json={"status": status}
        )

    async def set_endpoint_status(
        self, agent_id: str, endpoint_name: str, *, enabled: bool
    ) -> Dict[str, Any]:
        status = "enabled" if enabled else "disabled"
        return await self._builder._client._arequest(
            "PATCH",
            _BUILDER,
            f"/agent/{agent_id}/endpoint/{endpoint_name}/status",
            json={"status": status},
        )

    async def revisions(self, agent_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "GET", _BUILDER, f"/agent/{agent_id}/revisions"
        )

    async def revision(self, agent_id: str, revision_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "GET", _BUILDER, f"/agent/{agent_id}/revisions/{revision_id}"
        )

class _DeployClient:
    def __init__(self, builder: BuilderClient) -> None:
        self._builder = builder

    async def build(self, agent_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST", _BUILDER, "/builder/", json={"agent_id": agent_id}
        )

    async def deploy(self, agent_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST", _BUILDER, "/deploy/", json={"agent_id": agent_id}
        )

    async def reconcile(self, agent_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST", _BUILDER, "/deploy/reconcile", json={"agent_id": agent_id}
        )


class _SecretsClient:
    def __init__(self, builder: BuilderClient) -> None:
        self._builder = builder

    async def vault_status(self) -> Dict[str, Any]:
        return await self._builder._client._arequest("GET", _BUILDER, "/vault/status")

    async def create_secret(
        self,
        name: str,
        value: str,
        *,
        description: Optional[str] = None,
        passphrase: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"name": name, "value": value}
        if description:
            body["description"] = description
        if passphrase:
            body["passphrase"] = passphrase
        return await self._builder._client._arequest("POST", _BUILDER, "/secret/create", json=body)

    async def list_secrets(
        self, *, skip: int = 0, limit: int = 50, search: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"skip": skip, "limit": limit}
        if search:
            params["search"] = search
        return await self._builder._client._arequest("GET", _BUILDER, "/secret/list", params=params)

    async def get_secret(
        self, secret_id: str, *, passphrase: Optional[str] = None
    ) -> Dict[str, Any]:
        params = {"passphrase": passphrase} if passphrase else None
        return await self._builder._client._arequest(
            "GET", _BUILDER, f"/secret/{secret_id}", params=params
        )

    async def update_secret(self, secret_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "PUT", _BUILDER, f"/secret/{secret_id}", json=body
        )

    async def delete_secret(self, secret_id: str, *, force: bool = False) -> Dict[str, Any]:
        params = {"force": "true"} if force else None
        return await self._builder._client._arequest(
            "DELETE", _BUILDER, f"/secret/{secret_id}", params=params
        )

    async def secret_usage(self, secret_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest("GET", _BUILDER, f"/secret/{secret_id}/usage")


class _MappingsClient:
    def __init__(self, builder: BuilderClient) -> None:
        self._builder = builder

    async def get(self, mapping_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest("GET", _BUILDER, f"/mappings/{mapping_id}")

    async def get_many(self, mapping_ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch up to 100 mappings by id (parallel GET /mappings/{id})."""
        unique = list({m.strip() for m in mapping_ids if m and str(m).strip()})[:100]
        if not unique:
            return []

        async def _fetch(mid: str) -> Optional[Dict[str, Any]]:
            try:
                doc = await self.get(mid)
            except Exception:
                return None
            if not isinstance(doc, dict):
                return None
            return {
                "mapping_id": doc.get("_id") or doc.get("id") or mid,
                "name": doc.get("name"),
                "mapping_type": doc.get("mapping_type"),
                "mapping": doc.get("mapping") or {},
            }

        results = await asyncio.gather(*(_fetch(mid) for mid in unique))
        return [doc for doc in results if doc]


class _McpClient:
    def __init__(self, builder: BuilderClient) -> None:
        self._builder = builder

    async def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._builder._client._arequest("POST", _BUILDER, "/mcp/servers", json=body)

    async def list(
        self, *, status: Optional[str] = None, skip: int = 0, limit: int = 50
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"skip": skip, "limit": limit}
        if status:
            params["status"] = status
        return await self._builder._client._arequest("GET", _BUILDER, "/mcp/servers", params=params)

    async def get(self, server_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest("GET", _BUILDER, f"/mcp/servers/{server_id}")

    async def update(self, server_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "PATCH", _BUILDER, f"/mcp/servers/{server_id}", json=body
        )

    async def delete(self, server_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "DELETE", _BUILDER, f"/mcp/servers/{server_id}"
        )

    async def start(self, server_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST", _BUILDER, f"/mcp/servers/{server_id}/start"
        )

    async def stop(self, server_id: str) -> Dict[str, Any]:
        return await self._builder._client._arequest(
            "POST", _BUILDER, f"/mcp/servers/{server_id}/stop"
        )


class BuilderClient:
    def __init__(self, client: ViksaClient) -> None:
        self._client = client
        self.agents = _AgentsClient(self)
        self.deploy = _DeployClient(self)
        self.secrets = _SecretsClient(self)
        self.mappings = _MappingsClient(self)
        self.mcp = _McpClient(self)
