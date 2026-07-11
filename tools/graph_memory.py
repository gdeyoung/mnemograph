"""
Graph Memory user-facing tool.
Provides search, insights, relationships, stats, export, and health check.
"""

import asyncio

from helpers.tool import Tool, Response


class GraphMemoryTool(Tool):

    async def execute(self, query="", action="search", entity_name="",
                      limit=10, domain="", export_dir="/a0/shared/backup",
                      import_path="", **kwargs):
        """
        Graph memory operations.

        Args:
            action: One of: search, insights, relationships, stats, export, import, health
            query: Search query (for search/insights)
            entity_name: Entity name (for relationships)
            limit: Max results
            domain: Filter by domain
            export_dir: Directory for export
            import_path: JSONL file path for import
        """
        from usr.plugins._graph_memory.helpers import entity_registry
        from usr.plugins._graph_memory.helpers import graph_db
        from usr.plugins._graph_memory.helpers import graph_lifecycle

        if action == "search":
            if not query:
                return Response(
                    message="Error: query is required for search.",
                    break_loop=False,
                )
            results = await entity_registry.search(
                query, limit=int(limit),
                domain=domain if domain else None,
            )
            if not results:
                return Response(
                    message=f"No entities found matching '{query}'.",
                    break_loop=False,
                )
            formatted = []
            for r in results:
                desc = r.get("description", "")
                line = (
                    f"**{r['name']}** ({r['type']}/{r['domain']}, conf={r.get('confidence', 0.5):.2f})"
                )
                if desc:
                    line += f"\n  {desc}"
                formatted.append(line)
            text = f"Found {len(results)} entities for '{query}':\n\n" + "\n\n".join(formatted)
            return Response(message=text, break_loop=False)

        elif action == "insights":
            if not query:
                return Response(
                    message="Error: query is required for insights.",
                    break_loop=False,
                )
            # Get entities + their relationships
            entities = await entity_registry.search(query, limit=int(limit))
            if not entities:
                return Response(
                    message=f"No entities found for '{query}'.",
                    break_loop=False,
                )
            lines = [f"## Insights for '{query}'"]
            for ent in entities[:5]:
                lines.append(f"\n### {ent['name']} ({ent['type']}/{ent['domain']})")
                rels = await entity_registry.get_relationships(ent["name"], limit=10)
                if rels:
                    lines.append("Relationships:")
                    for r in rels:
                        other = (r["target_name"] if r["source_name"] == ent["name"]
                                 else r["source_name"])
                        lines.append(f"  - {r['rel_type']}: {other} (conf={r.get('confidence', 0.5):.2f})")
                else:
                    lines.append("  No relationships found.")
            text = "\n".join(lines)
            return Response(message=text, break_loop=False)

        elif action == "relationships":
            if not entity_name:
                return Response(
                    message="Error: entity_name is required for relationships.",
                    break_loop=False,
                )
            rels = await entity_registry.get_relationships(entity_name, limit=int(limit))
            if not rels:
                return Response(
                    message=f"No relationships found for '{entity_name}'.",
                    break_loop=False,
                )
            formatted = []
            for r in rels:
                formatted.append(
                    f"- {r['source_name']} →[{r['rel_type']}]→ {r['target_name']} "
                    f"(conf={r.get('confidence', 0.5):.2f})"
                )
            text = f"Relationships for **{entity_name}** ({len(rels)} found):\n\n" + "\n".join(formatted)
            return Response(message=text, break_loop=False)

        elif action == "stats":
            stats = await entity_registry.get_stats()
            lines = ["## Graph Memory Statistics"]
            lines.append(f"- Entities: **{stats['entity_count']}**")
            lines.append(f"- Relationships: **{stats['relationship_count']}**")
            lines.append(f"- Schema Version: {stats['schema_version']}")
            if stats.get("type_distribution"):
                lines.append("\n### By Type")
                for t, c in sorted(stats["type_distribution"].items(), key=lambda x: -x[1]):
                    lines.append(f"  - {t}: {c}")
            if stats.get("domain_distribution"):
                lines.append("\n### By Domain")
                for d, c in sorted(stats["domain_distribution"].items(), key=lambda x: -x[1]):
                    lines.append(f"  - {d}: {c}")
            text = "\n".join(lines)
            return Response(message=text, break_loop=False)

        elif action == "export":
            result = await graph_lifecycle.graph_export(export_dir)
            if "error" in result:
                return Response(message=f"Export failed: {result['error']}", break_loop=False)
            text = (
                f"Graph exported successfully.\n"
                f"- File: `{result['filepath']}`\n"
                f"- Entities: {result['entity_count']}\n"
                f"- Relationships: {result['relationship_count']}\n"
                f"- Checksum: `{result['checksum'][:32]}...`"
            )
            return Response(message=text, break_loop=False)

        elif action == "import":
            if not import_path:
                return Response(
                    message="Error: import_path is required for import.",
                    break_loop=False,
                )
            result = await graph_lifecycle.graph_import(import_path, mode="merge")
            if "error" in result:
                return Response(message=f"Import failed: {result['error']}", break_loop=False)
            entity_registry.invalidate_cache()
            text = (
                f"Graph imported successfully.\n"
                f"- Entities: {result['imported_entities']}\n"
                f"- Relationships: {result['imported_relationships']}\n"
                f"- Memory IDs: {result['imported_memory_ids']}\n"
                f"- Checksum verified: {result['checksum_verified']}"
            )
            return Response(message=text, break_loop=False)

        elif action == "health":
            health = await graph_lifecycle.run_health_check()
            lines = ["## Graph Memory Health Check"]
            lines.append(f"- Status: **{health['status']}**")
            lines.append(f"- Integrity: {health['integrity']}")
            lines.append(f"- Orphaned relationships: {health['orphaned_relationships']}")
            lines.append(f"- Invalid entities: {health['invalid_entities']}")
            lines.append(f"- Entity count: {health['entity_count']}")
            lines.append(f"- Relationship count: {health['relationship_count']}")
            lines.append(f"- Schema version: {health['schema_version']}")
            if health.get("last_backup"):
                lines.append(f"- Last backup: {health['last_backup']}")
            cleanup = health.get("auto_cleanup")
            if cleanup:
                lines.append("")
                lines.append("### ⚡ Auto-Cleanup Executed")
                if cleanup.get("error"):
                    lines.append(f"- ⚠️ Cleanup error: {cleanup['error']}")
                else:
                    lines.append(f"- Orphans removed: {cleanup.get('orphans_removed', 0)}")
                    lines.append(f"- Invalid entities removed: {cleanup.get('invalid_entities_removed', 0)}")
                    lines.append(f"- VACUUM: {'✅' if cleanup.get('vacuumed') else '⚠️ skipped'}")
                    lines.append(f"- Pre-cleanup backup: `{cleanup.get('backup_path', 'N/A')}`")
            text = "\n".join(lines)
            return Response(message=text, break_loop=False)

        else:
            return Response(
                message=(
                    "Unknown action. Available: search, insights, relationships, "
                    "stats, export, import, health"
                ),
                break_loop=False,
            )


export = {"graph_memory": GraphMemoryTool}
