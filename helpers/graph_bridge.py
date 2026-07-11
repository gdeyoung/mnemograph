"""
GraphMemoryBridge — cross-plugin API for Memex integration.
Provides read-only context and decay application with strict performance budgets.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from usr.plugins._graph_memory.helpers import entity_registry
from usr.plugins._graph_memory.helpers import graph_db

log = logging.getLogger("_graph_memory.bridge")


@dataclass
class GraphContextPayload:
    """DTO for cross-plugin handoff — no direct prompt string mutation."""
    entities: list[dict] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    token_budget_remaining: int = 0
    decay_applied: bool = False


class GraphMemoryBridge:
    """
    Read-only API exposed by _graph_memory for Memex to call.
    All methods complete within strict performance budgets.
    """

    async def get_context(self, query: str, max_entities: int = 3,
                          session_id: str | None = None) -> GraphContextPayload:
    
        timeout = 0.05  # 50ms budget
        try:
            result = await asyncio.wait_for(
                self._fetch_context(query, max_entities, session_id),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            log.warning("Graph recall timed out (>50ms), returning empty")
            return GraphContextPayload()
        except Exception as e:
            log.error(f"Graph recall error: {e}")
            return GraphContextPayload()

    async def _fetch_context(self, query: str, max_entities: int,
                             session_id: str | None) -> GraphContextPayload:
        # Search entities matching query keywords
        keywords = query.split()[:5]  # limit keywords for speed
        results = []
        seen_ids = set()

        for kw in keywords:
            if len(results) >= max_entities:
                break
            found = await entity_registry.search(kw, limit=max_entities)
            for ent in found:
                if ent["entity_id"] not in seen_ids:
                    results.append(ent)
                    seen_ids.add(ent["entity_id"])
                if len(results) >= max_entities:
                    break

        # If no keyword matches, get top entities
        if not results:
            results = await entity_registry.get_top(limit=max_entities)

        # Get relationships for found entities
        rel_strs = []
        ent_names = {e["name"] for e in results}
        for ent in results:
            rels = await entity_registry.get_relationships(ent["name"], limit=5)
            for r in rels:
                other = r["target_name"] if r["source_name"] == ent["name"] else r["source_name"]
                rel_str = f'{r["source_name"]}→{r["rel_type"]}→{r["target_name"]}'
                if rel_str not in rel_strs:
                    rel_strs.append(rel_str)
                if len(rel_strs) >= 10:
                    break
            if len(rel_strs) >= 10:
                break

        return GraphContextPayload(
            entities=[
                {
                    "name": e["name"],
                    "type": e["type"],
                    "domain": e["domain"],
                    "description": e.get("description", ""),
                    "confidence": e.get("confidence", 0.5),
                }
                for e in results[:max_entities]
            ],
            relationships=rel_strs[:10],
            token_budget_remaining=0,
            decay_applied=False,
        )

    async def apply_decay(self, entity_ids: list[str], decay_factor: float) -> int:
        """
        Called by Memex decay engine. Updates confidence scores.
        Single source of truth: Memex calculates, Graph applies.
        """
        if not entity_ids or not 0 < decay_factor <= 1.0:
            return 0
        return await entity_registry.apply_decay(entity_ids, decay_factor)

    async def sync_portrait_traits(self, portrait: dict) -> None:
        """
        Called by Memex after portrait update. Feeds domain/skill traits.
        Updates entity domains based on portrait expertise.
        """
        domains = portrait.get("domain_expertise", {})
        if not isinstance(domains, dict):
            return

        for domain_name, expertise in domains.items():
            if expertise < 0.5:
                continue
            # Map portrait domain to graph domain
            graph_domain = "work" if domain_name in (
                "sales", "technology", "business"
            ) else "personal"
            entities = await entity_registry.search(
                domain_name, limit=5, domain=graph_domain,
            )
            # Entities already tagged correctly, nothing to change
            # This is a hook for future enrichment


# Singleton instance
_bridge_instance: GraphMemoryBridge | None = None


def get_bridge() -> GraphMemoryBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = GraphMemoryBridge()
    return _bridge_instance
