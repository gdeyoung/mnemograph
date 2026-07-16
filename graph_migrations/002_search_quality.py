"""
Migration 002: Search quality and session tracking.

Adds:
- FTS5 virtual table for full-text entity search with sync triggers
- graph_session_state table for re-extraction optimization
- canonical_name column on graph_entities for dedup support
- graph_entity_embeddings sidecar table (for optional semantic search)
"""

SCHEMA_SQL = """
-- FTS5 virtual table for full-text entity search
CREATE VIRTUAL TABLE IF NOT EXISTS graph_entities_fts
USING fts5(name, description, content='graph_entities', content_rowid='rowid');

-- Populate from existing data
INSERT INTO graph_entities_fts(rowid, name, description)
SELECT rowid, name, description FROM graph_entities;

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS graph_entities_ai AFTER INSERT ON graph_entities BEGIN
  INSERT INTO graph_entities_fts(rowid, name, description)
  VALUES (new.rowid, new.name, new.description);
END;

CREATE TRIGGER IF NOT EXISTS graph_entities_ad AFTER DELETE ON graph_entities BEGIN
  INSERT INTO graph_entities_fts(graph_entities_fts, rowid, name, description)
  VALUES('delete', old.rowid, old.name, old.description);
END;

CREATE TRIGGER IF NOT EXISTS graph_entities_au AFTER UPDATE ON graph_entities BEGIN
  INSERT INTO graph_entities_fts(graph_entities_fts, rowid, name, description)
  VALUES('delete', old.rowid, old.name, old.description);
  INSERT INTO graph_entities_fts(rowid, name, description)
  VALUES (new.rowid, new.name, new.description);
END;

-- Session extraction tracking (for re-extraction optimization)
CREATE TABLE IF NOT EXISTS graph_session_state (
  session_id TEXT PRIMARY KEY,
  last_extracted_msg_index INTEGER DEFAULT 0,
  last_extraction_at TEXT,
  total_extractions INTEGER DEFAULT 0
);

-- Entity canonical name column (for dedup)
ALTER TABLE graph_entities ADD COLUMN canonical_name TEXT;

CREATE INDEX IF NOT EXISTS idx_entities_canonical
  ON graph_entities(canonical_name);

-- Optional semantic search sidecar table
CREATE TABLE IF NOT EXISTS graph_entity_embeddings (
  entity_id TEXT PRIMARY KEY,
  embedding BLOB,
  model_name TEXT,
  computed_at TEXT
);
"""

ROLLBACK_SQL = """
DROP TRIGGER IF EXISTS graph_entities_ai;
DROP TRIGGER IF EXISTS graph_entities_ad;
DROP TRIGGER IF EXISTS graph_entities_au;
DROP TABLE IF EXISTS graph_entities_fts;
DROP TABLE IF EXISTS graph_session_state;
DROP INDEX IF EXISTS idx_entities_canonical;
-- Note: ALTER TABLE DROP COLUMN not supported in SQLite; canonical_name remains
DROP TABLE IF EXISTS graph_entity_embeddings;
"""

CHECKSUM = "m002_search_quality_fts5_session_canonical_v1"
DESCRIPTION = "FTS5 search, session state tracking, canonical names for dedup, embeddings sidecar"
