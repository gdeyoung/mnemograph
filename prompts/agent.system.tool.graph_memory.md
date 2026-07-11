# Graph Memory Tool

Search and manage the knowledge graph of named entities, relationships, and domains.

## Actions

| Action | Description | Required Args |
|--------|-------------|---------------|
| `search` | Search entities by name or description | `query` |
| `insights` | Get entities + their relationships for a topic | `query` |
| `relationships` | Get all relationships for an entity | `entity_name` |
| `stats` | Graph statistics (entity/relationship counts, type/domain distribution) | — |
| `export` | Export graph to JSONL backup | `export_dir` (default: /a0/shared/backup) |
| `import` | Import graph from JSONL backup | `import_path` |
| `health` | Run health check (integrity, orphaned relationships, schema version) | — |

## Optional Args

- `limit`: Max results (default: 10)
- `domain`: Filter by domain (work, personal, platform, research, general)

## Usage Examples

```
<tool_name>graph_memory</tool_name>
<tool_args>{"action": "search", "query": "Docker"}</tool_args>
```

```
<tool_name>graph_memory</tool_name>
<tool_args>{"action": "insights", "query": "AI infrastructure", "limit": 5}</tool_args>
```

```
<tool_name>graph_memory</tool_name>
<tool_args>{"action": "relationships", "entity_name": "Agent Zero"}</tool_args>
```

```
<tool_name>graph_memory</tool_name>
<tool_args>{"action": "stats"}</tool_args>
```

```
<tool_name>graph_memory</tool_name>
<tool_args>{"action": "health"}</tool_args>
```
