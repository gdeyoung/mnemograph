"""
Backup, recovery, and lifecycle management for graph_memory.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from usr.plugins._graph_memory.helpers import graph_db

log = logging.getLogger("_graph_memory.lifecycle")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _compute_canonical(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r'\b(inc|corp|corporation|ltd|limited|the)\b\.?', '', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _cleanup_orphans_sync() -> dict:
    import shutil
    result = {
        "orphans_removed": 0,
        "invalid_entities_removed": 0,
        "backup_path": None,
        "vacuumed": False,
    }
    db_path = graph_db._get_db_path()
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_name = f"graph_pre_cleanup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
    backup_path = os.path.join(backup_dir, backup_name)
    shutil.copy2(db_path, backup_path)
    result["backup_path"] = backup_path
    conn = graph_db.get_connection()
    invalid_cursor = conn.execute(
        "DELETE FROM graph_entities WHERE length(name) < 2"
    )
    result["invalid_entities_removed"] = invalid_cursor.rowcount
    conn.commit()
    orphan_cursor = conn.execute(
        "DELETE FROM graph_relationships "
        "WHERE source_name NOT IN (SELECT name FROM graph_entities) "
        "OR target_name NOT IN (SELECT name FROM graph_entities)"
    )
    result["orphans_removed"] = orphan_cursor.rowcount
    conn.commit()
    try:
        conn.execute("VACUUM")
        result["vacuumed"] = True
    except Exception as e:
        log.warning("VACUUM failed (non-fatal): %s", e)
    # Backfill canonical names if column exists
    try:
        rows = conn.execute(
            "SELECT entity_id, name FROM graph_entities WHERE canonical_name IS NULL"
        ).fetchall()
        for row in rows:
            canon = _compute_canonical(row["name"])
            conn.execute(
                "UPDATE graph_entities SET canonical_name = ? WHERE entity_id = ?",
                (canon, row["entity_id"])
            )
        if rows:
            conn.commit()
            log.info("Backfilled canonical_name for %d entities", len(rows))
    except Exception:
        pass  # canonical_name column may not exist
    log.info(
        "Auto-cleanup: removed %d orphans, %d invalid entities, vacuumed=%s",
        result["orphans_removed"],
        result["invalid_entities_removed"],
        result["vacuumed"],
    )
    return result


async def run_schema_migrations():
    await asyncio.to_thread(graph_db.ensure_schema)
    return graph_db.get_schema_version()


GRAPH_TABLES = [
    "graph_entities",
    "graph_relationships",
    "graph_entity_memory_ids",
]


def _export_sync(target_dir: str) -> dict:
    os.makedirs(target_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = os.path.join(target_dir, f"graph_snapshot_{timestamp}.jsonl")
    conn = graph_db.get_connection()
    entities = [dict(r) for r in conn.execute(
        "SELECT * FROM graph_entities ORDER BY created_at ASC"
    ).fetchall()]
    relationships = [dict(r) for r in conn.execute(
        "SELECT * FROM graph_relationships ORDER BY created_at ASC"
    ).fetchall()]
    memory_ids = [dict(r) for r in conn.execute(
        "SELECT * FROM graph_entity_memory_ids"
    ).fetchall()]
    schema_version = graph_db.get_schema_version()
    header = {
        "type": "graph_memory_export",
        "schema_version": schema_version,
        "exported_at": _now_iso(),
        "entity_count": len(entities),
        "relationship_count": len(relationships),
        "memory_id_count": len(memory_ids),
    }
    lines = [json.dumps(header)]
    for ent in entities:
        lines.append(json.dumps({"record_type": "entity", **ent}))
    for rel in relationships:
        lines.append(json.dumps({"record_type": "relationship", **rel}))
    for mid in memory_ids:
        lines.append(json.dumps({"record_type": "memory_id", **mid}))
    content = "\n".join(lines) + "\n"
    checksum = hashlib.sha256(content.encode()).hexdigest()
    header["checksum"] = checksum
    lines[0] = json.dumps(header)
    content = "\n".join(lines) + "\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    snapshot_id = uuid.uuid4().hex[:16]
    try:
        conn.execute(
            "INSERT OR REPLACE INTO graph_backup_manifest "
            "(snapshot_id, created_at, schema_version, entity_count, "
            " relationship_count, db_checksum, backup_path, integrity_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, _now_iso(), schema_version, len(entities),
             len(relationships), checksum, filepath, "ok"),
        )
        conn.commit()
    except Exception:
        pass
    return {
        "snapshot_id": snapshot_id,
        "filepath": filepath,
        "entity_count": len(entities),
        "relationship_count": len(relationships),
        "checksum": checksum,
    }


async def graph_export(target_dir: str) -> dict:
    return await asyncio.to_thread(_export_sync, target_dir)


def _import_sync(source_path: str, mode: str = "merge") -> dict:
    if not os.path.isfile(source_path):
        return {"error": f"File not found: {source_path}"}
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = [l for l in content.strip().split("\n") if l.strip()]
    if not lines:
        return {"error": "Empty file"}
    header = json.loads(lines[0])
    stored_checksum = header.get("checksum", "")
    header_copy = dict(header)
    header_copy.pop("checksum", None)
    verify_lines = [json.dumps(header_copy)] + lines[1:]
    verify_content = "\n".join(verify_lines) + "\n"
    computed_checksum = hashlib.sha256(verify_content.encode()).hexdigest()
    if stored_checksum and stored_checksum != computed_checksum:
        return {"error": "Checksum mismatch"}
    conn = graph_db.get_connection()
    imported_entities = 0
    imported_rels = 0
    imported_mids = 0
    try:
        conn.execute("BEGIN")
        if mode == "replace":
            for table in GRAPH_TABLES:
                conn.execute(f"DELETE FROM {table}")
        for line in lines[1:]:
            record = json.loads(line)
            rtype = record.get("record_type", "")
            if rtype == "entity":
                conn.execute(
                    "INSERT OR REPLACE INTO graph_entities "
                    "(entity_id, name, type, domain, confidence, "
                    " mention_count, first_seen, last_seen, description, "
                    " aliases, session_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.get("entity_id", uuid.uuid4().hex[:16]),
                        record["name"], record["type"], record["domain"],
                        record.get("confidence", 0.5),
                        record.get("mention_count", 1),
                        record.get("first_seen", _now_iso()),
                        record.get("last_seen", _now_iso()),
                        record.get("description", ""),
                        record.get("aliases", "[]"),
                        record.get("session_id"),
                        record.get("created_at", _now_iso()),
                    ),
                )
                imported_entities += 1
            elif rtype == "relationship":
                conn.execute(
                    "INSERT OR REPLACE INTO graph_relationships "
                    "(source_name, target_name, rel_type, confidence, "
                    " source_doc, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record["source_name"], record["target_name"],
                        record.get("rel_type", "related_to"),
                        record.get("confidence", 0.5),
                        record.get("source_doc", ""),
                        record.get("created_at", _now_iso()),
                    ),
                )
                imported_rels += 1
            elif rtype == "memory_id":
                conn.execute(
                    "INSERT OR IGNORE INTO graph_entity_memory_ids "
                    "(entity_id, memory_id) VALUES (?, ?)",
                    (record["entity_id"], record["memory_id"]),
                )
                imported_mids += 1
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        log.error(f"Import failed, rolled back: {e}")
        return {"error": str(e), "rolled_back": True}
    return {
        "imported_entities": imported_entities,
        "imported_relationships": imported_rels,
        "imported_memory_ids": imported_mids,
        "checksum_verified": stored_checksum == computed_checksum,
    }


async def graph_import(source_path: str, mode: str = "merge") -> dict:
    return await asyncio.to_thread(_import_sync, source_path, mode)


def _health_check_sync() -> dict:
    conn = graph_db.get_connection()
    integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
    integrity = integrity_row[0] if integrity_row else "unknown"
    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    orphaned = conn.execute(
        "SELECT COUNT(*) as c FROM graph_relationships r "
        "WHERE NOT EXISTS (SELECT 1 FROM graph_entities e WHERE e.name = r.source_name) "
        "OR NOT EXISTS (SELECT 1 FROM graph_entities e WHERE e.name = r.target_name)"
    ).fetchone()["c"]
    invalid = conn.execute(
        "SELECT COUNT(*) as c FROM graph_entities WHERE length(name) < 2"
    ).fetchone()["c"]
    stats = graph_db.get_stats()
    last_backup_row = conn.execute(
        "SELECT created_at FROM graph_backup_manifest "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    last_backup = last_backup_row["created_at"] if last_backup_row else None
    status = "healthy"
    if integrity != "ok":
        status = "corrupt"
    elif orphaned > 0:
        status = "degraded"
    elif invalid > 0:
        status = "degraded"
    cleanup_result = None
    if status == "degraded" and integrity == "ok":
        try:
            cleanup_result = _cleanup_orphans_sync()
            orphaned_after = conn.execute(
                "SELECT COUNT(*) as c FROM graph_relationships r "
                "WHERE NOT EXISTS (SELECT 1 FROM graph_entities e WHERE e.name = r.source_name) "
                "OR NOT EXISTS (SELECT 1 FROM graph_entities e WHERE e.name = r.target_name)"
            ).fetchone()["c"]
            invalid_after = conn.execute(
                "SELECT COUNT(*) as c FROM graph_entities WHERE length(name) < 2"
            ).fetchone()["c"]
            if orphaned_after == 0 and invalid_after == 0:
                status = "healthy"
                orphaned = 0
                invalid = 0
            else:
                status = "degraded"
                orphaned = orphaned_after
                invalid = invalid_after
            stats = graph_db.get_stats()
        except Exception as e:
            log.error("Auto-cleanup failed: %s", e)
            cleanup_result = {"error": str(e)}
    return {
        "integrity": integrity,
        "fk_violations": len(fk_rows),
        "orphaned_relationships": orphaned,
        "invalid_entities": invalid,
        "schema_version": stats["schema_version"],
        "entity_count": stats["entity_count"],
        "relationship_count": stats["relationship_count"],
        "last_backup": last_backup,
        "status": status,
        "auto_cleanup": cleanup_result,
    }


async def run_health_check() -> dict:
    return await asyncio.to_thread(_health_check_sync)


async def cleanup_orphans() -> dict:
    return await asyncio.to_thread(_cleanup_orphans_sync)


def _wal_checkpoint_sync():
    conn = graph_db.get_connection()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


async def wal_checkpoint():
    await asyncio.to_thread(_wal_checkpoint_sync)
