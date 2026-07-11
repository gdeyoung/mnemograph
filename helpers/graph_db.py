"""
SQLite operations for _graph_memory plugin.
Thread-local connections, schema management, and CRUD.
All sync operations are designed to be called via asyncio.to_thread().
"""

import sqlite3
import threading
import uuid
import importlib.util
import os
from datetime import datetime, timezone

_local = threading.local()
_write_semaphore = None


def _get_write_semaphore():
    """Lazily initialize the write semaphore (needs event loop)."""
    global _write_semaphore
    if _write_semaphore is None:
        import asyncio
        _write_semaphore = asyncio.Semaphore(2)
    return _write_semaphore


def _get_db_path() -> str:
    """Return path to plugin's own SQLite database."""
    import os
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "graph.db")


def get_connection() -> sqlite3.Connection:
    """Thread-local connection with WAL mode and hardening."""
    if not hasattr(_local, "conn") or _local.conn is None:
        path = _get_db_path()
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def close_connection():
    """Close thread-local connection."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


# ─── Schema Management ───────────────────────────────────────

def get_schema_version() -> int:
    """Return current schema version, 0 if not initialized."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(version) as v FROM graph_schema_meta"
        ).fetchone()
        return row["v"] if row and row["v"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def run_migrations():
    """Apply pending migrations from graph_migrations/ directory."""
    conn = get_connection()
    current = get_schema_version()

    import importlib
    import os

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "graph_migrations",
    )
    if not os.path.isdir(migrations_dir):
        return

    files = sorted(
        f for f in os.listdir(migrations_dir)
        if f.endswith(".py") and not f.startswith("__")
    )

    for fname in files:
        mod_name = fname[:-3]
        parts = mod_name.split("_", 1)
        try:
            ver = int(parts[0])
        except (ValueError, IndexError):
            continue
        if ver <= current:
            continue

        spec = importlib.util.spec_from_file_location(
            f"_graph_memory.migrations.{mod_name}",
            os.path.join(migrations_dir, fname),
        )
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sql = getattr(mod, "SCHEMA_SQL", "")
        rollback = getattr(mod, "ROLLBACK_SQL", "")
        checksum = getattr(mod, "CHECKSUM", "")
        desc = getattr(mod, "DESCRIPTION", "")

        if sql:
            conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO graph_schema_meta "
            "(version, checksum, description, rollback_sql) VALUES (?, ?, ?, ?)",
            (ver, checksum, desc, rollback),
        )
        conn.commit()


def ensure_schema():
    """Create tables if needed and run migrations. Safe to call repeatedly."""
    conn = get_connection()
    # Create schema_meta first so get_schema_version works
    conn.execute(
        "CREATE TABLE IF NOT EXISTS graph_schema_meta ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
        "  checksum TEXT NOT NULL,"
        "  description TEXT,"
        "  rollback_sql TEXT"
        ")"
    )
    conn.commit()
    run_migrations()


# ─── Entity CRUD ─────────────────────────────────────────────

def upsert_entity(name, etype, domain, description="", aliases=None,
                   session_id=None, confidence=0.5):
    """Insert or update an entity. Returns entity_id."""
    conn = get_connection()
    now = _now_iso()
    eid = uuid.uuid4().hex[:16]
    aliases_json = "[]"
    if aliases:
        import json
        aliases_json = json.dumps(aliases)

    cur = conn.execute(
        "SELECT entity_id, mention_count FROM graph_entities WHERE name = ?",
        (name,),
    )
    existing = cur.fetchone()
    if existing:
        conn.execute(
            "UPDATE graph_entities SET mention_count = mention_count + 1, "
            "last_seen = ?, confidence = MAX(confidence, ?), "
            "description = COALESCE(NULLIF(?, ''), description) "
            "WHERE entity_id = ?",
            (now, confidence, description, existing["entity_id"]),
        )
        conn.commit()
        return existing["entity_id"]

    conn.execute(
        "INSERT INTO graph_entities "
        "(entity_id, name, type, domain, confidence, mention_count, "
        " first_seen, last_seen, description, aliases, session_id) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (eid, name, etype, domain, confidence, now, now, description,
         aliases_json, session_id),
    )
    conn.commit()
    return eid


