from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]


def test_platform_runtime_has_no_public_publishing_workflow() -> None:
    workflows = _REPOSITORY_ROOT / ".github" / "workflows"

    assert not (workflows / "publish-platform-runtime.yml").exists()
    public_sdk_workflow = (workflows / "build-and-publish.yml").read_text(encoding="utf-8")
    assert "platform-runtime-publish" not in public_sdk_workflow
    assert "pypi.org/p/maeyr-platform-runtime" not in public_sdk_workflow
    assert '"maeyr-platform-runtime==' not in public_sdk_workflow
    assert "verify_public_sdk_distribution.py" in public_sdk_workflow


def test_platform_runtime_policy_is_explicitly_internal_only() -> None:
    policy = (_REPOSITORY_ROOT / ".github" / "PYPI_PUBLISHING.md").read_text(encoding="utf-8")

    assert "must never be uploaded to PyPI" in policy
    assert "--no-index --no-build-isolation --no-deps" in policy
    assert "pip check" in policy


def test_distribution_metadata_blocks_public_registry_uploads() -> None:
    project = (_PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"Private :: Do Not Upload"' in project
