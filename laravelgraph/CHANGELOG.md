# Changelog

All notable changes to LaravelGraph will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-03-27

### Added

#### CLI
- `laravelgraph changelog` — view the release changelog directly in the terminal with
  Rich rendering; supports `--latest`, `--version <x.y.z>`, and `--all` flags

#### MCP Tools
- `laravelgraph_resolve_column` — varchar/char/text columns now include a live **Value Sample**
  table (top distinct values by count, capped at 30 distinct values, suppressed for
  high-cardinality columns) giving agents instant insight into what values actually live
  in production columns
- `laravelgraph_impact` — when a symbol has 0 downstream impact but IS a route entry point,
  a contextual note is shown explaining why (no PHP callers) along with the matching
  HTTP route(s) and a suggestion to use `laravelgraph_request_flow`
- `laravelgraph_events` — new **Scheduled Tasks** section showing all `ScheduledTask` nodes,
  frequency, and a prominent `⚠ Scheduler disabled` warning when all tasks are commented out
- `laravelgraph_context` — dispatch section now shows inline condition hints
  (e.g. `` `when: if ($plan == 1)` ``) and a header note when multiple targets fire
  conditionally; falls back to "read source" message when no conditions could be extracted

#### Pipeline
- **Phase 23 — Scheduler**: `_detect_commented_tasks()` helper scans the `schedule()` body
  line-by-line (handles `//`, `#`, `*`, and `/* … */` block comments) and populates
  `ctx.stats["scheduler_disabled"]` and `ctx.stats["scheduler_commented_tasks"]` so agents
  can warn when scheduled jobs have been silently disabled
- **Phase 5 — Calls**: `_extract_condition_hint()` walks backwards up to 4 non-blank lines
  from each dispatch call looking for `if / elseif / case / switch` guards;
  `_find_dispatches()` now returns `(class, type, condition_hint)` triples and the
  `condition` string is stored on every `DISPATCHES` edge in the graph

#### Schema
- `DISPATCHES` relationship — new `condition STRING` property stores the guarding
  `if`/`case` expression text, enabling agents to understand conditional dispatch logic
  without reading the source

#### Doctor
- Check **6d — Scheduler status**: fails with detail when `scheduler_disabled` is True
  (all `$schedule->` calls in `Kernel.php` are commented out)

#### list_repos
- `laravelgraph_list_repos` / `laravelgraph list` — shows `Scheduler disabled — N task(s)
  commented out` warning when the scheduler is fully disabled in the indexed project

#### Provider Configuration
- `--activate` / `-a` flag on `laravelgraph configure` — switch the active LLM provider
  without re-entering credentials (e.g. `laravelgraph configure --activate groq`)
- Model picker in the configure wizard — every provider now exposes a curated list of
  exact API model IDs with one-line descriptions; press Enter to accept the default or
  type to filter / enter a custom ID
- All 18 providers (anthropic, openai, openrouter, groq, mistral, deepseek, gemini, xai,
  together, fireworks, perplexity, cerebras, cohere, novita, huggingface, ollama,
  lmstudio, vllm) now carry an accurate `"models"` list in `PROVIDER_REGISTRY`

### Fixed

