# Maeyr SDK and platform runtime architecture

This repository has two installable distributions. Public `maeyr` under `src/maeyr` provides typed sync/async HTTP clients, agent runtime helpers, manifest validation, developer CLI, and an MCP bridge. The private `maeyr-platform-runtime` under `packages/maeyr-platform-runtime` provides shared security, tenant identity, Mongo/Redis helpers, metrics, tracing, and execution contracts for backend services.

The public client transport owns reusable `httpx` clients, request timeouts, pagination, and retries. Backend services consume a reviewed immutable private-runtime commit through their Docker build contexts; the private runtime is not bundled into the public PyPI wheel. Service composition and data ownership remain in the consuming repositories.

`.github/workflows/ci.yml` and the PyPI release gate now exercise both distributions. The release build verifies the public wheel excludes private code. The runtime has a separate package manifest and test tree.
