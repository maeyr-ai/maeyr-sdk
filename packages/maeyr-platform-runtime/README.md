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

- service-specific environment loading or application startup ordering;
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

## Logging policy

All application services use `MAEYR_LOG_LEVEL`, defaulting to `WARNING`.
`MAEYR_LOG_OVERRIDES` accepts JSON with `accounts`, `organizations`, and
`projects` maps of exact tenant identifiers to `INFO`, `DEBUG`, or `WARNING`.
The most specific configured scope wins: project, organization, account, then
the global level. Configuration is validated before use; wildcards and unknown
levels or scopes are rejected.

The deployment configuration is the source of these environment variables:

```yaml
application:
  logging:
    level: WARNING
    overrides:
      accounts:
        AC-example: INFO
      organizations:
        OI-example: DEBUG
      projects:
        PI-example: WARNING
```

Python services use the shared structured logger and handler filter, including
third-party logs such as `httpx`. The Node server and hosted worker adapters use
the same policy contract. An authentication boundary must verify tenant context
before it can enable a scoped override; inbound headers and failed credentials
cannot enable verbose logging. Background jobs and isolated hosted agents use
their server-owned tenant context or its resolved threshold. Console diagnostics
and customer progress delivery remain separate.

Services must call `configure_logging()` at startup and use `get_logger()`.
Do not set logger thresholds per request or attach unfiltered handlers: concurrent
requests must retain independent tenant thresholds. The root logger accepts the
lowest configured level, while each output handler enforces the verified
request's policy. JSON output retains tenant and trace identifiers and redacts
sensitive fields and URL queries.

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
