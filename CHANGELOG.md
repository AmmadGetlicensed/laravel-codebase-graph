# Changelog

All notable changes to LaravelGraph will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/laravelgraph/laravelgraph/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/laravelgraph/laravelgraph/releases/tag/v0.1.0
