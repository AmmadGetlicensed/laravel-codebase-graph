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
