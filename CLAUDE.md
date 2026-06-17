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

# Evaluation harness (proves the tools actually answer correctly)
python -m eval.run_eval --mode structural --app tiny    # deterministic, no API key (CI gate)
python -m eval.run_eval --mode agent --app tiny          # A/B accuracy WITH vs WITHOUT (needs ANTHROPIC_API_KEY)
python -m eval.run_eval --mode structural --app real     # needs LARAVELGRAPH_EVAL_REAL_APP (see eval/realapp.md)
# Dataset: eval/dataset/*.yaml (ground-truth facts). Scorecards: eval/results/.

# Agent instruction installer
laravelgraph agent install .                         # install for Claude Code (writes to CLAUDE.md)
laravelgraph agent install . --tool opencode         # install for OpenCode (.opencode/instructions.md)
laravelgraph agent install . --tool cursor           # install for Cursor (.cursorrules)
laravelgraph agent install . --tool all              # install for all three tools

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

LaravelGraph is a **33-phase analysis pipeline** that indexes a Laravel/PHP codebase into a local KuzuDB graph database, then exposes it via an MCP server to AI agents.

### Data flow

```
Laravel project on disk
  → Pipeline (33 phases) → .laravelgraph/graph.kuzu  (KuzuDB)
                         → .laravelgraph/summaries.json (LLM symbol summary cache)
                         → .laravelgraph/db_context.json (LLM DB context cache)
  → MCP server (FastMCP) ← Claude Code / other agents
MySQL/RDS databases
  → Phase 24 (live introspection) → graph nodes + edges
```

### Key packages

