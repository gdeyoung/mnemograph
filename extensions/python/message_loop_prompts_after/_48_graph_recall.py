"""
Graph recall enrichment hook (message_loop_prompts_after/_48).
LRU cache + DB fallback, <50ms budget via asyncio.wait_for.
Runs BEFORE Memex decay rerank (_53).
"""

import asyncio
import logging

from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

log = logging.getLogger("_graph_memory.recall")


class GraphRecall(Extension):

    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return

        config = plugins.get_plugin_config("_graph_memory", self.agent)
        if not config or not config.get("recall_enabled", True):
            return

        rollout = config.get("rollout_phase", "full")
        if rollout == "shadow":
            # Shadow mode: recall runs but result is only logged
            pass

        # Only inject on first iteration and if user message is substantial
        if loop_data.iteration > 2:
            return

        user_msg = ""
        if loop_data.user_message:
            try:
                user_msg = loop_data.user_message.output_text()
            except Exception:
                user_msg = ""
        if not user_msg or len(user_msg) < 10:
            return

        max_entities = config.get("recall_max_entities", 3)
        timeout_ms = config.get("recall_timeout_ms", 50)
        timeout_sec = timeout_ms / 1000.0

        try:
            from usr.plugins._graph_memory.helpers.graph_bridge import get_bridge

            bridge = get_bridge()
            session_id = self.agent.context.id if self.agent.context else None

            payload = await asyncio.wait_for(
                bridge.get_context(user_msg, max_entities=max_entities,
                                   session_id=session_id),
                timeout=timeout_sec,
            )

            if not payload.entities:
                return

            # Build recall text for extras_temporary
            lines = []
            lines.append("## Relevant Knowledge Graph Entities")
            for ent in payload.entities:
                desc = ent.get("description", "")
                line = f"- **{ent['name']}** ({ent['type']}/{ent['domain']})"
                if desc:
                    line += f": {desc}"
                lines.append(line)

            if payload.relationships:
                lines.append("  Relationships:")
                for rel in payload.relationships[:5]:
                    lines.append(f"    - {rel}")

            recall_text = "\n".join(lines)

            # Log in shadow mode, inject in full mode
            if rollout == "shadow":
                log.info(f"Shadow recall: {len(payload.entities)} entities found")
            else:
                loop_data.extras_temporary["graph_context"] = recall_text

        except asyncio.TimeoutError:
            log.warning("Graph recall timed out (>50ms), skipping")
        except Exception as e:
            log.error(f"Graph recall error: {e}")
