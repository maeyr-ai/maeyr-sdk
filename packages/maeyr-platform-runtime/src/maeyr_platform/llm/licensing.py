"""A single funding boundary for every Maeyr-funded provider call.

The provider's existing immutable usage ledger remains the invoice evidence.
Auth owns turn allowances, model-call bounds, and authorized spending holds.
Unknown provider usage leaves a hold pending reconciliation, never a refund.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Callable
from typing import Any, TypedDict

from fastapi import HTTPException

from maeyr_platform.llm.models import LLMScope
from maeyr_platform.metrics.context import get_usage_context
from maeyr_platform.usage_limits import UsageLimitClient

_funding_event: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "maeyr_llm_funding_event", default=None
)


class _TenantScope(TypedDict):
    org_id: str | None
    project_id: str | None


class _ObservedTokens(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


def current_funding_event() -> dict[str, Any] | None:
    value = _funding_event.get()
    return dict(value) if value is not None else None


def clear_funding_event() -> None:
    _funding_event.set(None)


async def reconcile_funding_event(authority: UsageLimitClient, event: dict[str, Any]) -> None:
    """Replay actual token settlement after the durable usage ledger accepts it.

    Producers invoke this before acknowledging their existing durable outbox.
    A transport failure keeps the event pending; duplicate settlements are safe.
    Customer credentials and estimated usage never create a token invoice.
    """
    funding = (event.get("metadata") or {}).get("license_funding")
    if not isinstance(funding, dict) or funding.get("funding") not in {"included", "tokens"}:
        return
    if event.get("credential_source") != "platform" or event.get("usage_status") not in {
        "observed",
        "reconciled",
    }:
        return
    usage = _observed_usage(
        {
            "usage": {
                "prompt_tokens": event.get("prompt_tokens"),
                "completion_tokens": event.get("completion_tokens"),
                "prompt_tokens_details": {
                    "cached_tokens": (event.get("token_details") or {}).get(
                        "cached_input_tokens",
                        (event.get("token_details") or {}).get("cached_tokens", 0),
                    )
                },
            }
        }
    )
    if usage is None:
        return
    await authority.settle_usage(
        str(event["account_id"]),
        "ai_token_spend",
        str(funding["operation_id"]),
        org_id=event.get("org_id"),
        project_id=event.get("project_id"),
        model_id=funding["model_id"],
        rate_card_version=funding["rate_card_version"],
        provider_request_id=event.get("provider_request_id"),
        **usage,
    )


def current_ai_turn_id() -> str:
    context = get_usage_context()
    if context is not None:
        identity = (
            (context.metadata or {}).get("license_turn_id")
            or context.activity_id
            or context.sub_resource_id
        )
        if identity:
            return str(identity)
    from maeyr_platform.tracing.context import get_trace_context

    trace = get_trace_context()
    if trace is not None and trace.activity_id:
        return str(trace.activity_id)
    raise HTTPException(503, "A model call requires a stable usage activity")


def _token_input_bound(kwargs: dict[str, Any]) -> int:
    """Conservative UTF-8 bound for text/tool input, including framing.

    Each text BPE token consumes at least one byte. Framing and tool overhead
    are reserved separately. URL lengths cannot bound image/audio tokens, so
    multimodal platform spend requires a separately priced capability gate.
    """
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(422, "Managed AI requires bounded text messages")
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(422, "Managed AI messages are invalid")
        content = message.get("content")
        if isinstance(content, list) and any(
            not isinstance(part, dict) or part.get("type") != "text" for part in content
        ):
            raise HTTPException(403, "Use customer credentials for this AI capability")
    input_fields = {
        key: value
        for key, value in kwargs.items()
        if key in {"messages", "tools", "tool_choice", "response_format", "functions"}
    }
    try:
        size = len(json.dumps(input_fields, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Managed AI input cannot be bounded") from exc
    tool_count = len(kwargs.get("tools") or kwargs.get("functions") or [])
    return size + 512 * (len(messages) + tool_count) + 1024


def _observed_usage(response: Any) -> _ObservedTokens | None:
    usage = getattr(response, "usage", None)
    if isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def read(source: Any, key: str, default: Any = None) -> Any:
        return (
            source.get(key, default) if isinstance(source, dict) else getattr(source, key, default)
        )

    prompt = read(usage, "prompt_tokens")
    output = read(usage, "completion_tokens")
    if type(prompt) is not int or prompt < 0 or type(output) is not int or output < 0:
        return None
    details = read(usage, "prompt_tokens_details")
    cached = read(details, "cached_tokens", 0) if details is not None else 0
    if type(cached) is not int or cached < 0 or cached > prompt:
        return None
    return {"input_tokens": prompt, "output_tokens": output, "cached_input_tokens": cached}


class LicensedProviderClient:
    """Proxy an SDK client without caching a tenant's funding decision.

    Resolution/client caches may be warm across an expiry or a revoked budget.
    Authorization therefore happens immediately before each physical call.
    BYOK clients are never wrapped and never consume funded-turn allowances.
    """

    def __init__(
        self,
        client: Any,
        *,
        authority: UsageLimitClient,
        scope: LLMScope,
        turn_identity: Callable[[], str] = current_ai_turn_id,
        path: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._authority = authority
        self._scope = scope
        self._turn_identity = turn_identity
        self._path = path

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._client, name)
        path = (*self._path, name)
        if callable(value):
            if path[-3:] == ("chat", "completions", "create"):

                async def licensed_call(*args: Any, **kwargs: Any) -> Any:
                    if args:
                        raise HTTPException(422, "Managed AI requires explicit request bounds")
                    return await self._call(value, kwargs)

                return licensed_call
            if name in {"create", "generate", "edit", "stream", "with_streaming_response"}:
                # Image, speech, embeddings, and alternative raw SDK entrypoints
                # cannot draw unpriced units from a funded chat allowance.
                async def unsupported(*_args: Any, **_kwargs: Any) -> Any:
                    raise HTTPException(403, "Use customer credentials for this AI capability")

                return unsupported
            if name in {"close", "aclose"}:
                return value
        if name in {
            "chat",
            "completions",
            "responses",
            "images",
            "audio",
            "embeddings",
            "speech",
            "transcriptions",
            "translations",
            "with_raw_response",
            "with_streaming_response",
        }:
            return LicensedProviderClient(
                value,
                authority=self._authority,
                scope=self._scope,
                turn_identity=self._turn_identity,
                path=path,
            )
        return value

    async def _call(self, call: Any, kwargs: dict[str, Any]) -> Any:
        clear_funding_event()
        turn_id = self._turn_identity()
        if not turn_id:
            raise HTTPException(503, "A model call requires a stable usage activity")
        turn_id = "ai-turn:" + hashlib.sha256(turn_id.encode()).hexdigest()
        operation_id = "ai-call:" + secrets.token_hex(24)
        model = str(kwargs.get("model") or "")
        output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
        if not model or type(output) is not int or output < 1:
            raise HTTPException(422, "Managed AI requires a model and an output token bound")
        # n>1 can multiply paid output behind an otherwise valid single bound.
        if kwargs.get("n", 1) != 1:
            raise HTTPException(422, "Managed AI supports one completion per call")
        if kwargs.get("stream"):
            stream_options = kwargs.get("stream_options") or {}
            if not isinstance(stream_options, dict):
                raise HTTPException(422, "Managed AI streaming options are invalid")
            # Request provider actuals even if an upstream caller forgot the
            # option; estimates cannot settle a funded monetary reservation.
            kwargs["stream_options"] = {**stream_options, "include_usage": True}
        authorization = await self._authority.authorize_ai_call(
            self._scope.account_id,
            org_id=self._scope.org_id,
            project_id=self._scope.project_id,
            operation_id=operation_id,
            turn_operation_id=turn_id,
            model_id=model,
            input_tokens=_token_input_bound(kwargs),
            max_output_tokens=output,
        )
        if authorization.get("authorized") is not True or authorization.get("funding") not in {
            "included",
            "tokens",
        }:
            raise HTTPException(503, "Managed AI authorization is invalid")
        output_cap = authorization.get("max_output_tokens")
        if type(output_cap) is not int or output_cap < 1 or output_cap > output:
            raise HTTPException(503, "Managed AI output allowance is invalid")
        kwargs["max_completion_tokens" if "max_completion_tokens" in kwargs else "max_tokens"] = (
            output_cap
        )
        _funding_event.set(
            {
                "operation_id": operation_id,
                "turn_operation_id": turn_id,
                "funding": authorization["funding"],
                "model_id": model,
                "rate_card_version": authorization.get("rate_card_version"),
            }
        )
        scope_args: _TenantScope = {
            "org_id": self._scope.org_id, "project_id": self._scope.project_id
        }

        async def settle(response: Any) -> None:
            usage = _observed_usage(response)
            if usage is None:
                # The existing provider ledger records unavailable/estimated
                # usage. Keep this hold for later provider reconciliation.
                return
            try:
                await self._authority.settle_usage(
                    self._scope.account_id,
                    "ai_token_spend",
                    operation_id,
                    **scope_args,
                    model_id=model,
                    rate_card_version=authorization.get("rate_card_version"),
                    **usage,
                    provider_request_id=getattr(response, "id", None),
                )
            except HTTPException:
                # A failed settlement must not discard the successful provider
                # response before the producer's durable ledger can accept it.
                # Its reserved budget stays unavailable pending reconciliation.
                self._authority._logger.warning("AI settlement retained for reconciliation")

        response = await call(**kwargs)
        if kwargs.get("stream"):
            return _LicensedStream(response, settle)
        await settle(response)
        return response


class _LicensedStream:
    def __init__(self, stream: Any, settle: Callable[[Any], Any]) -> None:
        self._stream = stream
        self._iterator: AsyncIterator[Any] = stream.__aiter__()
        self._settle = settle
        self._usage: Any = None
        self._finished = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __aiter__(self) -> _LicensedStream:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            await self._finish()
            raise
        if _observed_usage(chunk) is not None:
            self._usage = chunk
        return chunk

    async def _finish(self) -> None:
        if not self._finished:
            self._finished = True
            if self._usage is not None:
                task = asyncio.create_task(self._settle(self._usage))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise

    async def close(self) -> None:
        try:
            await self._stream.close()
        finally:
            await self._finish()

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self) -> _LicensedStream:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()
