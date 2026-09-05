"""Shared Uvicorn process runner independent of service configuration."""

from __future__ import annotations

import logging
import multiprocessing
import os
import socket
from typing import Any


def bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Read a bounded integer without letting malformed environment state abort boot."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def configure_uvicorn_logging() -> None:
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


def run_uvicorn_app(
    app: Any,
    *,
    host: str,
    port: int,
    web_concurrency: int | None = None,
    limit_concurrency: int | None = None,
    backlog: int = 1024,
) -> None:
    """Run a Starlette-compatible app with optional prefork workers."""
    import uvicorn

    configure_uvicorn_logging()
    concurrency = (
        int(os.environ.get("WEB_CONCURRENCY", "1"))
        if web_concurrency is None
        else max(1, int(web_concurrency))
    )
    uvicorn_limits: dict[str, Any] = {"backlog": max(1, int(backlog))}
    if limit_concurrency is not None:
        uvicorn_limits["limit_concurrency"] = max(1, int(limit_concurrency))

    if concurrency <= 1:
        uvicorn.run(
            app,
            host=host,
            port=port,
            loop="uvloop",
            http="httptools",
            access_log=False,
            log_level="error",
            server_header=False,
            date_header=False,
            **uvicorn_limits,
        )
        return

    bind_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bind_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bind_sock.bind((host, port))
    bind_sock.listen(uvicorn_limits["backlog"])
    bind_sock.set_inheritable(True)

    def run_worker(sock: socket.socket) -> None:
        import asyncio

        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        uvicorn.run(
            app,
            fd=sock.fileno(),
            loop="uvloop",
            http="httptools",
            access_log=False,
            log_level="error",
            server_header=False,
            date_header=False,
            **uvicorn_limits,
        )

    workers: list[multiprocessing.Process] = []
    for _ in range(concurrency):
        process = multiprocessing.Process(target=run_worker, args=(bind_sock,))
        process.start()
        workers.append(process)
    try:
        for process in workers:
            process.join()
    except KeyboardInterrupt:
        for process in workers:
            process.terminate()


__all__ = ["bounded_env_int", "configure_uvicorn_logging", "run_uvicorn_app"]
