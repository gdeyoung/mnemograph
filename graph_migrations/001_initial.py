"""Migration 001: Initial schema for _graph_memory plugin."""

VERSION = 1
DESCRIPTION = "Initial graph_memory schema — entities, relationships, memory_ids, schema_meta, backup_manifest"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_entities (
    entity_id     TEXT PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    type          TEXT NOT NULL,
    domain        TEXT NOT NULL,
    confidence    REAL DEFAULT 0.5,
    mention_count INTEGER DEFAULT 1,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    description   TEXT,
    aliases       TEXT,
    session_id    TEXT,
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_entity_name ON graph_entities(name);
CREATE INDEX IF NOT EXISTS idx_entity_domain ON graph_entities(domain);
CREATE INDEX IF NOT EXISTS idx_entity_type ON graph_entities(type);
CREATE INDEX IF NOT EXISTS idx_entity_session ON graph_entities(session_id);

CREATE TABLE IF NOT EXISTS graph_relationships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    target_name TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    confidence  REAL DEFAULT 0.5,
    source_doc  TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(source_name, target_name, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON graph_relationships(source_name);
CREATE INDEX IF NOT EXISTS idx_rel_target ON graph_relationships(target_name);
CREATE INDEX IF NOT EXISTS idx_rel_type ON graph_relationships(rel_type);

CREATE TABLE IF NOT EXISTS graph_entity_memory_ids (
    entity_id   TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    PRIMARY KEY (entity_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_emid_memory ON graph_entity_memory_ids(memory_id);

CREATE TABLE IF NOT EXISTS graph_schema_meta (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    checksum        TEXT NOT NULL,
    description     TEXT,
    rollback_sql    TEXT
);

CREATE TABLE IF NOT EXISTS graph_backup_manifest (
    snapshot_id         TEXT PRIMARY KEY,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    schema_version      INTEGER,
    entity_count        INTEGER,
    relationship_count  INTEGER,
    db_checksum         TEXT,
    backup_path         TEXT,
    integrity_status    TEXT DEFAULT 'pending'
);
"""

ROLLBACK_SQL = """
DROP TABLE IF EXISTS graph_backup_manifest;
DROP TABLE IF EXISTS graph_schema_meta;
DROP TABLE IF EXISTS graph_entity_memory_ids;
DROP TABLE IF EXISTS graph_relationships;
DROP TABLE IF EXISTS graph_entities;
"""

import hashlib
CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()[:16]
