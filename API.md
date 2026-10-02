# SDK API

Public imports include `maeyr.client.MaeyrClient` and related typed clients for Auth, Builder, Chat, Marketplace, MCP, Pulse, Scheduler, Workflow, and webhooks. `maeyr-agent-validate` and `maeyr-mcp-bridge` are CLI entrypoints. Public API compatibility is governed by `src/maeyr` and the package's semantic version.

The private `maeyr_platform` namespace exposes shared runtime contracts to backend services; it is installed separately from a reviewed source SHA. It should not be treated as part of the public PyPI distribution. Tracing's new task admission changes scheduling limits, not span or tenant wire formats.

This source inventory was not checked against a live public API or every consuming service version. Tests and wheel-content verification are the local contract evidence.
