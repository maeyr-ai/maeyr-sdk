# Maeyr SDK (`maeyr`)

[![PyPI version](https://img.shields.io/pypi/v/maeyr)](https://pypi.org/project/maeyr/)
[![Python](https://img.shields.io/pypi/pyversions/maeyr)](https://pypi.org/project/maeyr/)
[![License](https://img.shields.io/github/license/maeyr-ai/maeyr-sdk)](https://github.com/maeyr-ai/maeyr-sdk/blob/main/LICENSE)
[![CI](https://github.com/maeyr-ai/maeyr-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/maeyr-ai/maeyr-sdk/actions/workflows/ci.yml)

Official Python SDK for the [Maeyr](https://maeyr.com) platform. Use it to author agents locally, call platform APIs from scripts and automation, and validate agent manifests before deploy.

| Module | Purpose |
|--------|---------|
| [`maeyr.runtime`](#agent-runtime) | Same API as injected `Maeyr.py` (`mcp_endpoint`, `MaeyrAuth`, A2A `context()`) |
| [`maeyr.client`](#platform-http-client) | Typed async HTTP client for `https://api.maeyr.com` |
| [`maeyr.devtools`](#development-tooling) | AST + schema validation for generated agents |
| [`maeyr.mcp_bridge`](#mcp-bridge-cursor--claude) | Stdio MCP proxy to the hosted Maeyr MCP gateway |
| [`maeyr.models`](#data-models) | Pydantic models for API requests and A2A envelopes |

---

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Agent runtime](#agent-runtime)
  - [mcp_endpoint](#mcp_endpoint)
  - [MaeyrAuth](#maeyrauth)
  - [A2A context](#a2a-context)
  - [Injected Maeyr.py](#injected-maeyri-py)
- [Platform HTTP client](#platform-http-client)
  - [Authentication](#client-authentication)
  - [MaeyrClient reference](#maeyrclient-reference)
  - [Errors](#errors)
  - [SSE streaming](#sse-streaming)
- [MCP bridge (Cursor / Claude)](#mcp-bridge-cursor--claude)
- [Development tooling](#development-tooling)
- [Data models](#data-models)
- [Examples](#examples)
- [Contributing](#contributing)
- [Changelog](#changelog)
- [License](#license)

---

## Installation

**Requirements:** Python 3.10+

```bash
pip install maeyr
```

With dev dependencies (tests, ruff, build):

```bash
pip install "maeyr[dev]"
```

---

## Quick start

### Author an agent endpoint

```python
from typing import Any, Dict

import httpx

from maeyr.runtime import MaeyrAuth, mcp_endpoint

BASE_URL = "https://api.aviationstack.com/v1"


@mcp_endpoint(description="Get flights between source and destination")
async def get_flights_between(payload: Dict[str, Any]):
    source = payload.get("source")
    destination = payload.get("destination")
    api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/flights",
            params={
                "access_key": api_key,
                "dep_iata": source,
                "arr_iata": destination,
            },
        )
        data = response.json()
        return {"flights": data.get("data", [])}
```

### Call the platform API

```python
import asyncio

from maeyr import MaeyrClient


async def main():
    async with MaeyrClient(
        access_token="YOUR_ACCESS_TOKEN",
        org_id="YOUR_ORG_ID",
        project_id="YOUR_PROJECT_ID",
    ) as client:
        me = await client.auth.me()
        agents = await client.builder.agents.list(limit=10)
        print(me.email, agents.get("total", len(agents)))


asyncio.run(main())
```

---

## Agent runtime

Import from `maeyr.runtime` (or `Maeyr` after platform injection). This is the **canonical** implementation of the file the platform writes as `Maeyr.py` on every agent.

### `mcp_endpoint`

Decorator that marks an async function as an MCP-style agent endpoint. Metadata is used by the platform UI and validators; execution routing uses `agent_endpoints` in the agent manifest, not decorator introspection.

```python
from maeyr.runtime import mcp_endpoint

@mcp_endpoint(description="Human-readable description for docs and UI")
async def my_tool(payload: dict):
    ...
```

**Convention:** one parameter named `payload: Dict[str, Any]`, with inputs read via `payload.get("name")` or `payload["name"]`.

### `MaeyrAuth`

Reads auth configuration injected at deploy time as environment variables.

| Environment variable | Meaning |
|---------------------|---------|
| `{method_id}.{param_name}` | Resolved secret or param value (e.g. `bearer_token.api_key`) |
| `MAEYR_AUTH_ENABLED_METHODS` | Comma-separated method ids with **all** params resolved |
| `MAEYR_AUTH_CONFIGURED_METHODS` | All method ids declared on the agent (including incomplete) |

| Method | Description |
|--------|-------------|
| `get_configured_methods()` | Method ids declared for this agent |
| `is_method_configured(method_id)` | Whether the method belongs to this agent |
| `get_enabled_methods()` | Method ids fully enabled at deploy |
| `is_method_enabled(method_id)` | Whether the method is in the enabled set |
| `get_param(method_id, param_name)` | Param value or `None` |
| `require_param(method_id, param_name)` | Param value or raises `MaeyrAuthError` |
| `get_method_params(method_id)` | Dict of all params for one method |
| `preferred_method(*candidates)` | First enabled candidate, or `None` |
| `require_method(*candidates)` | First enabled candidate, or raises |

```python
from maeyr.runtime import MaeyrAuth, MaeyrAuthError

# Single required credential
api_key = MaeyrAuth.require_param("aviationstack_api", "api_key")

# Multiple auth methods
method = MaeyrAuth.preferred_method("oauth_client", "bearer_token")
if method == "oauth_client":
    client_id = MaeyrAuth.require_param("oauth_client", "client_id")
else:
    api_key = MaeyrAuth.require_param("bearer_token", "api_key")
```

Platform docs: [Agent auth and credentials](https://docs.maeyr.com/docs/agents/auth-and-credentials).

### A2A context

Agent-to-agent calls can attach an optional envelope under the reserved payload key `__maeyr_a2a__`. Use `context()` to read correlation metadata inside your endpoint.

| Symbol | Role |
|--------|------|
| `A2A_PAYLOAD_KEY` | `"__maeyr_a2a__"` — wire key in workflow inputs |
| `attach_envelope(inputs, envelope)` | Attach envelope to an inputs dict (callers / integrators) |
| `context()` | Copy of the current call envelope, or `{}` |

`A2AContext` is a `TypedDict` documenting common keys: `run_id`, `parent_step_id`, `caller_agent`, `callee_agent`, `endpoint`, `idempotency_key`, `deadline_at`, `metadata`.

```python
from maeyr.runtime import attach_envelope, context

# Building a call (integrator / orchestrator)
inputs = attach_envelope(
    {"source": "JFK", "destination": "LAX"},
    {"run_id": "run-123", "parent_step_id": "step-9"},
)

# Inside an agent endpoint
ctx = context()
run_id = ctx.get("run_id")
parent = ctx.get("parent_step_id")
```

### Injected `Maeyr.py`

The platform injects `Maeyr.py` into every agent’s file list. Generate the canonical module body from the SDK:

```python
from maeyr.runtime.inject import to_module_source

body = to_module_source()
```

---

## Platform HTTP client

Base URL default: `https://api.maeyr.com`. Requests are routed per service prefix (`/auth`, `/builder`, `/chat`, etc.), matching the platform API gateway.

### Client authentication

Protected routes expect:

| Header | Value |
|--------|--------|
| `Authorization` | `Bearer <access_token>` or `Bearer <api_key>` |
| `X-Tenant-Org-Id` | Active organization id |
| `X-Tenant-Project-Id` | Active project id |

The SDK supports three ways to authenticate:

| Mode | How to create the client |
|------|---------------------------|
| **JWT** (default) | `MaeyrClient(access_token="...")` or `MAEYR_ACCESS_TOKEN` |
| **API key** | `MaeyrClient.from_api_key("...")` or `MAEYR_API_KEY` |
| **Email / password** | `await MaeyrClient.from_login(email, password)` or `MAEYR_EMAIL` + `MAEYR_PASSWORD` |

Set **`base_url`** on the client (or `MAEYR_BASE_URL`) for staging, self-hosted, or regional gateways. Default: `https://api.maeyr.com`.

```python
from maeyr import MaeyrClient

# JWT from the console or a prior login
async with MaeyrClient(access_token="eyJ...", org_id="org", project_id="proj") as client:
    me = await client.auth.me()

# Project API key (optional validate=True to resolve org/project)
client = MaeyrClient.from_api_key("vk_...", base_url="https://api.maeyr.com")

# Login
client = await MaeyrClient.from_login("you@example.com", "password", base_url="https://api.maeyr.com")
```

**Environment variables** (for `MaeyrClient.from_env()` — first match wins):

| Variable | Description |
|----------|-------------|
| `MAEYR_API_KEY` | Project API key |
| `MAEYR_ACCESS_TOKEN` | JWT access token |
| `MAEYR_EMAIL` + `MAEYR_PASSWORD` | Log in and obtain a JWT |
| `MAEYR_ORG_ID` | Tenant org id (optional) |
| `MAEYR_PROJECT_ID` | Tenant project id (optional) |
| `MAEYR_REFRESH_TOKEN` | Enables automatic refresh on 401 (JWT only) |
| `MAEYR_BASE_URL` | API base URL (default `https://api.maeyr.com`) |

```python
from maeyr import MaeyrClient

client = MaeyrClient.from_env()
```

Validate an API key without a full session: `await client.auth.validate_api_key("vk_...")`.

### `MaeyrClient` reference

```python
from maeyr import MaeyrClient

async with MaeyrClient(
    access_token="...",        # JWT, or use from_api_key / from_login
    org_id="...",
    project_id="...",
    refresh_token="...",       # optional; JWT only
    base_url="https://api.maeyr.com",  # configurable
    timeout=60.0,
) as client:
    ...
```

Sub-clients are created on the root client:

```text
MaeyrClient
├── auth (+ auth.orgs, auth.projects)
├── builder.agents | deploy | secrets | mappings | mcp
├── chat (+ triggers, approvals)
├── pulse
├── workflow.executions
├── scheduler
└── marketplace.listings | workforce | publishers
```

Use `MaeyrClient.webhook(trigger_id, webhook_token=...)` for public webhook routes (no JWT).
Use `client.request(method, prefix, path)` for any route not yet wrapped.

#### `client.auth`

| Method | HTTP | Description |
|--------|------|-------------|
| `login(email, password)` | `POST /auth/individual/login` | Returns `TokenResponse`; updates client tokens |
| `login_sync(...)` | same | Synchronous variant |
| `refresh()` | `POST /auth/refresh` | Requires `refresh_token` on client |
| `refresh_sync()` | same | Synchronous variant |
| `me()` | `GET /auth/me` | Current user (`UserResponse`) |
| `me_sync()` | same | Synchronous variant |
| `switch_org(org_id)` | `POST /auth/switch-org` | New tokens + org context |
| `switch_project(project_id)` | `POST /auth/switch-project` | New tokens + project context |
| `logout()`, `logout_all()` | `POST /auth/logout*` | End session(s) |
| `usage()` | `GET /auth/usage` | Plan usage |
| `list_sessions()`, `revoke_session(id)` | session management | |
| `create_api_key`, `list_api_keys`, `revoke_api_key`, `delete_api_key` | `/auth/key/api` | API keys |
| `auth.orgs.*`, `auth.projects.*` | `/org`, `/project` | Org/project CRUD |

#### `client.builder.agents`

| Method | HTTP | Description |
|--------|------|-------------|
| `create(request)` | `POST /agent/create` | `AgentCreationRequest` → agent doc |
| `list(skip, limit, search)` | `GET /agent/list` | Paginated agent list |
| `get(agent_id)` | `GET /agent/{id}` | Agent detail |
| `update(agent_id, request)` | `PUT /agent/{id}` | `AgentUpdateRequest` |
| `delete(agent_id)` | `DELETE /agent/{id}` | Returns typed `AgentDeletionResult`; only `result.complete` means deletion finished. `approval_pending` and `quota_release_pending` must be retried/observed. |
| `set_endpoint_status(agent_id, name, enabled=...)` | `PATCH /agent/{id}/endpoint/{name}/status` | Enable/disable endpoint |
| `revisions`, `revision`, `share`, `set_status` | agent lifecycle | |
| `iter_all()` | paginated `list` | Async iterator over all agents |
| `secrets.update_secret`, `delete_secret`, `secret_usage` | vault secrets | |
| `mcp.update`, `delete`, `start`, `stop` | hosted MCP servers | |

#### `client.builder.deploy`

| Method | HTTP | Description |
|--------|------|-------------|
| `build(agent_id)` | `POST /builder/` | Start build job |
| `deploy(agent_id)` | `POST /deploy/` | Schedule deploy |
| `reconcile(agent_id)` | `POST /deploy/reconcile` | Hot-reload config (cloud) |

#### `client.builder.secrets`

| Method | HTTP | Description |
|--------|------|-------------|
| `vault_status()` | `GET /vault/status` | Vault configuration state |
| `create_secret(name, value, description=...)` | `POST /secret/create` | Create vault secret |
| `list_secrets(skip, limit, search)` | `GET /secret/list` | List secrets |
| `get_secret(secret_id)` | `GET /secret/{id}` | Get secret metadata/value |

#### `client.builder.mappings`

| Method | HTTP | Description |
|--------|------|-------------|
| `get(mapping_id)` | `GET /mappings/{id}` | Mapping detail |
| `get_many(mapping_ids)` | parallel `GET` | Up to 100 mappings (used by MCP bridge) |

#### `client.builder.mcp`

| Method | HTTP | Description |
|--------|------|-------------|
| `create(body)` | `POST /mcp/servers` | Register hosted MCP server |
| `list(status, skip, limit)` | `GET /mcp/servers` | List servers |
| `get(server_id)` | `GET /mcp/servers/{id}` | Server detail |

#### `client.chat`

| Method | HTTP | Description |
|--------|------|-------------|
| `indent_finder(message, conversation_id=..., workforce_id=...)` | `POST /chat/indent_finder` | Chat / intent routing |
| `stream_indent_finder(...)` | `POST /chat/indent_finder/stream` | Async iterator of SSE JSON events |
| `list_conversations(skip, limit)` | `GET /chat/conversations` | Conversation list |
| `get_conversation(id)` | `GET /chat/conversations/{id}` | Messages + metadata |
| `generate_agent(prompt)` | `POST /chat/generate/agent` | AI agent generation |
| `fix_agent(body)` | `POST /chat/fix/agent` | AI agent fix pass |
| `cancel_execution`, `active_execution`, `stream_execution` | execution control | |
| `patch_conversation`, `delete_conversation`, `search`, `token_stats` | conversations | |
| `approvals.list/get/decide` | HITL approvals | |

#### `client.chat.triggers`

| Method | HTTP | Description |
|--------|------|-------------|
| `create(body)` | `POST /chat/trigger` | Create trigger |
| `list(skip, limit)` | `GET /chat/trigger` | List triggers |
| `get(trigger_id)` | `GET /chat/trigger/{id}` | Trigger detail |
| `update(trigger_id, body)` | `PATCH /chat/trigger/{id}` | Update trigger |
| `delete(trigger_id)` | `DELETE /chat/trigger/{id}` | Delete trigger |
| `test(trigger_id)` | SSE test run | |
| `list_executions(trigger_id)` | execution history | |

#### `client.marketplace`

| Method | Description |
|--------|-------------|
| `listings.create/search/list/get/publish/install` | Agent listings |
| `workforce.publish/search/install` | Workforce listings |
| `publishers.create_profile/me/update_profile` | Publisher profile |
| `categories()`, `installations()` | Catalog metadata |

#### `client.pulse`

Typed models: `EndpointExecutionRequest`, `EndpointExecutionResponse`, `AgentInvokeRequest`, `AgentInvokeResponse`.

| Method | HTTP | Description |
|--------|------|-------------|
| `execute(request)` | `POST /pulse/executor/execute` | Run endpoint via Temporal (sync result) |
| `execute_sync(request)` | same | Synchronous HTTP client |
| `invoke(request)` | `POST /pulse/executor/invoke` | Fire-and-forget invoke |

Endpoint path format: `{agent_alias}.{module}.{function}` (e.g. `my_agent.main.search`).

```python
from maeyr.models.executor import AgentType, EndpointExecutionRequest

result = await client.pulse.execute(
    EndpointExecutionRequest(
        agent_id="...",
        agent_type=AgentType.CLOUD,
        endpoint="my_agent.main.search",
        inputs={"query": "flights to LAX"},
    )
)
```

#### `client.workflow`

| Method | HTTP | Description |
|--------|------|-------------|
| `start(workflow_id, trigger_source=...)` | `POST /workflow/start` | Start workflow |
| `get(workflow_id)` | `GET /workflow/id/{id}` | Workflow definition |
| `list(skip, limit)` | `GET /workflow/list` | List workflows |
| `delete(workflow_id)` | `DELETE /workflow/{id}` | Delete workflow |

#### `client.workflow.executions`

| Method | HTTP | Description |
|--------|------|-------------|
| `create(workflow_id, schedule_id=...)` | `POST /workflow/execution/create` | Create execution |
| `get(execution_id)` | `GET /workflow/execution/{id}` | Execution detail |
| `list(skip, limit)` | `GET /workflow/execution/list` | List executions |
| `start(execution_id)` | `POST /workflow/execution/{id}/start` | Start execution |
| `retry`, `stop`, `delete`, `patch_tasks`, `list_for_workflow` | execution lifecycle | |

#### `client.scheduler`

| Method | HTTP | Description |
|--------|------|-------------|
| `create(body, schedule_id=...)` | `POST /scheduler/schedule/create` | Create schedule with a stable retry ID |
| `list(skip, limit)` | `GET /scheduler/schedule/list` | List schedules |
| `get(schedule_id)` | `GET /scheduler/schedule/{id}` | Schedule detail |
| `update(schedule_id, body)` | `PATCH /scheduler/schedule/{id}` | Update schedule |
| `delete(schedule_id)` | `DELETE /scheduler/schedule/{id}` | Delete schedule |
| `pause(schedule_id)` | `POST /scheduler/schedule/{id}/pause` | Pause schedule |
| `resume(schedule_id)` | `POST /scheduler/schedule/{id}/resume` | Resume schedule |
| `run_now(schedule_id)` | `POST /scheduler/schedule/{id}/run-now` | Trigger immediate run |

#### Webhooks (no JWT)

```python
wh = MaeyrClient.webhook("trigger-id", webhook_token="...")
await wh.invoke({"event": "order.created"})
async for evt in wh.stream({"event": "order.created"}):
    print(evt)
```

**Coverage note:** The SDK wraps major public platform flows. Routes not yet listed can be called via `await client.request("GET", "/builder", "/path")`.

### Error handling (v0.2)

The client maps HTTP failures to typed exceptions and parses FastAPI `detail` payloads.

| Exception | HTTP | When |
|-----------|------|------|
| `MaeyrTransportError` | — | Timeouts, connection failures, DNS |
| `MaeyrAuthenticationError` | 401 | Invalid or expired token |
| `MaeyrPermissionError` | 403 | RBAC / tenant denial |
| `MaeyrNotFoundError` | 404 | Missing resource |
| `MaeyrConflictError` | 409 | State conflict |
| `MaeyrValidationError` | 422 | Schema / field validation |
| `MaeyrRateLimitError` | 429 | Rate limited (`retry_after` set) |
| `MaeyrServerError` | 5xx | Platform or gateway errors |
| `MaeyrApiError` | other | Base class for all API errors |

```python
from maeyr import MaeyrClient, MaeyrNotFoundError, MaeyrValidationError

try:
    await client.builder.agents.get("missing-id")
except MaeyrNotFoundError as e:
    print(e.status_code, e.detail_message, e.request_id)
    for d in e.details:
        print(d.field, d.message)
except MaeyrValidationError as e:
    print(e.body)
```

| `MaeyrApiError` attribute | Description |
|---------------------------|-------------|
| `status_code` | HTTP status |
| `method`, `path`, `service` | Request context |
| `body` | Raw JSON or text |
| `details` | List of `ErrorDetail` (message, field, code) |
| `request_id` | From `X-Request-Id` / correlation headers when present |
| `retry_after` | Seconds from `Retry-After` on 429 |
| `detail_message` | First human-readable message |

**Retries:** transient failures (`429`, `502`, `503`, `504`) and connection errors retry with exponential backoff (configurable via `RetryConfig`).

**401 refresh:** if `refresh_token` is set, one automatic token refresh and retry per request.

```python
from maeyr.client import ClientConfig, RetryConfig

client = MaeyrClient(
    token,
    config=ClientConfig(
        retry=RetryConfig(max_retries=5),
        idempotency_key="create-agent-42",
    ),
)
```

### Pagination

```python
async for agent in client.builder.agents.iter_all(search="flight"):
    print(agent["agent_name"])

async for conv in client.chat.iter_conversations():
    ...
```

### SSE streaming

`client.chat.stream_indent_finder` yields parsed JSON objects from `data: {...}` SSE lines.
Both `indent_finder` variants accept `schedule_id=...`; reuse it when retrying
an ambiguous chat turn that may have created a schedule.

`MaeyrClient.iter_sse_lines(response)` is a static helper for custom streaming endpoints.

```python
async for event in client.chat.stream_indent_finder("Find my last deployment"):
    print(event)
```

---

## MCP bridge (Cursor / Claude)

**Recommended:** connect Cursor directly to the hosted MCP gateway (no SDK process).

**Header auth** (when your client supports custom headers):

```json
{
  "mcpServers": {
    "maeyr": {
      "url": "https://api.maeyr.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:MAEYR_MCP_TOKEN}"
      }
    }
  }
}
```

**URL token** (when the client only accepts a connection URL — same `mcp_` key):

```json
{
  "mcpServers": {
    "maeyr": {
      "url": "https://api.maeyr.com/mcp?token=${env:MAEYR_MCP_TOKEN}"
    }
  }
}
```

```bash
export MAEYR_MCP_TOKEN="mcp_..."
```

Create tokens in the Maeyr console → **MCP Tokens**. Token policy controls which agents and scopes are available. Prefer the header when possible — URL tokens may appear in access logs.

### Stdio proxy (`maeyr-mcp-bridge`)

For clients that only support **stdio** MCP (older Claude Desktop setups), the SDK provides a thin proxy to **mcp-gateway-service** — registry, mappings, schemas, and execution all live on the gateway (Mongo + pulse), not in the SDK.

```bash
pip install "maeyr[mcp]"
export MAEYR_MCP_TOKEN="mcp_..."

# All agents allowed by the token
maeyr-mcp-bridge

# Single agent scope
maeyr-mcp-bridge --agent-alias github_mcp_agent
```

```json
{
  "mcpServers": {
    "maeyr": {
      "command": "maeyr-mcp-bridge",
      "env": {
        "MAEYR_MCP_TOKEN": "mcp_...",
        "MAEYR_BASE_URL": "https://api.maeyr.com"
      }
    }
  }
}
```

Scoped URL: `https://api.maeyr.com/mcp/agents/{agent_alias}` (set via `--agent-alias` or `MAEYR_AGENT_ALIAS`).

### Environment variables

| Variable | Description |
|----------|-------------|
| `MAEYR_MCP_TOKEN` | **Required.** MCP token from the console |
| `MAEYR_BASE_URL` | API gateway base (default `https://api.maeyr.com`) |
| `MAEYR_MCP_GATEWAY_URL` | Full MCP URL override (optional) |
| `MAEYR_AGENT_ALIAS` | Default `--agent-alias` for scoped `/mcp/agents/{alias}` |

---

## Development tooling

Validate agent manifests **before** pushing to the platform.

### CLI

```bash
maeyr-agent-validate ./path/to/agent/
```

Expects `agent.json` (or any JSON file passed as directory — reads `agent.json` inside the path) matching `AgentGenerationResponse` shape, including embedded `main.py` in `files[]`.

### Python API

```python
from maeyr.devtools import (
    AgentValidationError,
    validate_agent_manifest,
    validate_a2a_envelope,
)

# Raises AgentValidationError on failure
validate_agent_manifest(manifest_dict)

# Returns list of issue strings (empty = valid)
issues = validate_a2a_envelope(
    envelope_dict,
    endpoint_dict,
    agent_inputs_list,
)
```

**`validate_agent_manifest` checks:**

- `main.py` present with Python mime type
- Non-empty `agent_endpoints`
- Unique input/output/endpoint names
- Each `main` endpoint has `@mcp_endpoint` and matching async function
- `payload` / parameter usage matches declared `inputs`
- Endpoint input/output references exist on the agent

---

## Data models

Pydantic v2 models in `maeyr.models` (import as needed):

| Module | Types |
|--------|-------|
| `maeyr.models.agent` | `AgentCreationRequest`, `AgentUpdateRequest`, `AgentGenerationResponse`, `AgentEndpoint`, `AgentInput`, `AgentOutput`, … |
| `maeyr.models.auth` | `LoginRequest`, `TokenResponse`, `UserResponse`, … |
| `maeyr.models.a2a` | `A2AEnvelope`, `A2AResponse`, `A2AStatus`, `A2A_PROTOCOL_VERSION` |
| `maeyr.models.executor` | `EndpointExecutionRequest`, `EndpointExecutionResponse`, `AgentInvokeRequest`, … |

---

## Examples

| Path | Description |
|------|-------------|
| [`examples/aviation_agent/main.py`](examples/aviation_agent/main.py) | Aviationstack flight queries with `MaeyrAuth` + `@mcp_endpoint` |

Run locally after `pip install -e .` and `pip install httpx`.

---

## Contributing

```bash
git clone git@github.com:maeyr-ai/maeyr-sdk.git
cd maeyr-sdk
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
ruff format src tests
```

Build release wheels from the source distribution in a clean wheel directory,
not from a reusable `build/lib` tree. This prevents files removed during a
module consolidation from leaking into a later wheel:

```bash
python -m build --sdist
python -m pip wheel --no-deps --no-build-isolation \
  --wheel-dir dist/wheelhouse dist/maeyr-*.tar.gz
```

Before publishing, inspect the wheel, confirm `maeyr/py.typed` is present,
and run imports with the wheel as the only project path. The current MCP bridge
artifact contains only `__init__.py`, `cli.py`, and `gateway.py`; the retired
discovery, mappings, registry, server, and tools modules must not reappear.
The exact source-to-wheel check is executable:

```bash
python scripts/verify_wheel_manifest.py \
  dist/wheelhouse/maeyr-*.whl src/maeyr
```

CI runs on push and pull requests to `main` (Python 3.10–3.12). Release versions are tagged as `v*` on this repository.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
