# Maeyr SDK examples

## `aviation_agent/`

Sample agent with multiple `@mcp_endpoint` handlers and `MaeyrAuth.require_param` for an external API key.

- Run locally: `pip install maeyr httpx` and import from `maeyr.runtime`.
- On the platform: paste `main.py` into the agent editor; configure `aviationstack_api` in the **Auth** tab; do not commit `Maeyr.py`.
- The example uses AviationStack HTTPS with explicit request timeouts. Cloud workers allow public HTTPS on port 443; DevSpace uses an outbound proxy, so a plain HTTP URL that works there can still time out in a deployed worker.
- Request, HTTP, provider, and invalid-response failures raise a sanitized error so the run fails visibly. A successful empty `data` list remains a valid empty result; errors never include the access key, request URL, or raw provider message.
- Changing this local example does not update an existing agent. Copy the revised source into its platform editor, save it, and redeploy the agent to update Run/chat execution. DevSpace file sync alone does not update the deployed cloud worker's agent snapshot.

## Validate before deploy

```bash
pip install "maeyr[dev]"
maeyr-agent-validate ./path/to/agent-directory/
```

Expects `agent.json` (manifest) with `files[]` including `main.py`.

## Platform HTTP client

See [README.md](../README.md#platform-http-client) for `MaeyrClient`, auth modes, and typed errors.
