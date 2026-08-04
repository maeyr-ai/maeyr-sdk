"""Domain-neutral identification of applications speaking the MCP protocol."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_CLIENT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"cursor", re.I), "cursor", "Cursor"),
    (re.compile(r"claude|anthropic", re.I), "claude", "Claude"),
    (re.compile(r"windsurf", re.I), "windsurf", "Windsurf"),
    (re.compile(r"github\s*copilot|copilot-chat", re.I), "copilot", "GitHub Copilot"),
    (
        re.compile(r"visual\s*studio\s*code|vscode|vs\s*code", re.I),
        "vscode",
        "VS Code",
    ),
    (
        re.compile(r"jetbrains|intellij|pycharm|webstorm|goland", re.I),
        "jetbrains",
        "JetBrains",
    ),
    (re.compile(r"\bzed\b", re.I), "zed", "Zed"),
    (re.compile(r"continue-dev|continue\b", re.I), "continue", "Continue"),
    (re.compile(r"\bcline\b", re.I), "cline", "Cline"),
    (re.compile(r"roo\s*code|roocode", re.I), "roo", "Roo Code"),
    (re.compile(r"mcp[- ]?inspector", re.I), "mcp_inspector", "MCP Inspector"),
    (re.compile(r"modelcontextprotocol", re.I), "mcp_sdk", "MCP SDK"),
    (re.compile(r"openai|chatgpt", re.I), "openai", "OpenAI"),
    (re.compile(r"gemini|google-ai", re.I), "gemini", "Gemini"),
    (
        re.compile(r"viksa[- ]?mcp[- ]?bridge", re.I),
        "viksa_mcp_bridge",
        "Viksa MCP Bridge",
    ),
    (re.compile(r"viksa[- ]?ai|viksa[- ]?sdk", re.I), "viksa_sdk", "Viksa SDK"),
)
_PRODUCT_VERSION_RE = re.compile(r"^([^/\s]+)(?:/([^\s]+))?")


@dataclass(frozen=True, slots=True)
class MCPClientInfo:
    """Safe client metadata suitable for trace labels and resource references."""

    client: str
    client_name: str
    client_version: str | None = None
    user_agent: str | None = None
    protocol_version: str | None = None

    @property
    def source_label(self) -> str:
        suffix = f" {self.client_version}" if self.client_version else ""
        return f"MCP Call · {self.client_name}{suffix}"

    def trace_attributes(self) -> dict[str, str]:
        attributes = {
            "source": self.source_label,
            "mcp.client": self.client,
            "mcp.client_name": self.client_name,
        }
        if self.client_version:
            attributes["mcp.client_version"] = self.client_version
        if self.protocol_version:
            attributes["mcp.protocol_version"] = self.protocol_version
        if self.user_agent:
            attributes["mcp.user_agent"] = self.user_agent[:256]
        return attributes

    def trace_labels(self) -> list[str]:
        return [f"mcp-client:{self.client}"]

    def trace_resource_refs(self) -> dict[str, str]:
        references = {"mcp_client": self.client}
        if self.client_version:
            references["mcp_client_version"] = self.client_version
        return references


def _header(headers: Mapping[str, str], name: str) -> str | None:
    raw = headers.get(name.lower())
    normalized = str(raw).strip() if raw else ""
    return normalized or None


def _parse_product_version(user_agent: str) -> tuple[str | None, str | None]:
    token = user_agent.strip().split(" ", 1)[0]
    match = _PRODUCT_VERSION_RE.match(token) if token else None
    if match is None:
        return (token or None), None
    return (match.group(1) or None), (match.group(2) or None)


def _match_client(user_agent: str) -> tuple[str, str, str | None]:
    product, version = _parse_product_version(user_agent)
    searchable = f"{product or ''} {user_agent}"
    for pattern, slug, display in _CLIENT_PATTERNS:
        if pattern.search(searchable):
            return slug, display, version
    if product:
        slug = re.sub(r"[^a-z0-9]+", "_", product.lower()).strip("_") or "unknown"
        return slug, product, version
    return "unknown", "Unknown", None


def resolve_mcp_client(headers: Mapping[str, str]) -> MCPClientInfo:
    """Resolve an MCP caller from explicit metadata or its user-agent string."""

    override = _header(headers, "x-mcp-client") or _header(headers, "x-viksa-mcp-client")
    user_agent = _header(headers, "user-agent") or ""
    protocol_version = _header(headers, "mcp-protocol-version")
    if override:
        slug = re.sub(r"[^a-z0-9]+", "_", override.lower()).strip("_") or "unknown"
        _, version = _parse_product_version(user_agent)
        return MCPClientInfo(
            client=slug,
            client_name=override,
            client_version=version,
            user_agent=user_agent or None,
            protocol_version=protocol_version,
        )
    slug, display, version = _match_client(user_agent)
    return MCPClientInfo(
        client=slug,
        client_name=display,
        client_version=version,
        user_agent=user_agent or None,
        protocol_version=protocol_version,
    )


def resolve_mcp_client_for_api_key(
    headers: Mapping[str, str],
    *,
    user_id: str | None = None,
) -> MCPClientInfo:
    """Use Direct API identity for MCP-scoped keys that omit a user agent."""

    info = resolve_mcp_client(headers)
    if info.client != "unknown":
        return info
    if (user_id or "").strip().lower() == "api_key:mcp":
        return MCPClientInfo(client="direct_api", client_name="Direct API")
    return info


__all__ = ["MCPClientInfo", "resolve_mcp_client", "resolve_mcp_client_for_api_key"]
