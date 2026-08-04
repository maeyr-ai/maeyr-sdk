# Viksa AI SDK (`viksa-ai`)

[![PyPI version](https://img.shields.io/pypi/v/viksa-ai)](https://pypi.org/project/viksa-ai/)
[![Python](https://img.shields.io/pypi/pyversions/viksa-ai)](https://pypi.org/project/viksa-ai/)
[![License](https://img.shields.io/github/license/viksa-ai/viksa-sdk)](https://github.com/viksa-ai/viksa-sdk/blob/main/LICENSE)
[![CI](https://github.com/viksa-ai/viksa-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/viksa-ai/viksa-sdk/actions/workflows/ci.yml)

Official Python SDK for the [Viksa AI](https://viksaai.com) platform. Use it to author agents locally, call platform APIs from scripts and automation, and validate agent manifests before deploy.

| Module | Purpose |
|--------|---------|
| [`viksa_ai.runtime`](#agent-runtime) | Same API as injected `ViksaAI.py` (`mcp_endpoint`, `ViksaAuth`, A2A `context()`) |
| [`viksa_ai.client`](#platform-http-client) | Typed async HTTP client for `https://api.viksaai.com` |
| [`viksa_ai.devtools`](#development-tooling) | AST + schema validation for generated agents |
| [`viksa_ai.mcp_bridge`](#mcp-bridge-cursor--claude) | Stdio MCP proxy to the hosted Viksa MCP gateway |
| [`viksa_ai.models`](#data-models) | Pydantic models for API requests and A2A envelopes |

---

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Agent runtime](#agent-runtime)
  - [mcp_endpoint](#mcp_endpoint)
  - [ViksaAuth](#viksaauth)
  - [A2A context](#a2a-context)
  - [Injected ViksaAI.py](#injected-viksai-py)
- [Platform HTTP client](#platform-http-client)
  - [Authentication](#client-authentication)
  - [ViksaClient reference](#viksaclient-reference)
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
pip install viksa-ai
```

With dev dependencies (tests, ruff, build):

```bash
pip install "viksa-ai[dev]"
```

---

## Quick start

### Author an agent endpoint

```python
from typing import Any, Dict

import httpx

from viksa_ai.runtime import ViksaAuth, mcp_endpoint

BASE_URL = "https://api.aviationstack.com/v1"


@mcp_endpoint(description="Get flights between source and destination")
async def get_flights_between(payload: Dict[str, Any]):
    source = payload.get("source")
    destination = payload.get("destination")
    api_key = ViksaAuth.require_param("aviationstack_api", "api_key")
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

from viksa_ai import ViksaClient


async def main():
    async with ViksaClient(
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

Import from `viksa_ai.runtime` (or `ViksaAI` after platform injection). This is the **canonical** implementation of the file the platform writes as `ViksaAI.py` on every agent.

### `mcp_endpoint`

Decorator that marks an async function as an MCP-style agent endpoint. Metadata is used by the platform UI and validators; execution routing uses `agent_endpoints` in the agent manifest, not decorator introspection.

```python
from viksa_ai.runtime import mcp_endpoint

@mcp_endpoint(description="Human-readable description for docs and UI")
async def my_tool(payload: dict):
    ...
```

**Convention:** one parameter named `payload: Dict[str, Any]`, with inputs read via `payload.get("name")` or `payload["name"]`.

### `ViksaAuth`

Reads auth configuration injected at deploy time as environment variables.

| Environment variable | Meaning |
|---------------------|---------|
| `{method_id}.{param_name}` | Resolved secret or param value (e.g. `bearer_token.api_key`) |
| `VIKSA_AUTH_ENABLED_METHODS` | Comma-separated method ids with **all** params resolved |
| `VIKSA_AUTH_CONFIGURED_METHODS` | All method ids declared on the agent (including incomplete) |

| Method | Description |
|--------|-------------|
| `get_configured_methods()` | Method ids declared for this agent |
| `is_method_configured(method_id)` | Whether the method belongs to this agent |
| `get_enabled_methods()` | Method ids fully enabled at deploy |
| `is_method_enabled(method_id)` | Whether the method is in the enabled set |
| `get_param(method_id, param_name)` | Param value or `None` |
| `require_param(method_id, param_name)` | Param value or raises `ViksaAuthError` |
| `get_method_params(method_id)` | Dict of all params for one method |
| `preferred_method(*candidates)` | First enabled candidate, or `None` |
| `require_method(*candidates)` | First enabled candidate, or raises |

```python
from viksa_ai.runtime import ViksaAuth, ViksaAuthError

# Single required credential
api_key = ViksaAuth.require_param("aviationstack_api", "api_key")

# Multiple auth methods
method = ViksaAuth.preferred_method("oauth_client", "bearer_token")
if method == "oauth_client":
    client_id = ViksaAuth.require_param("oauth_client", "client_id")
else:
    api_key = ViksaAuth.require_param("bearer_token", "api_key")
```

Platform docs: [Agent auth and credentials](https://docs.viksaai.com/docs/agents/auth-and-credentials).

### A2A context

Agent-to-agent calls can attach an optional envelope under the reserved payload key `__viksa_a2a__`. Use `context()` to read correlation metadata inside your endpoint.

| Symbol | Role |
|--------|------|
| `A2A_PAYLOAD_KEY` | `"__viksa_a2a__"` — wire key in workflow inputs |
| `attach_envelope(inputs, envelope)` | Attach envelope to an inputs dict (callers / integrators) |
| `context()` | Copy of the current call envelope, or `{}` |

`A2AContext` is a `TypedDict` documenting common keys: `run_id`, `parent_step_id`, `caller_agent`, `callee_agent`, `endpoint`, `idempotency_key`, `deadline_at`, `metadata`.

```python
from viksa_ai.runtime import attach_envelope, context

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

### Injected `ViksaAI.py`

The platform injects `ViksaAI.py` into every agent’s file list. Generate the canonical module body from the SDK:

```python
from viksa_ai.runtime.inject import to_module_source

body = to_module_source()
```

---

## Platform HTTP client

Base URL default: `https://api.viksaai.com`. Requests are routed per service prefix (`/auth`, `/builder`, `/chat`, etc.), matching the platform API gateway.

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
| **JWT** (default) | `ViksaClient(access_token="...")` or `VIKSA_ACCESS_TOKEN` |
| **API key** | `ViksaClient.from_api_key("...")` or `VIKSA_API_KEY` |
| **Email / password** | `await ViksaClient.from_login(email, password)` or `VIKSA_EMAIL` + `VIKSA_PASSWORD` |

Set **`base_url`** on the client (or `VIKSA_BASE_URL`) for staging, self-hosted, or regional gateways. Default: `https://api.viksaai.com`.

```python
from viksa_ai import ViksaClient

# JWT from the console or a prior login
async with ViksaClient(access_token="eyJ...", org_id="org", project_id="proj") as client:
    me = await client.auth.me()

# Project API key (optional validate=True to resolve org/project)
client = ViksaClient.from_api_key("vk_...", base_url="https://api.viksaai.com")

# Login
client = await ViksaClient.from_login("you@example.com", "password", base_url="https://api.viksaai.com")
```

**Environment variables** (for `ViksaClient.from_env()` — first match wins):

| Variable | Description |
|----------|-------------|
| `VIKSA_API_KEY` | Project API key |
| `VIKSA_ACCESS_TOKEN` | JWT access token |
| `VIKSA_EMAIL` + `VIKSA_PASSWORD` | Log in and obtain a JWT |
| `VIKSA_ORG_ID` | Tenant org id (optional) |
| `VIKSA_PROJECT_ID` | Tenant project id (optional) |
| `VIKSA_REFRESH_TOKEN` | Enables automatic refresh on 401 (JWT only) |
| `VIKSA_BASE_URL` | API base URL (default `https://api.viksaai.com`) |

```python
from viksa_ai import ViksaClient

client = ViksaClient.from_env()
```

Validate an API key without a full session: `await client.auth.validate_api_key("vk_...")`.

### `ViksaClient` reference

```python
from viksa_ai import ViksaClient

async with ViksaClient(
    access_token="...",        # JWT, or use from_api_key / from_login
    org_id="...",
    project_id="...",
    refresh_token="...",       # optional; JWT only
    base_url="https://api.viksaai.com",  # configurable
    timeout=60.0,
) as client:
    ...
```

Sub-clients are created on the root client:

```text
ViksaClient
├── auth (+ auth.orgs, auth.projects)
├── builder.agents | deploy | secrets | mappings | mcp
├── chat (+ triggers, approvals)
├── pulse
├── workflow.executions
├── scheduler
└── marketplace.listings | workforce | publishers
```

Use `ViksaClient.webhook(trigger_id, webhook_token=...)` for public webhook routes (no JWT).
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
from viksa_ai.models.executor import AgentType, EndpointExecutionRequest

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
wh = ViksaClient.webhook("trigger-id", webhook_token="...")
await wh.invoke({"event": "order.created"})
async for evt in wh.stream({"event": "order.created"}):
    print(evt)
```

**Coverage note:** The SDK wraps major public platform flows. Routes not yet listed can be called via `await client.request("GET", "/builder", "/path")`.

### Error handling (v0.2)

The client maps HTTP failures to typed exceptions and parses FastAPI `detail` payloads.

| Exception | HTTP | When |
|-----------|------|------|
| `ViksaTransportError` | — | Timeouts, connection failures, DNS |
| `ViksaAuthenticationError` | 401 | Invalid or expired token |
| `ViksaPermissionError` | 403 | RBAC / tenant denial |
| `ViksaNotFoundError` | 404 | Missing resource |
| `ViksaConflictError` | 409 | State conflict |
| `ViksaValidationError` | 422 | Schema / field validation |
| `ViksaRateLimitError` | 429 | Rate limited (`retry_after` set) |
| `ViksaServerError` | 5xx | Platform or gateway errors |
| `ViksaApiError` | other | Base class for all API errors |

```python
from viksa_ai import ViksaClient, ViksaNotFoundError, ViksaValidationError

try:
    await client.builder.agents.get("missing-id")
except ViksaNotFoundError as e:
    print(e.status_code, e.detail_message, e.request_id)
    for d in e.details:
        print(d.field, d.message)
except ViksaValidationError as e:
    print(e.body)
```

| `ViksaApiError` attribute | Description |
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
from viksa_ai.client import ClientConfig, RetryConfig

client = ViksaClient(
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

`ViksaClient.iter_sse_lines(response)` is a static helper for custom streaming endpoints.

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
    "viksa": {
      "url": "https://api.viksaai.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:VIKSA_MCP_TOKEN}"
      }
    }
  }
}
```

**URL token** (when the client only accepts a connection URL — same `mcp_` key):

```json
{
  "mcpServers": {
    "viksa": {
      "url": "https://api.viksaai.com/mcp?token=${env:VIKSA_MCP_TOKEN}"
    }
  }
}
```

```bash
export VIKSA_MCP_TOKEN="mcp_..."
```

Create tokens in the Viksa console → **MCP Tokens**. Token policy controls which agents and scopes are available. Prefer the header when possible — URL tokens may appear in access logs.

### Stdio proxy (`viksa-mcp-bridge`)

For clients that only support **stdio** MCP (older Claude Desktop setups), the SDK provides a thin proxy to **mcp-gateway-service** — registry, mappings, schemas, and execution all live on the gateway (Mongo + pulse), not in the SDK.

```bash
pip install "viksa-ai[mcp]"
export VIKSA_MCP_TOKEN="mcp_..."

# All agents allowed by the token
viksa-mcp-bridge

# Single agent scope
viksa-mcp-bridge --agent-alias github_mcp_agent
```

```json
{
  "mcpServers": {
    "viksa": {
      "command": "viksa-mcp-bridge",
      "env": {
        "VIKSA_MCP_TOKEN": "mcp_...",
        "VIKSA_BASE_URL": "https://api.viksaai.com"
      }
    }
  }
}
```

Scoped URL: `https://api.viksaai.com/mcp/agents/{agent_alias}` (set via `--agent-alias` or `VIKSA_AGENT_ALIAS`).

### Environment variables

| Variable | Description |
|----------|-------------|
| `VIKSA_MCP_TOKEN` | **Required.** MCP token from the console |
| `VIKSA_BASE_URL` | API gateway base (default `https://api.viksaai.com`) |
| `VIKSA_MCP_GATEWAY_URL` | Full MCP URL override (optional) |
| `VIKSA_AGENT_ALIAS` | Default `--agent-alias` for scoped `/mcp/agents/{alias}` |

---

## Development tooling

Validate agent manifests **before** pushing to the platform.

### CLI

```bash
viksa-agent-validate ./path/to/agent/
```

Expects `agent.json` (or any JSON file passed as directory — reads `agent.json` inside the path) matching `AgentGenerationResponse` shape, including embedded `main.py` in `files[]`.

### Python API

```python
from viksa_ai.devtools import (
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

Pydantic v2 models in `viksa_ai.models` (import as needed):

| Module | Types |
|--------|-------|
| `viksa_ai.models.agent` | `AgentCreationRequest`, `AgentUpdateRequest`, `AgentGenerationResponse`, `AgentEndpoint`, `AgentInput`, `AgentOutput`, … |
| `viksa_ai.models.auth` | `LoginRequest`, `TokenResponse`, `UserResponse`, … |
| `viksa_ai.models.a2a` | `A2AEnvelope`, `A2AResponse`, `A2AStatus`, `A2A_PROTOCOL_VERSION` |
| `viksa_ai.models.executor` | `EndpointExecutionRequest`, `EndpointExecutionResponse`, `AgentInvokeRequest`, … |

---

## Examples

| Path | Description |
|------|-------------|
| [`examples/aviation_agent/main.py`](examples/aviation_agent/main.py) | Aviationstack flight queries with `ViksaAuth` + `@mcp_endpoint` |

Run locally after `pip install -e .` and `pip install httpx`.

---

## Contributing

```bash
git clone git@github.com:viksa-ai/viksa-sdk.git
cd viksa-sdk
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
  --wheel-dir dist/wheelhouse dist/viksa_ai-*.tar.gz
```

Before publishing, inspect the wheel, confirm `viksa_ai/py.typed` is present,
and run imports with the wheel as the only project path. The current MCP bridge
artifact contains only `__init__.py`, `cli.py`, and `gateway.py`; the retired
discovery, mappings, registry, server, and tools modules must not reappear.
The exact source-to-wheel check is executable:

```bash
python scripts/verify_wheel_manifest.py \
  dist/wheelhouse/viksa_ai-*.whl src/viksa_ai
```

CI runs on push and pull requests to `main` (Python 3.10–3.12). Release versions are tagged as `v*` on this repository.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
