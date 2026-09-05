"""
Redis Pub/Sub client for distributed execution streaming and cross-pod cancellation.

Design:
- Receives a service-owned Redis client through dependency injection
- Provides publish, subscribe, execution registry, and event history
- Graceful degradation: all methods return safe defaults when Redis is unavailable
- Used by ExecutionBroadcaster for cross-pod event delivery and cancellation
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, cast

logger = logging.getLogger("[maeyr_platform.redis.pubsub]")

# =============================================================================
# KEY NAMING CONVENTIONS
# =============================================================================
_KEY_PREFIX = "chat:"
_EXECUTION_CHANNEL_PREFIX = f"{_KEY_PREFIX}execution:"
_CANCEL_CHANNEL = f"{_KEY_PREFIX}cancel"  # Single channel for all cancel requests
_ACTIVE_EXECUTIONS_KEY = f"{_KEY_PREFIX}active_executions"
_HISTORY_PREFIX = f"{_KEY_PREFIX}history:"

# TTL settings
_EXECUTION_REGISTRY_TTL = 3900  # 65 minutes (execution timeout + buffer)
_HISTORY_TTL_AFTER_COMPLETE = 300  # 5 minutes after execution ends
_MAX_HISTORY_EVENTS = 500

# Feature flag
REDIS_PUBSUB_ENABLED = os.getenv("DISABLE_REDIS_PUBSUB", "").lower() != "true"


class RedisPubSubManager:
    """
    Redis Pub/Sub manager for distributed execution streaming.

    Provides:
    - Execution registry (HASH): track which pod owns which execution
    - Event pub/sub (CHANNELS): broadcast events across pods
    - Event history (LIST): catch-up for reconnecting clients
    - Cancel channel: propagate cancellation across pods
    """

    __slots__ = ("_client", "_pod_id", "_is_available", "_sub_tasks")

    def __init__(self, client: Any, *, pod_id: str | None = None) -> None:
        self._client = client
        self._pod_id = pod_id or os.getenv("HOSTNAME") or f"chat-{os.getpid()}"
        self._is_available = False
        self._sub_tasks: Dict[str, asyncio.Task[Any]] = {}

    async def connect(self) -> bool:
        """Connect to Redis. Returns True if successful."""
        if not REDIS_PUBSUB_ENABLED:
            logger.info("Redis Pub/Sub disabled by DISABLE_REDIS_PUBSUB flag")
            return False

        try:
            await self._client.connect()
            self._is_available = True
            logger.info(f"Redis Pub/Sub ready (pod={self._pod_id})")
            return True
        except Exception as e:
            logger.warning(f"Redis Pub/Sub unavailable, using local-only mode: {e}")
            self._is_available = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        # Cancel any active subscriber tasks
        for task in self._sub_tasks.values():
            task.cancel()
        self._sub_tasks.clear()

        if self._is_available:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting Redis: {e}")
            self._is_available = False
            logger.info("Redis Pub/Sub disconnected")

    @property
    def is_available(self) -> bool:
        return self._is_available and REDIS_PUBSUB_ENABLED

    @property
    def pod_id(self) -> str:
        return self._pod_id

    # =========================================================================
    # EXECUTION REGISTRY (Redis HASH)
    # =========================================================================

    async def register_execution(
        self,
        conversation_id: str,
        message_id: str,
        user_id: str,
        org_id: str,
        project_id: str,
    ) -> bool:
        """Register a new execution in Redis (atomic HSETNX)."""
        if not self.is_available:
            return False

        try:
            info = json.dumps(
                {
                    "message_id": message_id,
                    "pod_id": self._pod_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "user_id": user_id,
                    "org_id": org_id,
                    "project_id": project_id,
                }
            )
            created = await self._client.redis.hsetnx(_ACTIVE_EXECUTIONS_KEY, conversation_id, info)
            if created:
                await self._client.redis.expire(_ACTIVE_EXECUTIONS_KEY, _EXECUTION_REGISTRY_TTL)
                logger.debug(
                    f"Registered execution in Redis: conv={conversation_id[:8]}, pod={self._pod_id}"
                )
            return bool(created)
        except Exception as e:
            logger.error(f"Failed to register execution in Redis: {e}")
            return False

    async def get_execution_info(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Get info about an active execution from Redis."""
        if not self.is_available:
            return None

        try:
            data = await self._client.redis.hget(_ACTIVE_EXECUTIONS_KEY, conversation_id)
            if data:
                return cast(Dict[str, Any], json.loads(data))
            return None
        except Exception as e:
            logger.error(f"Failed to get execution info from Redis: {e}")
            return None

    async def unregister_execution(self, conversation_id: str) -> None:
        """Remove an execution from the Redis registry."""
        if not self.is_available:
            return

        try:
            await self._client.redis.hdel(_ACTIVE_EXECUTIONS_KEY, conversation_id)
        except Exception as e:
            logger.error(f"Failed to unregister execution from Redis: {e}")

    # =========================================================================
    # EVENT PUB/SUB
    # =========================================================================

    async def publish_event(
        self,
        conversation_id: str,
        event_type: str,
        data: Optional[Dict[str, Any]],
        message_id: str,
        sequence: int,
    ) -> bool:
        """Publish an execution event to the Redis channel + append to history."""
        if not self.is_available:
            return False

        try:
            channel = f"{_EXECUTION_CHANNEL_PREFIX}{conversation_id}"
            payload = json.dumps(
                {
                    "type": "event",
                    "event": event_type,
                    "data": data,
                    "message_id": message_id,
                    "sequence": sequence,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            # Publish to channel (for live subscribers on any pod)
            await self._client.redis.publish(channel, payload)

            # Also store in history list (for reconnecting clients)
            history_key = f"{_HISTORY_PREFIX}{conversation_id}"
            await self._client.redis.rpush(history_key, payload)
            await self._client.redis.ltrim(history_key, -_MAX_HISTORY_EVENTS, -1)
            await self._client.redis.expire(history_key, _EXECUTION_REGISTRY_TTL)

            return True
        except Exception as e:
            logger.error(f"Failed to publish event to Redis: {e}")
            return False

    async def publish_completion(
        self,
        conversation_id: str,
        message_id: str,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """Publish execution completion and clean up Redis state."""
        if not self.is_available:
            return

        try:
            channel = f"{_EXECUTION_CHANNEL_PREFIX}{conversation_id}"
            payload = json.dumps(
                {
                    "type": "complete" if success else "error",
                    "message_id": message_id,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._client.redis.publish(channel, payload)

            # Cleanup: remove from active, set short TTL on history
            await self.unregister_execution(conversation_id)
            history_key = f"{_HISTORY_PREFIX}{conversation_id}"
            await self._client.redis.expire(history_key, _HISTORY_TTL_AFTER_COMPLETE)
        except Exception as e:
            logger.error(f"Failed to publish completion to Redis: {e}")

    async def get_event_history(self, conversation_id: str) -> List[Dict[str, Any]]:
        """Get event history for catch-up on reconnection."""
        if not self.is_available:
            return []

        try:
            history_key = f"{_HISTORY_PREFIX}{conversation_id}"
            events = await self._client.redis.lrange(history_key, 0, -1)
            return [json.loads(e) for e in events]
        except Exception as e:
            logger.error(f"Failed to get event history from Redis: {e}")
            return []

    async def subscribe(self, conversation_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Subscribe to live execution events from Redis.

        Yields event dicts as they arrive. Ends on 'complete' or 'error' type.
        """
        if not self.is_available:
            return

        channel = f"{_EXECUTION_CHANNEL_PREFIX}{conversation_id}"
        pubsub = self._client.redis.pubsub()

        try:
            await pubsub.subscribe(channel)
            logger.debug(f"Redis subscribed to {channel}")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        event = json.loads(message["data"])
                        yield event
                        if event.get("type") in ("complete", "error", "cancelled"):
                            break
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid Redis message: {str(message['data'])[:100]}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis subscription error for {conversation_id[:8]}: {e}")
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

    # =========================================================================
    # CROSS-POD CANCELLATION
    # =========================================================================

    async def publish_cancel(
        self, conversation_id: str, user_context: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        Publish a cancellation request to the cancel channel.
        All pods receive this and cancel the execution if they own it.
        """
        if not self.is_available:
            return False

        try:
            payload = json.dumps(
                {
                    "type": "cancel",
                    "conversation_id": conversation_id,
                    "user_context": user_context,
                    "requesting_pod": self._pod_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            receivers = await self._client.redis.publish(_CANCEL_CHANNEL, payload)
            logger.info(f"Published cancel for {conversation_id[:8]}, receivers={receivers}")
            return int(receivers) > 0
        except Exception as e:
            logger.error(f"Failed to publish cancel to Redis: {e}")
            return False

    async def subscribe_cancellations(
        self,
        callback: Callable[[str, Dict[str, Any] | None], Awaitable[None]],
    ) -> None:
        """
        Subscribe to the cancel channel. Runs as a long-lived background task.

        Args:
            callback: async function(conversation_id, user_context) called on cancel messages
        """
        if not self.is_available:
            return

        pubsub = self._client.redis.pubsub()
        try:
            await pubsub.subscribe(_CANCEL_CHANNEL)
            logger.info(f"Subscribed to cancel channel (pod={self._pod_id})")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        if data.get("type") == "cancel":
                            conv_id = data.get("conversation_id")
                            user_ctx = data.get("user_context")
                            requesting_pod = data.get("requesting_pod")
                            # Don't re-cancel if this pod sent the cancel
                            if requesting_pod != self._pod_id and conv_id:
                                logger.info(
                                    "Received cross-pod cancel for %s from %s",
                                    conv_id[:8],
                                    requesting_pod,
                                )
                                await callback(conv_id, user_ctx)
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        logger.error(f"Error processing cancel message: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Cancel subscription error: {e}")
        finally:
            try:
                await pubsub.unsubscribe(_CANCEL_CHANNEL)
                await pubsub.close()
            except Exception:
                pass

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    async def health_check(self) -> Dict[str, Any]:
        """Get Redis Pub/Sub health status."""
        if not self.is_available:
            return {"status": "disabled" if not REDIS_PUBSUB_ENABLED else "disconnected"}

        try:
            start = asyncio.get_event_loop().time()
            await self._client.redis.ping()
            latency_ms = (asyncio.get_event_loop().time() - start) * 1000

            active_count = await self._client.redis.hlen(_ACTIVE_EXECUTIONS_KEY)

            return {
                "status": "connected",
                "pod_id": self._pod_id,
                "latency_ms": round(latency_ms, 2),
                "active_executions_redis": active_count,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}
