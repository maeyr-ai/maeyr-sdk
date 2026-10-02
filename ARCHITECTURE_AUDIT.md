# Maeyr SDK and private runtime architecture audit

Audit date: 2026-10-01. This is a source and local-test audit. The repository
publishes a public SDK and contains a separately built private platform runtime.
It is a library repository, so Kubernetes resources and MongoDB collection
indexes belong to consuming services rather than this repository.

## Current architecture and execution flow

- Public `src/maeyr` supports Python 3.10+, typed async/sync HTTP clients,
  agent runtime helpers, manifest validation, and an MCP bridge. The transport
  reuses `httpx` clients, applies explicit timeout and safe idempotency rules,
  and retries configured transient responses.
- `packages/maeyr-platform-runtime/src/maeyr_platform` provides private shared
  security, Mongo/Redis adapters, metrics, tracing, tenant identity, and
  execution utilities to independently built services. It has its own
  `pyproject.toml`, tests, and migration notes.
- The public SDK CI runs Ruff, pytest, coverage, and distribution checks on
  Python 3.10/3.11/3.12. The workflows now also enter the private runtime's
  test tree. Backend Docker builds consume a reviewed runtime commit SHA.

## Findings and changes

| Area | Evidence | Status and next step |
| --- | --- | --- |
| Trace task fan-out | Legacy `tracing/server_span.py`, `http_client.py`, `recorder.py`, and `transport.py` created detached tasks per span and per sink failure. | Fixed: shared task admission caps in-flight trace I/O, tracks drops, drains on recorder stop, and coalesces immediate flush and dead-letter drain tasks. Validate under slow Redis/HTTP sinks. |
| Trace fallback durability | `tracing/recorder.py` uses a capped 20,000-item memory deque when durable admission fails. | Memory is bounded; overflow loses oldest telemetry. Keep explicit drop metrics and decide whether the durable outbox meets the desired trace loss SLO. |
| HTTP retry timing | `src/maeyr/client/transport.py:20-24` uses capped exponential delay without jitter or an overall request deadline. | Add jitter and a total deadline after establishing compatibility expectations; current mutation retries already require idempotency keys. |
| Runtime CI | `.github/workflows/ci.yml` previously ran public SDK checks only. | Fixed private runtime Ruff and pytest in CI and PyPI pre-release gate, plus direct dependency audit before publish. Strict Mypy, private wheel check, and remote execution evidence remain open. |

## Reliability, security, and performance

The public client uses connection reuse and per-request timeout settings.
The runtime's newer `BufferedDispatcher` offers bounded queues and a supervised
worker; legacy trace call sites still need the new task cap until they migrate
to that dispatcher. Drops are preferable to uncontrolled process memory growth
for best-effort telemetry, but the trace-loss policy must be explicit. The
optional OTLP exporter is present but has no current call site in this repo.

No service-owned MongoDB collection is defined here, so `DATABASE_INDEXES.md`
records the no-owned-index boundary. Consumers must document and explain their own queries.
No live dependency, soak, chaos, or benchmark run was performed.

## Migration and validation plan

1. Release the private runtime to services by reviewed immutable SHA and
   monitor `background_tasks_inflight`/`background_tasks_dropped` during slow
   sink tests and rollout.
2. Migrate remaining legacy fire-and-forget tracing entrypoints to the existing
   bounded dispatcher, preserving span/tenant wire formats.
3. Add private runtime CI and run wheel import tests in each consuming service.
4. Benchmark HTTP retry/client behavior and trace throughput before changing
   connection pool, queue, or sampling defaults.

Local verification: 309 private runtime tests passed in the Directory Sync
test environment, including the new trace task bounds and wheel contents; 60
public SDK tests passed in the SDK environment. Public and private runtime
Ruff passed. Consumer-service suites ran separately. No live trace
sink or production service was contacted.
