"""Canonical runtime-pod and credential-key configuration for Directory/Volt."""

from __future__ import annotations

import os
from typing import Dict

from pydantic_settings import BaseSettings


def _parse_keyring(raw: str) -> Dict[str, str]:
    """Parse comma-separated ``version:key-material`` entries."""
    out: Dict[str, str] = {}
    for item in (raw or "").split(","):
        token = item.strip()
        if not token or ":" not in token:
            continue
        version, key_material = token.split(":", 1)
        version = version.strip()
        key_material = key_material.strip()
        if version and key_material:
            out[version] = key_material
    return out


class RuntimeSettings(BaseSettings):
    """Environment-backed runtime settings shared by Directory and Volt."""

    CREDENTIAL_KEY_VERSION: str = os.environ.get("RUNTIME_CREDENTIAL_KEY_VERSION", "v1")
    CREDENTIAL_ENCRYPTION_KEY: str = os.environ.get("RUNTIME_CREDENTIAL_ENCRYPTION_KEY", "")
    CREDENTIAL_KEYRING: Dict[str, str] = _parse_keyring(
        os.environ.get("RUNTIME_CREDENTIAL_KEYRING", "")
    )

    # Slack is a stable external protocol endpoint. Runtime-pod namespace,
    # image, pull-secret, and naming fields previously declared here had no
    # consumers; keeping them would create a second, unauthoritative deployment
    # contract outside the master configuration.
    SLACK_API_BASE_URL: str = os.environ.get(
        "VOLT_RUNTIME_SLACK_API_BASE_URL", "https://slack.com/api"
    ).rstrip("/")
    SLACK_API_TIMEOUT_SECONDS: float = float(
        os.environ.get("VOLT_RUNTIME_SLACK_API_TIMEOUT_SECONDS", "8")
    )

    @property
    def keyring(self) -> Dict[str, str]:
        out = dict(self.CREDENTIAL_KEYRING)
        if self.CREDENTIAL_ENCRYPTION_KEY:
            out[self.CREDENTIAL_KEY_VERSION] = self.CREDENTIAL_ENCRYPTION_KEY
        return out


runtime_settings = RuntimeSettings()

__all__ = ["RuntimeSettings", "runtime_settings"]
