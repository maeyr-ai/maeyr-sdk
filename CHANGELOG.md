# Changelog

## 0.2.8 (2026-06-06)

- **Breaking:** `viksa-mcp-bridge` is now a stdio proxy to **mcp-gateway-service** (Streamable HTTP); requires `VIKSA_MCP_TOKEN`, not `VIKSA_API_KEY` + builder discovery
- Removed duplicated `mcp_bridge` registry/tool/mapping logic from the SDK (canonical implementation lives in mcp-gateway-service)
- Prefer Cursor `url` + `VIKSA_MCP_TOKEN` over stdio when possible

## 0.2.7 (2026-06-06)

- MCP bridge: honor `outputs[].nullable` in tool `outputSchema` (JSON Schema `["type", "null"]`, omit from `required`)
- MCP bridge: fill absent nullable output fields with `null` in structured tool results
- MCP bridge: treat builder `type: dict` outputs as unconstrained JSON Schema (accepts string/number/object at runtime)
- MCP bridge: infer output JSON Schema types from `outputs[].example` when provided
- SDK: `AgentOutput.nullable` / `AgentUpdateRequest.outputs` aligned with builder-service models

## 0.2.6 (2026-06-06)

- MCP bridge: tool names use `{agent_alias}_{endpoint}` (no `viksa_` prefix) for Cursor length limits
- MCP bridge: return structured MCP output when `outputSchema` is set
- MCP bridge: resolve Temporal `task_queue` as `{org}-{project}-CLOUD` so pulse executions reach workers

## 0.2.5 (2026-06-06)

- MCP bridge: use underscore tool names (`viksa_{alias}_{endpoint}`) for Cursor and other MCP clients that reject dots

## 0.2.4 (2026-06-06)

- Fix builder HTTP client paths: all routes now use `/builder/...` gateway prefix (fixes MCP bridge 404s)

## 0.2.3 (2026-06-06)

- MCP bridge: resolve `mapping_id` via builder `/mappings/{id}` and enrich tool input schemas
- MCP bridge: emit `outputSchema` from agent `outputs` + endpoint `outputs[]`
- MCP bridge: expose `ai_guidelines` in server instructions and `viksa://agent/{id}/guidelines` resources
- MCP bridge: `viksa://mappings` resource catalog; live registry refresh (`--refresh-interval`, default 60s)
- MCP bridge: disambiguate tool names when multiple agents share alias+endpoint (`viksa.{alias}.{ep}.{id}`)
- SDK: `client.builder.mappings.get` / `get_many`

## 0.2.2 (2026-06-06)

- `viksa-mcp-bridge` CLI: expose deployed Viksa agent endpoints as MCP tools over stdio
- Optional `[mcp]` extra (`pip install "viksa-ai[mcp]"`) for Cursor / Claude Desktop integration
- Tool discovery from builder API; execution via `pulse.execute`

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
