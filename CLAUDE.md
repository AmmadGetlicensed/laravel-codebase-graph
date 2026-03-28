# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run tests (coverage is required — 80% minimum enforced)
pytest tests/ --override-ini="addopts=" -v          # Run all tests
pytest tests/unit/ --override-ini="addopts=" -v     # Unit tests only
pytest tests/integration/ --override-ini="addopts=" -v  # Integration tests only
pytest tests/unit/parsing/test_php_parser.py --override-ini="addopts=" -v  # Single file

# Lint / type-check
ruff check laravelgraph/
mypy laravelgraph/

# Install as CLI tool (production)
pipx install .
pipx reinstall laravelgraph   # After any source changes — required because MCP uses the pipx binary

# Plugin management
laravelgraph plugin list .              # list all plugins with health/contribution
laravelgraph plugin suggest .           # suggest applicable domain plugins
laravelgraph plugin scaffold <name> .   # scaffold plugin from graph context
laravelgraph plugin validate <file>     # validate plugin safety
laravelgraph plugin enable <name> .     # enable disabled plugin
laravelgraph plugin disable <name> .    # disable plugin (keep file)
laravelgraph plugin delete <name> .     # permanently delete plugin + data
laravelgraph plugin prompt <name> "..." # attach system prompt to plugin

# Log management
laravelgraph logs                        # show recent logs
laravelgraph logs --level error         # filter by level
laravelgraph logs --tool <name>         # filter by MCP tool
laravelgraph logs --since 2h            # last 2 hours
laravelgraph logs tail                  # live tail (Ctrl+C to stop)
laravelgraph logs stats                 # log statistics
laravelgraph logs clear                 # clear old logs (>30 days)
laravelgraph logs clear --all           # clear all logs (with confirmation)
```

> **Important:** The MCP server (`laravelgraph serve`) is launched by Claude Code via `pipx`. Any change to source files must be followed by `pipx reinstall laravelgraph` before the changes take effect in the running MCP server.

## Architecture

LaravelGraph is a **26-phase analysis pipeline** that indexes a Laravel/PHP codebase into a local KuzuDB graph database, then exposes it via an MCP server to AI agents.

### Data flow

```
Laravel project on disk
  → Pipeline (26 phases) → .laravelgraph/graph.kuzu  (KuzuDB)
                         → .laravelgraph/summaries.json (LLM symbol summary cache)
                         → .laravelgraph/db_context.json (LLM DB context cache)
  → MCP server (FastMCP) ← Claude Code / other agents
MySQL/RDS databases
  → Phase 24 (live introspection) → graph nodes + edges
```

### Key packages

- **`laravelgraph/pipeline/`** — 26 sequential phases. Each is a single `.py` file with a `run(ctx: PipelineContext)` function. `PipelineContext` (in `orchestrator.py`) is the shared state object carrying `db`, `config`, parsed file maps, and the FQN index.
  - Phases 24–26 handle database intelligence: live DB introspection (PyMySQL), model-table linking, and DB access analysis (static, zero AI cost).

- **`laravelgraph/core/`** — `graph.py` wraps KuzuDB (CRUD + Cypher), `schema.py` defines 50+ node types and 100+ relationship types, `registry.py` manages the global `~/.laravelgraph/repos.json` index.

- **`laravelgraph/parsers/`** — PHP (tree-sitter + regex fallback), Blade templates, and Composer JSON parsing.

- **`laravelgraph/mcp/server.py`** — The FastMCP server. All 23 MCP tools and 9 resources live here as `@mcp.tool()` / `@mcp.resource()` decorated functions.

- **`laravelgraph/mcp/summarize.py`** — 18-provider LLM registry (`PROVIDER_REGISTRY`). All OpenAI-compatible providers share `_call_openai_compat()`; only Anthropic uses its native SDK. `generate_summary()` returns `(str | None, str)` — never raises.

- **`laravelgraph/mcp/cache.py`** — `SummaryCache`: file-backed JSON sidecar at `.laravelgraph/summaries.json`. Mtime-based auto-invalidation — if the source file changes, the cached summary is discarded on the next tool call.

- **`laravelgraph/mcp/db_cache.py`** — `DBContextCache`: file-backed JSON sidecar at `.laravelgraph/db_context.json`. Hash-based invalidation — if the table's column structure changes, the cached annotation is discarded. Stores table, column, and procedure annotations.

- **`laravelgraph/search/hybrid.py`** — BM25 + vector (fastembed) + fuzzy (rapidfuzz) with RRF ranking.

- **`laravelgraph/config.py`** — Pydantic config model. `SummaryConfig` uses `api_keys: dict`, `models: dict`, `base_urls: dict`. `DatabaseConnectionConfig` per-connection config. Config priority: env vars → `.laravelgraph/config.json` → `~/.laravelgraph/config.json` → defaults.

- **`laravelgraph/cli.py`** — Typer application. All CLI commands including `analyze`, `serve`, `doctor`, `configure`, `providers`, `status`, `query`, `context`, `impact`, `routes`, `models`, `events`, `schema`, `bindings`, `dead-code`, `warm`, and `db-connections` sub-commands.

### Storage layout (per project)

```
<project>/.laravelgraph/
  graph.kuzu/         KuzuDB database (directory)
  summaries.json      LLM-generated semantic summaries (mtime-invalidated)
  db_context.json     LLM-generated DB table/column/procedure annotations (hash-invalidated)
  config.json         Project-level config overrides (including databases[])
