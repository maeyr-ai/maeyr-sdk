"""Canonical non-blocking remote span sink for trace-service."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from viksa_platform.security.internal_key_guard import assert_production_internal_key
from viksa_platform.security.internal_request_signing import sign_internal_request

from .context import get_trace_context
from .errors import attach_error_to_span_kwargs
from .ids import (
    generate_span_id,
    generate_trace_id,
    normalize_parent_span_id,
    normalize_span_id,
    normalize_trace_id,
)
from .tenant import valid_span_tenant_scope, valid_tenant_id

logger = logging.getLogger(__name__)

_recorders: dict[str, RemoteTraceRecorder] = {}


def _trace_service_url() -> str:
    return (os.getenv("TRACE_SERVICE_URL") or "http://trace-service:8000").rstrip("/")


def _trace_internal_key() -> str:
    return os.getenv("TRACE_INTERNAL_KEY") or ""


def _production_environment() -> bool:
    return any(
        str(os.getenv(name) or "").strip().lower() in {"prod", "production"}
        for name in ("APP_ENVIRONMENT", "ENVIRON", "ENV")
    )


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_int_with_legacy(
    name: str,
    legacy_name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Read the canonical setting while preserving the pre-runtime SDK name."""
    selected = name if os.getenv(name) is not None else legacy_name
    return _bounded_int_env(selected, default, minimum=minimum, maximum=maximum)


_QUEUE_SPANS = _bounded_int_with_legacy(
    "TRACE_REMOTE_QUEUE_SPANS",
    "TRACE_REMOTE_QUEUE_MAX_SPANS",
    10_000,
    minimum=1,
    maximum=100_000,
)
_BATCH_SPANS = _bounded_int_with_legacy(
    "TRACE_REMOTE_BATCH_SPANS",
    "TRACE_REMOTE_BATCH_SIZE",
    100,
    minimum=1,
    maximum=1_000,
)
_MAX_RETRIES = _bounded_int_with_legacy(
    "TRACE_REMOTE_MAX_RETRIES",
    "TRACE_REMOTE_RETRY_ATTEMPTS",
    3,
    minimum=1,
    maximum=10,
)
_MAX_CONNECTIONS = _bounded_int_env(
    "TRACE_REMOTE_MAX_CONNECTIONS",
    4,
    minimum=1,
    maximum=32,
)
_MAX_KEEPALIVE_CONNECTIONS = min(
    _MAX_CONNECTIONS,
    _bounded_int_env(
        "TRACE_REMOTE_MAX_KEEPALIVE_CONNECTIONS",
        2,
        minimum=0,
        maximum=32,
    ),
)
_RETRY_BASE_SECONDS = _bounded_float_env(
    "TRACE_REMOTE_RETRY_BACKOFF_SECONDS",
    0.2,
    minimum=0.0,
    maximum=2.0,
)
_ACK_HTTP_TIMEOUT_SECONDS = _bounded_float_env(
    "TRACE_ACK_HTTP_TIMEOUT_SECONDS",
    0.25,
    minimum=0.01,
    maximum=1.0,
)
_ACK_HTTP_CIRCUIT_SECONDS = _bounded_float_env(
    "TRACE_ACK_HTTP_CIRCUIT_SECONDS",
    5.0,
    minimum=0.1,
    maximum=60.0,
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _serialize_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_json_safe(span) for span in spans]


def assert_trace_producer_configuration(service: str) -> None:
    """Reject a missing Trace endpoint or weak Trace key before production traffic."""
    assert_production_internal_key(
        _trace_internal_key(),
        env_name="TRACE_INTERNAL_KEY",
        service_name=service,
        minimum_bytes=32,
    )
    configured_url = str(os.getenv("TRACE_SERVICE_URL") or "").strip()
    if _production_environment() and not configured_url:
        raise RuntimeError(f"{service}: TRACE_SERVICE_URL must be set in production.")
    parsed = urlsplit(configured_url or _trace_service_url())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            f"{service}: TRACE_SERVICE_URL must be an absolute HTTP(S) URL "
            "without credentials, query, or fragment."
        )


