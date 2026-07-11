"""
LLM-based entity and relationship extraction from conversation text.
Uses the utility model for extraction, with strict JSON output parsing.
"""

import json
import logging
import asyncio

from usr.plugins._graph_memory.helpers.entity_validator import (
    normalize_name,
    validate_entity_name,
    detect_pii,
    is_valid_entity,
    VALID_ENTITY_TYPES,
    VALID_DOMAINS,
    VALID_REL_TYPES,
)
from usr.plugins._graph_memory.helpers import entity_registry
from usr.plugins._graph_memory.helpers import graph_db

log = logging.getLogger("_graph_memory.extractor")


SYSTEM_PROMPT = """You are an entity extraction engine. Extract named entities and relationships from the conversation.

Output ONLY valid JSON with this exact structure:
{
  "entities": [
    {"name": "Docker", "type": "technology", "domain": "platform", "description": "Container platform", "confidence": 0.9}
  ],
  "relationships": [
    {"source": "Docker", "target": "Ollama", "type": "runs_on", "confidence": 0.8}
  ]
}

Rules:
- Entity types: person, organization, technology, concept, project, skill, location, tool, framework, language
- Domains: work, personal, platform, research, general
- Relationship types: uses, depends_on, runs_on, related_to, part_of, owns, built_with, alternative_to, predecessor_of, competes_with
- Only extract REAL named entities (people, products, technologies, organizations, projects, concepts)
- Do NOT extract: filenames, file paths, URLs, code snippets, environment variables, numbers, API endpoints
- Do NOT extract generic words ("the", "system", "server") unless they are a proper noun
- Confidence: 0.9 = explicitly named, 0.7 = clearly referenced, 0.5 = mentioned in passing
- Maximum 10 entities and 15 relationships per extraction"""


def _build_message(conversation_text: str) -> str:
    truncated = conversation_text[:8000]
    return f"Extract entities and relationships from this conversation.\n\nConversation:\n{truncated}"


def _safe_parse_json(raw: str) -> dict:
    """Parse JSON response with fallback handling."""
    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()

    try:
        result = json.loads(raw)
        result["_parse_ok"] = True
        return result
    except json.JSONDecodeError:
        # Try DirtyJson-style parsing for partial JSON
        try:
            from helpers.json import DirtyJson
            result = DirtyJson.parse_string(raw)
            if isinstance(result, dict):
                result["_parse_ok"] = True
                return result
        except Exception:
            pass
        return {"entities": [], "relationships": [], "_parse_ok": False}


async def extract_and_store(agent, conversation_text: str, session_id: str = "") -> dict:
    """
    Extract entities + relationships via utility model, validate, and store.
    Returns summary of what was extracted.
    """
    if not conversation_text or len(conversation_text) < 50:
        return {"entities": 0, "relationships": 0, "skipped": True}

    try:
        response = await agent.call_utility_model(
            system=SYSTEM_PROMPT,
            message=_build_message(conversation_text),
            background=True,
        )
    except Exception as e:
        log.error(f"Utility model call failed: {e}")
        return {"entities": 0, "relationships": 0, "error": str(e), "parse_failed": False}

    parsed = _safe_parse_json(response)
    parse_ok = parsed.get("_parse_ok", False)
    if not parse_ok:
        log.warning(f"JSON parse failed. Raw response (first 200 chars): {response[:200]}")
    raw_entities = parsed.get("entities", [])
    raw_rels = parsed.get("relationships", [])

    # ── Validate and store entities ──
    stored_entities = 0
    entity_name_map = {}  # name -> entity_id for relationship linking

    for ent in raw_entities:
        if not isinstance(ent, dict):
            continue
        raw_name = ent.get("name", "")
        if not raw_name:
            continue

        normed = normalize_name(raw_name)
        valid, reason = validate_entity_name(normed)
        if not valid:
            log.debug(f"Rejected entity '{normed}': {reason}")
            continue
        if detect_pii(normed):
            log.debug(f"Rejected entity '{normed}': PII detected")
            continue

        etype = ent.get("type", "concept")
        if etype not in VALID_ENTITY_TYPES:
            etype = "concept"
        domain = ent.get("domain", "general")
        if domain not in VALID_DOMAINS:
            domain = "general"
        confidence = float(ent.get("confidence", 0.5))
        confidence = max(0.1, min(1.0, confidence))

        eid = await entity_registry.create_or_update_entity(
            name=normed,
            etype=etype,
            domain=domain,
            description=ent.get("description", ""),
            session_id=session_id,
            confidence=confidence,
        )
        if eid:
            stored_entities += 1
            entity_name_map[normed] = eid

    # ── Validate and store relationships ──
    stored_rels = 0
    for rel in raw_rels:
        if not isinstance(rel, dict):
            continue
        source = normalize_name(rel.get("source", ""))
        target = normalize_name(rel.get("target", ""))
        rel_type = rel.get("type", "related_to")

        if not source or not target:
            continue
        if rel_type not in VALID_REL_TYPES:
            rel_type = "related_to"
        if detect_pii(source) or detect_pii(target):
            continue

        confidence = float(rel.get("confidence", 0.5))
        confidence = max(0.1, min(1.0, confidence))

        await asyncio.to_thread(
            graph_db.upsert_relationship,
            source, target, rel_type, confidence, session_id,
        )
        stored_rels += 1

    return {
        "entities": stored_entities,
        "relationships": stored_rels,
        "rejected": len(raw_entities) - stored_entities,
        "parse_failed": not parse_ok,
    }
