from __future__ import annotations

import asyncio
from typing import Any

import pytest

from maeyr_platform.llm import (
    AuthLLMConfigurationResolver,
    CredentialSource,
    LLMAuthenticationError,
    LLMCapability,
    LLMConfigurationError,
    LLMProvider,
    LLMScope,
    LLMScopeType,
    ResolvedLLMConfiguration,
    UniversalLLMClient,
    normalize_provider_error,
    select_effective_configuration,
)


def _config(
    *,
    revision: int = 1,
    source: CredentialSource = CredentialSource.CUSTOMER,
    config_id: str = "LLM-test",
    source_scope: LLMScopeType | None = None,
    credential_source: CredentialSource | None = None,
) -> ResolvedLLMConfiguration:
    selected_source = credential_source or source
    selected_scope = source_scope or (
        LLMScopeType.ACCOUNT
        if selected_source is CredentialSource.CUSTOMER
        else LLMScopeType.PLATFORM
    )
    return ResolvedLLMConfiguration(
        config_id=config_id,
        revision=revision,
        provider=LLMProvider.OPENAI,
        source_scope=selected_scope,
        credential_source=selected_source,
        models={"chat": "gpt-test", "embeddings": "embed-test"},
        credentials={"api_key": "never-serialize-me"},
        credential_fingerprint="0123456789ab",
    )


def test_hierarchy_is_project_org_account_platform_and_skips_disabled() -> None:
    scope = LLMScope(account_id="AC-1234", org_id="OI-1234", project_id="PI-1234")
    selected, value = select_effective_configuration(
        scope,
        {
            "project": {"enabled": False, "name": "project"},
            "organization": {"enabled": True, "name": "org"},
            "account": {"enabled": True, "name": "account"},
        },
    )
    assert selected is LLMScopeType.ORGANIZATION
    assert value and value["name"] == "org"
    selected, value = select_effective_configuration(scope, {})
    assert selected is LLMScopeType.PLATFORM
    assert value is None


def test_customer_configuration_never_becomes_billable_or_serializes_secret() -> None:
    config = _config()
    assert config.billable_to_customer is False
    assert "never-serialize-me" not in config.model_dump_json()


def test_missing_customer_capability_fails_instead_of_platform_fallback() -> None:
    config = _config()
    with pytest.raises(LLMConfigurationError, match="transcription model"):
        config.model_for(LLMCapability.TRANSCRIPTION)


