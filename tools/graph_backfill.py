#!/usr/bin/env python3
"""
Graph Memory Batch Backfill Script

Processes files through the existing graph_memory extraction pipeline with:
  1. Token-aware chunking (not summarization)
  2. Layered dedup (case-insensitive + fuzzy + semantic)
  3. SQLite WAL checkpoint (not JSON)
  4. GPU-aware adaptive rate limiting
  5. Soft caps per phase
  6. Source-specific quality targets

Usage:
    python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/
    python3 graph_backfill.py --source recent_conversations --path /a0/usr/chats/ --days 30
    python3 graph_backfill.py --source older_conversations --path /a0/usr/chats/ --days 30 --older
    python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/ --limit 5
    python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/ --resume
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# ─── Path Setup ──────────────────────────────────────────────
# Plugin imports use `usr.plugins._graph_memory...` which requires
# /a0 on sys.path since `usr` lives at /a0/usr.
_A0_ROOT = "/a0"
if _A0_ROOT not in sys.path:
    sys.path.insert(0, _A0_ROOT)

from usr.plugins._graph_memory.helpers import graph_db
from usr.plugins._graph_memory.helpers import entity_registry
from usr.plugins._graph_memory.helpers.graph_extractor import (
    extract_and_store,
    SYSTEM_PROMPT,
    _build_message,
    _safe_parse_json,
)
from usr.plugins._graph_memory.helpers.entity_validator import (
    normalize_name,
    validate_entity_name,
    detect_pii,
)

# ─── Constants (CC-Mandated Requirements 5 & 6) ──────────────

SOFT_CAPS: dict[str, int] = {
    "knowledge_base": 3000,
    "recent_conversations": 2000,
    "older_conversations": 1000,
}

HARD_STOP: int = 7500

QUALITY_TARGETS: dict[str, dict[str, float]] = {
    "knowledge_base": {"max_rejection_rate": 0.20, "min_confidence": 0.3},
    "recent_conversations": {"max_rejection_rate": 0.30, "min_confidence": 0.3},
    "older_conversations": {"max_rejection_rate": 0.50, "min_confidence": 0.4},
}

# CC-mandated throttling (VRAM exhaustion prevention on 16GB GPU)
INTER_FILE_SLEEP_S = 5          # Sleep between files to let GPU breathe
BATCH_SIZE = 5                  # Process N files, then pause
BATCH_PAUSE_S = 30              # Pause between batches
CIRCUIT_BREAKER_THRESHOLD = 3   # Consecutive failures before halting
RETRY_BACKOFF_S = [5, 10, 20]   # Exponential backoff for retries (Fix 3)
MAX_CHUNKS_PER_FILE = 50        # Skip files with more chunks (increased from 10 for conversations)

# LLM endpoints for entity extraction
# Primary: mediaserver (Gemma4-26B-QAT), Fallback: spark1 (122B DFlash)
LLM_ENDPOINTS: list[dict[str, str]] = [
    {"url": "http://192.168.1.250:11435", "model": "Gemma4-26B-QAT"},
]

LOG_FILE = "/a0/usr/workdir/logs/graph_backfill.log"
BACKUP_DIR = "/a0/shared/backup"

# Token approximation: 1 token ≈ 0.75 words (conservative OpenAI estimate)
WORDS_PER_TOKEN = 0.75

# ─── Logging ─────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = logging.getLogger("graph_backfill")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("[backfill] %(message)s"))
logger.addHandler(_console_handler)


# ═══════════════════════════════════════════════════════════
# CC Requirement 1: Token-Aware Chunking
# ═══════════════════════════════════════════════════════════

def chunk_text(text: str, max_tokens: int = 2000, overlap: int = 200) -> list[str]:
    """Split text into overlapping token chunks.

    Uses word-count approximation (~1 token = 0.75 words) since tiktoken
    is not installed. Each chunk overlaps the previous by 'overlap' tokens.

    Args:
        text: Input text to chunk.
        max_tokens: Maximum tokens per chunk.
        overlap: Overlap in tokens between consecutive chunks.

    Returns:       List of text chunks.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    total_words = len(words)
    words_per_chunk = int(max_tokens * WORDS_PER_TOKEN)
    overlap_words = int(overlap * WORDS_PER_TOKEN)

    if total_words <= words_per_chunk:
        return [text.strip()]

    step = max(1, words_per_chunk - overlap_words)
    chunks: list[str] = []

    pos = 0
    while pos < total_words:
        end = min(pos + words_per_chunk, total_words)
        chunk = " ".join(words[pos:end]).strip()
        if chunk:
            chunks.append(chunk)
        if end >= total_words:
            break
        pos += step

    return chunks


