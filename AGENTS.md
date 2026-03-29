# AGENTS.md — LaravelGraph

**Generated:** 2026-03-27  **Commit:** ae005ce  **Branch:** main

## OVERVIEW

LaravelGraph is a 26-phase Python analysis pipeline that indexes Laravel/PHP projects into a local KuzuDB graph database and exposes the result as an MCP server (23 tools, 9 resources) to AI coding agents.

**Stack:** Python 3.11+, KuzuDB (graph DB), tree-sitter (PHP AST), fastembed (local ONNX vectors), FastMCP, Typer CLI, PyMySQL, Pydantic v2.

## STRUCTURE

```
laravelgraph/
├── pipeline/         # 26-phase analysis engine (phase_NN_name.py + orchestrator.py)
├── mcp/              # FastMCP server — all 23 tools, caches, LLM summarize
├── core/             # graph.py (KuzuDB CRUD), schema.py (50+ node/100+ rel types), registry.py
├── parsers/          # php.py (tree-sitter + regex fallback), blade.py, composer.py
├── search/           # hybrid.py — BM25 + fastembed + rapidfuzz + RRF
├── analysis/         # impact.py — blast radius BFS
├── watch/            # watcher.py — watchfiles integration for live re-index
├── config.py         # Pydantic config model — loads from env > project > global JSON
├── cli.py            # Typer app — all CLI commands
└── logging.py        # structlog setup — 5 named loggers (main, pipeline, mcp, perf, errors)
tests/
├── unit/             # Per-module unit tests; use tmp_graph fixture, no pipeline runs
├── integration/      # Full pipeline against tiny-laravel-app fixture; ~5-10s/class
├── fixtures/         # tiny-laravel-app/ — minimal Laravel app used by all tests
└── conftest.py       # tiny_app_root + tmp_graph fixtures
```

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add a pipeline phase | `laravelgraph/pipeline/phase_NN_name.py` + register in `orchestrator.py` |
| Add an MCP tool | `laravelgraph/mcp/server.py` — `@mcp.tool()` decorated function |
| Add an LLM provider | `laravelgraph/mcp/summarize.py` — one entry in `PROVIDER_REGISTRY` |
| Change graph schema | `laravelgraph/core/schema.py` — `NODE_TYPES` / `REL_TYPES` lists |
| Add a CLI command | `laravelgraph/cli.py` — Typer `@app.command()` |
| Change config defaults | `laravelgraph/config.py` — Pydantic model fields |
| Test a pipeline phase | `tests/integration/pipeline/` |
| Test an MCP tool | `tests/integration/mcp/` |
| Unit test graph ops | `tests/unit/graph/` |
| Add test data | `tests/fixtures/tiny-laravel-app/` |

## CODE MAP

| Symbol | File | Role |
|--------|------|------|
| `PipelineContext` | `pipeline/orchestrator.py:21` | Shared dataclass passed through all 26 phases |
| `Pipeline.run()` | `pipeline/orchestrator.py:53` | Executes ordered phase list; catches errors per-phase |
| `GraphDB` | `core/graph.py:17` | KuzuDB wrapper — `upsert_node()`, `upsert_edge()`, Cypher exec |
| `NODE_TYPES` / `REL_TYPES` | `core/schema.py` | Lists defining all DDL for the graph schema |
| `create_server()` | `mcp/server.py:34` | Constructs FastMCP with all tools and resources |
| `SummaryCache` | `mcp/cache.py:22` | JSON sidecar — mtime-based per-file invalidation |
| `DBContextCache` | `mcp/db_cache.py` | JSON sidecar — hash-based (column structure) invalidation |
| `PROVIDER_REGISTRY` | `mcp/summarize.py:28` | 18-provider dict; `sdk="openai"` shares `_call_openai_compat()` |
| `generate_summary()` | `mcp/summarize.py` | Returns `(str \| None, str)` — never raises |
| `HybridSearch` | `search/hybrid.py` | BM25(40%) + vector(40%) + fuzzy(20%) with RRF |
| `Config.load()` | `config.py` | env vars > `.laravelgraph/config.json` > `~/.laravelgraph/config.json` |

## CONVENTIONS