- **`laravelgraph/pipeline/`** — 33 sequential phases (registered in `orchestrator.py`'s `all_phases` list, which controls execution order — note phase 10 runs after 18, so file numbering ≠ run order). Each is a single `.py` file with a `run(ctx: PipelineContext)` function. `PipelineContext` (in `orchestrator.py`) is the shared state object carrying `db`, `config`, parsed file maps, and the FQN index. A phase that raises is caught, recorded in `ctx.errors`, and the pipeline continues.
  - Phases 24–26 handle database intelligence: live DB introspection (PyMySQL), model-table linking, and DB access analysis (static, zero AI cost).

- **`laravelgraph/core/`** — `graph.py` wraps KuzuDB (CRUD + Cypher), `schema.py` defines the graph schema as two lists: `NODE_TYPES` (50 node labels) and `REL_TYPES` (55 relationship types, each declared with its allowed `(from_label, to_label)` pairs), `registry.py` manages the global `~/.laravelgraph/repos.json` index.

- **`laravelgraph/parsers/`** — PHP (tree-sitter + regex fallback), Blade templates, and Composer JSON parsing.

- **`laravelgraph/mcp/server.py`** — The FastMCP server (~6000 lines). Exposes **12 primary tools** (`search`, `cypher`, `sql`, `context`, `feature_context`, `impact`, `map`, `trace`, `db`, `risks`, `tests`, `status`) plus ~38 legacy single-purpose tools kept as **deprecated aliases** (still callable, undocumented as primary). The 8 new dispatchers (`map`/`db`/`trace`/`risks`/`tests`/`search`/`sql`/`status`) route by `kind`/`mode` into the legacy implementations — FastMCP keeps decorated functions directly callable, so there is no behavior change. Also registers 10 resources and an `on_call_tool` middleware that stamps every response with index age + a staleness warning. All built inside `create_server()`.

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

### Adding / changing an MCP tool

Prefer extending one of the 12 primary tools (add a `kind`/`mode` branch to the relevant dispatcher near the bottom of `create_server()`) rather than adding a new top-level tool — keeping the surface small is deliberate (too many tools degrade agent tool-selection). If you do add a `@mcp.tool()`, update the **PRIMARY TOOLS** section in the server instructions string. The 8 dispatchers call the legacy implementations directly; legacy tools remain registered as deprecated aliases for one release, then get removed.

### MCP tool surface (12 primaries)

| Tool | Covers |
|------|--------|
| `search` | hybrid BM25 + vector + fuzzy symbol search |
| `cypher` | raw Cypher over the core graph |
| `sql` | read-only SQL against a live DB |
| `context` | 360° view of one symbol (+ `include_source`) |
| `feature_context` | one-call feature overview (routes→models→events→jobs) |
| `impact` | blast radius of a change |
| `map(kind=…)` | routes / models / events / bindings / api / outbound / config / repos |
| `trace(kind=…)` | request / job / table_write / git_diff execution paths |
| `db(mode=…)` | schema / context / column / procedure / connections / procedures / quality / boundary |
| `risks(kind=…)` | dead_code / security / race / perf / cross_cutting |
| `tests(mode=…)` | suggest tests / coverage |
| `status` | providers + indexed repos + index age |

### Evaluation harness (`eval/`)

The product's core claim ("real structural knowledge, no hallucination") is measured, not asserted.

- `eval/client.py` — drives MCP tools through FastMCP's in-memory `Client` (tools are nested in `create_server()` and not importable directly). `index_app()` copies a fixture to a temp dir and runs the pipeline; `run_calls()` batches tool calls against one server.
- `eval/run_eval.py` — **structural** mode (deterministic ground-truth fact checks, no LLM, CI gate) and **agent** mode (A/B: an LLM agent answers each question WITH the LaravelGraph tools vs file-access-only, LLM-judged → `accuracy_with` vs `accuracy_without`).
- `eval/dataset/tiny.yaml` — ground-truth questions for `tests/fixtures/tiny-laravel-app`. `eval/dataset/real.yaml` + `eval/realapp.md` — opt-in real-app eval (live DB) for the DB-intelligence moat.
- CI gate: `tests/integration/eval/` asserts 100% structural correctness.

### Index-age + staleness middleware

`_make_index_age_middleware()` registers an `on_call_tool` FastMCP middleware that appends an `_Index age: …_` footer to every tool response, plus `⚠️ N source file(s) changed since indexing` when project `.php` mtimes are newer than `Registry.indexed_at`. Middleware is the single boundary hook (there is no shared response formatter — outputs are hand-built in 400+ places), and internal tool-to-tool calls bypass it so footers never embed mid-output. The age/staleness computation is cached ~15s.

### Incremental re-index (`watch/watcher.py`)

`laravelgraph serve --watch` (or `laravelgraph watch`) re-runs only the phases a changed file can affect, keeping high-value edges fresh:

- any `.php` → `3,4,5,6,7,20,21,22,28,32,33`
- `app/Models/*` → `+ 13,25,26,31`; `app/Events|Listeners|Jobs/*` → `+ 17`
- `routes/*.php` → `14,15` (full route rebuild); `database/migrations/*.php` → `19`; `*.blade.php` → `18`
- whole-graph phases `8,9,10` run on a debounced batch; embeddings (12) refresh only on a full `analyze`.

`GraphDB.build_fqn_index()` / `build_class_map()` rebuild the cross-file maps (otherwise in-memory-only per pipeline run) from the persisted graph. Each handler opens its own `GraphDB`, `close()`s it (KuzuDB write-lock safety), and calls `Registry.touch()` so the index-age footer stays accurate.

**Pipeline phases 32–33:** Phase 32 (`phase_32_http_clients.py`) detects outbound `Http::`/Guzzle/`curl_*` calls (`HttpClientCall` nodes, `CALLS_EXTERNAL` edges; surfaced by `map(kind="outbound")`). Phase 33 (`phase_33_notifications.py`) parses `via()` on `Notification` subclasses and detects `Mailable` classes.

**Agent Instruction Installer (`laravelgraph/agent_installer.py`):** `laravelgraph agent install` writes an agent instruction block teaching the tool hierarchy and investigation protocol. Idempotent. Targets: `CLAUDE.md`, `.opencode/instructions.md`, `.cursorrules`.

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

# Full re-index after code changes (requires server stopped due to KuzuDB write lock).
# For live updates, prefer `serve --watch` — it incrementally re-indexes changed
# files (including routes/events/models) without a full rebuild.
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
