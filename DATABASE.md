# SDK data ownership

The public SDK keeps request configuration, retry state, and connection pools in process; it does not own a database. The private runtime supplies Mongo and Redis helper APIs but does not define a service-owned collection. Consuming services choose database names, tenant filters, indexes, retention, backup, and migrations.

Legacy tracing code uses bounded in-memory buffering when durable trace admission fails. That buffer is best-effort telemetry, not an authoritative event store; overflow can drop the oldest spans. Consumers must decide their trace-loss SLO and configure durable sinks accordingly.

See `DATABASE_INDEXES.md` for the explicit no-owned-index boundary. No live service data, backup, or restore evidence was inspected here.
