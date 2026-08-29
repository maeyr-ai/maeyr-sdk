"""AST validation for generated agent manifests."""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional, Set, Union

from maeyr.models.agent import AgentGenerationResponse


class AgentValidationError(Exception):
    pass


def _validate_unique_names(agent: AgentGenerationResponse) -> None:
    def _dupes(items: List[str]) -> List[str]:
        seen: Set[str] = set()
        dup: Set[str] = set()
        for item in items:
            if item in seen:
                dup.add(item)
            seen.add(item)
        return sorted(dup)

    input_dupes = _dupes([i.name for i in agent.inputs])
    if input_dupes:
        raise AgentValidationError(f"Duplicate input names are not allowed: {input_dupes}")

    output_dupes = _dupes([o.name for o in agent.outputs])
    if output_dupes:
        raise AgentValidationError(f"Duplicate output names are not allowed: {output_dupes}")

    endpoint_dupes = _dupes([e.name for e in agent.agent_endpoints])
    if endpoint_dupes:
        raise AgentValidationError(f"Duplicate endpoint names are not allowed: {endpoint_dupes}")


def _validate_main_py(agent: AgentGenerationResponse) -> str:
    for f in agent.files:
        if f.name == "main.py" and f.mime_type.value == "python":
            return f.content
    raise AgentValidationError("main.py file is mandatory and missing")


def _validate_endpoints_populated(agent: AgentGenerationResponse) -> None:
    if not agent.agent_endpoints:
        raise AgentValidationError(
            "agent_endpoints list is empty. You must expose at least one endpoint."
        )


def _function_arg_names(func_node: ast.AsyncFunctionDef) -> Set[str]:
    a = func_node.args
    names: List[str] = []
    names.extend(arg.arg for arg in a.posonlyargs)
    names.extend(arg.arg for arg in a.args)
    names.extend(arg.arg for arg in a.kwonlyargs)
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return {n for n in names if n not in ("self", "cls")}


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _slice_to_str(s: ast.AST) -> Optional[str]:
    if isinstance(s, ast.Constant) and isinstance(s.value, str):
        return s.value
    return None


def _get_first_get_arg(call: ast.Call) -> Optional[str]:
    if not call.args:
        return None
    return _const_str(call.args[0])


def _collect_input_keys_from_payload_function(func_node: ast.AsyncFunctionDef) -> Set[str]:
    keys: Set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "get"
                and isinstance(f.value, ast.Name)
                and f.value.id == "payload"
            ):
                s = _get_first_get_arg(node)
                if s is not None:
                    keys.add(s)
        if isinstance(node, ast.Subscript):
            if not isinstance(node.value, ast.Name) or node.value.id != "payload":
                continue
            s = _slice_to_str(node.slice)
            if s is not None:
                keys.add(s)
    return keys


def _decorator_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _has_mcp_endpoint_decorator(func_node: ast.AsyncFunctionDef) -> bool:
    for decorator in func_node.decorator_list:
        if _decorator_name(decorator) == "mcp_endpoint":
            return True
    return False


def _validate_code_structure(agent: AgentGenerationResponse, code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise AgentValidationError(f"Generated code has syntax errors: {e}") from e

    async_functions = {
        node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    declared_input_names = {i.name for i in agent.inputs}
    used_input_names: Set[str] = set()

    for endpoint in agent.agent_endpoints:
        if endpoint.module != "main":
            continue
        if endpoint.name not in async_functions:
            raise AgentValidationError(
                f"Endpoint '{endpoint.name}' is declared but the async function "
                f"`async def {endpoint.name}(...):` is missing in main.py"
            )
        func_node = async_functions[endpoint.name]
        if not _has_mcp_endpoint_decorator(func_node):
            raise AgentValidationError(
                f"Function '{endpoint.name}' must be decorated with @mcp_endpoint."
            )
        arg_names = _function_arg_names(func_node)
        is_payload = arg_names == {"payload"} or (len(arg_names) == 1 and "payload" in arg_names)

        if is_payload:
            keys = _collect_input_keys_from_payload_function(func_node)
            unknown = keys - declared_input_names
            if unknown:
                raise AgentValidationError(
                    f"Function '{endpoint.name}' reads from payload the keys {sorted(unknown)} "
                    f"that are NOT listed in the global 'inputs' array."
                )
            for ref in endpoint.inputs:
                if ref.required and ref.input_ref not in keys:
                    raise AgentValidationError(
                        f"Endpoint '{endpoint.name}': input '{ref.input_ref}' "
                        f"is marked required but not read in this function."
                    )
            used_input_names.update(keys)
        else:
            unknown_args = arg_names - declared_input_names
            if unknown_args:
                raise AgentValidationError(
                    f"Function '{endpoint.name}' has parameters {list(unknown_args)} that are not "
                    f"declared in 'inputs'."
                )
            used_input_names.update(arg_names)

    unused = declared_input_names - used_input_names
    if unused:
        raise AgentValidationError(
            f"The following declared inputs are never read in any main.py endpoint: {list(unused)}."
        )


def _validate_endpoint_references(agent: AgentGenerationResponse) -> None:
    declared_input_names = {i.name for i in agent.inputs}
    declared_output_names = {o.name for o in agent.outputs}
    for endpoint in agent.agent_endpoints:
        for input_ref in endpoint.inputs:
            if input_ref.input_ref not in declared_input_names:
                raise AgentValidationError(
                    f"Endpoint '{endpoint.name}' references input '{input_ref.input_ref}' "
                    f"which is NOT declared in the global 'inputs' list."
                )
        unknown_outputs = [
            out for out in (endpoint.outputs or []) if out not in declared_output_names
        ]
        if unknown_outputs:
            raise AgentValidationError(
                f"Endpoint '{endpoint.name}' references unknown outputs {unknown_outputs}."
            )


def validate_agent_manifest(
    manifest: Union[AgentGenerationResponse, Dict[str, Any]],
) -> None:
    """
    Validate an agent generation manifest (raises ``AgentValidationError`` on failure).

    Accepts a parsed :class:`AgentGenerationResponse` or a JSON-serializable dict.
    """
    if isinstance(manifest, dict):
        agent = AgentGenerationResponse.model_validate(manifest)
    else:
        agent = manifest
    code = _validate_main_py(agent)
    _validate_unique_names(agent)
    _validate_endpoints_populated(agent)
    _validate_endpoint_references(agent)
    _validate_code_structure(agent, code)
