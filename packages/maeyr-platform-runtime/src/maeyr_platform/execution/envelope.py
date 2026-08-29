"""Canonical tool-result envelope unwrap helpers.

Pulse / Chrona endpoints return a standard envelope:

    {"execution_id": "...", "status": "COMPLETED",
     "result": {...real payload...}, "error": null}

The agent executor needs to forward the inner payload to the synthesis
LLM (the model doesn't need to learn what `execution_id` is). But it
must NOT confuse "no error reported" (envelope with ``error: None``) for
"task failed". A previous version did exactly that and silently dropped
every successful result, leaving synthesis with no data. See
``tests/test_unwrap_task_output.py``.
"""

from __future__ import annotations

from typing import Any

# Keys that may carry the real payload inside a tool envelope. Order
# matters: ``result`` is the canonical Pulse / Chrona key, the others
# are legacy / vendor-specific shapes we still see in production.
_ENVELOPE_PAYLOAD_KEYS = ("result", "response", "data")


def unwrap_task_output(output: Any) -> Any:
    """Pick the meaningful payload out of a tool-result envelope.

    Behaviour:

      * If ``output`` isn't a dict, it's returned unchanged.
      * If ``output`` is a dict with a *truthy* ``error`` field, the
        whole envelope is returned (so synthesis can surface the
        error). Falsy errors (``None``, ``""``, ``0``, ``[]``, ``{}``)
        are treated as "no error".
      * Otherwise the first non-empty inner payload found at
        ``result`` / ``response`` / ``data`` is returned.
      * If none of those keys exist (or all are empty), the whole dict
        is returned.

    Empty inner payloads (``[]``, ``{}``, ``""``, ``None``) are NOT
    unwrapped: synthesis should still see the envelope so it knows the
    upstream actually returned an empty success rather than no signal.
    """
    if not isinstance(output, dict):
        return output

    if output.get("error"):
        return output

    for key in _ENVELOPE_PAYLOAD_KEYS:
        if key in output:
            inner = output[key]
            if inner not in (None, "", [], {}):
                return inner

    return output