class RemoteTraceRecorder:
    __slots__ = (
        "_base_url",
        "_ack_http_disabled_until",
        "_ack_http_timeout_seconds",
        "_batch_spans",
        "_client",
        "_closing",
        "_drain_task",
        "_durably_queued_spans",
        "_dropped_spans",
        "_enrich",
        "_failed_spans",
        "_key",
        "_max_retries",
        "_queue",
        "_sent_spans",
        "service",
    )

    def __init__(
        self,
        service: str,
        *,
        enrich: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        base_url: str | None = None,
        internal_key: str | None = None,
        max_queue_spans: int = _QUEUE_SPANS,
        batch_spans: int = _BATCH_SPANS,
        max_retries: int = _MAX_RETRIES,
        ack_http_timeout_seconds: float = _ACK_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.service = service
        self._base_url = (base_url or _trace_service_url()).rstrip("/")
        self._ack_http_disabled_until = 0.0
        self._ack_http_timeout_seconds = max(
            0.01,
            min(1.0, float(ack_http_timeout_seconds)),
        )
        self._key = internal_key or _trace_internal_key()
        self._enrich = enrich
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, min(100_000, int(max_queue_spans)))
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._client: Any | None = None
        self._closing = False
        self._durably_queued_spans = 0
        self._dropped_spans = 0
        self._failed_spans = 0
        self._sent_spans = 0
        self._batch_spans = max(1, min(1_000, int(batch_spans)))
        self._max_retries = max(1, min(10, int(max_retries)))

    @property
    def pending_spans(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_spans(self) -> int:
        return self._dropped_spans

    @property
    def sent_spans(self) -> int:
        return self._sent_spans

    @property
    def durably_queued_spans(self) -> int:
        return self._durably_queued_spans

    @property
    def delivery_stats(self) -> dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "sent": self._sent_spans,
            "failed": self._failed_spans,
            "dropped": self._dropped_spans,
        }

    def start(self) -> None:
        self._closing = False

    def _ensure_drain_task(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            dropped = self._queue.qsize()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._dropped_spans += dropped
            logger.warning(
                "Remote trace spans dropped service=%s count=%s reason=no_event_loop",
                self.service,
                dropped,
            )
            return
        self._drain_task = loop.create_task(
            self._drain(),
            name=f"trace_drain_{self.service}",
        )

    def _ensure_drain(self) -> None:
        """Compatibility name retained for callers of the original runtime SDK."""
        self._ensure_drain_task()

    def schedule_push(self, spans: list[dict[str, Any]]) -> None:
        if not spans:
            return
        self._key = _trace_internal_key() or self._key
        if not self._key:
            self._dropped_spans += len(spans)
            logger.warning(
                "Skipping remote trace push for %s: TRACE_INTERNAL_KEY is not configured",
                self.service,
            )
            return
        if self._closing:
            self._dropped_spans += len(spans)
            return

        safe = _serialize_spans(spans)
        accepted = 0
        invalid = 0
        overflow = 0
        for span in safe:
            if not valid_span_tenant_scope(span):
                self._dropped_spans += 1
                invalid += 1
                continue
            try:
                self._queue.put_nowait(span)
                accepted += 1
            except asyncio.QueueFull:
                self._dropped_spans += 1
                overflow += 1
        if accepted:
            self._ensure_drain_task()
        if invalid:
            logger.warning(
                "Remote trace spans dropped service=%s count=%s "
                "reason=invalid_tenant_scope dropped_total=%s",
                self.service,
                invalid,
                self._dropped_spans,
            )
        if overflow:
            logger.warning(
                "Remote trace queue full service=%s count=%s dropped_total=%s",
                self.service,
                overflow,
                self._dropped_spans,
            )

    def schedule_record(self, **kwargs: Any) -> None:
        doc = self.build_span_doc(**kwargs)
        if doc:
            self.schedule_push([doc])
        else:
            logger.warning(
                "Dropped %s span: invalid tenant scope",
                self.service,
            )

    def build_span_doc(self, **kwargs: Any) -> dict[str, Any] | None:
        span_kwargs = dict(kwargs)
        attach_error_to_span_kwargs(span_kwargs)
        ctx = get_trace_context()
        now = datetime.now(timezone.utc)
        account_id = span_kwargs.get("account_id") or (ctx.account_id if ctx else None)
        if not account_id:
            account_id = span_kwargs.get("namespace") or "unknown"

        is_root = bool(span_kwargs.get("is_root", False))
        if span_kwargs.get("span_id"):
            sid = normalize_span_id(str(span_kwargs["span_id"]))
        elif is_root:
            sid = normalize_span_id(generate_span_id())
        else:
            sid = normalize_span_id(str((ctx.span_id if ctx else "") or generate_span_id()))
        tid = normalize_trace_id(
            str(span_kwargs.get("trace_id") or (ctx.trace_id if ctx else "") or generate_trace_id())
        )

        status = span_kwargs.get("status", "ok")
        is_completion = bool(span_kwargs.get("_is_completion"))
        is_running_start = status == "running" and not is_completion
        started = span_kwargs.get("started_at") or now

        parent_span_id = span_kwargs.get("parent_span_id")
        if is_root:
            parent_span_id = None
        elif parent_span_id is None and ctx:
            raw_parent = ctx.span_id if ctx.span_id != sid else ctx.parent_span_id
            parent_span_id = normalize_parent_span_id(raw_parent)
        else:
            parent_span_id = normalize_parent_span_id(parent_span_id)

        doc: dict[str, Any] = {
            "_id": sid,
            "span_id": sid,
            "trace_id": tid,
            "parent_span_id": parent_span_id,
            "activity_id": span_kwargs.get("activity_id") or (ctx.activity_id if ctx else None),
            "account_id": account_id,
            "org_id": span_kwargs.get("org_id") or (ctx.org_id if ctx else "") or "",
            "project_id": span_kwargs.get("project_id") or (ctx.project_id if ctx else "") or "",
            "user_id": span_kwargs.get("user_id") or (ctx.user_id if ctx else None),
            "user_email": span_kwargs.get("user_email") or (ctx.user_email if ctx else None),
            "service": span_kwargs.get("service") or self.service,
            "span_kind": span_kwargs.get("span_kind", "internal"),
            "span_name": span_kwargs.get("span_name", "internal"),
            "operation": span_kwargs.get("operation"),
            "status": status,
            "started_at": started,
            "duration_ms": 0 if is_running_start else int(span_kwargs.get("duration_ms") or 0),
            "resource_refs": span_kwargs.get("resource_refs")
            or (ctx.resource_refs if ctx else None),
            "attributes": span_kwargs.get("attributes"),
            "labels": span_kwargs.get("labels"),
            "entity_type": span_kwargs.get("entity_type") or (ctx.entity_type if ctx else None),
            "entity_id": span_kwargs.get("entity_id") or (ctx.entity_id if ctx else None),
            "is_root": is_root,
            "model": span_kwargs.get("model"),
            "prompt_tokens": span_kwargs.get("prompt_tokens"),
            "completion_tokens": span_kwargs.get("completion_tokens"),
            "tokens_used": span_kwargs.get("tokens_used"),
            "cost_usd": span_kwargs.get("cost_usd"),
            "date_bucket": now.strftime("%Y-%m-%d"),
        }
        if not is_running_start:
            doc["ended_at"] = span_kwargs.get("ended_at") or now
        if is_completion:
            doc["_is_completion"] = True
        if self._enrich:
            doc = self._enrich(doc)
        if not valid_span_tenant_scope(doc):
            return None
        return doc

    async def record_span(self, **kwargs: Any) -> None:
        self.schedule_record(**kwargs)

    async def push_spans(self, spans: list[dict[str, Any]]) -> None:
        self.schedule_push(spans)

    async def push_spans_acknowledged(self, spans: list[dict[str, Any]]) -> bool:
        """Await trace-service acknowledgement for critical lifecycle events.

        Redis is the primary acknowledgement boundary, so trace-service HTTP
        latency never sits on the healthy request path. If Redis is unavailable,
        one short circuit-broken HTTP attempt is allowed. The transport may
        acknowledge a configured external producer outbox (for example Mongo)
        before any process-local retry is used.
        """
        if not spans:
            return True
        self._key = _trace_internal_key() or self._key
        if not self._key or self._closing:
            self._dropped_spans += len(spans)
            return False
        safe = _serialize_spans(spans)
        if any(not valid_span_tenant_scope(span) for span in safe):
            self._dropped_spans += len(safe)
            return False
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for span in safe:
            tenant = (
                str(span.get("account_id") or ""),
                str(span.get("org_id") or ""),
                str(span.get("project_id") or ""),
            )
            grouped.setdefault(tenant, []).append(span)
        acknowledged = True
        for tenant_batch in grouped.values():
            unresolved = await self._enqueue_durable_fallback(tenant_batch)
            if not unresolved:
                continue

            delivered = False
            loop = asyncio.get_running_loop()
            if loop.time() >= self._ack_http_disabled_until:
                try:
                    delivered, _retryable = await asyncio.wait_for(
                        self._post_once(unresolved),
                        timeout=self._ack_http_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    delivered = False
                if delivered:
                    self._ack_http_disabled_until = 0.0
                    self._sent_spans += len(unresolved)
                    continue
                self._ack_http_disabled_until = loop.time() + _ACK_HTTP_CIRCUIT_SECONDS

            acknowledged = False
            self._failed_spans += len(unresolved)
            self.schedule_push(unresolved)
        return acknowledged

    async def _enqueue_durable_fallback(
        self,
        spans: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist lifecycle events to the shared Redis trace outbox.

        Returning only unresolved documents lets callers distinguish durable
        acceptance from a process-local retry. The trace-service consumes this
        queue idempotently by span id, so an uncertain HTTP response followed by
        a Redis write cannot inflate the canonical trace or token totals.
        """
        from .transport import enqueue_span

        unresolved: list[dict[str, Any]] = []
        for span in spans:
            if await enqueue_span(span):
                self._durably_queued_spans += 1
            else:
                unresolved.append(span)
        if len(unresolved) != len(spans):
            logger.warning(
                "Remote trace HTTP delivery deferred to durable outbox "
                "service=%s queued=%s unresolved=%s",
                self.service,
                len(spans) - len(unresolved),
                len(unresolved),
            )
        return unresolved

    async def record_span_acknowledged(self, **kwargs: Any) -> bool:
        doc = self.build_span_doc(**kwargs)
        if not doc:
            self._dropped_spans += 1
            return False
        return await self.push_spans_acknowledged([doc])

    @property
    def _batch_size(self) -> int:
        """Compatibility attribute for the original runtime implementation."""
        return self._batch_spans

    @_batch_size.setter
    def _batch_size(self, value: int) -> None:
        self._batch_spans = max(1, min(1_000, int(value)))

    @property
    def _retry_attempts(self) -> int:
        """Compatibility attribute for the original runtime implementation."""
        return self._max_retries

    @_retry_attempts.setter
    def _retry_attempts(self, value: int) -> None:
        self._max_retries = max(1, min(10, int(value)))

    def _request_parts(
        self,
        spans: list[dict[str, Any]],
    ) -> tuple[str, bytes, dict[str, str]]:
        import json

        url = f"{self._base_url}/internal/metrics/spans"
        path = "/internal/metrics/spans"
        from .internal_headers import internal_tenant_headers_from_span

        tenant_hdrs = internal_tenant_headers_from_span(spans[0])
        if tenant_hdrs is None:
            raise ValueError("remote trace batch requires full tenant scope")
        account_id = tenant_hdrs["X-Internal-Account-Id"]
        org_id = tenant_hdrs["X-Internal-Org-Id"]
        project_id = tenant_hdrs["X-Internal-Project-Id"]
        expected_tenant = (account_id, org_id, project_id)
        if not all(valid_tenant_id(value) for value in expected_tenant) or any(
            (
                str(span.get("account_id") or ""),
                str(span.get("org_id") or ""),
                str(span.get("project_id") or ""),
            )
            != expected_tenant
            for span in spans
        ):
            raise ValueError("remote trace batch crosses tenant boundaries")
        body_bytes = json.dumps(
            {"spans": _serialize_spans(spans)},
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **tenant_hdrs,
        }
        headers.update(
            sign_internal_request(
                self._key,
                method="POST",
                path=path,
                body=body_bytes,
                service=self.service,
                account_id=account_id,
                org_id=org_id,
                project_id=project_id,
            )
        )
        return url, body_bytes, headers

    async def _post_once(
        self,
        spans: list[dict[str, Any]],
    ) -> tuple[bool, bool]:
        try:
            import httpx

            url, body_bytes, headers = self._request_parts(spans)
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=5.0,
                    limits=httpx.Limits(
                        max_connections=_MAX_CONNECTIONS,
                        max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
                    ),
                )
            response = await self._client.post(
                url,
                content=body_bytes,
                headers=headers,
            )
            if 200 <= response.status_code < 300:
                return True, False
            retryable = response.status_code == 429 or response.status_code >= 500
            logger.warning(
                "Remote trace push rejected service=%s status=%s retryable=%s",
                self.service,
                response.status_code,
                retryable,
            )
            return False, retryable
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # telemetry failures must never escape
            logger.warning(
                "Remote trace push failed service=%s error_type=%s",
                self.service,
                type(exc).__name__,
            )
            return False, True

    async def _post_with_retry(self, spans: list[dict[str, Any]]) -> bool:
        for attempt in range(1, self._max_retries + 1):
            success, retryable = await self._post_once(spans)
            if success:
                return True
            if not retryable or attempt >= self._max_retries:
                return False
            await asyncio.sleep(min(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 2.0))
        return False

    async def _post(self, spans: list[dict[str, Any]]) -> bool:
        """Compatibility shim for callers that previously awaited one push."""
        return await self._post_with_retry(spans)

    async def _drain(self) -> None:
        try:
            while not self._closing or not self._queue.empty():
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=0.25,
                    )
                except asyncio.TimeoutError:
                    continue
                batch = [first]
                while len(batch) < self._batch_spans:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
                for span in batch:
                    tenant = (
                        str(span.get("account_id") or ""),
                        str(span.get("org_id") or ""),
                        str(span.get("project_id") or ""),
                    )
                    grouped.setdefault(tenant, []).append(span)
                processed = 0
                try:
                    for tenant_batch in grouped.values():
                        delivered = await self._post_with_retry(tenant_batch)
                        if delivered:
                            self._sent_spans += len(tenant_batch)
                        else:
                            unresolved = await self._enqueue_durable_fallback(tenant_batch)
                            if unresolved:
                                # The request-local queue has exhausted its HTTP
                                # retries. Preserve the remaining documents in
                                # the transport's bounded dead-letter buffer so
                                # a temporary Redis/trace outage does not turn
                                # into an unobserved drop.
                                from .transport import re_enqueue_spans

                                await re_enqueue_spans(unresolved)
                            self._failed_spans += len(unresolved)
                        processed += len(tenant_batch)
                except asyncio.CancelledError:
                    dropped = len(batch) - processed
                    self._dropped_spans += dropped
                    if dropped:
                        logger.warning(
                            "Remote trace drain cancelled service=%s dropped=%s dropped_total=%s",
                            self.service,
                            dropped,
                            self._dropped_spans,
                        )
                    raise
                except Exception as exc:  # isolate each telemetry batch
                    failed = len(batch) - processed
                    self._failed_spans += failed
                    logger.error(
                        "Remote trace batch failed service=%s count=%s error_type=%s",
                        self.service,
                        failed,
                        type(exc).__name__,
                    )
                finally:
                    for _span in batch:
                        self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the background drain contained
            logger.error(
                "Remote trace drain failed service=%s error_type=%s",
                self.service,
                type(exc).__name__,
            )
        finally:
            self._drain_task = None

    async def close(self, *, timeout_seconds: float = 5.0) -> None:
        self._closing = True
        if not self._queue.empty():
            self._ensure_drain_task()
        task = self._drain_task
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.0, float(timeout_seconds)),
                )
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        self._dropped_spans += dropped
        if dropped:
            logger.warning(
                "Remote trace shutdown dropped service=%s count=%s dropped_total=%s",
                self.service,
                dropped,
                self._dropped_spans,
            )
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # shutdown is best effort
                logger.warning(
                    "Remote trace client close failed service=%s error_type=%s",
                    self.service,
                    type(exc).__name__,
                )

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Compatibility shutdown name retained for runtime SDK consumers."""
        await self.close(timeout_seconds=timeout)


def get_remote_recorder(service: str, **kwargs: Any) -> RemoteTraceRecorder:
    if service not in _recorders:
        _recorders[service] = RemoteTraceRecorder(service, **kwargs)
    return _recorders[service]


def configure_remote_sink(service: str, **kwargs: Any) -> RemoteTraceRecorder:
    """Register remote HTTP sink for record_span when no local Mongo flush handler."""
    from . import recorder as rec

    assert_trace_producer_configuration(service)
    recorder = get_remote_recorder(service, **kwargs)
    recorder.start()
    rec.set_remote_recorder(recorder)
    return recorder


async def shutdown_remote_sinks(*, timeout: float = 5.0) -> None:
    """Drain all configured remote sinks during application shutdown."""
    recorders = list(_recorders.values())
    if recorders:
        results = await asyncio.gather(
            *(recorder.shutdown(timeout=timeout) for recorder in recorders),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning(
                    "Remote trace sink shutdown failed error_type=%s",
                    type(result).__name__,
                )
    _recorders.clear()


async def shutdown_remote_traces(
    service: str | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Compatibility shutdown API used by service-local hardened adapters."""
    names = [service] if service else list(_recorders)
    recorders = [_recorders[name] for name in names if name is not None and name in _recorders]
    if recorders:
        await asyncio.gather(
            *(recorder.close(timeout_seconds=timeout_seconds) for recorder in recorders)
        )
    for name in names:
        if name is not None:
            _recorders.pop(name, None)
