"""Bounded, revision-aware universal client lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from viksa_platform.llm.errors import normalize_provider_error
from viksa_platform.llm.models import LLMCapability, LLMScope, ResolvedLLMConfiguration

ClientT = TypeVar("ClientT")


class LLMConfigurationResolver(Protocol):
    async def __call__(
        self,
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration: ...


@dataclass(frozen=True)
class ResolvedClient(Generic[ClientT]):
    client: ClientT
    configuration: ResolvedLLMConfiguration
    model: str

    async def call(
        self,
        operation: Callable[..., Awaitable[Any]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute one SDK operation with stable, secret-free error semantics."""

        try:
            return await operation(*args, **kwargs)
        except BaseException as exc:
            raise normalize_provider_error(
                exc,
                provider=self.configuration.provider.value,
                credential_source=self.configuration.credential_source.value,
                source_scope=self.configuration.source_scope.value,
            ) from exc


class UniversalLLMClient(Generic[ClientT]):
    """Resolve once per TTL and reuse a bounded number of provider clients."""

    def __init__(
        self,
        resolver: LLMConfigurationResolver,
        client_factory: Callable[[ResolvedLLMConfiguration], ClientT | Awaitable[ClientT]],
        *,
        resolution_ttl_seconds: float = 15.0,
        max_resolutions: int = 1024,
        max_clients: int = 64,
        borrowed_clients: tuple[ClientT, ...] = (),
    ) -> None:
        if resolution_ttl_seconds <= 0:
            raise ValueError("resolution_ttl_seconds must be positive")
        if max_clients <= 0:
            raise ValueError("max_clients must be positive")
        if max_resolutions <= 0:
            raise ValueError("max_resolutions must be positive")
        self._resolver = resolver
        self._client_factory = client_factory
        self._ttl = resolution_ttl_seconds
        self._max_resolutions = max_resolutions
        self._max_clients = max_clients
        self._borrowed_clients = borrowed_clients
        self._resolution_cache: OrderedDict[
            tuple[LLMScope, LLMCapability], tuple[float, ResolvedLLMConfiguration]
        ] = OrderedDict()
        self._resolution_inflight: dict[
            tuple[LLMScope, LLMCapability], asyncio.Task[ResolvedLLMConfiguration]
        ] = {}
        self._clients: OrderedDict[tuple[str, int, str], ClientT] = OrderedDict()
        self._resolution_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()

    async def for_scope(
        self,
        scope: LLMScope,
        capability: LLMCapability | str = LLMCapability.CHAT,
    ) -> ResolvedClient[ClientT]:
        selected_capability = (
            capability
            if isinstance(capability, LLMCapability)
            else LLMCapability(str(capability))
        )
        config = await self._resolve(scope, selected_capability)
        model = config.model_for(selected_capability)
        client = await self._client(config)
        return ResolvedClient(client=client, configuration=config, model=model)

    async def invalidate(self, scope: LLMScope | None = None) -> None:
        async with self._resolution_lock:
            if scope is None:
                self._resolution_cache.clear()
            else:
                for key in tuple(self._resolution_cache):
                    if key[0] == scope:
                        self._resolution_cache.pop(key, None)

    async def close(self) -> None:
        async with self._resolution_lock:
            inflight = list(self._resolution_inflight.values())
            self._resolution_inflight.clear()
            self._resolution_cache.clear()
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        async with self._client_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await self._dispose_client(client)
        resolver_close = getattr(self._resolver, "close", None)
        if resolver_close is not None:
            result = resolver_close()
            if inspect.isawaitable(result):
                await result

    async def _resolve(
        self,
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        key = (scope, capability)
        now = time.monotonic()
        cached = self._resolution_cache.get(key)
        if cached and cached[0] > now:
            self._resolution_cache.move_to_end(key)
            return cached[1]
        async with self._resolution_lock:
            now = time.monotonic()
            cached = self._resolution_cache.get(key)
            if cached and cached[0] > now:
                self._resolution_cache.move_to_end(key)
                return cached[1]
            self._resolution_cache.pop(key, None)
            task = self._resolution_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._resolve_and_cache(key, scope, capability)
                )
                self._resolution_inflight[key] = task
        # A cancelled HTTP request must not cancel the shared resolution that
        # other requests for this tenant are already awaiting.
        return await asyncio.shield(task)

    async def _resolve_and_cache(
        self,
        key: tuple[LLMScope, LLMCapability],
        scope: LLMScope,
        capability: LLMCapability,
    ) -> ResolvedLLMConfiguration:
        task = asyncio.current_task()
        try:
            resolved = await self._resolver(scope, capability)
            async with self._resolution_lock:
                self._resolution_cache[key] = (time.monotonic() + self._ttl, resolved)
                self._resolution_cache.move_to_end(key)
                while len(self._resolution_cache) > self._max_resolutions:
                    self._resolution_cache.popitem(last=False)
            return resolved
        finally:
            async with self._resolution_lock:
                if self._resolution_inflight.get(key) is task:
                    self._resolution_inflight.pop(key, None)

    async def _client(self, config: ResolvedLLMConfiguration) -> ClientT:
        key = config.client_cache_key
        client = self._clients.get(key)
        if client is not None:
            self._clients.move_to_end(key)
            return client
        async with self._client_lock:
            client = self._clients.get(key)
            if client is not None:
                self._clients.move_to_end(key)
                return client
            created = self._client_factory(config)
            client = await created if inspect.isawaitable(created) else created
            self._clients[key] = client
            while len(self._clients) > self._max_clients:
                _, evicted = self._clients.popitem(last=False)
                await self._dispose_client(evicted)
            return client

    async def _dispose_client(self, client: ClientT) -> None:
        # Platform clients are owned by the service that injected them. The
        # tenant runtime may cache and evict those references, but must never
        # close a shared client that is still serving other requests.
        if any(client is borrowed for borrowed in self._borrowed_clients):
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result
