# viksa-platform-runtime

`viksa-platform-runtime` is the independently versioned, PEP 561-typed boundary
for cross-cutting Viksa service behavior. Version `0.2.0` provides:

- instance-based, bounded trace and usage recorders;
- typed trace, usage, tenant, and caller contexts;
- transport and bounded-lifecycle protocols for dependency injection;
- exact-body HMAC request signing with current/previous key verification;
- immutable secret-strength classification for service-owned startup policy;
- tenant-safe display extraction and recursive secret redaction;
- structure-aware truncation for bounded LLM context payloads; and
- thin functional facades for staged migration from copied `common/` modules.

Version `0.2.0` also owns the stable tracing primitives historically copied as
`common.platform_traces.ids`, `tracestate`, `tenant`, `sampling`, `constants`,
`errors`, `labels`, `semconv`, `workflow`, and `internal_headers`. Those legacy
module paths may be retained as identity-preserving import-only aliases.

It also owns the fleet's legacy-compatible platform-metrics modules and shared
internal request-signing, tenant-header, tenant-guard, internal-key, and JWT
secret helpers. The compatibility tenant guard uses FastAPI's historical
`HTTPException` contract; Pydantic v2 and FastAPI are therefore explicit
runtime dependencies of this release.

Tenant-facing display and redaction code should import
`viksa_platform.security.tenant_safe_display`. Existing service-owned display
module paths may remain as identity-preserving import-only aliases during the
migration.

Shared structured-payload truncation should import
`viksa_platform.truncation.smart_truncate`. The helper preserves whole list
items, emits an explicit downsampling note, and retains the historical
`DEFAULT_SYNTHESIS_BUDGET` contract.

The fleet migration has a hard release dependency: version `0.2.0` must exist
in the package index used by production builds before any service-side alias or
requirement pin is merged or deployed. Local source and wheel installs are for
validation only; production builds must never substitute an unpinned package
or workspace path. See the release gate in [`MIGRATION.md`](MIGRATION.md).

The instance APIs are canonical. Functional `configure_*`, `start_*`,
`record_*`, and `stop_*` helpers hold process-global state only to support a
bounded compatibility migration. New service code should construct recorders,
signers, and verifiers in its composition root and inject their protocols.

## Ownership exclusions

Apart from the exact legacy `requires_internal_signature` compatibility helper,
this package intentionally does **not** own:

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

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check src tests
mypy
python -m build
```

The distribution supports Python 3.10–3.12.

See [`MIGRATION.md`](MIGRATION.md) for the audited mapping from copied service
modules to the canonical APIs and for the boundaries intentionally deferred to
service-owned adapters.
