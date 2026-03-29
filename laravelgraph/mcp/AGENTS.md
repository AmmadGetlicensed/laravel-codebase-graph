# AGENTS.md — laravelgraph/mcp/

## OVERVIEW

FastMCP server exposing 23 tools and 9 resources. All tool logic lives in `server.py`. Supporting modules handle caching, LLM summarization, and explain logic.

## STRUCTURE

```
mcp/
├── server.py        # create_server() — all @mcp.tool() and @mcp.resource() decorators
├── summarize.py     # PROVIDER_REGISTRY (18 providers) + generate_summary()
├── cache.py         # SummaryCache — mtime-invalidated JSON sidecar
├── db_cache.py      # DBContextCache — column-hash-invalidated JSON sidecar
├── query_cache.py   # QueryResultCache — TTL-based cache for live SQL results
├── explain.py       # laravelgraph_explain tool implementation
└── warm_queries.py  # Pre-warming logic for cold start
```

## KEY PATTERNS

**Tool registration:** `server.py` uses `@mcp.tool()` from FastMCP. Each tool is a plain async or sync function with a docstring that becomes the MCP tool description.

**Summary generation:** `generate_summary(symbol, source, config)` returns `(str | None, str)` — `(summary_text, error_msg)`. Never raises. Call only when `config.summary.enabled`.

**Cache invalidation:**
- `SummaryCache` — keyed by `(node_id, file_path)`, invalidated when file mtime changes.
- `DBContextCache` — keyed by `(table, connection)`, invalidated when column structure hash changes.
- `QueryResultCache` — TTL-based (default 5 min), bypassed with `bypass_cache=True`.

**Provider resolution:** `summarize.py` auto-detects cloud provider by scanning env vars in `PROVIDER_REGISTRY` order. Local providers (ollama, lmstudio, vllm) must be explicitly configured — they have no env var.

## ADDING AN MCP TOOL

1. Add `@mcp.tool()` decorated function in `server.py`.
2. Docstring = tool description shown to agents (keep it action-oriented).
3. Parameters become the tool's JSON schema — use `Optional[str] = ""` for optional args.
4. Test in `tests/integration/mcp/`.

## ADDING AN LLM PROVIDER

One dict entry in `PROVIDER_REGISTRY` in `summarize.py`:
```python
"myprovider": {
    "sdk": "openai",          # "openai" or "anthropic"
    "env_var": "MYPROV_KEY",
    "default_model": "my-model-v1",
    "base_url": "https://api.myprov.com/v1",
    "label": "My Provider",
    "local": False,
    "models": [("my-model-v1", "description")],
}
```

## AUTO-GENERATION MCP TOOLS (Plugin System)

Three tools support the plugin auto-generation lifecycle. These live in `server.py` alongside the core tools.

### `laravelgraph_request_plugin(description)`

Agents call this to generate a new plugin from a natural language description. The system:
1. Extracts graph context relevant to the description (models, routes, tables)
2. Generates plugin code via the configured LLM with a schema-aware prompt
3. Runs 4-layer validation (AST → schema → execution → LLM-judge)
4. On failure, iterates up to 3 times feeding critique back into the LLM
5. Deploys the validated plugin to `.laravelgraph/plugins/<name>.py`
6. Returns a success message with the plugin name and tool prefix, or an error with the final critique

Parameters:
- `description: str` — natural language description of what the plugin should analyse or expose
- `name: str = ""` — optional explicit plugin name (slugified from description if omitted)

### `laravelgraph_update_plugin(name, critique)`

Regenerates an existing plugin with targeted feedback. The system reads the current plugin source, injects the critique into the generation prompt, and runs the full 4-layer validation again. On success, replaces the plugin file. On failure, leaves the old file in place and returns the critique.

Parameters:
- `name: str` — plugin name (matches `PLUGIN_MANIFEST["name"]`)
- `critique: str` — specific feedback on what to change (e.g. "also return route URIs", "handle missing models gracefully")

### `laravelgraph_remove_plugin(name, reason)`

Removes a plugin and its associated data. The system:
1. Deletes `.laravelgraph/plugins/<name>.py`
2. Calls `PluginGraphDB.delete_plugin_data(name)` to remove plugin-written graph nodes
3. Calls `PluginMetaStore.delete(name)` to remove usage statistics
4. Logs the removal reason to prevent redundant future requests for the same plugin

Parameters:
- `name: str` — plugin name to remove
- `reason: str` — why the plugin is being removed (logged for audit)

## `laravelgraph_cypher` Enhancement

The `laravelgraph_cypher` tool accepts an optional `graph` parameter:

- `graph="core"` (default) — queries the core `graph.kuzu` (read-only; all 26-phase analysis data)
- `graph="plugin"` — queries `plugin_graph.kuzu` (plugin-written runtime knowledge)

This allows agents to inspect what data plugins have stored without switching tools.

## ANTI-PATTERNS

- **Do not call `generate_summary()` in a loop** without checking the cache first.
- **Do not load GraphDB at module level** — `create_server()` is called at serve time; graph may not exist yet.
- **Do not add business logic to server.py tools** — tools call helpers in `explain.py` or query `GraphDB` directly via Cypher.
- **Do not add new caches without TTL or invalidation strategy** — stale caches are a known production issue.