~/.laravelgraph/
  repos.json          Global registry of indexed projects
  config.json         Global config defaults (including databases[])
  logs/               Structured logs
```

### Adding a new pipeline phase

1. Create `laravelgraph/pipeline/phase_NN_name.py` with `run(ctx: PipelineContext) -> None`
2. Register it in `orchestrator.py`'s phase list
3. Use `ctx.db.upsert_node()` / `ctx.db.upsert_edge()` for graph writes

### Adding a new MCP tool

Add a `@mcp.tool()` decorated function to `mcp/server.py`. Update the server instructions string at the top of `create_server()`.

### Plugin System

```
Plugin System:
  plugins/plugin_graph.py  DualDB + PluginGraphDB (writable runtime graph)
  plugins/meta.py          PluginMetaStore (status, usage, contribution scoring)
  plugins/generator.py     Domain-anchored generation + 4-layer validation
  plugins/loader.py        MCP + pipeline plugin loading; scan_plugin_manifests; _ToolCollector
  plugins/self_improve.py  Proactive self-improvement on server startup
  logging_manager.py       Log reading, filtering, tailing utilities
```

**What plugins are:** Product-specific domain lenses over the graph. Built-in MCP tools give generic Laravel intelligence (routes, models, events, dead code). Plugins give intelligence about a specific product's domain — "what is the order lifecycle?", "how does driver assignment work?" These are questions that require knowing the actual routes, models, and events in that particular app.

**How generation works (`plugins/generator.py`):**

1. **Stage 1 — Domain anchor resolution (no LLM):** `_resolve_domain_anchors()` runs pure Python + Cypher to find which Feature nodes, Routes, EloquentModels, Events, and Jobs in the graph implement the requested domain. Uses a two-phase approach:
   - Phase A: Match against Feature nodes (phase 27 clusters) by token overlap
   - Phase B fallback: Scan Route URIs, EloquentModel names, Event names by substring
   - Phase C: Expand events to their listener classes via `LISTENS_TO`

2. **Stage 2 — LLM spec generation:** The LLM receives real node names from the graph (not invented) and produces a compact JSON spec: `{ slug, prefix, tools: [{name, description, cypher_query, result_format}] }`. The LLM only chooses query patterns and substitutes real node names.

3. **Stage 3 — Deterministic code assembly:** Python is assembled from the spec — the LLM never writes Python. Every plugin gets 3 guaranteed tools:
   - `{prefix}summary` — hard-coded domain overview from anchors (no LLM, always works)
   - `{prefix}X` — LLM-specified query tools (Cypher populated from real node names)
   - `{prefix}store_discoveries` — writes findings to the plugin graph via `db().plugin().upsert_plugin_node()`

4. **Stage 4 — Validation:** 4-layer validation: AST/static → schema → execution sandbox → LLM judge. On judge failure the loop retries (up to `max_iterations`) with critique. On LLM failure after all retries, falls back to a template skeleton the user edits manually.

**Plugin graph writes:** `db().plugin().upsert_plugin_node(plugin_source, node_id, label, properties)` — plugins accumulate product-specific domain knowledge across sessions. Future agent calls can read these discoveries.

**Plugins live in the product:** `.laravelgraph/plugins/` inside the Laravel project — they are specific to that product's domain, not to Laravel in general.

**Plugin discovery at server startup (`plugins/loader.py`):**

`scan_plugin_manifests(plugins_dir)` reads every `.py` file via AST (no import, no side effects) and extracts `PLUGIN_MANIFEST` + tool function names. Called before `FastMCP()` is created so the results can be injected into the server instructions string. This means Claude sees all installed plugin names, descriptions, and tool names at the very start of every conversation — no `laravelgraph_suggest_plugins()` call needed.

**Hot dispatch — using plugins without restart (`laravelgraph_run_plugin_tool`):**

MCP tool lists are sent to the client at connection start. Native plugin tools only appear after the server restarts. `laravelgraph_run_plugin_tool(plugin_name, tool_name)` bypasses this by dynamically loading the plugin file via `_import_plugin_module`, registering its tools into a `_ToolCollector` (a lightweight mock that collects `@mcp.tool()` functions without touching FastMCP), then calling the requested function directly.

This means:
- Generate a plugin → call it immediately in the **same conversation** via `laravelgraph_run_plugin_tool`
- Next conversation: native tools are registered at startup AND listed in instructions automatically

```python
# Typical agent workflow after plugin generation:
laravelgraph_request_plugin("user management domain")
# → success message lists tool names
laravelgraph_run_plugin_tool("user-explorer", "usr_summary")    # immediate
laravelgraph_run_plugin_tool("user-explorer", "usr_routes")     # immediate
# Next conversation: usr_summary() / usr_routes() are native tools
```

## HTTP Serving (EC2 / Shared Server)

LaravelGraph supports two transport modes:

**Local stdio (default)** — Claude Code auto-starts the server, no manual steps:
```json
{
  "laravelgraph": {
    "type": "local",
    "command": ["bash", "-c", "laravelgraph serve \"$PWD\""],
    "enabled": true
  }
}
```

**Remote HTTP/SSE** — One server, many developers. Run on EC2:
```bash
# Start the HTTP server (use systemd/pm2 to keep alive)
laravelgraph serve /path/to/project --http --host 0.0.0.0 --port 3000 --api-key your-secret-key