- **Phase ordering:** Phase 10 (dead code) runs AFTER phase 18 (Blade) intentionally — BLADE_CALLS edges must exist first. Phases 24→25→26 are strictly ordered; see comments in `orchestrator.py`.
- **Phase errors are non-fatal:** Each phase is wrapped in `try/except`; errors append to `ctx.errors` and the pipeline continues.
- **Graph writes only via `ctx.db.upsert_node()` / `ctx.db.upsert_edge()`** — never raw Cypher in phases.
- **Summaries never raise** — `generate_summary()` returns `(None, error_msg)` on failure.
- **Config priority:** env vars beat project config beat global config.
- **`pipx reinstall laravelgraph` required** after source changes — the MCP server uses the pipx binary, not the dev install.

## ANTI-PATTERNS (THIS PROJECT)

- **Do not import phase modules at module level** in `orchestrator.py` — they are imported lazily inside `Pipeline.run()` to avoid circular imports.
- **Do not add new node/edge types without updating `schema.py`** — `GraphDB.__init__` creates DDL from `NODE_TYPES`/`REL_TYPES` at startup.
- **Do not write tests that depend on LLM calls** — summaries are opt-in; mock or disable via `Config(summary=SummaryConfig(enabled=False))`.
- **Do not assume `force_reinit=True` deletes the `.kuzu` directory** — KuzuDB lock files may block `shutil.rmtree`; the schema is dropped via Cypher as a fallback.
- **Do not use `--cov` flags when running tests manually** — pytest's `addopts` injects `--cov`; override with `--override-ini="addopts="`.

## UNIQUE STYLES

- `from __future__ import annotations` in every module (PEP 563 deferred evaluation).
- structlog for all logging — five named loggers: `get_logger()`, `get_pipeline_logger()`, `get_mcp_logger()`, `get_perf_logger()`, and errors-only.
- `node_id(type, fqn)` helper in `core/schema.py` generates consistent node IDs across phases.
- KuzuDB table names are the raw label strings from `NODE_TYPES`/`REL_TYPES` (e.g., `Class_`, `CALLS`).

## COMMANDS

```bash
pip install -e ".[dev]"                               # Dev install
pytest tests/ --override-ini="addopts=" -v            # Run all tests (no coverage)
pytest tests/unit/ --override-ini="addopts=" -v       # Unit tests only
pytest tests/integration/ --override-ini="addopts="   # Integration tests (~slow)
ruff check laravelgraph/                              # Lint
mypy laravelgraph/                                    # Type check
pipx reinstall laravelgraph                           # Reinstall after source changes (MCP uses pipx)
laravelgraph analyze /path/to/app                     # Index a Laravel project
laravelgraph serve /path/to/app                       # Start MCP server (stdio)
laravelgraph doctor /path/to/app                      # Full health check
```

## NOTES

- **Coverage gate: 80% minimum** — enforced by `--cov-fail-under=80` in `pyproject.toml`.
- **Storage layout:** `.laravelgraph/graph.kuzu` (rebuilt on `--full`), `summaries.json` (survives re-analyze), `db_context.json` (survives re-analyze). Summary cache is shared across developers on a team using the same index.
- `laravelgraph/dashboard/` — optional web dashboard requiring `pip install laravelgraph[dashboard]`; not part of core.
- `ruff` line-length 100, `target-version = "py311"`, selectors E/F/I/UP/B/SIM, E501 ignored.
- Embeddings use `BAAI/bge-small-en-v1.5` via fastembed ONNX — no cloud API calls during analyze.

## Plugin System

LaravelGraph supports a plugin system for domain-specific MCP tools.
Plugins live in `.laravelgraph/plugins/*.py` and are auto-loaded at server startup.

### Architecture
- **Plugin Graph** (`plugin_graph.kuzu`) — separate writable KuzuDB for plugin-stored knowledge
- **DualDB** — plugins get `db.core()` (read-only) and `db.plugin()` (writable)
- **Auto-generation** — agents call `laravelgraph_request_plugin()` to generate new tools
- **Self-improvement** — plugins auto-improve when performance thresholds are crossed
- **4-layer validation** — AST + schema + execution + LLM-judge before any plugin goes live

### Plugin lifecycle
1. Agent calls `laravelgraph_request_plugin("description")`
2. System generates plugin via LLM with graph context
3. 4-layer validation with up to 3 reflection iterations
4. Plugin deployed to `.laravelgraph/plugins/`
5. Available in next MCP session
6. Self-improvement monitors performance; auto-regenerates if underperforming

### Governance rules
- `tool_prefix` cannot start with `laravelgraph_`
- No DELETE/DROP/TRUNCATE on core graph
- No network access (requests, httpx, urllib blocked)
- All plugin nodes tagged with `plugin_source` automatically
