import httpx
import pytest

from viksa_ai.client import ViksaClient
from viksa_ai.client.errors import ViksaApiError


@pytest.mark.asyncio
async def test_auth_me_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json={"id": "u1", "email": "a@b.com", "account_id": "acc"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", org_id="o1", project_id="p1", base_url="https://api.test")
        client._async_client = http
        user = await client.auth.me()
        assert user.email == "a@b.com"


@pytest.mark.asyncio
async def test_api_error_on_404():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(404, json={"detail": "not found"})
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        client = ViksaClient("token", base_url="https://api.test")
        client._async_client = http
        with pytest.raises(ViksaApiError) as exc:
            await client.builder.agents.get("missing")
        assert exc.value.status_code == 404
