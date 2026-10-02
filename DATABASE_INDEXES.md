# SDK index applicability

Neither the public `maeyr` distribution nor the private `maeyr-platform-runtime` package owns a MongoDB collection or index migration. `maeyr_platform.mongo` offers reusable connection/projection helpers; collection schemas and query plans belong to Auth, Builder, Directory Sync, Marketplace, and other consuming services.

No index creation is proposed from this library repository. Each consumer should document its collection filters, index ownership, live `explain("executionStats")`, cardinality, and write cost. No live catalog or plan was queried in this audit.