# Health check (always public, no auth)
curl http://your-server:3000/health

# Re-index after code changes (requires server to be stopped due to KuzuDB write lock)
laravelgraph analyze /path/to/project
```

Each developer's agent config:
```json
{
  "laravelgraph": {
    "type": "sse",
    "url": "http://your-server:3000/sse",
    "headers": { "Authorization": "Bearer your-secret-key" }
  }
}
```

Config via environment variables:
```bash
LARAVELGRAPH_API_KEY=your-secret-key   # API key for HTTP auth
LARAVELGRAPH_PORT=3000                  # HTTP port
```

Or persist in `.laravelgraph/config.json`:
```json
{
  "mcp": {
    "transport": "http",
    "host": "0.0.0.0",
    "port": 3000,
    "api_key": "your-secret-key"
  }
}
```

**Note on Ollama with remote server:** Ollama runs on your laptop; the remote server can't reach `localhost:11434`. For EC2, configure a cloud LLM provider instead (Groq free tier recommended — fast, cheap, same quality):
```bash
laravelgraph configure  # on the EC2 server
```

## Tests

- Fixtures live in `tests/fixtures/tiny-laravel-app/` — a minimal Laravel app used by integration and unit tests.
- The `pyproject.toml` `addopts` injects `--cov` flags that require `pytest-cov`. When running manually without coverage, pass `--override-ini="addopts="`.
- Integration tests that run the full pipeline are slow (~5–10s); they use `scope="class"` fixtures to run the pipeline once per class.