# ═══════════════════════════════════════════════════════════
# CC Requirement 2: Layered Deduplication
# ═══════════════════════════════════════════════════════════

class EntityDeduplicator:
    """Three-layer entity deduplication.

    Layer 1: Case-insensitive exact match.
    Layer 2: Fuzzy string similarity (difflib >= 0.90).
    Layer 3: Semantic similarity via TF-IDF cosine (>= 0.90).

    Note: rapidfuzz is not installed; difflib.SequenceMatcher is used
    as the spec-allowed fallback. Layer 3 uses TF-IDF cosine similarity
    instead of embeddings because no embedding endpoint is configured
    in the graph_memory plugin or LLM backends.
    """

    def __init__(self):
        self._tfidf_cache: dict[str, Counter] = {}

    async def deduplicate(
        self, entity_name: str, existing_names: list[str]
    ) -> str | None:
        """Returns canonical name if duplicate found, None if new entity."""
        if not entity_name or not existing_names:
            return None

        normed = entity_name.strip()

        # Layer 1: Case-insensitive exact match
        for name in existing_names:
            if normed.lower() == name.lower():
                return name

        # Layer 2: Fuzzy string match (difflib, threshold 0.90)
        for name in existing_names:
            ratio = SequenceMatcher(None, normed.lower(), name.lower()).ratio()
            if ratio >= 0.90:
                return name

        # Layer 3: Semantic similarity via TF-IDF cosine (threshold 0.90)
        query_vec = self._tfidf_vector(normed)
        for name in existing_names:
            existing_vec = self._tfidf_vector_cached(name)
            sim = self._cosine_similarity(query_vec, existing_vec)
            if sim >= 0.90:
                return name

        return None

    def _tfidf_vector(self, text: str) -> Counter:
        """Build character-bigram frequency vector (lightweight TF-IDF)."""
        text_lower = text.lower().strip()
        if len(text_lower) < 2:
            return Counter({text_lower: 1})
        bigrams = [
            text_lower[i : i + 2]
            for i in range(len(text_lower) - 1)
        ]
        return Counter(bigrams)

    def _tfidf_vector_cached(self, text: str) -> Counter:
        """Get TF-IDF vector with caching for existing names."""
        if text not in self._tfidf_cache:
            self._tfidf_cache[text] = self._tfidf_vector(text)
        return self._tfidf_cache[text]

    @staticmethod
    def _cosine_similarity(vec_a: Counter, vec_b: Counter) -> float:
        """Compute cosine similarity between two Counter vectors."""
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(vec_a[k] * vec_b[k] for k in vec_a if k in vec_b)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    async def run_dedup_pass(self) -> dict:
        """Scan all entities and merge near-duplicates.

        Post-processing pass: after extract_and_store() runs, scan all
        entities for duplicates that slipped through exact-name matching.
        Merges by keeping the higher-mention entity as canonical and adding
        the duplicate's name as an alias.

        Returns merge statistics.
        """
        conn = graph_db.get_connection()
        rows = conn.execute(
            "SELECT entity_id, name, mention_count, confidence "
            "FROM graph_entities ORDER BY mention_count DESC, confidence DESC"
        ).fetchall()

        entities = [dict(r) for r in rows]
        if len(entities) < 2:
            return {"merged": 0, "checked": len(entities)}

        canonical_names: list[str] = []
        merges = 0

        for ent in entities:
            canonical = await self.deduplicate(ent["name"], canonical_names)
            if canonical:
                # Merge: update relationships, add alias, delete duplicate
                self._merge_entity(conn, canonical, ent)
                merges += 1
                logger.debug(f"Dedup merge: '{ent['name']}' -> '{canonical}'")
            else:
                canonical_names.append(ent["name"])

        conn.commit()
        entity_registry.invalidate_cache()
        return {"merged": merges, "checked": len(entities)}

    @staticmethod
    def _merge_entity(conn: sqlite3.Connection, canonical: str, duplicate: dict):
        """Merge duplicate entity into canonical (in-place SQL ops)."""
        # 1. Add alias to canonical entity
        canon_row = conn.execute(
            "SELECT aliases FROM graph_entities WHERE name = ?", (canonical,)
        ).fetchone()
        aliases = []
        if canon_row and canon_row["aliases"]:
            try:
                aliases = json.loads(canon_row["aliases"])
            except json.JSONDecodeError:
                aliases = []
        if duplicate["name"] not in aliases:
            aliases.append(duplicate["name"])
        conn.execute(
            "UPDATE graph_entities SET aliases = ? WHERE name = ?",
            (json.dumps(aliases), canonical),
        )

        # 2. Transfer mention_count to canonical
        conn.execute(
            "UPDATE graph_entities SET mention_count = mention_count + ? "
            "WHERE name = ?",
            (duplicate["mention_count"], canonical),
        )

        # 3. Update relationships pointing to duplicate
        conn.execute(
            "UPDATE OR IGNORE graph_relationships SET source_name = ? WHERE source_name = ?",
            (canonical, duplicate["name"]),
        )
        conn.execute(
            "UPDATE OR IGNORE graph_relationships SET target_name = ? WHERE target_name = ?",
            (canonical, duplicate["name"]),
        )

        # 4. Delete duplicate entity
        conn.execute(
            "DELETE FROM graph_entity_memory_ids WHERE entity_id = ?",
            (duplicate["entity_id"],),
        )
        conn.execute(
            "DELETE FROM graph_entities WHERE entity_id = ?",
            (duplicate["entity_id"],),
        )


