# Changelog

## 0.2.1 (2026-06-04)

- PyPI release workflow, SDK client expansion, API key auth alignment, docs and CI fixes

## 0.2.0 (2026-06-04)

- Typed HTTP error hierarchy (`ViksaNotFoundError`, `ViksaRateLimitError`, etc.)
- FastAPI `detail` parsing, request ID extraction, retries with backoff
- Expanded API clients: auth org/project, builder secrets/MCP, chat approvals/executions, workflow lifecycle, marketplace, webhooks
- Pagination helpers (`iter_pages`, `iter_all` on list resources)
- `ViksaClient.request()` escape hatch for uncovered routes

## 0.1.0 (2026-06-04)

- Initial release: agent runtime (`ViksaAuth`, `mcp_endpoint`, A2A `context()`)
- Platform HTTP client (`ViksaClient`) for auth, builder, chat, pulse, workflow, scheduler
- Dev tooling: `validate_agent_manifest`, `validate_a2a_envelope`, `viksa-agent-validate` CLI
- Aviation agent example
