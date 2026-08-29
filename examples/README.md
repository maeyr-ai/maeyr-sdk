# Maeyr SDK examples

## `aviation_agent/`

Sample agent with multiple `@mcp_endpoint` handlers and `MaeyrAuth.require_param` for an external API key.

- Run locally: `pip install maeyr httpx` and import from `maeyr.runtime`.
- On the platform: paste `main.py` into the agent editor; configure `aviationstack_api` in the **Auth** tab; do not commit `Maeyr.py`.

## Validate before deploy

```bash
pip install "maeyr[dev]"
maeyr-agent-validate ./path/to/agent-directory/
```

Expects `agent.json` (manifest) with `files[]` including `main.py`.

## Platform HTTP client

See [README.md](../README.md#platform-http-client) for `MaeyrClient`, auth modes, and typed errors.
