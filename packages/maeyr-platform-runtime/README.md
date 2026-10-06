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

PyMongo is a core dependency because the public resource-allocation and license
client contracts use canonical BSON `Int64` for wide grants. Importing these
contracts does not create a database client. Motor-backed database adapters remain
in the optional `mongo` extra, and Redis integrations remain optional.

The instance APIs are canonical. Functional `configure_*`, `start_*`,
`record_*`, and `stop_*` helpers provide the shared process-level lifecycle
used by current service composition roots. New service code should construct
recorders, signers, and verifiers in its composition root and inject their
protocols.

## Ownership exclusions

This package intentionally does **not** own:

- service-specific environment loading or application startup ordering;
- route-specific caller allowlists or authorization decisions;
- creation and connection settings for MongoDB, Redis, HTTP, Temporal,
  Kubernetes, or cloud-provider clients;
- durable queues, retries, dead-letter handling, or replay/idempotency stores;
- trace/metric ingestion repositories and analytics;
- service/domain event names or business resource semantics; or
- application lifespan ordering beyond the bounded lifecycle protocol.

Services must inject transports that provide the durability and retry semantics
their domain requires. A successful in-memory `record` call means only that the
item entered the bounded local queue; transport acknowledgement defines actual
delivery.

## Customer activity and platform observability

`tracing.policy.tenant_trace_category` is the shared positive admission policy.
Tenant `spans` and `traces` retain AI activity and meaningful usage billing:
LLM requests, agent/tool/MCP execution, workflows, channels, schedules,
triggers, approvals, evaluation, lifecycle updates, failures and cancellations.
Execution dependencies on external providers/tools remain correlated. Ordinary
platform HTTP requests, catalog/discovery calls and internal administration
are exported through the separate optional `OTLP_TRACES_ENDPOINT` sink instead.
No configured platform collector means those diagnostics are not persisted in
customer databases. Authentication and signed tenant boundaries still apply.
These diagnostics never enter Maeyr MongoDB collections, durable Redis trace
queues, MongoDB fallback outboxes, dead-letter collections or maintenance wakeups.
Exporter failure discards diagnostics without creating a database fallback.

Important AI/billing spans bypass probabilistic sampling. `TRACE_SAMPLE_RATE`
controls optional platform diagnostics. This is a retention policy, not a
promise of delivery under unlimited outages or storage: the bounded durable
outboxes, retries, acknowledgements and licensed trace/storage admission remain
in force. Billing ledgers remain the authority even if optional telemetry fails.
Trace-ingestion reservation/settlement and Trace's own control calls are excluded
from tenant tracing to prevent recursive billing traffic.

The receiver applies the same policy before storage admission and DLQ replay;
caller-supplied categories cannot override it. Stored root categories are
derived from admitted children, with AI taking precedence over billing. Run,
latency and AI-cost metrics use AI activity only; billing events have separate
counters. Historical HTTP-only records are excluded from customer queries and
metrics. Deploy the runtime, receiver and customer-activity UI together. Existing
HTTP-only documents are not automatically deleted by this source change.
Platform OTLP export uses a separate bounded in-flight budget, default 32
requests (`OTLP_MAX_IN_FLIGHT_EXPORTS`, range 1–128). An unavailable collector
cannot grow unbounded tasks/connections or consume tenant outbox capacity;
`otlp_export_stats()` exposes dropped diagnostic batches and in-flight work.

## Account MongoDB storage admission

`mongo_storage.guard_platform_mongo_client()` wraps a service-owned MongoDB
client after its connection check. All service composition roots must install
this adapter before exposing account database handles. Parent handles,
`with_options()` handles, CRUD, bulk writes and index operations remain guarded.
External customer MongoDB connectors are separate transports and are excluded.

The account record's `limits.max_mongodb_storage_bytes` is the finite byte grant.
Its `mongodb_storage` observation and reservation ledger are shared across
organizations and projects. New grants use 500 MB for Free, 5 GB for Pro and
25 GB for Team, in decimal bytes; Enterprise requires an explicit contract.
Missing or malformed persisted authority fails closed. Existing records are
corrected through an explicit application migration, never a runtime plan
fallback.

The indexed admission path reserves growth atomically, performs the bounded
write and settles its charge. `mongodb_storage_observer` provides the shared,
leased primary measurement of documents plus indexes. Cold refresh is bounded;
active observation uses an indexed due queue instead of scanning all accounts.
Observer readiness verifies complete, committed primary index metadata; an
unfinished, hidden, or TTL-modified observation index cannot satisfy it. Both
lease acquisition and publication require valid canonical BSON grants and
counters. Forced refresh cannot create partial accounting for an unprepared
account; explicit release preparation initializes and measures it first.
Transactions carry the data mutation and its reservation in the same session.
Once admission capacity is reached, every account database mutation is blocked,
including deletes; existing reads remain available.

Growth reservations are estimates, not a proof of exact physical index
allocation. See the [storage policy and MongoDB constraints](../../../auth-service/docs/account-mongodb-storage.md)
for guarantees, unsupported operations, rollout prerequisites and tests.

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
