"""
Canonical Execution Configuration Resolver

Handles merging of execution configs with proper precedence:
1. System defaults (lowest priority)
2. Agent-level config
3. Endpoint-level config (highest priority)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class FallbackBehavior(str, Enum):
    """Behavior when endpoint execution fails after all retries"""

    ERROR = "error"
    SKIP = "skip"
    DEFAULT_VALUE = "default_value"


class InputValidationMode(str, Enum):
    """How to handle input validation"""

    STRICT = "strict"
    LENIENT = "lenient"
    AUTO_FIX = "auto_fix"


class LoggingLevel(str, Enum):
    """Execution logging verbosity"""

    MINIMAL = "minimal"
    STANDARD = "standard"
    VERBOSE = "verbose"


@dataclass
class ResolvedExecutionConfig:
    """
    Resolved execution configuration for an endpoint.
    All fields have concrete values after merging agent and endpoint configs.
    """

    # Approval control
    requires_approval: bool = False

    # Retry configuration
    retry_count: int = 3
    retry_delay_seconds: int = 1
    retry_backoff_multiplier: float = 2.0

    # Timeout configuration
    timeout_seconds: int = 30

    # Rate limiting
    rate_limit_per_minute: Optional[int] = None

    # Concurrency control
    max_concurrent: Optional[int] = None
    cool_down_seconds: int = 0

    # Error handling
    fallback_behavior: FallbackBehavior = FallbackBehavior.ERROR
    fallback_value: Any = None

    # AI-assisted execution
    confidence_threshold: float = 0.0

    # Validation and logging
    input_validation_mode: InputValidationMode = InputValidationMode.STRICT
    logging_level: LoggingLevel = LoggingLevel.STANDARD

    # Cache TTL (kept for legacy support)
    cache_ttl: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "requires_approval": self.requires_approval,
            "retry_count": self.retry_count,
            "retry_delay_seconds": self.retry_delay_seconds,
            "retry_backoff_multiplier": self.retry_backoff_multiplier,
            "timeout_seconds": self.timeout_seconds,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "max_concurrent": self.max_concurrent,
            "cool_down_seconds": self.cool_down_seconds,
            "fallback_behavior": self.fallback_behavior.value
            if isinstance(self.fallback_behavior, Enum)
            else self.fallback_behavior,
            "fallback_value": self.fallback_value,
            "confidence_threshold": self.confidence_threshold,
            "input_validation_mode": self.input_validation_mode.value
            if isinstance(self.input_validation_mode, Enum)
            else self.input_validation_mode,
            "logging_level": self.logging_level.value
            if isinstance(self.logging_level, Enum)
            else self.logging_level,
            "cache_ttl": self.cache_ttl,
        }


# System defaults
SYSTEM_DEFAULTS = ResolvedExecutionConfig()


def resolve_execution_config(
    agent_config: Optional[Dict[str, Any]] = None,
    endpoint_config: Optional[Dict[str, Any]] = None,
    endpoint_legacy: Optional[Dict[str, Any]] = None,
) -> ResolvedExecutionConfig:
    """
    Resolve execution configuration by merging configs with proper precedence.

    Precedence (highest to lowest):
    1. Endpoint-level execution_config
    2. Endpoint-level legacy fields (timeout, rate_limit, retry_policy)
    3. Agent-level execution_config
    4. System defaults

    Args:
        agent_config: Agent-level execution_config dict
        endpoint_config: Endpoint-level execution_config dict
        endpoint_legacy: Endpoint-level legacy fields (timeout, rate_limit, retry_policy, cache_ttl)

    Returns:
        ResolvedExecutionConfig with all fields resolved
    """
    # Start with system defaults
    config = ResolvedExecutionConfig()

    # Apply agent-level config
    if agent_config:
        _apply_config(config, agent_config)

    # Apply endpoint legacy fields (for backward compatibility)
    if endpoint_legacy:
        if endpoint_legacy.get("timeout") is not None:
            config.timeout_seconds = endpoint_legacy["timeout"]
        if endpoint_legacy.get("rate_limit") is not None:
            config.rate_limit_per_minute = endpoint_legacy["rate_limit"]
        if endpoint_legacy.get("cache_ttl") is not None:
            config.cache_ttl = endpoint_legacy["cache_ttl"]
        # Handle legacy retry_policy dict
        if endpoint_legacy.get("retry_policy"):
            retry_policy = endpoint_legacy["retry_policy"]
            if "count" in retry_policy or "retry_count" in retry_policy:
                config.retry_count = retry_policy.get("count") or retry_policy.get(
                    "retry_count", config.retry_count
                )
            if "delay" in retry_policy or "retry_delay_seconds" in retry_policy:
                config.retry_delay_seconds = retry_policy.get("delay") or retry_policy.get(
                    "retry_delay_seconds", config.retry_delay_seconds
                )
            if "backoff" in retry_policy or "retry_backoff_multiplier" in retry_policy:
                config.retry_backoff_multiplier = retry_policy.get("backoff") or retry_policy.get(
                    "retry_backoff_multiplier", config.retry_backoff_multiplier
                )

    # Apply endpoint-level config (highest precedence)
    if endpoint_config:
        _apply_config(config, endpoint_config)

    return config


def _apply_config(config: ResolvedExecutionConfig, source: Dict[str, Any]) -> None:
    """Apply source config values to resolved config (mutates config)"""

    # Approval control
    if "requires_approval" in source and source["requires_approval"] is not None:
        config.requires_approval = source["requires_approval"]

    # Retry configuration
    if "retry_count" in source and source["retry_count"] is not None:
        config.retry_count = source["retry_count"]
    if "retry_delay_seconds" in source and source["retry_delay_seconds"] is not None:
        config.retry_delay_seconds = source["retry_delay_seconds"]
    if "retry_backoff_multiplier" in source and source["retry_backoff_multiplier"] is not None:
        config.retry_backoff_multiplier = source["retry_backoff_multiplier"]

    # Timeout configuration
    if "timeout_seconds" in source and source["timeout_seconds"] is not None:
        config.timeout_seconds = source["timeout_seconds"]

    # Rate limiting
    if "rate_limit_per_minute" in source:
        config.rate_limit_per_minute = source["rate_limit_per_minute"]

    # Concurrency control
    if "max_concurrent" in source:
        config.max_concurrent = source["max_concurrent"]
    if "cool_down_seconds" in source and source["cool_down_seconds"] is not None:
        config.cool_down_seconds = source["cool_down_seconds"]

    # Error handling
    if "fallback_behavior" in source and source["fallback_behavior"] is not None:
        fb = source["fallback_behavior"]
        if isinstance(fb, str):
            config.fallback_behavior = FallbackBehavior(fb)
        else:
            config.fallback_behavior = fb
    if "fallback_value" in source:
        config.fallback_value = source["fallback_value"]

    # AI-assisted execution
    if "confidence_threshold" in source and source["confidence_threshold"] is not None:
        config.confidence_threshold = source["confidence_threshold"]

    # Validation and logging
    if "input_validation_mode" in source and source["input_validation_mode"] is not None:
        ivm = source["input_validation_mode"]
        if isinstance(ivm, str):
            config.input_validation_mode = InputValidationMode(ivm)
        else:
            config.input_validation_mode = ivm
    if "logging_level" in source and source["logging_level"] is not None:
        ll = source["logging_level"]
        if isinstance(ll, str):
            config.logging_level = LoggingLevel(ll)
        else:
            config.logging_level = ll

    # Cache TTL
    if "cache_ttl" in source:
        config.cache_ttl = source["cache_ttl"]


def get_endpoint_execution_config(
    agent: Dict[str, Any],
    endpoint: Dict[str, Any],
) -> ResolvedExecutionConfig:
    """
    Get the resolved execution config for a specific endpoint.

    Args:
        agent: Full agent dict (with execution_config field)
        endpoint: Endpoint dict (with optional execution_config and legacy fields)

    Returns:
        ResolvedExecutionConfig for the endpoint
    """
    agent_config = agent.get("execution_config")
    endpoint_config = endpoint.get("execution_config")

    # Extract legacy endpoint fields
    endpoint_legacy = {
        "timeout": endpoint.get("timeout"),
        "rate_limit": endpoint.get("rate_limit"),
        "retry_policy": endpoint.get("retry_policy"),
        "cache_ttl": endpoint.get("cache_ttl"),
    }

    return resolve_execution_config(
        agent_config=agent_config,
        endpoint_config=endpoint_config,
        endpoint_legacy=endpoint_legacy,
    )


def should_require_approval(
    execution_config: ResolvedExecutionConfig,
    ai_confidence: float = 1.0,
    annotations: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Determine if an endpoint execution should require user approval.

    Args:
        execution_config: Resolved execution config
        ai_confidence: AI's confidence level for this action (0.0-1.0)
        annotations: MCP-style annotations (may contain destructive flag)

    Returns:
        True if approval is required, False otherwise
    """
    # If requires_approval is explicitly set to True
    if execution_config.requires_approval:
        # But check confidence threshold - if AI is confident enough, auto-approve
        if (
            execution_config.confidence_threshold > 0
            and ai_confidence >= execution_config.confidence_threshold
        ):
            return False
        return True

    # Check MCP annotations for destructive operations
    if annotations:
        if annotations.get("destructive", False):
            # Destructive operations should require approval unless confidence is high enough
            if (
                execution_config.confidence_threshold > 0
                and ai_confidence >= execution_config.confidence_threshold
            ):
                return False
            return True

    return False
