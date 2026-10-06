"""Strict, deterministic manifests for immutable prepared Python bundles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

SCHEMA = "maeyr-serverless-artifact-v1"
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")
SCOPE_FIELDS = ("account_id", "org_id", "project_id", "agent_id", "build_id")
IDENTITY_FIELDS = (
    *SCOPE_FIELDS,
    "source_revision",
    "dependency_lock_digest",
    "image_ref",
    "runtime_abi",
    "endpoints",
)


class ArtifactValidationError(ValueError):
    """Finite validation failure; never expose filesystem paths or dependency output."""


@dataclass(frozen=True)
class ArtifactLimits:
    max_files: int = 50000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_archive_bytes: int = 256 * 1024 * 1024
    # Signed claim/completion transports cap their entire envelope at 8 MiB.
    # Leave room for the scoped claim, status and fixed control metadata.
    max_manifest_bytes: int = 8 * 1024 * 1024 - 64 * 1024


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ArtifactValidationError("Artifact manifest is invalid") from exc


def validate_path(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        raise ArtifactValidationError("Artifact path is invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
        or "\\" in value
        or "\x00" in value
        or path.parts[0] not in {"code", "deps"}
        or len(path.parts) < 2
    ):
        raise ArtifactValidationError("Artifact path is invalid")
    return value


def validate_runtime_abi(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"python", "architecture", "platform"}
        or value.get("python") != "3.11"
        or value.get("architecture") not in {"amd64", "arm64"}
        or value.get("platform") != "linux"
    ):
        raise ArtifactValidationError("Artifact runtime ABI is invalid")
    return dict(value)


def validate_endpoints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        raise ArtifactValidationError("Artifact endpoints are invalid")
    names: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"endpoint", "module", "function", "execution_config"}
            or not isinstance(item["endpoint"], str)
            or not MODULE.fullmatch(item["endpoint"])
            or len(item["endpoint"]) > 512
            or item["endpoint"] in names
            or not isinstance(item["module"], str)
            or not MODULE.fullmatch(item["module"])
            or not isinstance(item["function"], str)
            or not FUNCTION.fullmatch(item["function"])
            or not isinstance(item["execution_config"], dict)
            or len(canonical_json(item["execution_config"])) > 16384
        ):
            raise ArtifactValidationError("Artifact endpoints are invalid")
        names.add(item["endpoint"])
    if value != sorted(value, key=lambda item: item["endpoint"]):
        raise ArtifactValidationError("Artifact endpoints are not canonical")
    return value


def artifact_key(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({key: value for key, value in manifest.items() if key != "artifact_key"})
    ).hexdigest()


def validate_manifest(value: Any, limits: ArtifactLimits = ArtifactLimits()) -> dict[str, Any]:
    required = {"schema", "artifact_key", *IDENTITY_FIELDS, "files"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != SCHEMA:
        raise ArtifactValidationError("Artifact manifest is invalid")
    for field in SCOPE_FIELDS:
        if not isinstance(value[field], str) or not IDENTIFIER.fullmatch(value[field]):
            raise ArtifactValidationError("Artifact scope is invalid")
    for field in ("source_revision", "dependency_lock_digest", "artifact_key"):
        if not isinstance(value[field], str) or not DIGEST.fullmatch(value[field]):
            raise ArtifactValidationError("Artifact digest is invalid")
    if not isinstance(value["image_ref"], str) or not IMAGE.fullmatch(value["image_ref"]):
        raise ArtifactValidationError("Artifact runner image is invalid")
    validate_runtime_abi(value["runtime_abi"])
    validate_endpoints(value["endpoints"])
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= limits.max_files:
        raise ArtifactValidationError("Artifact file count exceeds limits")
    paths: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ArtifactValidationError("Artifact file is invalid")
        path = validate_path(item["path"])
        if (
            path in paths
            or not isinstance(item["sha256"], str)
            or not DIGEST.fullmatch(item["sha256"])
        ):
            raise ArtifactValidationError("Artifact file is invalid")
        size = item["size"]
        if type(size) is not int or not 0 <= size <= limits.max_file_bytes:
            raise ArtifactValidationError("Artifact file exceeds limits")
        paths.add(path)
        total += size
    if total > limits.max_total_bytes or files != sorted(files, key=lambda item: item["path"]):
        raise ArtifactValidationError("Artifact files exceed limits or are not canonical")
    # File/parent collisions must not be interpreted differently by tar/runtime.
    if any(str(parent) in paths for path in paths for parent in PurePosixPath(path).parents):
        raise ArtifactValidationError("Artifact file paths conflict")
    if not any(path.startswith("code/") for path in paths):
        raise ArtifactValidationError("Artifact source is missing")
    if (
        len(canonical_json(value)) > limits.max_manifest_bytes
        or artifact_key(value) != value["artifact_key"]
    ):
        raise ArtifactValidationError("Artifact manifest digest is invalid")
    return value
