"""Per-conversation agent-state persistence, keyed by request UUID.

Stores the autogen agent's ``save_state()`` mapping so a conversation can be
resumed across HTTP requests: the same ``request_uuid`` reloads the agent's
prior context; a new UUID (via "New chat") starts fresh.

Backed by Redis (``redis.asyncio``); if Redis is unreachable it degrades to an
in-process dict so the app keeps working (with a warning). Configure with
``REDIS_URL`` (default ``redis://localhost:6379/0``) and optional ``CORA_STATE_TTL``
seconds (default 1 day).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STATE_TTL = int(os.getenv("CORA_STATE_TTL", "86400"))
_PREFIX = "cora:agent_state:"


class StateStore:
    def __init__(self, url: str = REDIS_URL, ttl: int = STATE_TTL):
        self.url = url
        self.ttl = ttl
        self._redis: Any = None          # client, or False if unavailable
        self._mem: Dict[str, str] = {}    # fallback

    async def _client(self):
        """Lazily connect to Redis; cache False on failure to fall back."""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                client = aioredis.from_url(self.url, decode_responses=True)
                await client.ping()
                self._redis = client
                log.info("state store: connected to Redis at %s", self.url)
            except Exception as exc:
                log.warning("state store: Redis unavailable (%s); using in-memory store", exc)
                self._redis = False
        return self._redis or None

    @staticmethod
    def _key(request_uuid: str, agent_name: Optional[str]) -> str:
        """Namespace state per agent so several agents can share one uuid."""
        if agent_name:
            return f"{_PREFIX}{agent_name}:{request_uuid}"
        return _PREFIX + request_uuid

    async def load(
        self, request_uuid: str, agent_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if not request_uuid:
            return None
        key = self._key(request_uuid, agent_name)
        client = await self._client()
        if client is not None:
            raw = await client.get(key)
        else:
            raw = self._mem.get(key)
        if not raw:
            return None
        try:
            state = json.loads(raw)
            log.info("state store: loaded state for %s", key)
            return state
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            log.warning("state store: corrupt state for %s: %s", key, exc)
            return None

    async def save(
        self, request_uuid: str, state: Dict[str, Any], agent_name: Optional[str] = None
    ) -> None:
        if not request_uuid:
            return
        key = self._key(request_uuid, agent_name)
        raw = json.dumps(state, default=str)
        client = await self._client()
        if client is not None:
            await client.set(key, raw, ex=self.ttl)
        else:
            self._mem[key] = raw
        log.info("state store: saved state for %s (%d bytes)", key, len(raw))

    async def clear(self, request_uuid: str, agent_name: Optional[str] = None) -> None:
        key = self._key(request_uuid, agent_name)
        client = await self._client()
        if client is not None:
            await client.delete(key)
        else:
            self._mem.pop(key, None)


_store: Optional[StateStore] = None


def get_store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store
