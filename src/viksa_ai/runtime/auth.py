import os
from typing import Dict, List, Optional


class ViksaAuthError(RuntimeError):
    """Raised when a required auth method or param is not configured at runtime.

    Use this to fail loudly when an agent expects a specific auth method to be
    enabled but it is not, or when a required param value is missing.
    """


class ViksaAuth:
    """Access configured authentication methods and their parameters.

    Auth params are injected as environment variables in the form
    ``{method_id}.{param_name}`` (e.g. ``bearer_token.api_key``). The set of
    fully-resolved method ids is published as a comma-separated list in
    ``VIKSA_AUTH_ENABLED_METHODS``. All methods declared for this agent
    (including those still missing secrets) are listed in
    ``VIKSA_AUTH_CONFIGURED_METHODS``.

    A method only appears in the enabled set when ALL of its declared params
    successfully resolved at deploy time; partial methods are excluded so
    ``is_method_enabled`` cannot return True for a method whose params would
    later read as ``None``. Use ``is_method_configured`` to check whether a
    method belongs to this agent; use ``get_param`` / ``require_param`` to read
    any param value that did resolve.
    """

    _ENABLED_ENV_KEY = "VIKSA_AUTH_ENABLED_METHODS"
    _CONFIGURED_ENV_KEY = "VIKSA_AUTH_CONFIGURED_METHODS"

    @staticmethod
    def _env_key(method_id: str, param_name: str) -> str:
        return "{}.{}".format(method_id, param_name)

    @staticmethod
    def get_configured_methods() -> List[str]:
        """Returns auth method ids declared/configured for this agent."""
        raw = os.environ.get(ViksaAuth._CONFIGURED_ENV_KEY, "")
        return [m.strip() for m in raw.split(",") if m.strip()]

    @staticmethod
    def is_method_configured(method_id: str) -> bool:
        """Check if an auth method is configured for this agent."""
        return method_id in ViksaAuth.get_configured_methods()

    @staticmethod
    def get_enabled_methods() -> List[str]:
        """Returns the list of enabled auth method ids (in declaration order)."""
        raw = os.environ.get(ViksaAuth._ENABLED_ENV_KEY, "")
        return [m.strip() for m in raw.split(",") if m.strip()]

    @staticmethod
    def is_method_enabled(method_id: str) -> bool:
        """Check if a specific auth method is enabled."""
        return method_id in ViksaAuth.get_enabled_methods()

    @staticmethod
    def get_param(method_id: str, param_name: str) -> Optional[str]:
        """Get a single parameter value for an auth method, or None if unset."""
        return os.environ.get(ViksaAuth._env_key(method_id, param_name))

    @staticmethod
    def require_param(method_id: str, param_name: str) -> str:
        """Like ``get_param`` but raises ``ViksaAuthError`` if the value is unset.

        Prefer this in agent code paths that genuinely require the param so the
        failure is loud and the error message points at the misconfiguration.
        """
        value = ViksaAuth.get_param(method_id, param_name)
        if value is None:
            raise ViksaAuthError(
                "Auth param '{}.{}' is not configured. Either the auth method "
                "is disabled or its vault secret failed to resolve at deploy time.".format(
                    method_id, param_name
                )
            )
        return value

    @staticmethod
    def get_method_params(method_id: str) -> Dict[str, str]:
        """Get all parameters for an auth method as a dict.

        Scans the environment for keys matching ``{method_id}.<param>`` and
        returns a mapping of param name to value. Nested dotted keys are
        ignored.
        """
        prefix = "{}.".format(method_id)
        return {
            k[len(prefix):]: v
            for k, v in os.environ.items()
            if k.startswith(prefix) and "." not in k[len(prefix):]
        }

    @staticmethod
    def preferred_method(*candidates: str) -> Optional[str]:
        """Return the first enabled method id from ``candidates``, or None.

        Useful for agents that support multiple auth methods and want to pick
        one at runtime in a fixed preference order, e.g.::

            method = ViksaAuth.preferred_method("oauth_client", "bearer_token")
            if method == "oauth_client":
                ...
        """
        enabled = ViksaAuth.get_enabled_methods()
        for candidate in candidates:
            if candidate in enabled:
                return candidate
        return None

    @staticmethod
    def require_method(*candidates: str) -> str:
        """Like ``preferred_method`` but raises ``ViksaAuthError`` if none match.

        With no candidates, returns the first enabled method or raises if none
        are enabled at all.
        """
        if candidates:
            chosen = ViksaAuth.preferred_method(*candidates)
            if chosen is None:
                raise ViksaAuthError(
                    "None of the requested auth methods are configured: {}. "
                    "Enabled methods: {}.".format(
                        list(candidates), ViksaAuth.get_enabled_methods()
                    )
                )
            return chosen
        enabled = ViksaAuth.get_enabled_methods()
        if not enabled:
            raise ViksaAuthError(
                "No auth methods are configured. Configure at least one auth "
                "method on this agent to use ViksaAuth.require_method()."
            )
        return enabled[0]
