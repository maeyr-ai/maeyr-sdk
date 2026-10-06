"""Strict deployment trust configuration shared by API and workers."""

import os
from urllib.parse import urlsplit


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Serverless internal origins must use TLS")
    return value.rstrip("/")


def durable_redis_url() -> str:
    value = required("SERVERLESS_REDIS_URL")
    parsed = urlsplit(value)
    if parsed.scheme != "rediss" or not parsed.hostname or parsed.fragment:
        raise ValueError("Durable execution Redis must use TLS")
    # Reject certificate bypass settings; all roles use verified TLS.
    from urllib.parse import parse_qs

    options = parse_qs(parsed.query, keep_blank_values=True)
    if any(name in options for name in ("ssl_cert_reqs", "ssl_check_hostname", "ssl")):
        raise ValueError("Execution Redis TLS policy cannot be overridden in the URL")
    return value


def shared_namespace() -> str:
    value = required("SERVERLESS_TEMPORAL_NAMESPACE")
    if value != "serverless":
        raise ValueError("Every serverless role requires the shared serverless namespace")
    return value
