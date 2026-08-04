# ruff: noqa: E501
"""Canonical user-interaction primitives for orchestration hosts.

The shared harness defines the ``ask_user`` contract, but hosts must explicitly
advertise and intercept the tool. This keeps replay, internal, and other
non-resumable execution paths from acquiring interactive pause semantics by
default.
"""

from __future__ import annotations

from typing import Any, Dict

ASK_USER_NAME = "ask_user"

ASK_USER_PROMPT = """## Asking the user
- When the `ask_user` tool is available and you are genuinely blocked on information only the user can provide, call it with one focused `question`.
- Include brief `context` explaining why the answer is needed. Include `options` only when there is a small, concrete set of valid choices.
- Do not stop with a free-text question when `ask_user` is available; the tool creates the resumable pause.
- Do not use `ask_user` to infer or bypass consent. Endpoint approvals are a separate control, and an answer to `ask_user` never approves an action."""


def ask_user_tool_schema() -> Dict[str, Any]:
    """Return the OpenAI function schema for the optional ``ask_user`` tool."""
    return {
        "type": "function",
        "function": {
            "name": ASK_USER_NAME,
            "description": (
                "Pause this execution and ask the user for information that is "
                "required to continue. Use only when sensible defaults and the "
                "available tools cannot resolve the missing information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The focused question the user must answer.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Briefly explain why this answer is needed.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional concrete choices the user may select.",
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    }
