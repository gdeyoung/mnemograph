# Mnemograph Improvement Plan

> Status: Draft | Author: Agent Zero (UTILITY) | Date: 2026-07-16
> Base: v1.0.0 (PR #384) | Target: v2.0.0

## Architecture Principles

1. Zero breaking changes - all schema changes via numbered migrations
2. Feature flags for every new subsystem via default_config.yaml
3. Backward-compatible recall - new methods fall back to old behavior
4. Dreaming is opt-in - disabled by default until tested
5. All new LLM calls respect existing circuit breaker + bounded queue

---

## Phase 1: Data Quality Foundation (3-4 days)
> Dependencies: None
> Risk: Medium (touches core upsert + adds schema)

### 1.1 Migration 002: FTS5 + Session Tracking + Canonical Names

New file: graph_migrations/002_search_quality.py

Adds:
- FTS5 virtual table on graph_entities (name, description) with sync triggers
- graph_session_state table (session_id, last_extracted_msg_index, last_extraction_at, total_extractions)
- canonical_name column on graph_entities + index

Rollback: drop FTS table/triggers, session_state table, canonical_name column

### 1.2 Entity Dedup - Fuzzy Matching on Upsert (Rec #1)

File: helpers/entity_registry.py (modify create_or_update_entity)

Logic:
- Compute canonical_name: lowercase, strip Inc/Corp/Ltd/The, collapse whitespace
- On upsert, check find_by_canonical(canonical, threshold=0.85)
- If match: merge (sum mention_count, add alias, max confidence)
- If no match: create new with canonical_name

New DB methods: find_by_canonical(), merge_into_entity()
Backfill script: compute canonical_name for all 861 existing entities

Config: dedup_enabled, dedup_similarity_threshold: 0.85, dedup_use_levenshtein: true

### 1.3 Confidence Decay - Periodic Trigger (Rec #2)

New file: helpers/decay_scheduler.py

- DecayScheduler class: every N extractions or 6 hours, apply confidence *= 0.95 to entities not seen in 7+ days
- Floor at 0.15
- Integration: called at top of _extraction_worker() - piggybacks on existing worker lifecycle

Config: decay_enabled, decay_factor: 0.95, decay_age_days: 7, decay_min_interval_hours: 6

### 1.4 Alias Population and Lookup (Rec #3)

File: helpers/entity_registry.py (modify get_entity, search)
File: helpers/graph_extractor.py (modify SYSTEM_PROMPT + storage)

Changes:
- Extractor: add aliases to SYSTEM_PROMPT, pass through to storage
- Registry: check aliases JSON array during get_entity and FTS5 search

Config: alias_lookup_enabled: true, alias_max_per_entity: 10

---

## Phase 2: Search and Recall Quality (2 days)
> Dependencies: Phase 1.1 (FTS5 migration)
> Risk: Low (additive with LIKE fallback)

### 2.1 FTS5-Based Search (Rec #4)

File: helpers/graph_db.py (modify search_teams)

- Try FTS5 MATCH first with prefix queries (docker* OR containers*)
- Fall back to LIKE if FTS5 unavailable or no results
- ORDER BY rank then mention_count

### 2.2 Stopword-Filtered Keyword Extraction (Rec #5)

New file: helpers/stopwords.py
Modify: extensions/python/system_prompt/_30_graph_context.py, helpers/graph_bridge.py

- STOPWORDS frozenset (~80 common English words)
- extract_keywords() filters stopwords + short words
- Replace raw query.split()[:5] in graph_bridge.py _fetch_context()

### 2.3 Optional: Semantic Search via Embeddings

New file: helpers/semantic_search.py (behind flag, opt-in)

- Sidecar table: graph_entity_embeddings (entity_id, embedding BLOB, model_name, computed_at)
- Pre-compute entity embeddings, cosine similarity on search
- Config: semantic_search_enabled: false

---

## Phase 3: Integration (3-4 days)
> Dependencies: Phase 1 (clean entity data)
> Risk: Medium (touches memory system)

### 3.1 FAISS Memory Linking (Rec #6)

File: extensions/python/monologue_end/_55_graph_extract.py (add post-extraction hook)

Logic:
- After extraction, search FAISS for memories mentioning each entity
- Store top 5 memory_ids in graph_entity_memory_ids
- New tool action: graph_memory(action="memory_links", entity_name="Docker")

### 3.2 Multi-Agent Graph Sharing (Rec #9)

New file: helpers/graph_sync.py

Workflow:
1. Each agent exports to /a0/shared/graphs/<agent>_graph.jsonl (daily)
2. Sync worker (UTILITY) merges by canonical_name, dedup relationships
3. Global graph at /a0/shared/graphs/global_graph.jsonl
4. Agents import global graph for enriched recall

Tool actions: sync_export, sync_import, sync_status
Config: multi_agent_sync_enabled: false, sync_interval_hours: 24

---

## Phase 4: The Dreaming Engine (3-4 days)
> Dependencies: Phase 1.2 (dedup), Phase 1.3 (decay)
> Risk: Medium (new subsystem, behind feature flag)

### 4.1 Nightly Consolidation Worker

New file: helpers/dreaming.py

DreamingEngine with 7 independent idempotent passes:

Pass 1 - Decay: confidence *= 0.95 for entities not seen in 7 days (floor 0.15)
Pass 2 - Dedup: fuzzy-merge entities with >0.85 similarity, merge aliases, sum mentions
Pass 3 - Prune: delete entities with confidence < 0.15 AND mention_count <= 1, cascade relationships
Pass 4 - Infer: 2-hop transitive relationships for depends_on/part_of/runs_on (conf 0.3)
Pass 5 - Strengthen: co-occurring entity pairs get confidence boost (max +0.20)
Pass 6 - Link: batch-populate graph_entity_memory_ids from FAISS
Pass 7 - Checkpoint: PRAGMA wal_checkpoint(TRUNCATE) + VACUUM

### 4.2 Trigger Mechanism

Three options (configurable):
- idle: every 50 extractions in _55_graph_extract.py (recommended)
- scheduled: cron task at 3 AM
- manual: graph_memory(action="dream") tool action

Config: dreaming_enabled: false, dreaming_trigger: idle, dreaming_idle_interval: 50
Per-pass toggles: dreaming_passes: {decay: true, dedup: true, ...}

---

## Phase 5: Optimization and Polish (2 days)
> Dependencies: Phase 1.1 (session tracking)
> Risk: Low

### 5.1 Re-Extraction Optimization (Rec #8)

File: extensions/python/monologue_end/_55_graph_extract.py

Current: extracts from last 15 messages every turn (wasteful)
New: track last_extracted_msg_index per session, only extract new messages
Expected: >70% reduction in utility model calls

New DB methods: get_session_state(), update_session_state()

### 5.2 Entity Type Validation (Rec #10)

File: helpers/entity_validator.py (add validate_entity_type)

- ENTITY_TYPE_REFERENCE dict maps known names to correct types
- Validate before storing: Docker->technology, Python->language, etc.
- Corrects LLM hallucinations (e.g., Docker as "person")

---

## Timeline and Versions

| Phase | Time | Version |
|-------|------|---------|
| 1: Data Quality | 3-4 days | v1.1.0 |
| 2: Search | 2 days | v1.1.0 |
| 3: Integration | 3-4 days | v1.2.0 |
| 5: Optimization | 2 days | v1.3.0 |
| 4: Dreaming | 3-4 days | v2.0.0 |
| Total | ~2-3 weeks | |

## Rollout Plan

1. Deploy v1.1.0 with rollout_phase: shadow
2. Switch to read_only - verify FTS5 returns better results
3. Switch to full - enable improved context injection
4. Deploy v2.0.0 with dreaming_enabled: false
5. Test dreaming manually via graph_memory(action="dream")
6. Set dreaming_trigger: idle for autonomous operation

## File Change Summary

| File | Action | Phase |
|------|--------|-------|
| graph_migrations/002_search_quality.py | NEW | 1 |
| helpers/graph_db.py | MODIFY (FTS5, session state, canonical) | 1, 2 |
| helpers/entity_registry.py | MODIFY (dedup, aliases) | 1 |
| helpers/entity_validator.py | MODIFY (type validation) | 1, 5 |
| helpers/graph_extractor.py | MODIFY (aliases, type check) | 1, 5 |
| helpers/decay_scheduler.py | NEW | 1 |
| helpers/stopwords.py | NEW | 2 |
| helpers/semantic_search.py | NEW (optional) | 2 |
| helpers/graph_sync.py | NEW | 3 |
| helpers/dreaming.py | NEW | 4 |
| helpers/graph_lifecycle.py | MODIFY (dedup in health check) | 1 |
| extensions/.../_55_graph_extract.py | MODIFY (session tracking, triggers) | 1, 4, 5 |
| extensions/.../_30_graph_context.py | MODIFY (stopword filtering) | 2 |
| tools/graph_memory.py | MODIFY (dream, sync, memory_links actions) | 3, 4 |
| default_config.yaml | MODIFY (all new config keys) | 1-5 |
| README.md | UPDATE | All |
