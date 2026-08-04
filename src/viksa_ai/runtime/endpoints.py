from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def mcp_endpoint(description: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to mark a function as an MCP endpoint with the given description."""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return func

    return decorator
