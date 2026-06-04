# Viksa AI SDK (`viksa-ai`)

Official Python SDK for the [Viksa AI](https://viksaai.com) platform.

- **Agent runtime** — `mcp_endpoint`, `ViksaAuth`, A2A `context()` (same as injected `ViksaAI.py`)
- **Dev tooling** — validate agent manifests and A2A envelopes locally
- **Platform client** — async/sync HTTP client for `https://api.viksaai.com`

## Install

```bash
pip install viksa-ai
```

## Agent runtime (local development)

```python
from typing import Any, Dict
import httpx
from viksa_ai.runtime import mcp_endpoint, ViksaAuth

BASE_URL = "https://api.example.com/v1"

@mcp_endpoint(description="Search items")
async def search(payload: Dict[str, Any]):
    api_key = ViksaAuth.require_param("my_api", "api_key")
    async with httpx.AsyncClient() as client:
        response = await client.get(BASE_URL, params={"key": api_key})
        return {"results": response.json()}
```

On the platform, an equivalent `ViksaAI.py` is injected automatically. Generate it for diff checks:

```python
from viksa_ai.runtime import to_module_source
print(to_module_source())
```

### Auth at runtime

Secrets resolve to environment variables `{method_id}.{param_name}`:

```python
api_key = ViksaAuth.require_param("aviationstack_api", "api_key")
```

See [Agent auth docs](https://docs.viksaai.com/docs/agents/auth-and-credentials).

### A2A context

```python
from viksa_ai.runtime import context

ctx = context()
run_id = ctx.get("run_id")
```

## Platform API client

```python
import asyncio
from viksa_ai import ViksaClient

async def main():
    async with ViksaClient(
        access_token="...",
        org_id="org-id",
        project_id="project-id",
    ) as client:
        me = await client.auth.me()
        agents = await client.builder.agents.list()
        print(me.email, agents)

asyncio.run(main())
```

Or from environment variables:

```bash
export VIKSA_ACCESS_TOKEN=...
export VIKSA_ORG_ID=...
export VIKSA_PROJECT_ID=...
```

```python
client = ViksaClient.from_env()
```

### Login flow

```python
async with ViksaClient(access_token="placeholder") as client:
    tokens = await client.auth.login("user@example.com", "password")
    # access_token / refresh_token updated on client
```

## Validate an agent locally

```bash
viksa-agent-validate ./examples/aviation_agent/
```

```python
from viksa_ai.devtools import validate_agent_manifest
import json

manifest = json.load(open("agent.json"))
validate_agent_manifest(manifest)  # raises AgentValidationError if invalid
```

## Examples

See [`examples/aviation_agent/`](examples/aviation_agent/) for a full Aviationstack sample.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

## License

Apache-2.0
