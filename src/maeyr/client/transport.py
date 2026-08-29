"""HTTP transport with retries, typed errors, and idempotency."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import httpx

from maeyr.client.config import ClientConfig, RetryConfig
from maeyr.client.errors import (
    MaeyrApiError,
    MaeyrRateLimitError,
    raise_for_response,
    wrap_transport_error,
)


def _compute_backoff(attempt: int, config: RetryConfig, retry_after: Optional[float]) -> float:
    if retry_after is not None and retry_after > 0:
        return min(retry_after, config.max_backoff_seconds)
    delay = config.backoff_factor * (2**attempt)
    return float(min(delay, config.max_backoff_seconds))


def _should_retry_status(status: int, config: RetryConfig) -> bool:
    return status in config.retry_status_codes


def _can_retry_request(method: str, headers: Dict[str, str]) -> bool:
    """Return whether repeating this request cannot silently duplicate work.

    Reads are safe to repeat. Mutations are retried only when the caller has
    supplied an idempotency key that the platform can bind to the operation.
    A timeout or gateway error does not prove that a mutation was not already
    committed by the upstream service.
    """

    if method.strip().upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    return any(
        key.casefold() == "idempotency-key" and bool(str(value).strip())
        for key, value in headers.items()
    )


class HttpTransport:
    """Shared async/sync HTTP execution for :class:`MaeyrClient`."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Dict[str, str],
        config: ClientConfig,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.config = config
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

    def _merge_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        merged = dict(self.headers)
        if self.config.idempotency_key:
            merged.setdefault("Idempotency-Key", self.config.idempotency_key)
        if extra:
            merged.update(extra)
        return merged

    def get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self.config.timeout,
                headers=self._merge_headers(),
            )
        return self._async_client

    def get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=self.config.timeout,
                headers=self._merge_headers(),
            )
        return self._sync_client

    def update_headers(self, headers: Dict[str, str]) -> None:
        removed = set(self.headers).difference(headers)
        self.headers = dict(headers)
        if self._async_client is not None:
            for key in removed:
                self._async_client.headers.pop(key, None)
            self._async_client.headers.update(self._merge_headers())
        if self._sync_client is not None:
            for key in removed:
                self._sync_client.headers.pop(key, None)
            self._sync_client.headers.update(self._merge_headers())

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    def _parse_success(self, response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def arequest(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Any:
        service = prefix.strip("/").split("/")[0] or prefix
        url = f"{self.base_url}{prefix}{path}"
        retry_cfg = retry or self.config.retry
        last_exc: Optional[BaseException] = None
        request_headers = self._merge_headers(headers)
        can_retry = _can_retry_request(method, request_headers)

        for attempt in range(retry_cfg.max_retries + 1):
            try:
                client = self.get_async_client()
                response = await client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                )
            except httpx.HTTPError as exc:
                last_exc = wrap_transport_error(exc, method=method, url=url)
                if (
                    can_retry
                    and retry_cfg.retry_on_connection_errors
                    and attempt < retry_cfg.max_retries
                ):
                    await asyncio.sleep(_compute_backoff(attempt, retry_cfg, None))
                    continue
                raise last_exc from exc

            if response.status_code < 400:
                return self._parse_success(response)

            retry_after: Optional[float] = None
            if response.status_code == 429:
                try:
                    raise_for_response(response, service=service, method=method, path=path)
                except MaeyrRateLimitError as rate_exc:
                    retry_after = rate_exc.retry_after
                    last_exc = rate_exc
                    if (
                        can_retry
                        and attempt < retry_cfg.max_retries
                        and _should_retry_status(429, retry_cfg)
                    ):
                        await asyncio.sleep(_compute_backoff(attempt, retry_cfg, retry_after))
                        continue
                    raise

            if (
                can_retry
                and _should_retry_status(response.status_code, retry_cfg)
                and attempt < retry_cfg.max_retries
            ):
                await asyncio.sleep(_compute_backoff(attempt, retry_cfg, retry_after))
                continue

            raise_for_response(response, service=service, method=method, path=path)

        if last_exc:
            raise last_exc
        raise MaeyrApiError(
            "Request failed after retries",
            status_code=0,
            service=service,
            method=method,
            path=path,
        )

    def request(
        self,
        method: str,
        prefix: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Any:
        service = prefix.strip("/").split("/")[0] or prefix
        url = f"{self.base_url}{prefix}{path}"
        retry_cfg = retry or self.config.retry
        last_exc: Optional[BaseException] = None
        request_headers = self._merge_headers(headers)
        can_retry = _can_retry_request(method, request_headers)

        for attempt in range(retry_cfg.max_retries + 1):
            try:
                client = self.get_sync_client()
                response = client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                )
            except httpx.HTTPError as exc:
                last_exc = wrap_transport_error(exc, method=method, url=url)
                if (
                    can_retry
                    and retry_cfg.retry_on_connection_errors
                    and attempt < retry_cfg.max_retries
                ):
                    time.sleep(_compute_backoff(attempt, retry_cfg, None))
                    continue
                raise last_exc from exc

            if response.status_code < 400:
                return self._parse_success(response)

            retry_after = None
            if response.status_code == 429:
                try:
                    raise_for_response(response, service=service, method=method, path=path)
                except MaeyrRateLimitError as rate_exc:
                    retry_after = rate_exc.retry_after
                    last_exc = rate_exc
                    if (
                        can_retry
                        and attempt < retry_cfg.max_retries
                        and _should_retry_status(429, retry_cfg)
                    ):
                        time.sleep(_compute_backoff(attempt, retry_cfg, retry_after))
                        continue
                    raise

            if (
                can_retry
                and _should_retry_status(response.status_code, retry_cfg)
                and attempt < retry_cfg.max_retries
            ):
                time.sleep(_compute_backoff(attempt, retry_cfg, retry_after))
                continue

            raise_for_response(response, service=service, method=method, path=path)

        if last_exc:
            raise last_exc
        raise MaeyrApiError(
            "Request failed after retries",
            status_code=0,
            service=service,
            method=method,
            path=path,
        )
