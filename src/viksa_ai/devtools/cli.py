"""CLI: validate agent directory (main.py + agent.json)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from viksa_ai.devtools.validate_agent import AgentValidationError, validate_agent_manifest
from viksa_ai.models.agent import AgentGenerationResponse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Viksa agent manifest")
    parser.add_argument(
        "path",
        type=Path,
        help="Directory containing agent.json (and referenced main.py in files[])",
    )
    args = parser.parse_args(argv)
    manifest_path = args.path / "agent.json"
    if not manifest_path.is_file():
        print(f"error: {manifest_path} not found", file=sys.stderr)
        return 1
    data = json.loads(manifest_path.read_text())
    try:
        validate_agent_manifest(data)
    except (AgentValidationError, ValueError) as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 1
    agent = AgentGenerationResponse.model_validate(data)
    print(f"ok: {agent.agent_name} ({len(agent.agent_endpoints)} endpoints)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
