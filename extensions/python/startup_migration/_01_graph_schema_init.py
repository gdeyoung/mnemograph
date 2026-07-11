"""Startup migration: Initialize graph_memory schema.

Runs on agent startup to ensure graph.db tables exist.
Safe to call repeatedly (idempotent — uses CREATE TABLE IF NOT EXISTS).
"""

from helpers.extension import Extension


class _01_graph_schema_init(Extension):
    def execute(self, **kwargs):
        try:
            from usr.plugins._graph_memory.helpers.graph_db import ensure_schema
            ensure_schema()
        except Exception as e:
            import logging
            logging.getLogger("_graph_memory").warning(
                f"Schema init skipped (non-fatal): {e}"
            )
