# PyPI publishing for `viksa-sdk`

The [Build and Publish to PyPI](workflows/build-and-publish.yml) workflow runs tests, builds the package with `python -m build`, checks artifacts with `twine check`, and uploads to [PyPI](https://pypi.org/project/viksa-ai/).

**Triggers**

- Push a Git tag matching `v*` (for example `v0.2.2`)
- Publish a GitHub Release
- Manual run: **Actions → Build and Publish to PyPI → Run workflow**

---

## Option A (recommended): PyPI trusted publishing

No long-lived API token is stored in GitHub. PyPI issues a short-lived token per workflow run.

### 1. Create the PyPI project (first time only)

1. Sign in at [pypi.org](https://pypi.org).
2. Create project **`viksa-ai`** if it does not exist yet (or claim the name you use in `pyproject.toml`).

### 2. Add a trusted publisher on PyPI

1. Open **Your projects → viksa-ai → Publishing**.
2. **Add a new pending publisher** (or **Manage publishers**).
3. Set:

   | Field | Value |
   |--------|--------|
   | PyPI project name | `viksa-ai` |
   | Owner | `viksa-ai` (GitHub org or your user) |
   | Repository name | `viksa-sdk` |
   | Workflow name | `Build and Publish to PyPI` |
   | Environment name | `pypi` (must match the workflow `environment: pypi`) |

4. Save. PyPI shows the publisher as **pending** until the first successful publish from that workflow.

### 3. GitHub environment (optional but recommended)

1. Repo **Settings → Environments → New environment** → name it `pypi`.
2. Add protection rules if you want (required reviewers, deployment branches).
3. You do **not** need to add `PYPI_API_TOKEN` when using trusted publishing only.

### 4. Release

**Recommended (tag-driven):**

```bash
# Bump version in pyproject.toml first, then:
git tag v0.2.2
git push origin v0.2.2
```

**Manual dispatch:** Actions → **Build and Publish to PyPI** → **Run workflow** on `main`. After a successful publish, the workflow creates and pushes `v<version>` from `pyproject.toml` if that tag does not exist yet.

Or create a GitHub Release from the tag; the workflow also runs on `release: published`.

---

## Option B: API token secret (`PYPI_API_TOKEN`)

Same pattern as [jsonQ](https://github.com/Srirammkm/jsonQ) (`secrets.PYPI_PASSWORD` / `secrets.pypi_password`). Use this if you are not using trusted publishing yet.

### 1. Create a PyPI API token

1. [pypi.org](https://pypi.org) → **Account settings → API tokens**.
2. **Add API token**.
3. Scope: **Entire account** (first upload) or **Project: viksa-ai** (after the project exists).
4. Copy the token once (starts with `pypi-`).

### 2. Add the GitHub secret

1. Open `https://github.com/viksa-ai/viksa-sdk/settings/secrets/actions`.
2. **New repository secret**:
   - **Name:** `PYPI_API_TOKEN`
   - **Value:** the `pypi-...` token (paste the full string)

For an organization repo, you can instead use **Organization secrets** and allow access for `viksa-sdk`.

### 3. Test with a dry run (local, optional)

```bash
python -m pip install build twine
python -m build
twine check dist/*
# Test upload to TestPyPI first:
twine upload --repository testpypi dist/* -u __token__ -p YOUR_TESTPYPI_TOKEN
```

TestPyPI token secret (optional): add `TESTPYPI_API_TOKEN` and a separate workflow job if you want TestPyPI uploads.

---

## jsonQ workflow reference

| jsonQ file | Purpose |
|------------|---------|
| `build-and-publish.yaml.bkp` | Tests → `python -m build` → `twine upload` with `TWINE_PASSWORD: ${{ secrets.PYPI_PASSWORD }}` |
| `release.yaml` | Matrix tests on `main` + `pypa/gh-action-pypi-publish` with `secrets.pypi_password` |

This repo uses one workflow (`build-and-publish.yml`) that combines pre-release tests (like jsonQ’s `test-before-release`) and `pypa/gh-action-pypi-publish@release/v1` (like jsonQ’s `release.yaml`), with optional `PYPI_API_TOKEN` or trusted publishing.

---

## Troubleshooting

| Error | Fix |
|--------|-----|
| `403 Invalid or non-existent authentication` | Trusted publisher owner/repo/workflow/environment must match exactly; or set `PYPI_API_TOKEN`. |
| `File already exists` | Bump `version` in `pyproject.toml`; PyPI does not allow re-uploading the same version. |
| Publish job skipped | Tag must match `v*`; or run workflow manually. |
| Tests fail | Fix on `main` first; CI workflow `ci.yml` should be green. |
