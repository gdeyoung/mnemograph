"""
Multi-agent graph synchronization via shared drive.
Exports/imports/merges agent graphs.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from usr.plugins._graph_memory.helpers import graph_db
from usr.plugins._graph_memory.helpers import entity_registry

log = logging.getLogger("_graph_memory.sync")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


async def export_to_shared(shared_dir: str = "/a0/shared/graphs", agent_name: str = "auto") -> dict:
    """Export this agent's graph to the shared drive."""
    if agent_name == "auto":
        agent_name = os.environ.get("AGENT_NAME", "local")
    
    os.makedirs(shared_dir, exist_ok=True)
    filepath = os.path.join(shared_dir, f"{agent_name}_graph.jsonl")
    
    def _run():
        conn = graph_db.get_connection()
        entities = [dict(r) for r in conn.execute(
            "SELECT * FROM graph_entities ORDER BY created_at ASC"
        ).fetchall()]
        relationships = [dict(r) for r in conn.execute(
            "SELECT * FROM graph_relationships ORDER BY created_at ASC"
        ).fetchall()]
        
        header = {
            "type": "graph_sync_export",
            "agent": agent_name,
            "exported_at": _now_iso(),
            "entity_count": len(entities),
            "relationship_count": len(relationships),
        }
        
        lines = [json.dumps(header)]
        for ent in entities:
            lines.append(json.dumps({"record_type": "entity", **ent}))
        for rel in relationships:
            lines.append(json.dumps({"record_type": "relationship", **rel}))
        
        with open(filepath, "w") as f:
            f.write("\n".join(lines) + "\n")
        
        return {"filepath": filepath, "entities": len(entities), "relationships": len(relationships)}
    
    return await asyncio.to_thread(_run)


async def merge_from_shared(shared_dir: str = "/a0/shared/graphs", global_file: str = "global_graph.jsonl") -> dict:
    """Import and merge the global graph from shared drive."""
    filepath = os.path.join(shared_dir, global_file)
    if not os.path.isfile(filepath):
        return {"error": f"Global graph not found at {filepath}"}
    
    def _run():
        with open(filepath, "r") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return {"error": "Empty file"}
        
        header = json.loads(lines[0])
        imported_entities = 0
        imported_rels = 0
        conn = graph_db.get_connection()
        conn.execute("BEGIN")
        
        try:
            for line in lines[1:]:
                record = json.loads(line)
                rtype = record.get("record_type", "")
                if rtype == "entity":
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_entities "
                        "(entity_id, name, type, domain, confidence, mention_count, "
                        " first_seen, last_seen, description, aliases, session_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.get("entity_id", ""),
                            record["name"], record.get("type", "concept"),
                            record.get("domain", "general"),
                            record.get("confidence", 0.5),
                            record.get("mention_count", 1),
                            record.get("first_seen", _now_iso()),
                            record.get("last_seen", _now_iso()),
                            record.get("description", ""),
                            record.get("aliases", "[]"),
                            record.get("session_id"),
                            record.get("created_at", _now_iso()),
                        )
                    )
                    imported_entities += 1
                elif rtype == "relationship":
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_relationships "
                        "(source_name, target_name, rel_type, confidence, source_doc, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            record["source_name"], record["target_name"],
                            record.get("rel_type", "related_to"),
                            record.get("confidence", 0.5),
                            record.get("source_doc", ""),
                            record.get("created_at", _now_iso()),
                        )
                    )
                    imported_rels += 1
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            return {"error": str(e)}
        
        entity_registry.invalidate_cache()
        return {"imported_entities": imported_entities, "imported_relationships": imported_rels}
    
    return await asyncio.to_thread(_run)


async def sync_status(shared_dir: str = "/a0/shared/graphs") -> dict:
    """Check sync state."""
    def _run():
        if not os.path.isdir(shared_dir):
            return {"sync_dir": shared_dir, "files": []}
        files = []
        for f in os.listdir(shared_dir):
            fpath = os.path.join(shared_dir, f)
            if f.endswith(".jsonl"):
                stat = os.stat(fpath)
                files.append({
                    "name": f,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
        return {"sync_dir": shared_dir, "files": files}
    
    return await asyncio.to_thread(_run)
