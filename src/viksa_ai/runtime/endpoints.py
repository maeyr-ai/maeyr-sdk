from typing import Callable


def mcp_endpoint(description: str):
    """Decorator to mark a function as an MCP endpoint with the given description."""
    def decorator(func: Callable):
        return func
    return decorator
