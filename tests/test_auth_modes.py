from unittest.mock import patch

import httpx
import pytest

from maeyr._constants import (
    ENV_ACCESS_TOKEN,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_EMAIL,
    ENV_PASSWORD,
)
from maeyr.client import MaeyrClient
from maeyr.models.auth import KeyValidationResponse, TokenResponse


def test_from_api_key_sets_bearer_and_kind():
    client = MaeyrClient.from_api_key("vk_test_key", base_url="https://api.example.com")
    assert client.auth_kind == "api_key"
    assert client.api_key == "vk_test_key"
    assert client.base_url == "https://api.example.com"
    assert client._transport.headers["Authorization"] == "Bearer vk_test_key"
    assert client.refresh_token is None
    client.close()


def test_from_api_key_validate_fills_tenant():
    with patch(
        "maeyr.client.auth.AuthClient.validate_api_key_sync",
        return_value=KeyValidationResponse(
            valid=True,
            org_id="org-1",
            project_id="proj-1",
        ),
    ):
        client = MaeyrClient.from_api_key(
            "vk_test_key",
            base_url="https://api.test",
            validate=True,
        )
    assert client.org_id == "org-1"
    assert client.project_id == "proj-1"
    client.close()


def test_from_api_key_validate_invalid():
    with patch(
        "maeyr.client.auth.AuthClient.validate_api_key_sync",
        return_value=KeyValidationResponse(valid=False, error="revoked"),
    ):
        with pytest.raises(ValueError, match="revoked"):
            MaeyrClient.from_api_key("bad", validate=True)


@pytest.mark.asyncio
async def test_from_login():
    tokens = TokenResponse(
        access_token="jwt-1",
        refresh_token="ref-1",
        expires_in=3600,
        user_id="u1",
        account_id="acc",
        org_id="o1",
        project_id="p1",
    )

    async def fake_login(self, email: str, password: str) -> TokenResponse:
        self._apply_tokens(tokens)
        return tokens

    with patch("maeyr.client.auth.AuthClient.login", new=fake_login):
        client = await MaeyrClient.from_login(
            "user@example.com",
            "secret",
            base_url="https://api.test",
        )
    assert client.access_token == "jwt-1"
    assert client.org_id == "o1"
    await client.aclose()


def test_from_login_sync():
    tokens = TokenResponse(
        access_token="jwt-1",
        refresh_token="ref-1",
        expires_in=3600,
        user_id="u1",
        account_id="acc",
    )

    def fake_login(self, email: str, password: str) -> TokenResponse:
        self._apply_tokens(tokens)
        return tokens

    with patch("maeyr.client.auth.AuthClient.login_sync", new=fake_login):
        client = MaeyrClient.from_login_sync("a@b.com", "pw", base_url="https://api.test")
    assert client.access_token == "jwt-1"
    client.close()


def test_login_omits_empty_bearer_header():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/individual/login"):
            assert request.headers.get("Authorization") is None
            return httpx.Response(
                200,
                json={
                    "access_token": "jwt-1",
                    "refresh_token": "ref-1",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "user_id": "u1",
                    "account_id": "acc",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.test") as http:
        client = MaeyrClient(access_token="", base_url="https://api.test")
        client._transport._sync_client = http
        client.auth.login_sync("a@b.com", "pw")
        assert client._transport.headers["Authorization"] == "Bearer jwt-1"
    client.close()


def test_from_env_api_key(monkeypatch):
    monkeypatch.delenv(ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(ENV_EMAIL, raising=False)
    monkeypatch.setenv(ENV_API_KEY, "key-from-env")
    monkeypatch.setenv(ENV_BASE_URL, "https://staging.api.test")
    client = MaeyrClient.from_env()
    assert client.auth_kind == "api_key"
    assert client.access_token == "key-from-env"
    assert client.base_url == "https://staging.api.test"
    client.close()


def test_from_env_access_token(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_ACCESS_TOKEN, "jwt-env")
    client = MaeyrClient.from_env()
    assert client.auth_kind == "access_token"
    assert client.access_token == "jwt-env"
    client.close()


def test_from_env_login(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.setenv(ENV_EMAIL, "a@b.com")
    monkeypatch.setenv(ENV_PASSWORD, "pw")
    tokens = TokenResponse(
        access_token="jwt-login",
        refresh_token="ref",
        expires_in=3600,
        user_id="u1",
        account_id="acc",
    )

    def fake_login(self, email: str, password: str) -> TokenResponse:
        self._apply_tokens(tokens)
        return tokens

    with patch("maeyr.client.auth.AuthClient.login_sync", new=fake_login):
        client = MaeyrClient.from_env()
    assert client.access_token == "jwt-login"
    client.close()


def test_from_env_requires_credential(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.delenv(ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(ENV_EMAIL, raising=False)
    monkeypatch.delenv(ENV_PASSWORD, raising=False)
    with pytest.raises(ValueError, match="MAEYR_API_KEY"):
        MaeyrClient.from_env()