def get_entity_by_name(name):
    """Fetch single entity by exact name."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM graph_entities WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def get_entity_by_id(entity_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM graph_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return dict(row) if row else None


def search_entities(query, limit=10, domain=None, etype=None):
    """Search entities by name/description LIKE."""
    conn = get_connection()
    sql = (
        "SELECT * FROM graph_entities "
        "WHERE (name LIKE ? OR description LIKE ?) "
    )
    params = [f"%{query}%", f"%{query}%"]
    if domain:
        sql += "AND domain = ? "
        params.append(domain)
    if etype:
        sql += "AND type = ? "
        params.append(etype)
    sql += "ORDER BY mention_count DESC, confidence DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_top_entities(limit=10, domain=None):
    """Get top entities by confidence × mention_count."""
    conn = get_connection()
    sql = (
        "SELECT * FROM graph_entities "
        "WHERE confidence >= 0.3 "
    )
    params = []
    if domain:
        sql += "AND domain = ? "
        params.append(domain)
    sql += "ORDER BY (confidence * mention_count) DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_entity_confidence(entity_ids, factor):
    """Apply decay: confidence = MAX(0.1, confidence * factor)."""
    if not entity_ids:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" * len(entity_ids))
    conn.execute(
        f"UPDATE graph_entities SET confidence = MAX(0.1, confidence * ?) "
        f"WHERE entity_id IN ({placeholders})",
        [factor] + list(entity_ids),
    )
    conn.commit()
    return len(entity_ids)


def delete_entity(entity_id):
    """Delete entity and its relationships/memory_links."""
    conn = get_connection()
    entity = get_entity_by_id(entity_id)
    if not entity:
        return False
    conn.execute("DELETE FROM graph_entities WHERE entity_id = ?", (entity_id,))
    conn.execute(
        "DELETE FROM graph_relationships "
        "WHERE source_name = ? OR target_name = ?",
        (entity["name"], entity["name"]),
    )
    conn.execute(
        "DELETE FROM graph_entity_memory_ids WHERE entity_id = ?",
        (entity_id,),
    )
    conn.commit()
    return True


# ─── Relationship CRUD ───────────────────────────────────────

def upsert_relationship(source_name, target_name, rel_type,
                         confidence=0.5, source_doc=""):
    """Insert or update a relationship (idempotent via UNIQUE constraint)."""
    conn = get_connection()
    now = _now_iso()
    conn.execute(
        "INSERT INTO graph_relationships "
        "(source_name, target_name, rel_type, confidence, source_doc, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_name, target_name, rel_type) DO UPDATE SET "
        "confidence = MAX(confidence, excluded.confidence)",
        (source_name, target_name, rel_type, confidence, source_doc, now),
    )
    conn.commit()


def get_relationships_for_entity(name, limit=20):
    """Get all relationships where entity is source or target."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM graph_relationships "
        "WHERE source_name = ? OR target_name = ? "
        "ORDER BY confidence DESC LIMIT ?",
        (name, name, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_relationships(limit=500):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM graph_relationships ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Memory ID Linkage ───────────────────────────────────────

def link_memory_id(entity_id, memory_id):
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO graph_entity_memory_ids (entity_id, memory_id) "
        "VALUES (?, ?)",
        (entity_id, memory_id),
    )
    conn.commit()


def get_memory_ids_for_entity(entity_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT memory_id FROM graph_entity_memory_ids WHERE entity_id = ?",
        (entity_id,),
    ).fetchall()
    return [r["memory_id"] for r in rows]


# ─── Stats ───────────────────────────────────────────────────

def get_stats():
    conn = get_connection()
    entity_count = conn.execute(
        "SELECT COUNT(*) as c FROM graph_entities"
    ).fetchone()["c"]
    rel_count = conn.execute(
        "SELECT COUNT(*) as c FROM graph_relationships"
    ).fetchone()["c"]
    type_dist = {}
    rows = conn.execute(
        "SELECT type, COUNT(*) as c FROM graph_entities GROUP BY type"
    ).fetchall()
    for r in rows:
        type_dist[r["type"]] = r["c"]
    domain_dist = {}
    rows = conn.execute(
        "SELECT domain, COUNT(*) as c FROM graph_entities GROUP BY domain"
    ).fetchall()
    for r in rows:
        domain_dist[r["domain"]] = r["c"]
    return {
        "entity_count": entity_count,
        "relationship_count": rel_count,
        "type_distribution": type_dist,
        "domain_distribution": domain_dist,
        "schema_version": get_schema_version(),
    }


def get_all_entities(limit=10000):
    """Fetch all entities (for export)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM graph_entities ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