@pytest.mark.asyncio
async def test_client_pool_is_revision_aware_and_resolution_is_cached() -> None:
    resolutions = 0
    created: list[int] = []

    async def resolver(
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        nonlocal resolutions
        assert capability is LLMCapability.CHAT
        resolutions += 1
        return _config(revision=resolutions)

    def factory(config: ResolvedLLMConfiguration) -> dict[str, Any]:
        created.append(config.revision)
        return {"revision": config.revision}

    runtime = UniversalLLMClient(resolver, factory, resolution_ttl_seconds=60)
    scope = LLMScope(account_id="AC-1234")
    first = await runtime.for_scope(scope)
    second = await runtime.for_scope(scope)
    assert first.client is second.client
    assert resolutions == 1
    assert created == [1]
    await runtime.invalidate(scope)
    third = await runtime.for_scope(scope)
    assert third.client is not first.client
    assert third.configuration.revision == 2


@pytest.mark.asyncio
async def test_resolution_cache_is_capability_aware() -> None:
    resolved_capabilities: list[LLMCapability] = []

    async def resolver(
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        resolved_capabilities.append(capability)
        return _config()

    runtime = UniversalLLMClient(resolver, lambda config: object())
    scope = LLMScope(account_id="AC-1234")
    await runtime.for_scope(scope, LLMCapability.CHAT)
    await runtime.for_scope(scope, LLMCapability.CHAT)
    await runtime.for_scope(scope, LLMCapability.EMBEDDINGS)
    assert resolved_capabilities == [LLMCapability.CHAT, LLMCapability.EMBEDDINGS]


@pytest.mark.asyncio
async def test_resolution_cache_is_bounded_and_deduplicates_only_the_same_scope() -> None:
    started: list[str] = []
    release = asyncio.Event()
    two_started = asyncio.Event()

    async def resolver(
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        del capability
        started.append(scope.account_id)
        if len(started) == 2:
            two_started.set()
        await release.wait()
        return _config(config_id=scope.account_id)

    runtime = UniversalLLMClient(
        resolver,
        lambda config: object(),
        max_resolutions=2,
    )
    scopes = [LLMScope(account_id=f"AC-{index:04d}") for index in range(3)]
    first = asyncio.create_task(runtime.for_scope(scopes[0]))
    duplicate = asyncio.create_task(runtime.for_scope(scopes[0]))
    second = asyncio.create_task(runtime.for_scope(scopes[1]))
    await asyncio.wait_for(two_started.wait(), timeout=1)
    assert sorted(started) == ["AC-0000", "AC-0001"]
    release.set()
    await asyncio.gather(first, duplicate, second)
    await runtime.for_scope(scopes[2])
    assert len(runtime._resolution_cache) == 2
    assert scopes[0] not in {key[0] for key in runtime._resolution_cache}


@pytest.mark.asyncio
async def test_runtime_closes_owned_resolver_session() -> None:
    class Resolver:
        def __init__(self) -> None:
            self.closed = False

        async def __call__(
            self,
            scope: LLMScope,
            capability: LLMCapability,
        ) -> ResolvedLLMConfiguration:
            del scope, capability
            return _config()

        async def close(self) -> None:
            self.closed = True

    resolver = Resolver()
    runtime = UniversalLLMClient(resolver, lambda config: object())
    await runtime.close()
    assert resolver.closed is True


@pytest.mark.asyncio
async def test_borrowed_platform_client_is_never_closed_by_pool_eviction() -> None:
    class Client:
        def __init__(self) -> None:
            self.close_count = 0

        async def aclose(self) -> None:
            self.close_count += 1

    platform_client = Client()
    customer_client = Client()
    platform_config = _config(
        config_id="platform",
        source_scope=LLMScopeType.PLATFORM,
        credential_source=CredentialSource.PLATFORM,
    )
    customer_config = _config(config_id="customer")

    async def resolver(
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        del capability
        return platform_config if scope.account_id == "AC-platform" else customer_config

    def factory(config: ResolvedLLMConfiguration) -> Client:
        return (
            platform_client
            if config.credential_source is CredentialSource.PLATFORM
            else customer_client
        )

    runtime = UniversalLLMClient(
        resolver,
        factory,
        max_clients=1,
        borrowed_clients=(platform_client,),
    )
    await runtime.for_scope(LLMScope(account_id="AC-platform"))
    await runtime.for_scope(LLMScope(account_id="AC-customer"))
    assert platform_client.close_count == 0

    await runtime.close()
    assert platform_client.close_count == 0
    assert customer_client.close_count == 1


def test_authentication_error_is_clear_and_does_not_leak_provider_body() -> None:
    class AuthenticationError(Exception):
        status_code = 401

    error = normalize_provider_error(
        AuthenticationError("secret-key-was-invalid"),
        provider="openai",
        credential_source="customer",
        source_scope="project",
    )
    assert isinstance(error, LLMAuthenticationError)
    assert "project" in str(error)
    assert "secret-key" not in str(error)


def test_auth_resolver_uses_local_platform_credentials_only_for_platform_marker() -> None:
    platform = _config(
        config_id="platform",
        source_scope=LLMScopeType.PLATFORM,
        credential_source=CredentialSource.PLATFORM,
    )
    resolved = AuthLLMConfigurationResolver.parse_response(
        {"credential_source": "platform"}, lambda: platform
    )
    assert resolved is platform
    assert resolved.billable_to_customer is True


def test_auth_resolver_builds_non_billable_customer_configuration() -> None:
    resolved = AuthLLMConfigurationResolver.parse_response(
        {
            "credential_source": "customer",
            "source_scope": "project",
            "config_id": "LLMC-1",
            "revision": 7,
            "provider": "groq",
            "models": {"chat": "llama-test"},
            "connection": {"base_url": "https://api.groq.com/openai/v1"},
            "credentials": {"api_key": "customer-secret"},
            "credential_fingerprint": "customer-fingerprint",
        },
        lambda: _config(),
    )
    assert resolved.source_scope is LLMScopeType.PROJECT
    assert resolved.credential_source is CredentialSource.CUSTOMER
    assert resolved.billable_to_customer is False
    assert resolved.credentials["api_key"] == "customer-secret"
