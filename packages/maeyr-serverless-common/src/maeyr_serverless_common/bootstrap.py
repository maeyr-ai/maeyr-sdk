"""Shared verified Temporal connection setup; no API or guest runtime imports."""

import os
from pathlib import Path
from typing import Any, Protocol, cast

from redis.asyncio import Redis

from .temporal.client import ServerlessTemporalSettings


class TemporalConnectionConfig(Protocol):
    @property
    def temporal_endpoint(self) -> str: ...

    @property
    def temporal_namespace(self) -> str: ...

    @property
    def temporal_queue_prefix(self) -> str: ...

    @property
    def shard_count(self) -> int: ...


def temporal_settings(
    config: TemporalConnectionConfig, *, build: bool = False
) -> ServerlessTemporalSettings:
    tls = os.environ.get("TEMPORAL_TLS_ENABLED", "true").lower()
    if tls not in {"true", "false"}:
        raise ValueError("TEMPORAL_TLS_ENABLED must be true or false")
    material: dict[str, Any] = {}
    for env, field in (
        ("TEMPORAL_TLS_CA_FILE", "server_root_ca_cert"),
        ("TEMPORAL_TLS_CERT_FILE", "client_cert"),
        ("TEMPORAL_TLS_KEY_FILE", "client_private_key"),
    ):
        if os.environ.get(env):
            path = Path(os.environ[env])
            if path.stat().st_size > 131_072:
                raise ValueError("Temporal TLS material exceeds its size bound")
            material[field] = path.read_bytes()
    if os.environ.get("TEMPORAL_TLS_SERVER_NAME"):
        material["server_name"] = os.environ["TEMPORAL_TLS_SERVER_NAME"]
    return ServerlessTemporalSettings(
        endpoint=config.temporal_endpoint,
        namespace=config.temporal_namespace,
        queue_prefix="maeyr-serverless-build-v1" if build else config.temporal_queue_prefix,
        shard_count=1 if build else config.shard_count,
        max_concurrent_activities=int(os.environ.get("SERVERLESS_ACTIVITY_SLOTS", "32")),
        tls_enabled=tls == "true",
        **material,
    )


def redis_client(url: str) -> Redis:
    return cast(
        Redis,
        Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=3,
            socket_timeout=5,
            max_connections=256,
        ),
    )