- **list_repos** — registry was polluted with 80+ pytest temp paths after running the
  test suite; paths matching `/pytest-N/`, `/var/folders/`, `/tmp/`, `\Temp\`, and
  `__pycache__` are now filtered out before display and deduplication
- **configure** — configuring a new LLM provider no longer silently resets
  `"provider"` to `"auto"`, displacing the previously active provider; the chosen
  provider is now saved explicitly
- **configure** — changing scope from project (1) to global (2) now defaults to global
  scope to match the most common use case
- **doctor — MCP Tool Signatures**: `project_root` variable name was undefined (should be
  `root`); `list_tools()` is async in FastMCP — now properly awaited via `asyncio.run()`;
  tool parameter lookup now uses `t.parameters["properties"]` instead of `t.inputSchema`
- **Phase 23 — Scheduler**: `_split_statements()` now strips commented lines before
  regex-matching `$schedule->`, so commented-out tasks no longer count toward
  `scheduled_tasks` in `ctx.stats`
- **Phase 5 — Calls**: `_DISPATCH_NEW_RE` now has a negative lookbehind `(?<!:)` so
  `Event::dispatch(new X())` is no longer double-matched by both `_EVENT_FACADE_RE`
  and `_DISPATCH_NEW_RE`
- **openrouter** default model corrected from `anthropic/claude-haiku-3` to
  `anthropic/claude-3-5-haiku`
- **fireworks** default model corrected from `llama-v3p1-8b-instruct` to
  `llama-v3p3-70b-instruct`

### Tests

- 120 new unit tests covering all 5 fixes (temp path filtering, varchar sampling,
  route entry point detection, scheduler comment detection, dispatch condition hints)
- Updated `test_phase05_dispatches.py` to handle the new triple return format of
  `_find_dispatches`

---

## [0.1.0] - 2026-03-21

### Added

#### Core Engine
- 23-phase analysis pipeline for Laravel/PHP codebases
- PHP AST parsing via tree-sitter-php with regex fallback
- Blade template parsing with component/directive extraction
- composer.json parsing with PSR-4 class map building
- KuzuDB embedded graph database with full schema

#### Analysis Phases
- **Phase 1**: File discovery with .gitignore support and Laravel role classification
- **Phase 2**: File/folder structure graph with CONTAINS relationships
- **Phase 3**: PHP AST parsing — classes, methods, traits, interfaces, enums, functions
- **Phase 4**: Import/namespace resolution with PSR-4 autoloading
- **Phase 5**: Call graph tracing with Facade resolution and confidence scores
- **Phase 6**: Heritage analysis — inheritance, interfaces, traits
- **Phase 7**: Type analysis — param types, return types, property types
- **Phase 8**: Community detection via Leiden algorithm (python-igraph)
- **Phase 9**: Execution flow detection with Laravel-aware entry points
- **Phase 10**: Dead code detection with Laravel-aware exemptions
- **Phase 11**: Git change coupling analysis (6-month window)
- **Phase 12**: Vector embeddings via fastembed (BAAI/bge-small-en-v1.5)
- **Phase 13**: Eloquent relationship graph (all 11 relationship types)
- **Phase 14**: Route analysis (web, api, console, channel files)
- **Phase 15**: Middleware chain resolution
- **Phase 16**: Service container binding map
- **Phase 17**: Event → Listener → Job dispatch graph
- **Phase 18**: Blade template inheritance and component graph
- **Phase 19**: Database schema reconstruction from migrations
- **Phase 20**: Config/env dependency mapping
- **Phase 21**: Dependency injection analysis
- **Phase 22**: API contract extraction (FormRequests, Resources)
- **Phase 23**: Scheduled task analysis

#### MCP Server (17 tools + 7 resources)
- `laravelgraph_query` — Hybrid BM25 + vector + fuzzy search
- `laravelgraph_context` — 360° symbol view
- `laravelgraph_impact` — Blast radius with depth grouping
- `laravelgraph_routes` — Full route map with middleware
- `laravelgraph_models` — Eloquent relationship graph
- `laravelgraph_request_flow` — Complete HTTP request trace
- `laravelgraph_dead_code` — Laravel-aware dead code report
- `laravelgraph_schema` — Database schema from migrations
- `laravelgraph_events` — Event/listener/job dispatch map
- `laravelgraph_bindings` — Service container binding map
- `laravelgraph_config_usage` — Config/env dependency map
- `laravelgraph_detect_changes` — Map git diff to affected symbols
- `laravelgraph_suggest_tests` — Suggest tests to run after changes
- `laravelgraph_explain` — Natural language feature explanation
- `laravelgraph_cypher` — Read-only Cypher queries
- `laravelgraph_list_repos` — All indexed repositories
- MCP Resources: overview, schema, routes, models, events, dead-code, bindings

#### CLI
- `laravelgraph analyze` — Index a Laravel project
- `laravelgraph status` — Show index status
- `laravelgraph list` — List indexed repositories
- `laravelgraph clean` — Delete index
- `laravelgraph query` — Hybrid search
- `laravelgraph context` — Symbol 360° view
- `laravelgraph impact` — Blast radius analysis
- `laravelgraph dead-code` — Dead code report
- `laravelgraph routes` — Route table
- `laravelgraph models` — Model relationship map
- `laravelgraph events` — Event/listener/job map
- `laravelgraph bindings` — Container binding map
- `laravelgraph schema` — Database schema
- `laravelgraph cypher` — Raw graph queries
- `laravelgraph serve` — Start MCP server (stdio + HTTP/SSE)
- `laravelgraph watch` — Live file watching
- `laravelgraph diff` — Structural branch comparison
- `laravelgraph setup` — Print MCP config for Claude/Cursor/Windsurf
- `laravelgraph export` — Export graph (JSON/DOT/GraphML)

#### Infrastructure
- Structured JSON logging across 5 log channels
- Watch mode with Rust-backed watchfiles
- Global repository registry
- Rich CLI output with tables, trees, progress bars
- GitHub Actions CI pipeline
- Test fixtures: tiny Laravel app

[Unreleased]: https://github.com/laravelgraph/laravelgraph/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/laravelgraph/laravelgraph/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/laravelgraph/laravelgraph/releases/tag/v0.1.0
