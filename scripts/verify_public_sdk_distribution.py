#!/usr/bin/env python3
"""Verify that public SDK artifacts do not contain or depend on private packages."""

from __future__ import annotations

import argparse
import json
import re
import tarfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from zipfile import ZipFile

_EXPECTED_DISTRIBUTION = "maeyr"
_PRIVATE_DISTRIBUTIONS = frozenset({"maeyr-platform-runtime"})


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _private_member_names(names: list[str]) -> list[str]:
    private_tokens = {
        _normalize_distribution(distribution) for distribution in _PRIVATE_DISTRIBUTIONS
    }
    return sorted(
        name
        for name in names
        if any(
            token in _normalize_distribution(part)
            for part in Path(name).parts
            for token in private_tokens
        )
    )


def _verify_metadata(payload: bytes, *, source: str) -> None:
    metadata = BytesParser(policy=default).parsebytes(payload)
    name = metadata.get("Name")
    if _normalize_distribution(name or "") != _EXPECTED_DISTRIBUTION:
        raise ValueError(f"{source} declares unexpected distribution name {name!r}")

    private = {_normalize_distribution(name) for name in _PRIVATE_DISTRIBUTIONS}
    for requirement in metadata.get_all("Requires-Dist", []):
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
        if match and _normalize_distribution(match.group(1)) in private:
            raise ValueError(f"{source} exposes private dependency {requirement!r}")


def verify_public_sdk_distribution(dist_directory: Path) -> dict[str, object]:
    wheels = sorted(dist_directory.glob("*.whl"))
    source_distributions = sorted(dist_directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_distributions) != 1:
        raise ValueError(
            "public release must contain exactly one wheel and one .tar.gz source distribution; "
            f"found {len(wheels)} wheel(s) and {len(source_distributions)} source distribution(s)"
        )

    wheel = wheels[0]
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        private_members = _private_member_names(names)
        if private_members:
            raise ValueError(f"public wheel contains private package paths: {private_members!r}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"public wheel has unexpected metadata paths: {metadata_names!r}")
        _verify_metadata(archive.read(metadata_names[0]), source=wheel.name)

    source_distribution = source_distributions[0]
    with tarfile.open(source_distribution, "r:gz") as archive:
        names = archive.getnames()
        private_members = _private_member_names(names)
        if private_members:
            raise ValueError(f"public sdist contains private package paths: {private_members!r}")
        package_info_names = [
            name for name in names if name.endswith("/PKG-INFO") and len(Path(name).parts) == 2
        ]
        if len(package_info_names) != 1:
            raise ValueError(f"public sdist has unexpected PKG-INFO paths: {package_info_names!r}")
        package_info = archive.extractfile(package_info_names[0])
        if package_info is None:
            raise ValueError("public sdist PKG-INFO is unreadable")
        _verify_metadata(package_info.read(), source=source_distribution.name)

    return {
        "distribution": _EXPECTED_DISTRIBUTION,
        "private_distributions_excluded": sorted(_PRIVATE_DISTRIBUTIONS),
        "source_distribution": source_distribution.name,
        "wheel": wheel.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_directory", type=Path)
    args = parser.parse_args(argv)
    result = verify_public_sdk_distribution(args.dist_directory.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
