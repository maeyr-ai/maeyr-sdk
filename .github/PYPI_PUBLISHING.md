# Package publishing policy

This repository contains one public distribution and one private application
component. They have deliberately different delivery paths.

| Distribution | Visibility | Delivery |
| --- | --- | --- |
| `viksa-ai` | Public SDK | `build-and-publish.yml` publishes tagged releases to PyPI |
| `viksa-platform-runtime` | Internal only | A reviewed private-repository commit is passed directly to service image builds |

## Internal platform runtime

`packages/viksa-platform-runtime` must never be uploaded to PyPI, TestPyPI, or
another public package registry. There is no runtime publishing workflow or
runtime release tag. Building its wheel and source distribution remains useful
for CI validation, but those artifacts stay inside the workflow that built them.

Service CI checks out this private repository using the least-privilege
`VIKSA_SDK_READ_TOKEN`, verifies a full commit SHA, and supplies
`packages/viksa-platform-runtime` as the named BuildKit context
`viksa_platform_runtime`. The service Dockerfile installs it with:

```text
pip install --no-index --no-build-isolation --no-deps /tmp/viksa-platform-runtime
pip install --no-build-isolation --constraint /tmp/viksa-platform-runtime.constraints -r requirements.txt /tmp/viksa-platform-runtime
pip check
```

Public service requirement files and dependency tables must not contain
`viksa-platform-runtime`. This separation prevents pip from consulting a public
index for an internal application component and prevents dependency confusion.
The constrained second pass may resolve the runtime's ordinary third-party
dependencies from the approved public index, but it must resolve the runtime
distribution itself only from the private local source. `pip check` makes
missing or incompatible transitive dependencies a build failure.
Workspace development uses
`devops/scripts/install_internal_service_dependencies.py`, which applies the
same local-source constraint while installing service requirements.

## Public `viksa-ai` releases

The root [build-and-publish workflow](workflows/build-and-publish.yml) builds the
public `viksa-ai` distribution from the repository root. Its `v<version>` tags,
PyPI credentials, and post-release index checks apply only to `viksa-ai`.

Local runtime validation is still supported:

```bash
python -m pip install -e 'packages/viksa-platform-runtime[dev]'
cd packages/viksa-platform-runtime
ruff check src tests
ruff format --check src tests
mypy
pytest
python -m build
python -m twine check dist/*
```

These commands do not authorize `twine upload` or any registry publication.
