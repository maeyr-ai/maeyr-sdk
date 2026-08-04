"""
Permission Checker Service

Provides FastAPI dependencies for checking user permissions at API endpoints.
This enforces access control at the API level using JWT-embedded permissions.

ZERO LATENCY: Permissions are extracted directly from JWT token.
No network calls to auth service required.

Usage:
    @router.post("/agents")
    async def create_agent(
        ...,
        current_user: Dict = Depends(require_permission("agent", "readwrite"))
    ):
        ...
"""

from logging import getLogger
from typing import Any, Awaitable, Callable, Dict, cast

from fastapi import Depends, HTTPException, status

from viksa_platform.auth.fastapi_validator import get_logged_user
from viksa_platform.security.tenant_safe_display import public_http_detail

logger = getLogger("[viksa_platform.auth.permission_checker]")
PermissionDependency = Callable[..., Awaitable[dict[str, Any]]]


class PermissionDeniedError(Exception):
    """Raised when user lacks required permission"""

    pass


def has_permission(access_data: Dict[str, Any], module: str, action: str) -> bool:
    """
    Check if user has specific permission based on JWT access data.

    Args:
        access_data: Access data from JWT token
        module: Module name (e.g., "agent", "workflows")
        action: Action type (e.g., "readonly", "readwrite", "delete")

    Returns:
        True if user has permission, False otherwise
    """
    # Super admins and account owners have all permissions
    if access_data.get("is_admin", False) or access_data.get("is_account_owner", False):
        return True

    # Check explicit denies first
    for deny in access_data.get("denied", []):
        if isinstance(deny, dict):
            if deny.get("module") == module and action in deny.get("actions", []):
                return False
        elif isinstance(deny, str):
            # Simple string deny format: "module:action"
            if deny == f"{module}:{action}":
                return False

    # Check if user has the required permission
    for perm in access_data.get("permissions", []):
        if perm.get("module") == module:
            if action in perm.get("actions", []):
                return True
            # "admin" or "all" action grants all other actions for that module
            if "admin" in perm.get("actions", []):
                return True
            if "all" in perm.get("actions", []):
                return True

    return False


def require_permission(
    module: str,
    action: str,
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """
    FastAPI dependency factory for permission checking.

    Extracts permissions from JWT token - NO NETWORK CALLS.

    Args:
        module: Module name (e.g., "agent", "workflows", "chat")
        action: Action type (e.g., "readonly", "readwrite", "delete", "execute")

    Returns:
        FastAPI dependency function

    Usage:
        @router.post("/agents")
        async def create_agent(
            agent_data: AgentCreate,
            current_user: Dict = Depends(require_permission("agent", "readwrite"))
        ):
            # Only reaches here if user has agent:readwrite permission
            ...
    """

    async def permission_check(
        current_user: Dict[str, Any] = Depends(get_logged_user),
    ) -> Dict[str, Any]:
        # Extract access data from JWT (already decoded by get_logged_user)
        access_data = current_user.get("access", {})

        # SECURITY FIX: Strict permission enforcement
        # Migration mode has been removed - tokens without access data are rejected
        # This prevents unauthorized access via stale tokens
        if not access_data:
            user_id = (
                current_user.get("user_id")
                or current_user.get("sub")
                or current_user.get("user", {}).get("id")
            )
            logger.warning(
                f"No access data for principal={user_id} auth={current_user.get('auth_method')}, "
                f"denying {module}:{action} (STRICT MODE - user must re-login)"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing access data. Please re-login to refresh your token.",
            )

        # Check permission
        if not has_permission(access_data, module, action):
            user_id = (
                current_user.get("user_id")
                or current_user.get("sub")
                or current_user.get("user", {}).get("id")
            )
            logger.warning(
                f"Permission denied: principal={user_id} auth={current_user.get('auth_method')} "
                f"module={module} action={action}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=public_http_detail(
                    message="Permission denied",
                    fallback="Permission denied",
                ),
            )

        # Return user data for use in endpoint
        return current_user

    return permission_check


def require_admin() -> Callable[..., Awaitable[Dict[str, Any]]]:
    """
    FastAPI dependency that requires super admin access.

    Usage:
        @router.delete("/org/{org_id}")
        async def delete_org(
            org_id: str,
            current_user: Dict = Depends(require_admin())
        ):
            ...
    """

    async def admin_check(
        current_user: Dict[str, Any] = Depends(get_logged_user),
    ) -> Dict[str, Any]:
        access_data = current_user.get("access", {})

        if not access_data.get("is_admin", False):
            user_id = (
                current_user.get("user_id")
                or current_user.get("sub")
                or current_user.get("user", {}).get("id")
            )
            logger.warning(
                f"Admin access denied: principal={user_id} auth={current_user.get('auth_method')}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
            )

        return current_user

    return admin_check


def get_user_permissions(current_user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract permission data from current user JWT payload.

    Useful for frontend consumption via API.

    Args:
        current_user: Decoded JWT payload

    Returns:
        Access data dict with is_admin, permissions, denied
    """
    access = current_user.get("access")
    if isinstance(access, dict):
        return cast(Dict[str, Any], access)
    return {
        "is_admin": False,
        "is_account_owner": False,
        "permissions": [],
        "denied": [],
    }


# Convenient pre-built dependencies for common permission checks
RequireAgentRead = require_permission("agent", "view")
RequireAgentWrite = require_permission("agent", "update")
RequireAgentDelete = require_permission("agent", "delete")

RequireScheduleRead = require_permission("schedule", "view")
RequireScheduleWrite = require_permission("schedule", "update")
RequireScheduleExecute = require_permission("schedule", "execute")

RequireDevspaceRead = require_permission("devspace", "view")
RequireDevspaceExecute = require_permission("devspace", "all")

RequireChatRead = require_permission("chat", "view")
RequireChatExecute = require_permission("chat", "create")

RequireOrgAdmin = require_permission("organization", "admin")
RequireAdmin = require_admin()


__all__ = [
    "PermissionDeniedError",
    "PermissionDependency",
    "RequireAdmin",
    "RequireAgentDelete",
    "RequireAgentRead",
    "RequireAgentWrite",
    "RequireChatExecute",
    "RequireChatRead",
    "RequireDevspaceExecute",
    "RequireDevspaceRead",
    "RequireOrgAdmin",
    "RequireScheduleExecute",
    "RequireScheduleRead",
    "RequireScheduleWrite",
    "get_user_permissions",
    "has_permission",
    "require_admin",
    "require_permission",
]