# ═══════════════════════════════════════════════════════════
# Stub Agent for Standalone LLM Calls
# ═══════════════════════════════════════════════════════════

class StubAgent:
    """Minimal agent stub for standalone extract_and_store() calls.

    Provides call_utility_model() that extract_and_store() expects,
    backed by direct HTTP calls to an OpenAI-compatible LLM endpoint.
    """

    def __init__(self, endpoints: list[dict[str, str]] | None = None):
        self.endpoints = endpoints or LLM_ENDPOINTS
        self._endpoint_idx = 0
        self._call_count = 0

    async def call_utility_model(
        self,
        system: str,
        message: str,
        background: bool = False,
        **kwargs,
    ) -> str:
        """Call LLM endpoint with automatic failover.

        Uses requests (sync) wrapped in asyncio.to_thread for async safety.
        aiohttp/httpx are not installed in this container.
        """
        import asyncio
        import requests as sync_requests

        def _do_call(url: str, payload: dict) -> str:
            """Sync HTTP call — runs in thread pool via to_thread."""
            resp = sync_requests.post(url, json=payload, timeout=120)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        last_error = None
        for attempt in range(len(self.endpoints)):
            endpoint = self.endpoints[
                (self._endpoint_idx + attempt) % len(self.endpoints)
            ]
            url = f"{endpoint['url']}/v1/chat/completions"
            payload = {
                "model": endpoint["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                "temperature": 0.3,
                "max_tokens": 4096,
            }
            try:
                content = await asyncio.to_thread(_do_call, url, payload)
                self._call_count += 1
                self._endpoint_idx = (
                    self._endpoint_idx + attempt
                ) % len(self.endpoints)
                return content
            except Exception as e:
                last_error = e
                logger.warning(
                    f"LLM endpoint {endpoint['url']} failed: "
                    f"{type(e).__name__}: {str(e)[:100]}"
                )
                continue

        raise RuntimeError(
            f"All LLM endpoints failed. Last error: {last_error}"
        )

    def get_utility_model(self):
        """Required by extension system, returns None for stub."""
        return None


# ═══════════════════════════════════════════════════════════
# CC Requirement 4: GPU-Aware Adaptive Rate Limiting
# ═══════════════════════════════════════════════════════════

class AdaptiveRateLimiter:
    """Bounded concurrency limiter for GPU-aware processing.

    Uses asyncio.Semaphore instead of fixed sleeps. Processes up to
    max_concurrent files simultaneously, releasing GPU pressure naturally.
    """

    def __init__(self, max_concurrent: int = 1):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self._active = 0

    async def acquire(self):
        await self.semaphore.acquire()
        self._active += 1

    def release(self):
        self._active -= 1
        self.semaphore.release()

    @property
    def active_count(self) -> int:
        return self._active


# ═══════════════════════════════════════════════════════════
# CC Requirement 3: SQLite WAL Checkpoint
# ═══════════════════════════════════════════════════════════

class CheckpointDB:
    """SQLite checkpoint table for resume capability.

    Stores in the same graph.db (WAL mode, separate table).
    Tracks per-file processing status for resume support.
    """

    TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS graph_backfill_checkpoint (
        source TEXT NOT NULL,
        file_path TEXT NOT NULL,
        status TEXT NOT NULL,
        entities_extracted INTEGER DEFAULT 0,
        entities_rejected INTEGER DEFAULT 0,
        relationships_extracted INTEGER DEFAULT 0,
        error_message TEXT,
        processed_at TEXT,
        PRIMARY KEY (source, file_path)
    )
    """

    def __init__(self):
        self._conn = graph_db.get_connection()

    def ensure_table(self):
        """Create checkpoint table if not exists."""
        self._conn.execute(self.TABLE_SQL)
        self._conn.commit()

    def is_processed(self, source: str, file_path: str) -> bool:
        """Check if a file was already successfully processed."""
        row = self._conn.execute(
            "SELECT status FROM graph_backfill_checkpoint "
            "WHERE source = ? AND file_path = ?",
            (source, file_path),
        ).fetchone()
        return row is not None and row["status"] == "processed"

    def mark_processed(
        self,
        source: str,
        file_path: str,
        entities: int,
        rejected: int,
        relationships: int,
    ):
        """Record successful file processing."""
        self._conn.execute(
            "INSERT OR REPLACE INTO graph_backfill_checkpoint "
            "(source, file_path, status, entities_extracted, "
            " entities_rejected, relationships_extracted, processed_at) "
            "VALUES (?, ?, 'processed', ?, ?, ?, ?)",
            (
                source,
                file_path,
                entities,
                rejected,
                relationships,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def mark_failed(
        self, source: str, file_path: str, error: str
    ):
        """Record failed file processing."""
        self._conn.execute(
            "INSERT OR REPLACE INTO graph_backfill_checkpoint "
            "(source, file_path, status, error_message, processed_at) "
            "VALUES (?, ?, 'failed', ?, ?)",
            (
                source,
                file_path,
                error[:500],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def get_processed_count(self, source: str) -> int:
        """Count processed files for a source."""
        row = self._conn.execute(
            "SELECT COUNT(*) as c FROM graph_backfill_checkpoint "
            "WHERE source = ? AND status = 'processed'",
            (source,),
        ).fetchone()
        return row["c"] if row else 0

    def get_failed_count(self, source: str) -> int:
        """Count failed files for a source."""
        row = self._conn.execute(
            "SELECT COUNT(*) as c FROM graph_backfill_checkpoint "
            "WHERE source = ? AND status = 'failed'",
            (source,),
        ).fetchone()
        return row["c"] if row else 0

    def get_phase_stats(self, source: str) -> dict:
        """Get aggregated stats for a source phase."""
        row = self._conn.execute(
            "SELECT "
            "  COALESCE(SUM(entities_extracted), 0) as entities, "
            "  COALESCE(SUM(entities_rejected), 0) as rejected, "
            "  COALESCE(SUM(relationships_extracted), 0) as rels "
            "FROM graph_backfill_checkpoint WHERE source = ?",
            (source,),
        ).fetchone()
        return {
            "entities": row["entities"] if row else 0,
            "rejected": row["rejected"] if row else 0,
            "relationships": row["rels"] if row else 0,
        }


# ═══════════════════════════════════════════════════════════
# File Discovery
# ═══════════════════════════════════════════════════════════

def discover_kb_files(path: str, limit: int = 0) -> list[str]:
    """Discover knowledge base markdown files."""
    root = Path(path)
    if not root.is_dir():
        logger.error(f"Path does not exist: {path}")
        return []

    files = sorted(root.rglob("*.md"))
    if limit > 0:
        files = files[:limit]
    return [str(f) for f in files]


def discover_chat_sessions(
    path: str, days: int, older: bool = False, limit: int = 0
) -> list[str]:
    """Discover chat session directories by modification time.

    Args:
        path: Base chats directory.
        days: Number of days for the time window.
        older: If True, select sessions OLDER than 'days' ago.
               If False, select sessions from last 'days' days.
        limit: Max sessions to return (0 = no limit).
    """
    root = Path(path)
    if not root.is_dir():
        logger.error(f"Path does not exist: {path}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()

    sessions: list[tuple[float, str]] = []

    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        # Skip non-session dirs
        if entry.name.startswith(".") or entry.name == "_archive":
            continue

        mtime = entry.stat().st_mtime

        if older:
            if mtime < cutoff_ts:
                sessions.append((mtime, str(entry)))
        else:
            if mtime >= cutoff_ts:
                sessions.append((mtime, str(entry)))

    # Sort: recent first for non-older, oldest first for older
    sessions.sort(key=lambda x: x[0], reverse=not older)

    result = [s[1] for s in sessions]
    if limit > 0:
        result = result[:limit]
    return result


def read_session_text(session_dir: str, max_messages: int = 0) -> str:
    """Read and concatenate message files from a chat session.

    Args:
        session_dir: Path to the chat session directory.
        max_messages: If > 0, only read the last N message files (by numeric order).
    """
    session_path = Path(session_dir)
    messages_dir = session_path / "messages"

    # Try messages/ subdir first, then session root
    search_dirs = [messages_dir, session_path] if messages_dir.is_dir() else [session_path]

    parts: list[str] = []
    for d in search_dirs:
        msg_files = sorted(d.glob("*.txt"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)

        # Slice to last N messages if requested
        if max_messages > 0 and len(msg_files) > max_messages:
            original_count = len(msg_files)
            msg_files = msg_files[-max_messages:]
            logger.info(
                f"  Truncated to last {max_messages} messages "
                f"(was {original_count}, now {len(msg_files)})"
            )

        for mf in msg_files:
            try:
                content = mf.read_text(encoding="utf-8", errors="replace")
                if content.strip():
                    parts.append(content.strip())
            except OSError as e:
                logger.debug(f"Could not read {mf}: {e}")

    return "\n\n".join(parts)


def read_file_text(file_path: str) -> str:
    """Read a text file with error handling."""
    try:
        return Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.error(f"Could not read {file_path}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
# JSONL Export
# ═══════════════════════════════════════════════════════════

def export_jsonl(source: str) -> str:
    """Export graph entities and relationships to JSONL backup.

    Returns the path to the exported file.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_path = os.path.join(
        BACKUP_DIR, f"graph_snapshot_{source}_{timestamp}.jsonl"
    )

    entities = graph_db.get_all_entities(limit=100000)
    relationships = graph_db.get_all_relationships(limit=100000)

    with open(export_path, "w", encoding="utf-8") as f:
        for ent in entities:
            record = {"type": "entity", "data": ent}
            f.write(json.dumps(record, default=str) + "\n")
        for rel in relationships:
            record = {"type": "relationship", "data": rel}
            f.write(json.dumps(record, default=str) + "\n")

    logger.info(
        f"Backup exported: {export_path} "
        f"({len(entities)} entities, {len(relationships)} relationships)"
    )
    return export_path


# ═══════════════════════════════════════════════════════════
# Processing Pipeline
# ═══════════════════════════════════════════════════════════

async def process_file(
    agent: StubAgent,
    source: str,
    file_path: str,
    is_session: bool,
    checkpoint: CheckpointDB,
    limiter: AdaptiveRateLimiter,
    file_idx: int,
    total_files: int,
    max_messages: int = 0,
) -> dict:
    """Process a single file through the extraction pipeline.

    Returns per-file stats dict.
    """
    rel_name = os.path.relpath(file_path, "/a0/usr") if file_path.startswith("/a0/usr") else file_path

    await limiter.acquire()
    try:
        logger.info(f"[{file_idx}/{total_files}] Processing: {rel_name}")

        # Read content
        if is_session:
            text = read_session_text(file_path, max_messages=max_messages)
            session_id = Path(file_path).name
        else:
            text = read_file_text(file_path)
            session_id = Path(file_path).stem

        if not text or len(text) < 50:
            logger.info(f"  Skipping: too short or empty")
            checkpoint.mark_processed(source, file_path, 0, 0, 0)
            return {"entities": 0, "rejected": 0, "relationships": 0, "chunks": 0}

        # Chunk text (CC Requirement 1)
        chunks = chunk_text(text, max_tokens=2000, overlap=200)
        total_chars = len(text)
        logger.info(f"  Chunks: {len(chunks)}, Total chars: {total_chars}")

        # Fix 4: Skip files with too many chunks
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            logger.warning(
                f"  Skipping: too many chunks ({len(chunks)} > {MAX_CHUNKS_PER_FILE}). "
                f"Process manually later."
            )
            checkpoint.mark_failed(
                source, file_path,
                f"skipped: too many chunks ({len(chunks)})"
            )
            return {
                "entities": 0, "rejected": 0, "relationships": 0,
                "chunks": len(chunks), "skipped": True,
            }

        total_entities = 0
        total_rejected = 0
        total_relationships = 0
        total_errors = 0
        total_parse_failed = 0

        # Process each chunk through extract_and_store()
        for i, chunk in enumerate(chunks):
            chunk_succeeded = False

            # Fix 3: Retry logic with exponential backoff
            for retry_idx, backoff in enumerate([0] + RETRY_BACKOFF_S):
                if retry_idx > 0:
                    logger.info(
                        f"  Chunk {i+1}/{len(chunks)} retry {retry_idx}/3 "
                        f"after {backoff}s backoff..."
                    )
                    await asyncio.sleep(backoff)

                try:
                    result = await extract_and_store(
                        agent, chunk, session_id=session_id
                    )

                    # Fix 6: Check for explicit error from extractor
                    if result.get("error"):
                        total_errors += 1
                        logger.warning(
                            f"  Chunk {i+1}/{len(chunks)} extraction error: "
                            f"{result['error'][:150]}"
                        )
                        continue  # retry

                    # Fix 2: Log parse failures with raw response preview
                    if result.get("parse_failed"):
                        total_parse_failed += 1
                        logger.warning(
                            f"  Chunk {i+1}/{len(chunks)} JSON parse failed "
                            f"— LLM returned unparseable response"
                        )
                        continue  # retry

                    ent_count = result.get("entities", 0)
                    rej_count = result.get("rejected", 0)
                    rel_count = result.get("relationships", 0)
                    total_entities += ent_count
                    total_rejected += rej_count
                    total_relationships += rel_count

                    # Fix 2: Verbose logging per chunk
                    if ent_count == 0 and rej_count == 0:
                        logger.warning(
                            f"  Chunk {i+1}/{len(chunks)} returned 0 entities, "
                            f"0 rejections — possible silent failure"
                        )
                    else:
                        logger.debug(
                            f"  Chunk {i+1}/{len(chunks)}: "
                            f"{ent_count} entities, {rej_count} rejected"
                        )

                    chunk_succeeded = True
                    break  # success, no more retries

                except Exception as e:
                    total_errors += 1
                    logger.warning(
                        f"  Chunk {i+1}/{len(chunks)} failed (attempt {retry_idx+1}): "
                        f"{type(e).__name__}: {str(e)[:100]}"
                    )
                    continue  # retry

            if not chunk_succeeded:
                logger.error(
                    f"  Chunk {i+1}/{len(chunks)} FAILED after all retries"
                )

        logger.info(
            f"  Entities: {total_entities}, Rejected: {total_rejected}, "
            f"Relationships: {total_relationships}"
        )
        if total_errors > 0 or total_parse_failed > 0:
            logger.warning(
                f"  Extraction errors: {total_errors}, "
                f"Parse failures: {total_parse_failed}"
            )

        # Fix 1: Treat 0-entity + 0-rejection results as failures
        if total_entities == 0 and total_rejected == 0:
            error_msg = (
                f"0 entities extracted with 0 rejections from {len(chunks)} chunks "
                f"(errors={total_errors}, parse_failed={total_parse_failed})"
            )
            logger.error(f"  SILENT FAILURE: {error_msg}")
            checkpoint.mark_failed(source, file_path, error_msg)
            return {
                "entities": 0, "rejected": 0, "relationships": 0,
                "chunks": len(chunks), "error": error_msg,
            }

        checkpoint.mark_processed(
            source, file_path, total_entities, total_rejected, total_relationships
        )

        return {
            "entities": total_entities,
            "rejected": total_rejected,
            "relationships": total_relationships,
            "chunks": len(chunks),
        }

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:200]}"
        logger.error(f"  FAILED: {error_msg}")
        checkpoint.mark_failed(source, file_path, error_msg)
        return {"entities": 0, "rejected": 0, "relationships": 0, "chunks": 0, "error": error_msg}

    finally:
        limiter.release()



