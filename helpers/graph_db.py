"""
SQLite operations for graph_memory plugin.
Thread-local connections, schema management, and CRUD.
All sync operations are designed to be called via asyncio.to_thread().
"""

import sqlite3
import threading
import uuid
import importlib.util
import os
import json
from datetime import datetime, timezone

_local = threading.local()
_write_semaphore = None


def _get_write_semaphore():
    global _write_semaphore
    if _write_semaphore is None:
        import asyncio
        _write_semaphore = asyncio.Semaphore(2)
    return _write_semaphore


def _get_db_path() -> str:
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "graph.db")


def get_connection() -> sqlite3.Connection:
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
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


# ─── Schema Management ───────────────────────────────────────

def get_schema_version() -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(version) as v FROM graph_schema_meta"
        ).fetchone()
        return row["v"] if row and row["v"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def run_migrations():
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
            f"graph_memory.migrations.{mod_name}",
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
    conn = get_connection()
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
                   session_id=None, confidence=0.5, canonical_name=None):
    """Insert or update an entity. Returns entity_id."""
    conn = get_connection()
    now = _now_iso()
    eid = uuid.uuid4().hex[:16]
    aliases_json = "[]"
    if aliases:
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

    # Compute canonical_name if not provided
    if not canonical_name:
        canonical_name = _compute_canonical_name(name)

    # Try FTS5 search for canonical_name match (dedup)
    try:
        existing_canon = conn.execute(
            "SELECT entity_id FROM graph_entities WHERE canonical_name = ? LIMIT 1",
            (canonical_name,)
        ).fetchone()
        if existing_canon:
            # Merge into existing entity
            conn.execute(
                "UPDATE graph_entities SET mention_count = mention_count + 1, "
                "last_seen = ?, confidence = MAX(confidence, ?) "
                "WHERE entity_id = ?",
                (now, confidence, existing_canon["entity_id"])
            )
            conn.commit()
            return existing_canon["entity_id"]
    except sqlite3.OperationalError:
        pass  # canonical_name column may not exist yet (pre-migration)

    conn.execute(
        "INSERT INTO graph_entities "
        "(entity_id, name, type, domain, confidence, mention_count, "
        " first_seen, last_seen, description, aliases, session_id, canonical_name) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (eid, name, etype, domain, confidence, now, now, description,
         aliases_json, session_id, canonical_name),
    )
    conn.commit()
    return eid


def _compute_canonical_name(name: str) -> str:
    """Lowercase, strip suffixes, collapse whitespace."""
    import re
    s = name.lower().strip()
    s = re.sub(r'\b(inc|corp|corporation|ltd|limited|the)\.?', '', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def get_entity_by_name(name):
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


def search_teams(query, limit=10, domain=None, etype=None):
    """Search entities — FTS5 first, LIKE fallback."""
    conn = get_connection()
    # Try FTS5
    try:
        fts_query = _build_fts_query(query)
        sql = (
            "SELECT e.* FROM graph_entities e "
            "JOIN graph_entities_fts f ON e.rowid = f.rowid "
            "WHERE graph_entities_fts MATCH ? "
        )
        params = [fts_query]
        if domain:
            sql += "AND e.domain = ? "
            params.append(domain)
        if etype:
            sql += "AND e.type = ? "
            params.append(etype)
        sql += "ORDER BY rank, e.mention_count DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        if rows:
            return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        pass
    # LIKE fallback
    return _search_like(query, limit, domain, etype)


def _build_fts_query(query: str) -> str:
    import re
    words = re.findall(r'\w+', query.lower())
    return ' OR '.join(f'{w}*' for w in words[:5])


def _search_like(query, limit=10, domain=None, etype=None):
    """Fallback LIKE search."""
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


# Keep old name as alias
search_teams = search_teams
search = search_teams


def get_top_entities(limit=10, domain=None):
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


# ─── Session State (Phase 5.1) ───────────────────────────────

def get_session_state(session_id):
    """Get extraction state for a session."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM graph_session_state WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def update_session_state(session_id, msg_index):
    """Update session extraction state."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO graph_session_state "
            "(session_id, last_extracted_msg_index, last_extraction_at, total_extractions) "
            "VALUES (?, ?, ?, "
            "  COALESCE((SELECT total_extractions FROM graph_session_state WHERE session_id = ?), 0) + 1"
            ")",
            (session_id, msg_index, _now_iso(), session_id)
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet


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
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM graph_entities ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
