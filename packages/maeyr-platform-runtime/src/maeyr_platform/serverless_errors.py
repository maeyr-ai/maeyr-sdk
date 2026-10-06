"""Finite public Serverless refusals shared by HTTP, Chat and trace boundaries."""

from __future__ import annotations

from collections.abc import Mapping

_PUBLIC_DENIALS: dict[tuple[int, str], str] = {
    (503, "serverless_disabled"): "Serverless execution is disabled for this deployment",
    (403, "serverless_http_403"): (
        "This agent is not authorized for serverless execution. Ask your workspace "
        "administrator to review access and the published build."
    ),
    (404, "serverless_http_404"): (
        "The agent, published build, or execution could not be found. Refresh the agent "
        "and check its published build before starting a new run."
    ),
    (422, "serverless_http_422"): (
        "The serverless request or published runtime is invalid. Review the selected "
        "function, inputs, and build configuration before trying again."
    ),
    (429, "serverless_http_429"): (
        "Serverless execution capacity or request limits have been reached. Wait for "
        "active runs to finish or the request limit to reset, then try again."
    ),
}

PUBLIC_SERVERLESS_DENIAL_CODES = frozenset(code for _, code in _PUBLIC_DENIALS)
PUBLIC_SERVERLESS_DENIAL_MESSAGES = frozenset(_PUBLIC_DENIALS.values())


def public_serverless_denial_detail(status_code: object, detail: object) -> dict[str, str] | None:
    """Project only exact native status/code pairs; never copy upstream text.

    Callers must classify a submission refusal separately from a status lookup
    failure. A missing poll result does not prove that execution never started.
    """
    if type(status_code) is not int or not isinstance(detail, Mapping):
        return None
    code = detail.get("code")
    if not isinstance(code, str):
        return None
    message = _PUBLIC_DENIALS.get((status_code, code))
    return {"code": code, "message": message} if message is not None else None
