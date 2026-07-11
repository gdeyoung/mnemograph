"""
Entity registry with LRU cache for fast recall.
Cache: dict-based LRU, max 2000 entries, TTL 60s.
"""

import asyncio
import time
from collections import OrderedDict

from usr.plugins._graph_memory.helpers import graph_db
from usr.plugins._graph_memory.helpers.entity_validator import (
    normalize_name,
    validate_entity_name,
    detect_pii,
    is_valid_entity,
)


class LRUCache:
    """Simple dict-based LRU cache with TTL."""

    def __init__(self, max_size: int = 2000, ttl_seconds: int = 60):
        self._store: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def get(self, key: str):
        if key not in self._store:
            return None
        entry = self._store[key]
        if time.monotonic() - entry["ts"] > self._ttl:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return entry["value"]

    def put(self, key: str, value):
        self._store[key] = {"value": value, "ts": time.monotonic()}
        self._store.move_to_end(key)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()


_cache = LRUCache(max_size=2000, ttl_seconds=60)


def get_cache() -> LRUCache:
    return _cache


# ─── Async wrappers (all DB ops via to_thread) ───────────────

async def create_or_update_entity(name, etype, domain, description="",
                                  aliases=None, session_id=None,
                                  confidence=0.5):
    """Create or update entity with validation. Returns entity_id or None."""
    normed = normalize_name(name)
    if not is_valid_entity(normed, etype, confidence):
        return None

    result = await asyncio.to_thread(
        graph_db.upsert_entity,
        normed, etype, domain, description, aliases, session_id, confidence,
    )
    _cache.invalidate(normed)
    return result


async def get_entity(name):
    """Get entity by name — checks LRU cache first, then DB."""
    normed = normalize_name(name)
    cached = _cache.get(normed)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(graph_db.get_entity_by_name, normed)
    if result:
        _cache.put(normed, result)
    return result


async def search(query, limit=10, domain=None, etype=None):
    """Search entities (bypasses cache for fresh results)."""
    return await asyncio.to_thread(
        graph_db.search_entities, query, limit, domain, etype,
    )


async def get_top(limit=10, domain=None):
    """Get top entities by confidence×mentions."""
    return await asyncio.to_thread(graph_db.get_top_entities, limit, domain)


async def link_memory(entity_id, memory_id):
    """Link an entity to a FAISS memory_id."""
    await asyncio.to_thread(graph_db.link_memory_id, entity_id, memory_id)


async def get_relationships(name, limit=20):
    """Get relationships for an entity."""
    return await asyncio.to_thread(
        graph_db.get_relationships_for_entity, name, limit,
    )


async def apply_decay(entity_ids, factor):
    """Apply decay factor to entity confidences."""
    count = await asyncio.to_thread(
        graph_db.update_entity_confidence, entity_ids, factor,
    )
    # Invalidate cache for changed entities
    for eid in entity_ids:
        entity = await asyncio.to_thread(graph_db.get_entity_by_id, eid)
        if entity:
            _cache.invalidate(entity["name"])
    return count


async def delete_entity(entity_id):
    """Delete entity and cascade."""
    entity = await asyncio.to_thread(graph_db.get_entity_by_id, entity_id)
    if entity:
        _cache.invalidate(entity["name"])
    return await asyncio.to_thread(graph_db.delete_entity, entity_id)


async def get_stats():
    """Get entity/relationship stats."""
    return await asyncio.to_thread(graph_db.get_stats)


def invalidate_cache():
    """Clear entire cache (after bulk operations)."""
    _cache.clear()
