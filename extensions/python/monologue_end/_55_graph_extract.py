"""
Graph entity extraction hook (monologue_end/_55).
Bounded asyncio.Queue with dedicated worker + circuit breaker.
Session-aware re-extraction optimization + decay/dreaming triggers.
"""

import asyncio
import logging
import time
from collections import deque

from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

log = logging.getLogger("_graph_memory.extract")

_extraction_queue: asyncio.Queue | None = None
_worker_started = False
_consecutive_failures = 0
_circuit_open_until = 0.0
_extraction_count = 0


def _get_queue(maxsize: int = 50) -> asyncio.Queue:
    global _extraction_queue
    if _extraction_queue is None:
        _extraction_queue = asyncio.Queue(maxsize=maxsize)
    return _extraction_queue


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        asyncio.create_task(_extraction_worker())
        _worker_started = True


async def _extraction_worker():
    global _consecutive_failures, _circuit_open_until, _extraction_count
    queue = _get_queue()
    while True:
        task = await queue.get()
        try:
            if time.monotonic() < _circuit_open_until:
                log.warning("Graph extraction circuit breaker open, skipping")
                queue.task_done()
                continue

            await _process_extraction(task)
            _consecutive_failures = 0
            _extraction_count += 1

            # Trigger decay check periodically
            config = task.get("config", {})
            if config.get("decay_enabled", True):
                try:
                    from usr.plugins._graph_memory.helpers import decay_scheduler
                    await decay_scheduler.run_if_due(config)
                except Exception as e:
                    log.debug(f"Decay scheduler skip: {e}")

            # Trigger dreaming cycle periodically
            dream_interval = config.get("dreaming_idle_interval", 50)
            if (config.get("dreaming_enabled", False) and
                    _extraction_count % dream_interval == 0):
                try:
                    from usr.plugins._graph_memory.helpers.dreaming import run_dream_cycle
                    log.info("Triggering dreaming cycle (idle interval reached)")
                    asyncio.create_task(run_dream_cycle(config))
                except Exception as e:
                    log.debug(f"Dreaming trigger skip: {e}")

        except Exception as e:
            _consecutive_failures += 1
            log.error(f"Graph extraction failed: {e}")
            if _consecutive_failures >= 3:
                _circuit_open_until = time.monotonic() + 300
                log.error("Graph extraction circuit breaker tripped (3 failures)")
                _consecutive_failures = 0
        finally:
            queue.task_done()


async def _process_extraction(task: dict):
    agent = task.get("agent")
    conversation_text = task.get("conversation_text", "")
    session_id = task.get("session_id", "")

    if not agent or not conversation_text:
        return

    from usr.plugins._graph_memory.helpers.graph_extractor import extract_and_store
    from usr.plugins._graph_memory.helpers import graph_lifecycle

    await graph_lifecycle.run_schema_migrations()

    result = await extract_and_store(agent, conversation_text, session_id)

    if result.get("skipped"):
        return

    log.info(
        f"Graph extraction: {result.get('entities', 0)} entities, "
        f"{result.get('relationships', 0)} relationships "
        f"({result.get('rejected', 0)} rejected)"
    )


class GraphExtract(Extension):

    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return

        config = plugins.get_plugin_config("_graph_memory", self.agent)
        if not config or not config.get("extraction_enabled", True):
            return

        min_msgs = config.get("extraction_min_messages", 3)
        msgs = self.agent.history.current.messages
        if len(msgs) < min_msgs:
            return

        session_id = self.agent.context.id if self.agent.context else ""
        msg_count = len(msgs)

        # Session-aware re-extraction optimization (Rec #8)
        from usr.plugins._graph_memory.helpers import graph_db
        state = await asyncio.to_thread(graph_db.get_session_state, session_id)
        last_idx = state.get("last_extracted_msg_index", 0) if state else 0

        if config.get("session_aware_extraction", True) and last_idx >= msg_count:
            return  # Already extracted everything

        # Only extract from new messages if session-aware
        if config.get("session_aware_extraction", True) and last_idx > 0:
            new_msgs = msgs[last_idx:]
            if len(new_msgs) < 3:
                return  # Not enough new content
            recent = new_msgs[-15:]
        else:
            recent = msgs[-15:]

        lines = []
        for m in recent:
            try:
                txt = m.output_text() if hasattr(m, "output_text") else str(m)
                if txt:
                    lines.append(txt)
            except Exception:
                continue
        conversation_text = "\n".join(lines)

        if len(conversation_text) < 50:
            return

        # Update session state
        await asyncio.to_thread(graph_db.update_session_state, session_id, msg_count)

        # Enqueue extraction task
        queue_maxsize = config.get("extraction_queue_maxsize", 50)
        queue = _get_queue(queue_maxsize)

        _ensure_worker()

        try:
            queue.put_nowait({
                "agent": self.agent,
                "conversation_text": conversation_text,
                "session_id": session_id,
                "config": config,
            })
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.task_done()
                log.warning("Graph extraction queue full, dropped oldest task")
                queue.put_nowait({
                    "agent": self.agent,
                    "conversation_text": conversation_text,
                    "session_id": session_id,
                    "config": config,
                })
            except Exception:
                log.error("Failed to enqueue extraction task after dropping oldest")
