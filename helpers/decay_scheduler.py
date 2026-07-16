"""
Periodic confidence decay for stale graph entities.
Piggybacks on the extraction worker's idle cycle.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from usr.plugins._graph_memory.helpers import graph_db
from usr.plugins._graph_memory.helpers import entity_registry

log = logging.getLogger("_graph_memory.decay")

_last_decay_run = 0.0


async def run_if_due(config: dict):
    """Run decay if enough time has elapsed since last run."""
    global _last_decay_run
    
    if not config.get("decay_enabled", True):
        return
    
    min_interval = config.get("decay_min_interval_hours", 6) * 3600
    now = time.monotonic()
    if now - _last_decay_run < min_interval:
        return
    
    _last_decay_run = now
    await _apply_decay(config)


async def _apply_decay(config: dict):
    """Apply confidence decay to entities not seen recently."""
    factor = config.get("decay_factor", 0.95)
    age_days = config.get("decay_age_days", 7)
    floor = config.get("decay_min_confidence_floor", 0.15)
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%fZ")
    
    def _get_stale_ids():
        conn = graph_db.get_connection()
        rows = conn.execute(
            "SELECT entity_id FROM graph_entities "
            "WHERE last_seen < ? AND confidence > ?",
            (cutoff_iso, floor)
        ).fetchall()
        return [r["entity_id"] for r in rows]
    
    ids = await asyncio.to_thread(_get_stale_ids)
    if not ids:
        log.debug("Decay: no stale entities found")
        return
    
    count = await entity_registry.apply_decay(ids, factor)
    log.info(f"Decay: applied factor {factor} to {count} stale entities (older than {age_days}d)")
