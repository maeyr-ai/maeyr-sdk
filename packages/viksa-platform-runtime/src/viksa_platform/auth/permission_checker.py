"""
Permission Checker Service

Provides FastAPI dependencies for checking user permissions at API endpoints.
This enforces access control at the API level using Auth's current decision.
`get_logged_user` validates the credential with Auth on every protected request;
the returned access map is therefore a live, tenant-scoped authorization result,
not a JWT permission cache.

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

_MODULE_ALIASES = {
    # Keep the historical API Fleet spelling isolated from credential
    # administration. A principal allowed to manage API definitions must not
    # thereby gain permission to create or revoke API keys.
    "api": "api_fleet",
    "trace": "traces",
    "worker": "workers",
}


def _canonical_module(module: str) -> str:
    normalized = module.strip().lower()
    return _MODULE_ALIASES.get(normalized, normalized)


def _normalized_string_set(value: Any) -> set[str] | None:
    """Return a normalized JSON string list, or ``None`` when it is malformed.

    Auth decisions cross a service boundary.  Treating dictionaries, strings,
    or mixed lists as iterable permission collections can accidentally turn a
    corrupt decision into authority (for example, ``"view"`` becoming five
    one-character actions).  A malformed decision therefore fails closed.
    """

    if not isinstance(value, list):
        return None
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized.add(item.strip().lower())
    return normalized


class PermissionDeniedError(Exception):
    """Raised when user lacks required permission"""

    pass


def has_permission(
    access_data: Dict[str, Any],
    module: str,
    action: str,
    *,
    grant_scope: str = "context",
) -> bool:
    """
    Check a permission in Auth's validated, current access decision.

    Args:
        access_data: Access data returned by Auth validation
        module: Module name (e.g., "agent", "workflows")
        action: Action type (e.g., "readonly", "readwrite", "delete")

    Returns:
        True if user has permission, False otherwise
    """
    if not isinstance(access_data, dict):
        return False
    if not isinstance(module, str) or not module.strip():
        return False
    if not isinstance(action, str) or not action.strip():
        return False
    module = _canonical_module(module)
    action = action.strip().lower()
    if grant_scope not in {"context", "organization", "account"}:
        return False

    # Super admins and account owners are explicit account-level principals.
    if access_data.get("is_admin") is True or access_data.get("is_account_owner") is True:
        return True

    # Check explicit denies first
    denied = access_data.get("denied", [])
    if not isinstance(denied, list):
        return False
    for deny in denied:
        if isinstance(deny, dict):
            raw_module = deny.get("module")
            deny_actions = _normalized_string_set(deny.get("actions"))
            if not isinstance(raw_module, str) or not raw_module.strip() or deny_actions is None:
                return False
            deny_module = _canonical_module(raw_module)
            if deny_module in {module, "*"} and (
                action in deny_actions or "*" in deny_actions
            ):
                return False
        elif isinstance(deny, str):
            # Simple string deny format: "module:action"
            deny_module, separator, deny_action = deny.partition(":")
            if not separator or not deny_module.strip() or not deny_action.strip():
                return False
            if _canonical_module(deny_module) in {module, "*"} and (
                deny_action.strip().lower() in {action, "*"}
            ):
                return False
        else:
            return False

    # Check if user has the required permission
    permission_field = {
        "context": "permissions",
        "organization": "organization_permissions",
        "account": "account_permissions",
    }[grant_scope]
    permissions = access_data.get(permission_field, [])
    if not isinstance(permissions, list):
        return False
    for perm in permissions:
        if not isinstance(perm, dict):
            return False
        raw_module = perm.get("module")
        actions = _normalized_string_set(perm.get("actions"))
        if not isinstance(raw_module, str) or not raw_module.strip() or actions is None:
            return False
        if _canonical_module(raw_module) == module:
            if action in actions:
                return True

    return False


def require_permission(
    module: str,
    action: str,
    *,
    grant_scope: str = "context",
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """
    FastAPI dependency factory for permission checking.

    Evaluates the live access data returned by Auth credential validation.

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

    if grant_scope not in {"context", "organization", "account"}:
        raise ValueError(f"unknown authorization grant scope {grant_scope!r}")

    async def permission_check(
        current_user: Dict[str, Any] = Depends(get_logged_user),
    ) -> Dict[str, Any]:
        # This access data was freshly resolved by Auth through get_logged_user.
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
        if not has_permission(
            access_data,
            module,
            action,
            grant_scope=grant_scope,
        ):
            user_id = (
                current_user.get("user_id")
                or current_user.get("sub")
                or current_user.get("user", {}).get("id")
            )
            logger.warning(
                f"Permission denied: principal={user_id} auth={current_user.get('auth_method')} "
                f"module={module} action={action} scope={grant_scope}"
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

    permission_check.required_permission = (  # type: ignore[attr-defined]
        _canonical_module(module),
        action.strip().lower(),
        grant_scope,
    )
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

        if not (
            access_data.get("is_admin") is True
            or access_data.get("is_account_owner") is True
        ):
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
    Extract permission data from the current Auth validation result.

    Useful for frontend consumption via API.

    Args:
        current_user: Current user result returned by Auth validation

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
RequireDevspaceExecute = require_permission("devspace", "execute")

RequireChatRead = require_permission("chat", "view")
RequireChatExecute = require_permission("chat", "create")

RequireOrgAdmin = require_permission("organization", "update")
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
