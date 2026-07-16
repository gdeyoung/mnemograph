# Changelog

## v2.0.0 — 2026-07-16

### Dreaming Engine
- **7-pass consolidation engine**: decay, dedup, prune, transitive inference, co-occurrence strengthening, FAISS linking (stub), WAL checkpoint
- Triggerable via idle counter, manual `graph_memory(action="dream")`, or scheduled task
- Per-pass enable/disable toggles in config

### Data Quality (Phase 1)
- **Entity dedup**: Canonical name computation + fuzzy merge on upsert (threshold 0.85)
- **Confidence decay**: Periodic decay for entities not seen in N days (factor 0.95, floor 0.15)
- **Alias support**: Extractor now captures and stores aliases; lookup checks alias arrays
- **Entity type validation**: Known entities (Docker, Python, etc.) get type-corrected automatically

### Search Quality (Phase 2)
- **FTS5 full-text search**: Replaced LIKE with SQLite FTS5 MATCH, with automatic LIKE fallback
- **Stopword filtering**: Keyword extraction now filters ~80 common English stopwords
- **Bridge recall**: Uses filtered keywords instead of raw query.split()

### Integration (Phase 3)
- **Multi-agent graph sync**: Export/merge workflow via shared drive (`/a0/shared/graphs/`)
- **New tool actions**: `sync_export`, `sync_import`, `sync_status`
- **FAISS linking**: Schema support for `graph_entity_memory_ids` (stub — awaiting memory API integration)

### Optimization (Phase 5)
- **Session-aware re-extraction**: Tracks `last_extracted_msg_index` per session; only extracts new messages (>70% fewer LLM calls)
- **Migration 002**: FTS5 virtual table + triggers, `graph_session_state`, `canonical_name` column, embeddings sidecar

### New Files
- `graph_migrations/002_search_quality.py`
- `helpers/decay_scheduler.py`
- `helpers/stopwords.py`
- `helpers/dreaming.py`
- `helpers/graph_sync.py`

### New Tool Actions
- `dream` — Run full dreaming consolidation cycle
- `sync_export` — Export graph to shared drive
- `sync_import` — Import/merge global graph
- `sync_status` — Show sync file state

## v1.0.0 — 2026-07-11

Initial release.
