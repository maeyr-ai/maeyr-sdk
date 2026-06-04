# Viksa AI SDK examples

## `aviation_agent/`

Sample agent with multiple `@mcp_endpoint` handlers and `ViksaAuth.require_param` for an external API key.

- Run locally: `pip install viksa-ai httpx` and import from `viksa_ai.runtime`.
- On the platform: paste `main.py` into the agent editor; configure `aviationstack_api` in the **Auth** tab; do not commit `ViksaAI.py`.

## Validate before deploy

```bash
pip install "viksa-ai[dev]"
viksa-agent-validate ./path/to/agent-directory/
```

Expects `agent.json` (manifest) with `files[]` including `main.py`.

## Platform HTTP client

See [README.md](../README.md#platform-http-client) for `ViksaClient`, auth modes, and typed errors.
