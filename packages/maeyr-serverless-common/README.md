# Private Serverless common package

Canonical internal contracts used by `serverless-service` and
`chrona-worker-serverless`: tenant-scoped commands, publication/ABI validation,
signed transport, Temporal reference types, worker fleet leases and exact billing
receipts. It contains neither HTTP server routes nor guest execution code.

Both images build this package from the same immutable SDK revision through the
named `maeyr_serverless_common` build context. It is private and must never be
resolved from a public package index. API or receipt changes require compatible
updates and protocol tests for both components.

Fleet and receipt tests use only disposable durable Redis with `appendonly yes`,
`appendfsync always` and `maxmemory-policy noeviction`; never point them at production.
