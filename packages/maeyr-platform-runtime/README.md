# maeyr-platform-runtime

`maeyr-platform-runtime` is the independently versioned, PEP 561-typed boundary
for cross-cutting Maeyr service behavior. Version `0.2.1` provides:

- instance-based, bounded trace and usage recorders;
- typed trace, usage, tenant, and caller contexts;
- transport and bounded-lifecycle protocols for dependency injection;
- exact-body HMAC request signing with current/previous key verification;
- immutable secret-strength classification for service-owned startup policy;
- tenant-safe display extraction and recursive secret redaction;
- structure-aware truncation for bounded LLM context payloads; and
- thin functional facades for staged migration from copied `common/` modules.

Version `0.2.1` also owns the stable tracing primitives historically copied as
`common.platform_traces.ids`, `tracestate`, `tenant`, `sampling`, `constants`,
`errors`, `labels`, `semconv`, `workflow`, and `internal_headers`. Those legacy
module paths may be retained as identity-preserving import-only aliases.

It also owns the fleet's legacy-compatible platform-metrics modules and shared
internal request-signing, tenant-header, tenant-guard, internal-key, and JWT
secret helpers. The compatibility tenant guard uses FastAPI's historical
`HTTPException` contract; Pydantic v2 and FastAPI are therefore explicit
runtime dependencies of this release.

Tenant-facing display and redaction code should import
`maeyr_platform.security.tenant_safe_display`. Existing service-owned display
module paths may remain as identity-preserving import-only aliases during the
migration.

Shared structured-payload truncation should import
`maeyr_platform.truncation.smart_truncate`. The helper preserves whole list
items, emits an explicit downsampling note, and retains the historical
`DEFAULT_SYNTHESIS_BUDGET` contract.

This package is private application infrastructure. It must never be published
to or resolved from public PyPI. Service images receive a reviewed SDK commit as
the named BuildKit context `maeyr_platform_runtime` and install this source with
`--no-index --no-build-isolation --no-deps` before installing their public
requirements and this same local source in one constrained resolver pass.
That pass installs the runtime's declared third-party dependencies without
allowing a same-named public package to substitute for the private source, and
`pip check` rejects an incomplete or incompatible environment. CI and release
automation must pin that private checkout to a full commit SHA. See the source
gate in [`MIGRATION.md`](MIGRATION.md).

The instance APIs are canonical. Functional `configure_*`, `start_*`,
`record_*`, and `stop_*` helpers provide the shared process-level lifecycle
used by current service composition roots. New service code should construct
recorders, signers, and verifiers in its composition root and inject their
protocols.

## Ownership exclusions

This package intentionally does **not** own:

- environment-variable loading or the decision of when startup policy applies;
- route-specific caller allowlists or authorization decisions;
- MongoDB, Redis, HTTP, Temporal, Kubernetes, or cloud-provider clients;
- durable queues, retries, dead-letter handling, or replay/idempotency stores;
- trace/metric ingestion repositories and analytics;
- service/domain event names or business resource semantics; or
- application lifespan ordering beyond the bounded lifecycle protocol.

Services must inject transports that provide the durability and retry semantics
their domain requires. A successful in-memory `record` call means only that the
item entered the bounded local queue; transport acknowledgement defines actual
delivery.

## Remote Trace trust boundary

Production trace producers must configure both `TRACE_SERVICE_URL` and a
minimum-32-byte `TRACE_INTERNAL_KEY`. These values are owned by Trace and are
not interchangeable with `CHAT_SERVICE_URL` or `CHAT_INTERNAL_KEY`. Trace key
rotation uses `TRACE_INTERNAL_KEY_PREVIOUS` only on the receiving service; new
outbound requests are always signed with the current Trace key.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check src tests
ruff format --check src tests
mypy
python -m build
python -m twine check dist/*
python ../../scripts/verify_python_release.py \
  --project-directory . \
  --dist-directory dist \
  --expected-name maeyr-platform-runtime
```

Building and checking a wheel is an internal validation step; it does not
authorize uploading this distribution to any public package registry.

The distribution supports Python 3.10–3.12.

See [`MIGRATION.md`](MIGRATION.md) for the audited mapping from copied service
modules to the canonical APIs and for the boundaries intentionally deferred to
service-owned adapters.
