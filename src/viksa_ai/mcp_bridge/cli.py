"""CLI: run Viksa agents as an MCP server over stdio."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from viksa_ai._constants import (
    ENV_AGENT_ALIAS,
    ENV_AGENT_ID,
    ENV_MCP_ALL_DEPLOYED,
    ENV_MCP_REFRESH_INTERVAL,
)
from viksa_ai.client import ViksaClient
from viksa_ai.mcp_bridge.discovery import BridgeTarget
from viksa_ai.mcp_bridge.registry import BridgeRegistry, refresh_registry
from viksa_ai.mcp_bridge.server import create_mcp_server

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expose Viksa agent endpoints as an MCP server (stdio) "
            "for Cursor or Claude Desktop"
        ),
    )
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument(
        "--agent-id",
        default=os.environ.get(ENV_AGENT_ID),
        help=f"Expose one agent by id (env: {ENV_AGENT_ID})",
    )
    target.add_argument(
        "--agent-alias",
        default=os.environ.get(ENV_AGENT_ALIAS),
        help=f"Expose one agent by alias (env: {ENV_AGENT_ALIAS})",
    )
    target.add_argument(
        "--all-deployed",
        action="store_true",
        default=os.environ.get(ENV_MCP_ALL_DEPLOYED, "").lower() in ("1", "true", "yes"),
        help=f"Expose all deployed agents in the project (env: {ENV_MCP_ALL_DEPLOYED})",
    )
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=int(os.environ.get(ENV_MCP_REFRESH_INTERVAL, "60")),
        help=(
            "Seconds between registry refreshes from builder-service "
            f"(0=disabled, env: {ENV_MCP_REFRESH_INTERVAL}, default: 60)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("VIKSA_MCP_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level for bridge diagnostics (default: WARNING)",
    )
    return parser


def _resolve_target(args: argparse.Namespace) -> BridgeTarget:
    if args.agent_id:
        return BridgeTarget(agent_id=args.agent_id)
    if args.agent_alias:
        return BridgeTarget(agent_alias=args.agent_alias)
    if args.all_deployed:
        return BridgeTarget(all_deployed=True)
    raise SystemExit(
        "error: specify --agent-id, --agent-alias, or --all-deployed "
        "(or set VIKSA_AGENT_ID / VIKSA_AGENT_ALIAS / VIKSA_MCP_ALL_DEPLOYED)"
    )


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "error: MCP support is not installed. Run: pip install 'viksa-ai[mcp]'",
            file=sys.stderr,
        )
        return 1

    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    target = _resolve_target(args)
    refresh_interval = max(0, int(args.refresh_interval))
    client = ViksaClient.from_env()

    registry = BridgeRegistry()
    async with client:
        await refresh_registry(registry, client, target)
        if registry.load_error:
            logger.error(
                "Initial Viksa load failed (MCP server will still start): %s",
                registry.load_error,
            )
        else:
            logger.info("Loaded %d Viksa tool(s)", len(registry.tools))
        server = create_mcp_server(
            client,
            registry,
            target=target,
            refresh_interval_seconds=refresh_interval,
        )
        await server.run_stdio()  # type: ignore[attr-defined]

    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(argv))
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("viksa-mcp-bridge failed: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
