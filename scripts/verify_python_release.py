#!/usr/bin/env python3
"""Verify Python release metadata, contents, and artifact cardinality."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from zipfile import ZipFile

from verify_wheel_manifest import compare_manifest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def _artifact_stem(distribution_name: str) -> str:
    return re.sub(r"[-_.]+", "_", distribution_name)


def _metadata(payload: bytes) -> tuple[str, str]:
    parsed = BytesParser(policy=default).parsebytes(payload)
    name = parsed.get("Name")
    version = parsed.get("Version")
    if not name or not version:
        raise ValueError("distribution metadata must include Name and Version")
    return name, version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(
    *,
    project_directory: Path,
    dist_directory: Path,
    expected_name: str,
    expected_version: str | None,
) -> dict[str, object]:
    with (project_directory / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)["project"]
    declared_name = project["name"]
    declared_version = project["version"]
    if declared_name != expected_name:
        raise ValueError(f"declared name {declared_name!r} != expected {expected_name!r}")
    if expected_version is not None and declared_version != expected_version:
        raise ValueError(f"declared version {declared_version!r} != expected {expected_version!r}")

    wheels = sorted(dist_directory.glob("*.whl"))
    source_distributions = sorted(dist_directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_distributions) != 1:
        raise ValueError(
            "release must contain exactly one wheel and one .tar.gz source distribution; "
            f"found {len(wheels)} wheel(s) and {len(source_distributions)} source distribution(s)"
        )
    wheel = wheels[0]
    source_distribution = source_distributions[0]
    stem = _artifact_stem(expected_name)
    expected_wheel_prefix = f"{stem}-{declared_version}-"
    expected_sdist_name = f"{stem}-{declared_version}.tar.gz"
    if not wheel.name.startswith(expected_wheel_prefix):
        raise ValueError(f"unexpected wheel filename: {wheel.name!r}")
    if source_distribution.name != expected_sdist_name:
        raise ValueError(f"unexpected source distribution filename: {source_distribution.name!r}")

    expected_dist_info = f"{stem}-{declared_version}.dist-info/METADATA"
    with ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if metadata_files != [expected_dist_info]:
            raise ValueError(f"unexpected wheel metadata path(s): {metadata_files!r}")
        wheel_name, wheel_version = _metadata(archive.read(expected_dist_info))
    if (wheel_name, wheel_version) != (expected_name, declared_version):
        raise ValueError(
            "wheel metadata mismatch: "
            f"{(wheel_name, wheel_version)!r} != {(expected_name, declared_version)!r}"
        )

    expected_root = f"{stem}-{declared_version}"
    required_sdist_paths = {
        f"{expected_root}/LICENSE",
        f"{expected_root}/MIGRATION.md",
        f"{expected_root}/PKG-INFO",
        f"{expected_root}/README.md",
        f"{expected_root}/pyproject.toml",
        f"{expected_root}/src/viksa_platform/py.typed",
    }
    with tarfile.open(source_distribution, "r:gz") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
        missing_sdist_paths = sorted(required_sdist_paths - members)
        if missing_sdist_paths:
            raise ValueError(f"source distribution is missing: {missing_sdist_paths!r}")
        package_info = archive.extractfile(f"{expected_root}/PKG-INFO")
        if package_info is None:
            raise ValueError("source distribution PKG-INFO is unreadable")
        sdist_name, sdist_version = _metadata(package_info.read())
    if (sdist_name, sdist_version) != (expected_name, declared_version):
        raise ValueError(
            "source distribution metadata mismatch: "
            f"{(sdist_name, sdist_version)!r} != {(expected_name, declared_version)!r}"
        )

    manifest = compare_manifest(wheel, project_directory / "src" / "viksa_platform")
    if not manifest["passed"]:
        raise ValueError(f"wheel package manifest mismatch: {manifest!r}")

    return {
        "distribution": expected_name,
        "manifest": manifest,
        "source_distribution": {
            "filename": source_distribution.name,
            "sha256": _sha256(source_distribution),
        },
        "version": declared_version,
        "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--dist-directory", required=True, type=Path)
    parser.add_argument("--expected-name", required=True)
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)
    result = verify_release(
        project_directory=args.project_directory.resolve(),
        dist_directory=args.dist_directory.resolve(),
        expected_name=args.expected_name,
        expected_version=args.expected_version,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
