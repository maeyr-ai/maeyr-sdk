from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from scripts.verify_wheel_manifest import compare_manifest


def _wheel(path: Path, files: dict[str, str]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return path


def test_manifest_accepts_exact_package_contents(tmp_path: Path) -> None:
    package = tmp_path / "example"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "py.typed").write_text("\n", encoding="utf-8")
    wheel = _wheel(
        tmp_path / "example.whl",
        {"example/__init__.py": "", "example/py.typed": "\n"},
    )

    result = compare_manifest(wheel, package)

    assert result["passed"] is True
    assert result["missing"] == []
    assert result["unexpected"] == []


def test_manifest_rejects_missing_and_resurrected_modules(tmp_path: Path) -> None:
    package = tmp_path / "example"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "current.py").write_text("VALUE = 1\n", encoding="utf-8")
    wheel = _wheel(
        tmp_path / "example.whl",
        {"example/__init__.py": "", "example/retired.py": "VALUE = 0\n"},
    )

    result = compare_manifest(wheel, package)

    assert result["passed"] is False
    assert result["missing"] == ["example/current.py"]
    assert result["unexpected"] == ["example/retired.py"]
