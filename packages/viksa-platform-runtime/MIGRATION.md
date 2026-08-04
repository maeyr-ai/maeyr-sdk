# Migration map from copied `common/` modules

Version `0.2.0` consolidates contracts, bounded mechanics, and the universally
shared pure tracing primitives,
not every service-specific implementation. The mappings below let a service
move imports without copying legacy source into this distribution.

## Tracing

| Existing API | Canonical API or migration action |
| --- | --- |
| `TraceContext`, `get_trace_context`, `set_trace_context` | Same names in `viksa_platform.tracing` |
| context binding/reset | `bind_trace_context`, `reset_trace_context` |
| `configure_recorder` / `configure_transport` | Same facade names; new code injects `BufferedTraceRecorder` through `TraceRecorder` |
| `start_recorder`, `stop_recorder`, `get_recorder_stats`, `queue_length` | Lifecycle facade plus typed `RecorderStats.queued` |
| `record_span(...)` | Construct immutable `SpanRecord`, then call recorder `record` or facade `record_span` |
| trace/span ID generation and W3C propagation | `TraceContext.new_root`, `.child`, `.from_headers`, `.inject_headers` |
| copied `ids`, `tracestate`, `tenant`, `sampling`, `constants`, `errors`, `labels`, `semconv`, `workflow`, `internal_headers` modules | Exact APIs in matching `viksa_platform.tracing.*` modules; legacy files become true import-only aliases |
| Redis/HTTP/OTLP transport | Implement `TraceTransport` in the service infrastructure adapter |
| ASGI/FastAPI middleware | Service adapter extracts/injects `TraceContext`; framework dependency is intentionally excluded |
| tenant-facing display extraction and generic recursive secret redaction | `viksa_platform.security.tenant_safe_display`; legacy service display modules become identity-preserving aliases |
| resume-state and domain-specific redaction policy | Remains service-owned; it must not be hidden inside generic tracing |
| workflow enrichment and service span constants | Remain domain-owned semantic conventions |
| Trace ingestion schema/repository/search | Remain Trace-service application and infrastructure code |

`viksa_platform.compat.tracing` re-exports the lifecycle facade during import
migration. It does not emulate service-specific span schemas.

## Metrics

| Existing API | Canonical API or migration action |
| --- | --- |
| `UsageContext` | Typed `viksa_platform.metrics.UsageContext` with `TenantContext` |
| `configure_recorder` / `configure_transport` | Same facade names; new code injects `BufferedMetricsRecorder` through `MetricsRecorder` |
| `start_recorder`, `stop_recorder`, `get_recorder_stats`, `queue_length` | Lifecycle facade plus typed `RecorderStats.queued` |
| `record_usage(...)` | Construct immutable `MetricEvent`, then call recorder `record` or facade `record_usage` |
| Redis/HTTP batching and recovery | Implement `MetricsTransport`; durability and recovery remain infrastructure-owned |
| token schema, entity/operation enums, and resource-reference semantics | Exact legacy-compatible contracts in the matching `viksa_platform.metrics.*` submodules; new code should prefer the typed `MetricEvent` API |
| ingestion endpoints and rollups | Remain Trace/Auth/Chat application code |
| copied `platform_metrics.constants`, `context`, `propagation`, `recorder`, `resource_refs`, `schema`, `transport` modules | Exact and additive-compatible APIs in matching `viksa_platform.metrics.*` modules; legacy files become true import-only aliases |

`viksa_platform.compat.metrics` re-exports the lifecycle facade. It does not
guess legacy resource or token semantics.

## Internal security and tenancy

| Existing API | Canonical API or migration action |
| --- | --- |
| `build_canonical_string`, `compute_signature` | Exact v1 facade in `viksa_platform.security.internal` |
| `sign_internal_request`, `verify_internal_signature` | Exact v1 facade; new code injects `RequestSigner` / `RequestVerifier` |
| current/previous secrets | Immutable `KeyRing`; verifier returns the matched `key_slot` |
| tenant header aliases | `TenantContext.from_headers` and `.as_headers` |
| `internal_tenant_headers*` | Exact call-shape facades in `viksa_platform.compat.internal_tenant_headers` |
| `requires_internal_signature` | Exact compatibility helper reads the historical environment flags; new code injects explicit policy into `RequestVerifier` |
| FastAPI tenant guards | Exact compatibility guards retain historical `HTTPException` behavior; new inbound adapters may translate `TenantContext` validation themselves |
| per-route caller allowlists | Application/inbound adapter authorizes `VerifiedCaller.caller` |
| repeated placeholder/weak-secret classification | `SecretStrengthPolicy`; each service still decides where and when to enforce it |
| copied internal tenant headers/guards and startup key/JWT guards | Matching `viksa_platform.security.*` compatibility modules; endpoint authorization remains service-owned |
| JWT/internal-key startup guards | Service bootstrap owns environment and secret-manager policy |
| nonce replay/idempotency store | Service infrastructure owns the durable store and endpoint policy |

`viksa_platform.compat.internal_request_signing` preserves the copied v1
function imports, including the historical environment-policy helper, without
owning route authorization.

## Structured payload truncation

| Existing API | Canonical API or migration action |
| --- | --- |
| copied `common.utils.truncate` / service-domain truncation modules | `viksa_platform.truncation`; legacy files become identity-preserving import-only aliases |
| `DEFAULT_SYNTHESIS_BUDGET`, `smart_truncate` | Same names and behavior in `viksa_platform.truncation` |
| service-specific token budgets and orchestration policy | Remain application-owned and pass an explicit `max_chars` value |

## Required release gate for service aliases

`viksa-platform-runtime==0.2.0` must be published to the Python package index
used by every service build before any service alias or requirement pin is
merged or deployed. Until that release exists, production image builds are
intentionally blocked: they must not fall back to an unpinned version, a local
workspace path, or a copied implementation. Local validation may use the exact
`0.2.0` wheel or this package's source tree only.

After publication, rebuild each service from a clean Docker context and verify
both the installed distribution version and the `viksa_platform/py.typed`
marker before releasing the service image.

## Safe removal gate

A service may delete its copied module only after:

1. its composition root injects package protocols and concrete adapters;
2. import/monkeypatch contract tests no longer target the copied module;
3. exact canonical-byte and tenant propagation tests pass;
4. bounded drain/shutdown is exercised in lifespan tests; and
5. the compiled image smoke test proves the pinned wheel and `py.typed` marker
   are present.
