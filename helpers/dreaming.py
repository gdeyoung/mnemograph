"""
Dreaming Engine — background graph consolidation.
7 independent idempotent passes that maintain graph quality.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from usr.plugins._graph_memory.helpers import graph_db
from usr.plugins._graph_memory.helpers import entity_registry

log = logging.getLogger("_graph_memory.dreaming")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _compute_canonical(name: str) -> str:
    """Lowercase, strip suffixes, collapse whitespace."""
    s = name.lower().strip()
    s = re.sub(r'\b(inc|corp|corporation|ltd|limited|the)\b\.?', '', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


async def _pass_decay(config: dict) -> dict:
    """Pass 1: Decay stale entity confidence."""
    if not config.get("dreaming_passes", {}).get("decay", True):
        return {"skipped": True}
    factor = config.get("decay_factor", 0.95)
    age_days = config.get("decay_age_days", 7)
    floor = config.get("decay_min_confidence_floor", 0.15)
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%fZ")
    
    def _run():
        conn = graph_db.get_connection()
        rows = conn.execute(
            "SELECT entity_id FROM graph_entities WHERE last_seen < ? AND confidence > ?",
            (cutoff_iso, floor)
        ).fetchall()
        ids = [r["entity_id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE graph_entities SET confidence = MAX(?, confidence * ?) "
                f"WHERE entity_id IN ({placeholders})",
                [floor, factor] + ids
            )
            conn.commit()
            return cur.rowcount
        return 0
    count = await asyncio.to_thread(_run)
    return {"decayed": count}


async def _pass_dedup(config: dict) -> dict:
    """Pass 2: Fuzzy-merge duplicate entities."""
    if not config.get("dreaming_passes", {}).get("dedup", True):
        return {"skipped": True}
    threshold = config.get("dedup_similarity_threshold", 0.85)
    
    def _run():
        conn = graph_db.get_connection()
        # Ensure all entities have canonical_name
        entities = [dict(r) for r in conn.execute(
            "SELECT entity_id, name, canonical_name, aliases FROM graph_entities"
        ).fetchall()]
        
        # Group by canonical name
        groups = {}
        for ent in entities:
            if not ent.get("canonical_name"):
                ent["canonical_name"] = _compute_canonical(ent["name"])
                conn.execute(
                    "UPDATE graph_entities SET canonical_name = ? WHERE entity_id = ?",
                    (ent["canonical_name"], ent["entity_id"])
                )
            canon = ent["canonical_name"]
            if canon not in groups:
                groups[canon] = []
            groups[canon].append(ent)
        conn.commit()
        
        # Merge groups with >1 entity
        merged = 0
        for canon, group in groups.items():
            if len(group) < 2:
                continue
            # Keep highest confidence as primary
            group.sort(key=lambda e: (e.get("confidence", 0.5), len(e.get("name", ""))), reverse=True)
            primary = group[0]
            aliases = set()
            try:
                aliases = set(json.loads(primary.get("aliases", "[]")))
            except Exception:
                pass
            
            for dup in group[1:]:
                aliases.add(dup["name"])
                # Merge mention_count
                conn.execute(
                    "UPDATE graph_entities SET mention_count = mention_count + "
                    "(SELECT mention_count FROM graph_entities WHERE entity_id = ?), "
                    "confidence = MAX(confidence, "
                    "(SELECT confidence FROM graph_entities WHERE entity_id = ?)) "
                    "WHERE entity_id = ?",
                    (dup["entity_id"], dup["entity_id"], primary["entity_id"])
                )
                # Update aliases
                aliases.add(dup["name"])
                # Delete relationships from dup that would collide with primary
                conn.execute(
                    "DELETE FROM graph_relationships "
                    "WHERE source_name = ? AND target_name IN "
                    "  (SELECT target_name FROM graph_relationships WHERE source_name = ?) "
                    "AND rel_type IN "
                    "  (SELECT rel_type FROM graph_relationships WHERE source_name = ?)",
                    (dup["name"], primary["name"], primary["name"])
                )
                conn.execute(
                    "DELETE FROM graph_relationships "
                    "WHERE target_name = ? AND source_name IN "
                    "  (SELECT source_name FROM graph_relationships WHERE target_name = ?) "
                    "AND rel_type IN "
                    "  (SELECT rel_type FROM graph_relationships WHERE target_name = ?)",
                    (dup["name"], primary["name"], primary["name"])
                )
                # Now safe to repoint remaining relationships
                conn.execute(
                    "UPDATE OR IGNORE graph_relationships SET source_name = ? WHERE source_name = ?",
                    (primary["name"], dup["name"])
                )
                conn.execute(
                    "UPDATE OR IGNORE graph_relationships SET target_name = ? WHERE target_name = ?",
                    (primary["name"], dup["name"])
                )
                # Delete any remaining (self-referencing) relationships
                conn.execute(
                    "DELETE FROM graph_relationships WHERE source_name = ? AND target_name = ?",
                    (dup["name"], dup["name"])
                )
                # Delete the duplicate
                conn.execute("DELETE FROM graph_entities WHERE entity_id = ?", (dup["entity_id"],))
                conn.execute(
                    "DELETE FROM graph_entity_memory_ids WHERE entity_id = ?",
                    (dup["entity_id"],)
                )
                merged += 1
            
            # Update aliases on primary
            if aliases:
                conn.execute(
                    "UPDATE graph_entities SET aliases = ? WHERE entity_id = ?",
                    (json.dumps(list(aliases)[:20]), primary["entity_id"])
                )
        
        conn.commit()
        return merged
    
    merged_count = await asyncio.to_thread(_run)
    entity_registry.invalidate_cache()
    return {"merged": merged_count}


async def _pass_prune(config: dict) -> dict:
    """Pass 3: Remove low-confidence single-mention entities."""
    if not config.get("dreaming_passes", {}).get("prune", True):
        return {"skipped": True}
    floor = config.get("prune_min_confidence", 0.15)
    
    def _run():
        conn = graph_db.get_connection()
        cur = conn.execute(
            "DELETE FROM graph_entities WHERE confidence < ? AND mention_count <= 1",
            (floor,)
        )
        pruned = cur.rowcount
        # Cascade delete orphaned relationships
        conn.execute(
            "DELETE FROM graph_relationships "
            "WHERE source_name NOT IN (SELECT name FROM graph_entities) "
            "OR target_name NOT IN (SELECT name FROM graph_entities)"
        )
        conn.commit()
        return pruned
    
    pruned = await asyncio.to_thread(_run)
    return {"pruned": pruned}


async def _pass_infer(config: dict) -> dict:
    """Pass 4: Find 2-hop transitive relationships."""
    if not config.get("dreaming_passes", {}).get("infer", True):
        return {"skipped": True}
    
    TRANSITIVE_TYPES = {"depends_on", "part_of", "runs_on"}
    
    def _run():
        conn = graph_db.get_connection()
        inferred = 0
        for rel_type in TRANSITIVE_TYPES:
            rows = conn.execute(
                "SELECT r1.source_name, r2.target_name "
                "FROM graph_relationships r1 "
                "JOIN graph_relationships r2 ON r1.target_name = r2.source_name "
                "WHERE r1.rel_type = ? AND r2.rel_type = ? "
                "AND r1.source_name != r2.target_name "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM graph_relationships r3 "
                "  WHERE r3.source_name = r1.source_name "
                "  AND r3.target_name = r2.target_name "
                "  AND r3.rel_type = ?"
                ")",
                (rel_type, rel_type, rel_type)
            ).fetchall()
            for row in rows:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_relationships "
                        "(source_name, target_name, rel_type, confidence, source_doc, created_at) "
                        "VALUES (?, ?, ?, ?, 'inferred', ?)",
                        (row["source_name"], row["target_name"],
                         rel_type, 0.3, _now_iso())
                    )
                    inferred += 1
                except Exception:
                    pass
        conn.commit()
        return inferred
    
    inferred_count = await asyncio.to_thread(_run)
    return {"inferred": inferred_count}


async def _pass_strengthen(config: dict) -> dict:
    """Pass 5: Boost confidence for frequently co-occurring entities."""
    if not config.get("dreaming_passes", {}).get("strengthen", True):
        return {"skipped": True}
    threshold = config.get("strengthen_co_occurrence_threshold", 3)
    
    def _run():
        conn = graph_db.get_connection()
        pairs = conn.execute(
            "SELECT e1.entity_id as eid1, e2.entity_id as eid2, COUNT(*) as co "
            "FROM graph_entities e1 "
            "JOIN graph_entities e2 ON e1.session_id = e2.session_id "
            "  AND e1.entity_id < e2.entity_id "
            "WHERE e1.session_id IS NOT NULL AND e2.session_id IS NOT NULL "
            "GROUP BY e1.entity_id, e2.entity_id "
            "HAVING co >= ? "
            "LIMIT 100",
            (threshold,)
        ).fetchall()
        bumped = 0
        for pair in pairs:
            boost = min(0.05 * pair["co"], 0.2)
            conn.execute(
                "UPDATE graph_entities SET confidence = MIN(1.0, confidence + ?) "
                "WHERE entity_id IN (?, ?)",
                (boost, pair["eid1"], pair["eid2"])
            )
            bumped += 2
        conn.commit()
        return bumped
    
    bumped_count = await asyncio.to_thread(_run)
    return {"strengthened": bumped_count}


async def _pass_link(config: dict) -> dict:
    """Pass 6: Populate FAISS memory links (stub — needs memory API)."""
    if not config.get("dreaming_passes", {}).get("link", False):
        return {"skipped": True}
    # This pass is a no-op until FAISS integration is tested
    # It will query agent.read_memory for entity mentions
    return {"linked": 0, "note": "FAISS linking not yet implemented"}


async def _pass_checkpoint(config: dict) -> dict:
    """Pass 7: WAL checkpoint and VACUUM."""
    if not config.get("dreaming_passes", {}).get("checkpoint", True):
        return {"skipped": True}
    
    def _run():
        conn = graph_db.get_connection()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            conn.execute("VACUUM")
        except Exception as e:
            log.warning(f"VACUUM failed (non-fatal): {e}")
        return True
    
    await asyncio.to_thread(_run)
    return {"checkpointed": True}


_DREAM_PASSES = [
    ("decay", _pass_decay),
    ("dedup", _pass_dedup),
    ("prune", _pass_prune),
    ("infer", _pass_infer),
    ("strengthen", _pass_strengthen),
    ("link", _pass_link),
    ("checkpoint", _pass_checkpoint),
]


async def run_dream_cycle(config: dict) -> dict:
    """Run one full dreaming cycle with all enabled passes."""
    results = {}
    for name, pass_fn in _DREAM_PASSES:
        try:
            result = await pass_fn(config)
            results[name] = result
            log.info(f"Dreaming pass '{name}': {result}")
        except Exception as e:
            log.error(f"Dreaming pass '{name}' failed: {e}")
            results[name] = {"error": str(e)}
    return results
