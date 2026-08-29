"""CLI: stdio MCP proxy to the hosted Maeyr MCP gateway."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from maeyr._constants import (
    ENV_AGENT_ALIAS,
    ENV_BASE_URL,
    ENV_MCP_GATEWAY_URL,
    ENV_MCP_TOKEN,
)
from maeyr.mcp_bridge.gateway import (
    resolve_gateway_url,
    resolve_mcp_token,
    run_stdio_gateway_proxy,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stdio MCP proxy to the Maeyr hosted MCP gateway (mcp-gateway-service). "
            "Prefer configuring Cursor with the gateway URL directly when possible."
        ),
    )
    parser.add_argument(
        "--mcp-token",
        default=os.environ.get(ENV_MCP_TOKEN),
        help=f"MCP token (env: {ENV_MCP_TOKEN})",
    )
    parser.add_argument(
        "--agent-alias",
        default=os.environ.get(ENV_AGENT_ALIAS),
        help=(
            f"Scope to one agent via /mcp/agents/{{alias}} (env: {ENV_AGENT_ALIAS}). "
            "Omit to use /mcp (all agents allowed by the token policy)."
        ),
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get(ENV_MCP_GATEWAY_URL),
        help=f"Full gateway MCP URL override (env: {ENV_MCP_GATEWAY_URL})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(ENV_BASE_URL),
        help=f"Platform API base URL (env: {ENV_BASE_URL}, default: https://api.maeyr.com)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MAEYR_MCP_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level for bridge diagnostics (default: WARNING)",
    )
    return parser


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "error: MCP support is not installed. Run: pip install 'maeyr[mcp]'",
            file=sys.stderr,
        )
        return 1

    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        token = resolve_mcp_token(args.mcp_token)
        url = resolve_gateway_url(
            base_url=args.base_url,
            agent_alias=args.agent_alias,
            gateway_url=args.gateway_url,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    logger.info("Proxying stdio MCP → %s", url)
    await run_stdio_gateway_proxy(gateway_url=url, mcp_token=token)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("maeyr-mcp-bridge failed: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
