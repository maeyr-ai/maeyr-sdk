# SDK disaster recovery

The two distributions are rebuildable from Git source and package metadata; they hold no authoritative business database. Preserve signed release tags, source provenance, built wheel artifacts, and the exact private-runtime SHAs used by backend images. Consumer databases, Redis, and trace sinks have their own recovery plans.

To recover a bad release, identify affected public versions and backend SHA pins, build/test the previous reviewed source, verify the public wheel excludes private runtime, and roll consumers through their normal deployment gate. Confirm client imports and one safe request path, then observe trace drops and service readiness.

No timed release rollback, artifact registry disaster drill, measured RPO/RTO, or live consumer recovery was performed.
