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

LaravelGraph is a **23-phase analysis pipeline** that indexes a Laravel/PHP codebase into a local KuzuDB graph database, then exposes it via an MCP server to AI agents.

### Data flow

```
Laravel project on disk
  → Pipeline (23 phases) → .laravelgraph/graph.kuzu  (KuzuDB)
                         → .laravelgraph/summaries.json (LLM summary cache)
  → MCP server (FastMCP) ← Claude Code / other agents
```

### Key packages

- **`laravelgraph/pipeline/`** — 23 sequential phases. Each is a single `.py` file with a `run(ctx: PipelineContext)` function. `PipelineContext` (in `orchestrator.py`) is the shared state object carrying `db`, `config`, parsed file maps, and the FQN index.

- **`laravelgraph/core/`** — `graph.py` wraps KuzuDB (CRUD + Cypher), `schema.py` defines 50+ node types and 100+ relationship types, `registry.py` manages the global `~/.laravelgraph/repos.json` index.

- **`laravelgraph/parsers/`** — PHP (tree-sitter + regex fallback), Blade templates, and Composer JSON parsing.

- **`laravelgraph/mcp/server.py`** — The FastMCP server. All 19 MCP tools and 9 resources live here as `@mcp.tool()` / `@mcp.resource()` decorated functions. This is the largest file (~90KB).

- **`laravelgraph/mcp/summarize.py`** — 18-provider LLM registry (`PROVIDER_REGISTRY`). All OpenAI-compatible providers share `_call_openai_compat()`; only Anthropic uses its native SDK. `generate_summary()` returns `(str | None, str)` — never raises.

- **`laravelgraph/mcp/cache.py`** — `SummaryCache`: file-backed JSON sidecar at `.laravelgraph/summaries.json`. Mtime-based auto-invalidation — if the source file changes, the cached summary is discarded on the next tool call.

- **`laravelgraph/search/hybrid.py`** — BM25 + vector (fastembed) + fuzzy (rapidfuzz) with RRF ranking.

- **`laravelgraph/config.py`** — Pydantic config model. `SummaryConfig` uses `api_keys: dict`, `models: dict`, `base_urls: dict` (not flat per-provider fields). Config priority: env vars → `.laravelgraph/config.json` → `~/.laravelgraph/config.json` → defaults.

- **`laravelgraph/cli.py`** — Typer application. All CLI commands including `analyze`, `serve`, `doctor`, `configure`, `providers`, `status`, `query`, `context`, `impact`, `routes`, `models`, `events`, `schema`, `bindings`, `dead-code`, `warm`.

### Storage layout (per project)

```
<project>/.laravelgraph/
  graph.kuzu/         KuzuDB database (directory)
  summaries.json      LLM-generated semantic summaries (mtime-invalidated)
  config.json         Project-level config overrides
~/.laravelgraph/
  repos.json          Global registry of indexed projects
  config.json         Global config defaults
  logs/               Structured logs
```

### Adding a new pipeline phase

1. Create `laravelgraph/pipeline/phase_NN_name.py` with `run(ctx: PipelineContext) -> None`
2. Register it in `orchestrator.py`'s phase list
3. Use `ctx.db.upsert_node()` / `ctx.db.upsert_edge()` for graph writes

### Adding a new MCP tool

Add a `@mcp.tool()` decorated function to `mcp/server.py`. Update the server instructions string at the top of `create_server()`.

## Tests

- Fixtures live in `tests/fixtures/tiny-laravel-app/` — a minimal Laravel app used by integration and unit tests.
- The `pyproject.toml` `addopts` injects `--cov` flags that require `pytest-cov`. When running manually without coverage, pass `--override-ini="addopts="`.
- Integration tests that run the full pipeline are slow (~5–10s); they use `scope="class"` fixtures to run the pipeline once per class.