async def _check_endpoint_health() -> bool:
    """Quick health check of the primary LLM endpoint. Returns True if healthy."""
    import requests as sync_requests
    import asyncio
    primary = LLM_ENDPOINTS[0] if LLM_ENDPOINTS else None
    if not primary:
        return False
    def _check():
        try:
            r = sync_requests.get(
                f"{primary['url']}/v1/models",
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False
    return await asyncio.to_thread(_check)


async def run_backfill(args: argparse.Namespace) -> int:
    """Main backfill orchestration.

    Returns exit code (0 = success, 1 = error).
    """
    start_time = time.time()
    source = args.source

    logger.info(f"=== Graph Backfill Starting ===")
    logger.info(f"Source: {source}")
    logger.info(f"Path: {args.path}")

    # Initialize DB schema + checkpoint table
    graph_db.ensure_schema()
    checkpoint = CheckpointDB()
    checkpoint.ensure_table()

    # ── Discover files ──
    is_session = source in ("recent_conversations", "older_conversations")

    if is_session:
        files = discover_chat_sessions(
            args.path, args.days, older=args.older, limit=args.limit
        )
        logger.info(f"Mode: conversations ({'older' if args.older else 'recent'} {args.days}d)")
    else:
        files = discover_kb_files(args.path, limit=args.limit)
        logger.info(f"Mode: knowledge_base")

    total_files = len(files)
    logger.info(f"Discovered: {total_files} files")

    if total_files == 0:
        logger.info("No files to process. Exiting.")
        return 0

    # ── Resume: filter already-processed files ──
    skipped = 0
    if args.resume:
        original = files
        files = [f for f in files if not checkpoint.is_processed(source, f)]
        skipped = len(original) - len(files)
        logger.info(f"Resume: skipping {skipped} already-processed files")

    if not files:
        logger.info("All files already processed. Nothing to do.")
        _print_summary(source, 0, 0, skipped, checkpoint, start_time)
        return 0

    # ── Initialize components ──
    agent = StubAgent()
    dedup = EntityDeduplicator()
    limiter = AdaptiveRateLimiter(max_concurrent=1)

    # ── Process files with bounded concurrency ──
    processed = 0
    failed = 0
    consecutive_failures = 0

    for i, file_path in enumerate(files, 1):
        # CC Requirement 5: Check soft cap
        current_stats = graph_db.get_stats()
        entity_count = current_stats["entity_count"]

        if entity_count >= SOFT_CAPS.get(source, 3000):
            logger.warning(
                f"Soft cap reached for {source}: "
                f"{entity_count} >= {SOFT_CAPS[source]}. Stopping."
            )
            break

        if entity_count >= HARD_STOP:
            logger.error(
                f"HARD STOP reached: {entity_count} >= {HARD_STOP}. "
                f"Emergency stop."
            )
            break

        # Process file
        result = await process_file(
            agent, source, file_path, is_session,
            checkpoint, limiter, i, len(files),
            max_messages=args.max_messages,
        )

        if result.get("error"):
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                logger.error(
                    f"CIRCUIT BREAKER: {consecutive_failures} consecutive failures. "
                    f"Halting backfill to protect GPU."
                )
                break
        else:
            processed += 1
            consecutive_failures = 0

        # Progress output
        active = limiter.active_count
        logger.info(
            f"  Progress: {i}/{len(files)} "
            f"({100*i//len(files)}%) | "
            f"Active: {active} concurrent | "
            f"Total entities: {entity_count}"
        )

        # CC-mandated throttling: inter-file sleep and batch pause
        if i < len(files):  # No sleep after last file
            # Batch pause every BATCH_SIZE files
            if i % BATCH_SIZE == 0:
                logger.info(f"  Batch pause: {BATCH_PAUSE_S}s (GPU cooldown after {BATCH_SIZE} files)...")
                await asyncio.sleep(BATCH_PAUSE_S)
            else:
                logger.info(f"  Sleeping {INTER_FILE_SLEEP_S}s (GPU throttle)...")
                await asyncio.sleep(INTER_FILE_SLEEP_S)

            # Health check before next file
            if not await _check_endpoint_health():
                logger.warning("Endpoint unhealthy before next file. Waiting 30s...")
                await asyncio.sleep(30)
                if not await _check_endpoint_health():
                    logger.error("Endpoint still unhealthy. Halting backfill.")
                    break

    # ── Post-processing: Run dedup pass ──
    logger.info("Running deduplication pass...")
    dedup_result = await dedup.run_dedup_pass()
    logger.info(
        f"Dedup: checked {dedup_result['checked']}, "
        f"merged {dedup_result['merged']}"
    )

    # ── Export backup ──
    export_path = export_jsonl(source)

    # ── Print summary ──
    _print_summary(source, processed, failed, skipped, checkpoint, start_time)

    # ── Quality check ──
    phase_stats = checkpoint.get_phase_stats(source)
    total_extracted = phase_stats["entities"]
    total_rejected = phase_stats["rejected"]
    if total_extracted + total_rejected > 0:
        rejection_rate = total_rejected / (total_extracted + total_rejected)
        target = QUALITY_TARGETS.get(source, {})
        max_rate = target.get("max_rejection_rate", 0.40)
        if rejection_rate > max_rate:
            logger.warning(
                f"Quality alert: rejection rate {rejection_rate:.1%} "
                f"exceeds target {max_rate:.0%} for {source}"
            )

    return 0


def _print_summary(
    source: str,
    processed: int,
    failed: int,
    skipped: int,
    checkpoint: CheckpointDB,
    start_time: float,
):
    """Print final summary report."""
    duration = time.time() - start_time
    mins = int(duration // 60)
    secs = int(duration % 60)

    stats = graph_db.get_stats()
    phase = checkpoint.get_phase_stats(source)

    # DB size
    db_path = graph_db._get_db_path()
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    db_size_kb = db_size // 1024

    # Domain distribution
    domains = stats.get("domain_distribution", {})
    domain_str = ", ".join(
        f"{k}={v}" for k, v in sorted(domains.items(), key=lambda x: -x[1])
    )

    # Type distribution
    types = stats.get("type_distribution", {})
    type_str = ", ".join(
        f"{k}={v}" for k, v in sorted(types.items(), key=lambda x: -x[1])
    )

    total_processed = checkpoint.get_processed_count(source)
    total_failed = checkpoint.get_failed_count(source)

    print()
    print("=== Graph Backfill Summary ===")
    print(f"Source: {source}")
    print(
        f"Files: {total_processed} processed, {total_failed} failed, "
        f"{skipped} skipped (already in checkpoint)"
    )
    print(
        f"Entities: {phase['entities']} extracted, "
        f"{phase['rejected']} rejected"
    )
    if phase["entities"] + phase["rejected"] > 0:
        rate = phase["rejected"] / (phase["entities"] + phase["rejected"]) * 100
        print(f"  (rejection rate: {rate:.1f}%)")
    print(f"Relationships: {phase['relationships']} stored")
    print(f"Domains: {domain_str}")
    print(f"Types: {type_str}")
    print(f"Duration: {mins}m {secs}s")
    print(f"DB size: {db_size_kb}KB")
    print(f"Total entities in graph: {stats['entity_count']}")
    print(f"Log: {LOG_FILE}")
    print("================================")

    logger.info(f"Backfill complete: {source} ({mins}m {secs}s)")


# ═══════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Graph Memory Batch Backfill Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Process knowledge base files
  python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/

  # Process recent conversations (last 30 days)
  python3 graph_backfill.py --source recent_conversations --path /a0/usr/chats/ --days 30

  # Process older conversations
  python3 graph_backfill.py --source older_conversations --path /a0/usr/chats/ --days 30 --older

  # Pilot mode (5 files only)
  python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/ --limit 5

  # Resume from checkpoint
  python3 graph_backfill.py --source knowledge_base --path /a0/usr/knowledge/custom/ --resume
""",
    )

    parser.add_argument(
        "--source",
        required=True,
        choices=["knowledge_base", "recent_conversations", "older_conversations"],
        help="Data source phase.",
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Base path to process (KB dir or chats dir).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days window for conversation filtering (default: 30).",
    )
    parser.add_argument(
        "--older",
        action="store_true",
        help="Select conversations OLDER than --days (for Phase C).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max files to process (0 = no limit, useful for pilot runs).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip already-processed files from checkpoint.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="For conversations: process only last N messages (0 = all).",
    )

    args = parser.parse_args()

    try:
        exit_code = asyncio.run(run_backfill(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
