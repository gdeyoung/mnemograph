"""
Graph context injection hook (system_prompt/_30).
Regex question detection → entity registry lookup → bounded context injection.
Max 3 entities, max 10 lines.
"""

import asyncio
import logging
import re

from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

log = logging.getLogger("_graph_memory.context")

# Regex patterns for detecting questions about entities
_QUESTION_PATTERNS = [
    re.compile(r"\bwhat\b.*\b(is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"\bwho\b.*\b(is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"\bhow\b.*\b(do|does|did|to)\b", re.IGNORECASE),
    re.compile(r"\bexplain\b", re.IGNORECASE),
    re.compile(r"\btell me about\b", re.IGNORECASE),
    re.compile(r"\brelationship between\b", re.IGNORECASE),
    re.compile(r"\bcompare\b", re.IGNORECASE),
    re.compile(r"\bdifference between\b", re.IGNORECASE),
]

# Skip injection for very short or code-heavy messages
_CODE_PATTERN = re.compile(r"```|def |class |import |from ", re.MULTILINE)


def _is_question(text: str) -> bool:
    if not text or len(text) < 15:
        return False
    for pat in _QUESTION_PATTERNS:
        if pat.search(text):
            return True
    return False


def _extract_keywords(text: str, max_kw: int = 5) -> list[str]:
    """Extract potential entity keywords from text."""
    # Simple keyword extraction: capitalized words and known tech terms
    words = text.split()
    keywords = []
    seen = set()
    for w in words:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "", w)
        if not clean or len(clean) < 2:
            continue
        if clean.lower() in seen:
            continue
        # Capitalized or known pattern
        if clean[0].isupper() or clean.isupper():
            keywords.append(clean)
            seen.add(clean.lower())
        if len(keywords) >= max_kw:
            break
    return keywords


class GraphContext(Extension):

    async def execute(self, system_prompt: list[str] = [], loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return

        config = plugins.get_plugin_config("_graph_memory", self.agent)
        if not config or not config.get("context_inject_enabled", True):
            return

        rollout = config.get("rollout_phase", "full")
        if rollout in ("shadow", "read_only"):
            return

        # Get user message
        user_msg = ""
        if loop_data.user_message:
            try:
                user_msg = loop_data.user_message.output_text()
            except Exception:
                user_msg = ""
        if not user_msg:
            return

        # Skip code-heavy messages
        if _CODE_PATTERN.search(user_msg):
            return

        # Only inject for questions
        if not _is_question(user_msg):
            return

        max_entities = config.get("context_inject_max_entities", 3)
        max_lines = config.get("context_inject_max_lines", 10)

        try:
            from usr.plugins._graph_memory.helpers import entity_registry

            keywords = _extract_keywords(user_msg, max_kw=5)
            if not keywords:
                return

            # Search for entities matching keywords
            found_entities = []
            seen_ids = set()
            for kw in keywords:
                if len(found_entities) >= max_entities:
                    break
                results = await asyncio.wait_for(
                    entity_registry.search(kw, limit=max_entities),
                    timeout=0.01,  # 10ms budget
                )
                for ent in results:
                    if ent["entity_id"] not in seen_ids:
                        found_entities.append(ent)
                        seen_ids.add(ent["entity_id"])
                    if len(found_entities) >= max_entities:
                        break

            if not found_entities:
                return

            # Build bounded context block
            lines = ["## Knowledge Graph Context"]
            for ent in found_entities[:max_entities]:
                desc = ent.get("description", "")
                line = f"- **{ent['name']}** ({ent['type']}, {ent['domain']})"
                if desc:
                    line += f" — {desc[:80]}"
                lines.append(line)
                if len(lines) >= max_lines:
                    break

            context_text = "\n".join(lines[:max_lines])
            system_prompt.append(context_text)

        except asyncio.TimeoutError:
            log.debug("Graph context injection timed out")
        except Exception as e:
            log.error(f"Graph context injection error: {e}")
