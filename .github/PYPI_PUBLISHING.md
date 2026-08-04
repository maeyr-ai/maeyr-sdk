# PyPI publishing for `viksa-sdk`

This repository contains two independently versioned distributions. They must
not share a tag namespace or release workflow.

| Distribution | Source | Release tag | Workflow | Target index |
| --- | --- | --- | --- | --- |
| `viksa-ai` | repository root | `v<version>` | `build-and-publish.yml` | production PyPI |
| `viksa-platform-runtime` | `packages/viksa-platform-runtime` | `platform-runtime-v<version>` | `publish-platform-runtime.yml` | production PyPI |

The platform runtime's service migration gate requires the exact package on the
index used by production builds. A local path, workspace checkout, unpinned
version, or TestPyPI artifact is not a production substitute.

## Platform runtime trusted publisher (required)

The dedicated [platform runtime workflow](workflows/publish-platform-runtime.yml)
uses PyPI trusted publishing. It does not read a long-lived API token. The
publish job has job-scoped `id-token: write` permission and is protected by the
`pypi-platform-runtime` GitHub environment.

Create a pending publisher on PyPI with these exact values before pushing the
first release tag:

| PyPI field | Exact value |
| --- | --- |
| PyPI project name | `viksa-platform-runtime` |
| Owner | `viksa-ai` |
| Repository name | `viksa-sdk` |
| Workflow name | `publish-platform-runtime.yml` |
| Environment name | `pypi-platform-runtime` |

PyPI's **Workflow name** field is the workflow filename, not the display name
shown in GitHub Actions. A correctly configured pending publisher may create
the project during its first trusted upload; if the project already exists,
add the publisher under that project's publishing settings.

Create the matching GitHub environment at **Settings → Environments → New
environment → `pypi-platform-runtime`**. Required reviewers are recommended.
Do not add `PYPI_API_TOKEN` to this environment: the runtime workflow is OIDC
only.

## Platform runtime release procedure

1. Change `version` in `packages/viksa-platform-runtime/pyproject.toml` and
   regenerate `packages/viksa-platform-runtime/uv.lock`.
2. Ensure CI passes on Python 3.10, 3.11, and 3.12. CI runs Ruff lint and format
   checks, strict Mypy, Pytest, a clean build, Twine metadata checks, and exact
   wheel/source-distribution verification.
3. Tag the reviewed release commit with the exact package version:

   ```bash
   git tag platform-runtime-v0.2.0
   git push origin platform-runtime-v0.2.0
   ```

Do not use `v0.2.0` for this package. The generic `v*` namespace belongs to the
root `viksa-ai` workflow.

The runtime workflow rejects a tag that does not exactly equal
`platform-runtime-v<pyproject version>`. It builds one wheel and one source
distribution, validates their names, versions, contents, and hashes, and passes
only those immutable artifacts to the OIDC publish job. It intentionally does
not use `skip-existing`; PyPI version reuse is an error. After upload, a final
job installs the exact version from `https://pypi.org/simple` and verifies the
installed distribution and `py.typed` marker.

The absence of `repository-url` on `pypa/gh-action-pypi-publish` means the
upload target is production PyPI. TestPyPI requires a separate workflow,
environment, and trusted-publisher registration; never point the production
release job at TestPyPI.

After the index validation job passes, rebuild each dependent service from a
clean Docker context and verify both `viksa-platform-runtime==<version>` and
`viksa_platform/py.typed` before releasing the service image.

## Local artifact validation (does not publish)

From the repository root:

```bash
python -m pip install -e 'packages/viksa-platform-runtime[dev]'
cd packages/viksa-platform-runtime
ruff check src tests
ruff format --check src tests
mypy
pytest
python -m build
python -m twine check dist/*
python ../../scripts/verify_python_release.py \
  --project-directory . \
  --dist-directory dist \
  --expected-name viksa-platform-runtime
```

These commands create local artifacts only. Do not run `twine upload`, create a
release tag, or dispatch a publishing workflow as part of validation.

## Root `viksa-ai` release authentication

The existing [root-package workflow](workflows/build-and-publish.yml) builds
`viksa-ai` from the repository root and currently passes
`secrets.PYPI_API_TOKEN` to the publishing action. Configure that repository
secret with an account-scoped token for a first upload or a `viksa-ai` project
token once the project exists.

If the root workflow is migrated to trusted publishing later, remove its
`password` input and register the exact workflow filename
`build-and-publish.yml` with environment `pypi`. Merely granting
`id-token: write` while still passing a password does not select trusted
publishing.

## Troubleshooting

| Error | Fix |
| --- | --- |
| `invalid-publisher` / OIDC `403` | Match owner, repository, workflow **filename**, and environment exactly. |
| `File already exists` | Bump the package version. PyPI artifacts are immutable. |
| Runtime workflow does not start | Tag must begin with `platform-runtime-v`. |
| Exact-tag gate fails | Tag must equal `platform-runtime-v<pyproject version>`. |
| Artifact verification fails | Remove stale `dist/`, rebuild, and inspect the reported metadata or manifest mismatch. |
