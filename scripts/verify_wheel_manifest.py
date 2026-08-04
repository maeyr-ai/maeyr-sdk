#!/usr/bin/env python3
"""Fail when a wheel omits current package files or resurrects stale ones."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZipFile


def source_manifest(package_directory: Path) -> frozenset[str]:
    package_name = package_directory.name
    return frozenset(
        f"{package_name}/{path.relative_to(package_directory).as_posix()}"
        for path in package_directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.name.endswith((".pyc", ".pyo"))
    )


def wheel_manifest(wheel: Path, package_name: str) -> frozenset[str]:
    prefix = f"{package_name}/"
    with ZipFile(wheel) as archive:
        return frozenset(
            name
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        )


def compare_manifest(wheel: Path, package_directory: Path) -> dict[str, object]:
    expected = source_manifest(package_directory)
    packaged = wheel_manifest(wheel, package_directory.name)
    missing = sorted(expected - packaged)
    unexpected = sorted(packaged - expected)
    return {
        "package": package_directory.name,
        "source_files": len(expected),
        "wheel_files": len(packaged),
        "missing": missing,
        "unexpected": unexpected,
        "passed": not missing and not unexpected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("package_directory", type=Path)
    args = parser.parse_args(argv)
    if not args.wheel.is_file():
        parser.error("wheel must be an existing file")
    if not args.package_directory.is_dir():
        parser.error("package_directory must be an existing directory")
    result = compare_manifest(args.wheel, args.package_directory)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
