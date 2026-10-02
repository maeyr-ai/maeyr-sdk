# SDK performance

`src/maeyr/client/transport.py` reuses sync and async `httpx` clients, applies per-request timeouts, and retries configured transient errors. Mutation retries require idempotency keys. Current backoff is capped exponential without jitter or an overall request deadline; benchmark behavior under bursts before changing the public client contract.

The private runtime's legacy tracing entrypoints now admit bounded background tasks, coalesce flush/dead-letter work, count drops, and drain at recorder shutdown. `BufferedDispatcher` also has a bounded queue and supervised worker. Drops bound process memory when sinks are slow but require an explicit telemetry-loss policy.

Measure client p95/p99, retry amplification, pool wait, span enqueue/drop rate, sink latency, and shutdown drain time under slow Redis/HTTP sinks. No live load, soak, or benchmark result was collected.
