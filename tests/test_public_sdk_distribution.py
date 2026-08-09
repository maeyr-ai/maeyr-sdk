from __future__ import annotations

import io
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.verify_public_sdk_distribution import verify_public_sdk_distribution

_PUBLIC_METADATA = b"Metadata-Version: 2.4\nName: viksa-ai\nVersion: 1.0.0\n"


def _artifacts(
    directory: Path,
    *,
    wheel_member: str = "viksa_ai/__init__.py",
    requirement: str | None = None,
) -> Path:
    metadata = _PUBLIC_METADATA
    if requirement:
        metadata += f"Requires-Dist: {requirement}\n".encode()
    metadata += b"\n"

    wheel = directory / "viksa_ai-1.0.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(wheel_member, "")
        archive.writestr("viksa_ai-1.0.0.dist-info/METADATA", metadata)

    sdist = directory / "viksa_ai-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, payload in {
            "viksa_ai-1.0.0/PKG-INFO": metadata,
            "viksa_ai-1.0.0/src/viksa_ai.egg-info/PKG-INFO": metadata,
            "viksa_ai-1.0.0/src/viksa_ai/__init__.py": b"",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return directory


def test_public_sdk_distribution_excludes_private_runtime(tmp_path: Path) -> None:
    result = verify_public_sdk_distribution(_artifacts(tmp_path))

    assert result["distribution"] == "viksa-ai"
    assert result["private_distributions_excluded"] == ["viksa-platform-runtime"]


def test_public_sdk_distribution_rejects_private_package_path(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, wheel_member="viksa_platform_runtime/__init__.py")

    with pytest.raises(ValueError, match="private package paths"):
        verify_public_sdk_distribution(artifacts)


def test_public_sdk_distribution_rejects_private_dependency(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path, requirement="viksa-platform-runtime==1.0.0")

    with pytest.raises(ValueError, match="private dependency"):
        verify_public_sdk_distribution(artifacts)
