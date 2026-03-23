# LaravelGraph

**Graph-powered code intelligence engine for Laravel/PHP codebases — built for AI agents.**

[![CI](https://github.com/laravelgraph/laravelgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/laravelgraph/laravelgraph/actions)
[![PyPI](https://img.shields.io/pypi/v/laravelgraph)](https://pypi.org/project/laravelgraph/)
[![Python](https://img.shields.io/pypi/pyversions/laravelgraph)](https://pypi.org/project/laravelgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What is this?

LaravelGraph indexes your Laravel project into a rich, queryable **knowledge graph** and exposes it to AI agents (Claude Code, Cursor, Windsurf, Aider, etc.) through an MCP server.

When an AI agent is working inside your codebase and asks "how does user registration work?" — instead of blindly reading files, it calls LaravelGraph tools and gets back:

- The exact route, controller, and action handling registration
- The actual PHP source of that method
- A semantic summary of what the method does and why it exists
- Which models it touches, which events it fires, which jobs it queues
- Which middleware protects it
- Which tests cover it

All from a single tool call. No hallucination. No guessing. Real structural knowledge extracted from your code.

---

## Who is this for?

**Laravel developers** who use AI coding assistants and want them to actually understand the codebase — not just read files blindly.

If you work on a large Laravel app and your AI assistant keeps asking "where is the User model?" or misses that `event(new OrderPlaced)` dispatches a listener that sends an email — LaravelGraph fixes that.

**Specifically useful for:**
- Large codebases where AI context windows run out
- Onboarding new team members (ask the agent anything about the codebase)
- Refactoring — know the blast radius before you change something
- Code reviews — trace the full request lifecycle in seconds
- Finding dead code that Laravel's magic makes hard to detect statically

---

## The Problem It Solves

Laravel is full of magic. AI agents struggle with it.

```php
// What does this actually do?
event(new UserRegistered($user));

// Which controller handles this route?
Route::post('/checkout', [CheckoutController::class, 'store'])->middleware(['auth', 'verified', 'throttle:api']);

// What does this relationship actually join?
return $this->belongsToMany(Product::class)->withPivot('quantity', 'price');
```

Generic code analysis tools don't understand:
- Facade resolution (`Auth::user()` → `Illuminate\Auth\AuthManager`)
- Eloquent magic methods and relationship types
- Route → middleware → controller → FormRequest chains
- Event → listener → job dispatch graphs
- Service container bindings
- Blade template inheritance

LaravelGraph was built specifically to understand all of these.

---

## How It Works

### 1. Analyze (one time, ~10-60 seconds)

```bash
laravelgraph analyze /path/to/your/laravel-app
```

Runs a **23-phase pipeline** that walks your project and builds a knowledge graph stored in KuzuDB (a graph database embedded in `.laravelgraph/graph.kuzu`). No PHP installation required — LaravelGraph parses PHP using tree-sitter (a native AST parser).

The pipeline extracts:
- Every class, method, trait, interface, enum
- Every call relationship (with confidence scores)
- Every Eloquent model and its 11 relationship types
- Every route with its full middleware stack
- Every event, listener, and job in the dispatch chain
- Every service container binding
- Every Blade template and its inheritance tree
- Database schema reconstructed from migration files
- Constructor and method injection graphs
- Dead code (with Laravel-aware exemptions)
- Git change coupling (files that always change together)
- 384-dimensional semantic embeddings for every symbol

This graph is stored locally in your project at `.laravelgraph/`. Nothing is sent anywhere.

### 2. Serve (always running during AI sessions)

```bash
laravelgraph serve /path/to/your/laravel-app
```

Starts an MCP server that exposes 19 tools and 9 resources to your AI agent. The agent calls these tools instead of reading files blindly.

### 3. Query (AI agent does this automatically)

When you ask your AI agent something about the codebase, it calls tools like:

```
laravelgraph_feature_context("checkout")
→ returns routes + controller source + models + events + jobs + config in one call

laravelgraph_context("CheckoutController::store")
→ returns 360° view: source, callers, callees, dispatches, rendered views, summary

laravelgraph_impact("Order")
→ returns every symbol that breaks if Order model changes
```

### 4. Semantic Summaries (lazy, cached, optional)

If you configure an LLM provider (Anthropic, OpenAI, Groq, Ollama, or 15 others), LaravelGraph generates a 2-4 sentence semantic summary of each symbol the first time it is queried. The summary is cached in `.laravelgraph/summaries.json` and reused on every subsequent call — no API cost after the first generation.

**Why this matters for cost:**
- Summary is generated once, used hundreds of times
- Agents read a 2-sentence summary instead of 50 lines of PHP source
- Dramatically reduces the tokens an agent needs to understand a symbol
- On a team, the same symbol queried by multiple developers hits the cache every time

```
First query  → LLM called → summary generated → cached → returned
Second query → cache hit  → returned instantly → zero API cost
File changed → cache auto-invalidated (mtime check) → regenerated on next query
```

The tool works fine with no LLM configured. Summaries are optional — they enrich responses but are never required.

---

## Installation

```bash
pipx install laravelgraph
```

> `pipx` is recommended because it installs the tool in its own isolated environment and makes the `laravelgraph` command available globally without polluting your Python environment.
>
> If you don't have pipx: `pip install pipx && pipx ensurepath`

**Requirements:** Python 3.11+. Does not require PHP, Composer, or any Laravel dependencies.

---

## Quickstart

```bash
# 1. Install
pipx install laravelgraph

# 2. Index your project
laravelgraph analyze /path/to/your/laravel-app

# 3. Verify everything is working
laravelgraph doctor /path/to/your/laravel-app

# 4. Configure an LLM provider (optional but recommended)
laravelgraph configure /path/to/your/laravel-app

# 5. Connect your AI agent
laravelgraph setup /path/to/your/laravel-app --claude   # Claude Code
laravelgraph setup /path/to/your/laravel-app --cursor   # Cursor
laravelgraph setup /path/to/your/laravel-app --windsurf # Windsurf

# 6. Start the MCP server
laravelgraph serve /path/to/your/laravel-app
```

---

## AI Agent Integration (MCP)

### Claude Code

```bash
laravelgraph setup /path/to/your/laravel-app --claude
```

Add the printed JSON to `~/.claude.json`:

```json
{
  "mcpServers": {
    "laravelgraph": {
      "command": "laravelgraph",
      "args": ["serve", "/path/to/your/laravel-app"]
    }
  }
}
```

### Cursor

```bash
laravelgraph setup /path/to/your/laravel-app --cursor
```

Add to `~/.cursor/mcp.json`.

### Windsurf

```bash
laravelgraph setup /path/to/your/laravel-app --windsurf
```

Add to `~/.windsurf/mcp_config.json`.

### Any MCP-compatible agent

```bash
laravelgraph serve /path/to/your/laravel-app --http --port 3000
```

Starts an HTTP/SSE server at `http://127.0.0.1:3000` compatible with any MCP client.

---

## MCP Tools Reference

| Tool | Description |
|------|-------------|
| `laravelgraph_feature_context` | **Start here.** Routes + controller source + models + events + jobs + config in one call |
| `laravelgraph_query` | Hybrid search — symbol names, concepts, natural language |
| `laravelgraph_context` | 360° view of any symbol: source code, callers, callees, summary, relationships |
| `laravelgraph_explain` | Natural language explanation of how a feature works end-to-end |
| `laravelgraph_impact` | Blast radius grouped by depth (direct / indirect / transitive) |
| `laravelgraph_routes` | Full route map with middleware stacks and controller bindings |
| `laravelgraph_models` | Eloquent relationship graph with foreign keys and pivot tables |
| `laravelgraph_request_flow` | Trace `middleware → controller → FormRequest → service → model → event → listener` |
| `laravelgraph_dead_code` | Unreachable code with Laravel-aware exemptions |
| `laravelgraph_schema` | Database schema reconstructed from migration files |
| `laravelgraph_events` | Event → listener → job dispatch map |
| `laravelgraph_bindings` | Service container binding map (what's bound, where, how) |
| `laravelgraph_config_usage` | All code depending on a config key or env variable |
| `laravelgraph_detect_changes` | Map a git diff to affected symbols and suggested tests |
| `laravelgraph_suggest_tests` | Find which test files to run after a change |
| `laravelgraph_provider_status` | Which LLM providers are configured, active, and working |
| `laravelgraph_cypher` | Read-only Cypher queries against the raw graph |
| `laravelgraph_list_repos` | All indexed repositories with stats |

### MCP Resources

| URI | Description |
|-----|-------------|
| `laravelgraph://overview` | Node and edge counts by type |
| `laravelgraph://schema` | Full graph schema reference |
| `laravelgraph://providers` | LLM provider configuration and status |
| `laravelgraph://summaries` | Semantic summary cache stats |
| `laravelgraph://routes` | Route table |
| `laravelgraph://models` | Model relationship map |
| `laravelgraph://events` | Event/listener map |
| `laravelgraph://dead-code` | Full dead code report |
| `laravelgraph://bindings` | Service container binding map |

---

## LLM Providers for Semantic Summaries

Semantic summaries are **optional**. The tool works fully without them — providers only enrich responses with AI-generated prose.

### Supported Providers

LaravelGraph supports 18 providers out of the box. All OpenAI-compatible providers (everything except Anthropic) share the same underlying implementation.

**Cloud providers** (API key required):

| Provider | Env Variable | Recommended Model |
|----------|-------------|-------------------|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` |
| OpenRouter | `OPENROUTER_API_KEY` | `anthropic/claude-haiku-3` |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| Mistral AI | `MISTRAL_API_KEY` | `mistral-small-latest` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| Google Gemini | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| xAI (Grok) | `XAI_API_KEY` | `grok-3-mini` |
| Together AI | `TOGETHER_API_KEY` | `Llama-3.3-70B-Instruct-Turbo` |
| Fireworks AI | `FIREWORKS_API_KEY` | `llama-v3p1-8b-instruct` |
| Perplexity | `PERPLEXITY_API_KEY` | `sonar` |
| Cerebras | `CEREBRAS_API_KEY` | `llama3.1-8b` |
| Cohere | `COHERE_API_KEY` | `command-r` |
| Novita AI | `NOVITA_API_KEY` | `llama-3.1-8b-instruct` |
| Hugging Face | `HF_TOKEN` | `Qwen2.5-Coder-32B-Instruct` |

**Local providers** (no API key, must be explicitly selected):

| Provider | Default URL | Notes |
|----------|-------------|-------|
| Ollama | `http://localhost:11434` | Run `ollama pull <model>` first |
| LM Studio | `http://localhost:1234` | Load a model in LM Studio first |
| vLLM | `http://localhost:8000` | Self-hosted inference server |

### Setting Up a Provider

**Option 1 — Interactive wizard (recommended):**

```bash
laravelgraph configure /path/to/your/laravel-app
```

Walks you through picking a provider, entering credentials, choosing a model, and saving config — project-level or global.

**Option 2 — Environment variable (simplest for cloud providers):**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or
export GROQ_API_KEY=gsk_...
```

Auto-detected. No config file needed. Provider selection is automatic — first set env var wins.

**Option 3 — Config file:**

`~/.laravelgraph/config.json` (global) or `<project>/.laravelgraph/config.json` (project):

```json
{
  "summary": {
    "provider": "groq",
    "api_keys": { "groq": "gsk_..." },
    "models":   { "groq": "llama-3.3-70b-versatile" }
  }
}
```

For local providers:

```json
{
  "summary": {
    "provider": "ollama",
    "models":    { "ollama": "qwen2.5-coder:7b" },
    "base_urls": { "ollama": "http://127.0.0.1:11434" }
  }
}
```

### Checking Provider Status

```bash
laravelgraph providers /path/to/your/laravel-app
```

Shows all 18 providers split into cloud and local tables — which are configured, which are active, which model is selected.

### How the Summary Cache Works

```
First time a symbol is queried:
  1. MCP tool called (e.g. laravelgraph_context)
  2. Source code read from disk
  3. LLM prompt built (symbol name + docblock + source, capped at 50 lines)
  4. LLM called → 2-4 sentence summary returned
  5. Summary stored in .laravelgraph/summaries.json with file mtime
  6. Summary included in tool response

Every subsequent query:
  1. MCP tool called
  2. Cache checked → hit
  3. Summary returned instantly — zero LLM cost

When source file changes:
  1. mtime of file checked against stored mtime
  2. If changed → cache entry deleted
  3. Summary regenerated on next query

When watch mode re-indexes a file:
  1. All cached summaries for that file are automatically invalidated
```

The cache persists across sessions, restarts, and re-analyses (as long as you don't delete `.laravelgraph/`). On a team using the same shared index, all developers benefit from cached summaries generated by the first person who queried each symbol.

---

## CLI Reference

### Core Commands

```bash
laravelgraph analyze [PATH]          # Index a Laravel project (builds the knowledge graph)
    --full                           # Force full rebuild (ignores incremental)
    --no-embeddings                  # Skip vector embedding generation
    --phases 1,2,3                   # Run only specific phases (for debugging)

laravelgraph doctor [PATH]           # Full health check: config, DB, tools, LLM provider
laravelgraph status [PATH]           # Show index status and stats for a project
laravelgraph list                    # List all indexed repositories
laravelgraph clean [PATH]            # Delete the index for a project
    --force / -f                     # Skip confirmation prompt
```

### LLM Provider Commands

```bash
laravelgraph providers [PATH]        # Show all 18 providers — configured, active, available
laravelgraph configure [PATH]        # Interactive wizard to set up an LLM provider
    --global / -g                    # Save to global config (applies to all projects)
```

### Search and Exploration

```bash
laravelgraph query QUERY [PATH]      # Hybrid search across all indexed symbols
    --limit / -n N                   # Max results (default 20)
    --role ROLE                      # Filter by Laravel role (controller, model, event...)

laravelgraph context SYMBOL [PATH]   # 360° view of a symbol
laravelgraph impact SYMBOL [PATH]    # Blast radius analysis
    --depth / -d N                   # BFS depth (default 3)

laravelgraph routes [PATH]           # Route table
    --method GET|POST|...            # Filter by HTTP method
    --uri /api/users                 # Filter by URI fragment

laravelgraph models [PATH]           # Eloquent model relationship map
    --model User                     # Filter to a specific model

laravelgraph events [PATH]           # Event → listener → job dispatch map
laravelgraph bindings [PATH]         # Service container binding map
laravelgraph dead-code [PATH]        # Dead code report
laravelgraph schema [PATH]           # Database schema
    --table users                    # Filter to a specific table

laravelgraph cypher "MATCH (n:Route) RETURN n.uri LIMIT 10" [PATH]
```

### Server and Watch

```bash
laravelgraph serve [PATH]            # Start MCP server (stdio, for AI agents)
    --watch / -w                     # Enable live file watching and re-indexing
    --http                           # Use HTTP/SSE transport instead of stdio
    --port N                         # HTTP port (default 3000)
    --host HOST                      # HTTP host (default 127.0.0.1)

laravelgraph watch [PATH]            # Live re-indexing on file changes (standalone)
```

### Setup and Export

```bash
laravelgraph setup [PATH]            # Print MCP config JSON for your AI agent
    --claude                         # Claude Code format
    --cursor                         # Cursor format
    --windsurf                       # Windsurf format

laravelgraph diff BASE..HEAD [PATH]  # Structural branch comparison
laravelgraph export [PATH]           # Export the graph
    --format json|dot|graphml        # Output format
    --output FILE                    # Output file (default: stdout)

laravelgraph version                 # Print version
```

---

## Health Check

Before connecting your AI agent, run:

```bash
laravelgraph doctor /path/to/your/laravel-app
```

This checks everything end-to-end:

```
Config          ✓  Config loaded
Dependencies    ✓  kuzu, fastmcp, typer, rich, anthropic, openai
Graph DB        ✓  Graph DB accessible — 1,234 nodes, 5,678 edges
MCP Tools       ✓  laravelgraph_query (2ms)
                ✓  laravelgraph_routes (0ms)
                ✓  laravelgraph_models (0ms)
                ✓  laravelgraph_events (0ms)
                ✓  laravelgraph_schema (0ms)
                ✓  laravelgraph_bindings (0ms)
LLM Provider    ✓  Provider: groq
                ✓  Model: llama-3.3-70b-versatile
                →  Sending test prompt...
                ✓  Live test passed (0.43s)
                   "Handles new user registration by validating..."
Optional        ✓  watchfiles installed — watch mode available
                ✓  fastembed installed — vector search available
```

Exits with code 0 on success, 1 on failure — usable in CI.

---

## Analysis Pipeline

LaravelGraph runs 23 phases in sequence during `analyze`:

| Phase | Name | What It Does |
|-------|------|-------------|
| 1 | File Discovery | Walks project, respects .gitignore, classifies files by Laravel role |
| 2 | Structure | Builds file/folder graph with CONTAINS relationships |
| 3 | AST Parsing | Parses all PHP — classes, methods, traits, interfaces, enums, docblocks |
| 4 | Import Resolution | Resolves `use` statements via PSR-4 autoloading |
| 5 | Call Graph | Traces method calls with confidence scores, resolves Facades |
| 6 | Heritage | Inheritance, interface implementation, trait usage |
| 7 | Type Analysis | Parameter, return type, and property type relationships |
| 8 | Community Detection | Leiden algorithm clustering via igraph |
| 9 | Execution Flows | BFS-traces from route/command/job/listener entry points |
| 10 | Dead Code | Multi-pass with Laravel-aware exemptions (see below) |
| 11 | Change Coupling | Git co-change analysis over 6-month window |
| 12 | Embeddings | 384-dim vectors via fastembed (local ONNX — no cloud) |
| 13 | Eloquent Relationships | All 11 relationship types, foreign keys, pivot tables |
| 14 | Route Analysis | Web, API, console, channel routes with full metadata |
| 15 | Middleware Resolution | Expands groups/aliases, builds full middleware stacks |
| 16 | Container Bindings | Parses service providers for singleton/bind/instance/contextual |
| 17 | Event/Listener/Job | Parses EventServiceProvider, traces full dispatch chains |
| 18 | Blade Templates | Template inheritance, @include, x-components, @livewire |
| 19 | Database Schema | Reconstructs tables/columns/types from migration files |
| 20 | Config/Env | Maps config() and env() calls to keys and variables |
| 21 | Dependency Injection | Constructor and method injection graph |
| 22 | API Contracts | FormRequest validation rules, API Resource shapes |
| 23 | Scheduled Tasks | Parses Kernel.php schedule definitions |

---

## Graph Schema

### Node Types

| Category | Types |
|----------|-------|
| File system | `File`, `Folder`, `Namespace` |
| PHP symbols | `Class_`, `Method`, `Function_`, `Trait_`, `Interface_`, `Enum_` |
| Laravel roles | `EloquentModel`, `Controller`, `Middleware`, `ServiceProvider` |
| async/events | `Job`, `Event`, `Listener` |
| HTTP layer | `Route`, `FormRequest`, `Resource` |
| Notifications | `Notification`, `Observer`, `Policy` |
| CLI | `Command`, `ScheduledTask` |
| Database | `Migration`, `DatabaseTable`, `DatabaseColumn` |
| DI/Config | `ServiceBinding`, `ConfigKey`, `EnvVariable` |
| Views | `BladeTemplate`, `BladeComponent`, `LivewireComponent` |
| Dev tooling | `Factory`, `Seeder` |
| Graph meta | `Community`, `Process` |

### Relationship Types

| Relationship | Connects |
|-------------|---------|
| `CONTAINS` | Folder → File, Namespace → Class |
| `DEFINES` | File → Class/Function |
| `CALLS` | Method → Method (with `confidence` score) |
| `EXTENDS_CLASS` | Class → Class |
| `IMPLEMENTS_INTERFACE` | Class → Interface |
| `USES_TRAIT` | Class → Trait |
| `HAS_RELATIONSHIP` | EloquentModel → EloquentModel (with `relationship_type`, `foreign_key`, `pivot_table`) |
| `ROUTES_TO` | Route → Controller/Closure |
| `APPLIES_MIDDLEWARE` | Route → Middleware |
| `DISPATCHES` | Method/Listener → Event/Job |
| `LISTENS_TO` | Listener → Event |
| `HANDLES` | Listener/Job → Event |
| `RENDERS_TEMPLATE` | Method/Class → BladeTemplate |
| `INCLUDES_TEMPLATE` | BladeTemplate → BladeTemplate |
| `EXTENDS_TEMPLATE` | BladeTemplate → BladeTemplate |
| `HAS_COMPONENT` | BladeTemplate → BladeComponent/LivewireComponent |
| `MIGRATES_TABLE` | Migration → DatabaseTable |
| `HAS_COLUMN` | DatabaseTable → DatabaseColumn |
| `REFERENCES_TABLE` | DatabaseColumn → DatabaseTable (foreign keys) |
| `INJECTS` | Class → Class (constructor/method injection) |
| `BINDS_TO` | ServiceBinding → Class |
| `VALIDATES_WITH` | Route/Controller → FormRequest |
| `TRANSFORMS_WITH` | Controller → Resource |
| `USES_CONFIG` | Method/Class → ConfigKey |
| `USES_ENV` | Method/Class → EnvVariable |
| `AUTHORIZES_WITH` | Controller → Policy |
| `SCHEDULES` | Command → ScheduledTask |
| `MEMBER_OF` | Class/Method → Community |
| `STEP_IN_PROCESS` | Method → Process |
| `COUPLED_WITH` | File → File (git co-change) |

---

## Dead Code Detection

LaravelGraph's dead code analysis understands Laravel's magic and **never flags**:

- Controller methods bound to routes
- Artisan command `handle()` methods
- Job `handle()` methods
- Event listener and subscriber methods
- Middleware `handle()` methods
- Service provider `register()` and `boot()` methods
- Eloquent accessors (`get*Attribute`), mutators (`set*Attribute`), scopes (`scope*`)
- Magic methods: `__construct`, `__call`, `__callStatic`, `__invoke`, `__get`, `__set`, `__toString`
- Methods in Policy, Observer, FormRequest classes
- Methods overriding non-dead parent class methods
- Trait methods (dynamically included — too dynamic to trace statically)

---

## Search

Three strategies merged with Reciprocal Rank Fusion (RRF):

| Strategy | Weight | Best For |
|----------|--------|----------|
| BM25 (rank_bm25) | 40% | Exact keyword matching on names and docblocks |
| Semantic (fastembed) | 40% | Conceptual queries: "payment processing", "user auth" |
| Fuzzy (rapidfuzz) | 20% | Typo tolerance and partial name matching |

**Ranking boosts:**
- Controllers, models, source files: 1.2×
- Test files: 0.5×
- Vendor, storage, bootstrap: excluded

Vector embeddings are generated locally using `BAAI/bge-small-en-v1.5` via fastembed (ONNX runtime). No cloud API calls during analyze or search.

---

## Data Storage

| Store | Location | Contains | Survives re-analyze? |
|-------|----------|----------|----------------------|
| Graph DB | `.laravelgraph/graph.kuzu` | All nodes, edges, embeddings | No (rebuilt each time) |
| Summary cache | `.laravelgraph/summaries.json` | AI-generated prose summaries | Yes |
| Config | `.laravelgraph/config.json` | Project-level settings | Yes |
| Registry | `~/.laravelgraph/repos.json` | All indexed project paths | Yes |
| Logs | `~/.laravelgraph/logs/` | Structured JSON logs | Yes |

The summary cache surviving re-analyze is intentional — you don't pay to regenerate summaries just because you re-indexed the codebase. Summaries are invalidated per-file when source files change (mtime check).

---

## Configuration

LaravelGraph loads config from three places in order, with later sources overriding earlier ones:

1. `~/.laravelgraph/config.json` — global defaults (all projects)
2. `<project>/.laravelgraph/config.json` — project-level overrides
3. Environment variables — highest priority

### Full Config Reference

```json
{
  "embedding": {
    "enabled": true,
    "model": "BAAI/bge-small-en-v1.5",
    "batch_size": 64,
    "dimensions": 384
  },
  "search": {
    "bm25_weight": 0.4,
    "vector_weight": 0.4,
    "fuzzy_weight": 0.2,
    "top_k": 20,
    "fuzzy_threshold": 0.6,
    "test_file_penalty": 0.5,
    "source_boost": 1.2
  },
  "pipeline": {
    "git_history_months": 6,
    "change_coupling_threshold": 0.3,
    "watch_debounce_seconds": 30.0,
    "max_file_size_kb": 512,
    "call_confidence_threshold": 0.3
  },
  "mcp": {
    "transport": "stdio",
    "host": "127.0.0.1",
    "port": 3000,
    "log_requests": true
  },
  "log": {
    "level": "INFO"
  },
  "summary": {
    "enabled": true,
    "provider": "auto",
    "api_keys":  { "anthropic": "sk-ant-..." },
    "models":    { "anthropic": "claude-haiku-4-5-20251001" },
    "base_urls": {},
    "max_source_lines": 50
  }
}
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key (auto-detected) |
| `OPENAI_API_KEY` | OpenAI API key (auto-detected) |
| `OPENROUTER_API_KEY` | OpenRouter API key (auto-detected) |
| `GROQ_API_KEY` | Groq API key (auto-detected) |
| `MISTRAL_API_KEY` | Mistral API key (auto-detected) |
| `DEEPSEEK_API_KEY` | DeepSeek API key (auto-detected) |
| `GEMINI_API_KEY` | Google Gemini API key (auto-detected) |
| `XAI_API_KEY` | xAI API key (auto-detected) |
| `TOGETHER_API_KEY` | Together AI API key (auto-detected) |
| `FIREWORKS_API_KEY` | Fireworks AI API key (auto-detected) |
| `PERPLEXITY_API_KEY` | Perplexity API key (auto-detected) |
| `CEREBRAS_API_KEY` | Cerebras API key (auto-detected) |
| `COHERE_API_KEY` | Cohere API key (auto-detected) |
| `NOVITA_API_KEY` | Novita AI API key (auto-detected) |
| `HF_TOKEN` | Hugging Face token (auto-detected) |
| `OLLAMA_BASE_URL` | Ollama base URL override |
| `LARAVELGRAPH_LOG_LEVEL` | Log level override (DEBUG/INFO/WARNING) |
| `LARAVELGRAPH_PORT` | MCP HTTP port override |

---

## Performance

| Metric | Target |
|--------|--------|
| Index tiny project (~20 files) | < 2 seconds |
| Index medium project (~200 files) | < 10 seconds |
| Index large project (~1000 files) | < 60 seconds |
| Hybrid search | < 100ms |
| Context/impact tool call | < 200ms |
| Watch mode single-file re-index | < 1 second |
| Memory (medium project) | < 500MB |
| Summary cache hit (no LLM call) | < 5ms |

---

## Logs

All logs are structured JSON, written to `~/.laravelgraph/logs/`:

| File | Contains |
|------|---------|
| `laravelgraph.log` | Main operational log |
| `laravelgraph-pipeline.log` | Phase-by-phase execution detail |
| `laravelgraph-mcp.log` | Every MCP tool request and response time |
| `laravelgraph-performance.log` | Timing and memory metrics |
| `laravelgraph-errors.log` | Errors and warnings only |

```bash
# Tail MCP requests live while an agent is connected
tail -f ~/.laravelgraph/logs/laravelgraph-mcp.log | jq .
```

---

## Comparison

| Feature | LaravelGraph | Generic PHP LSP | Generic code indexer |
|---------|:-----------:|:---------------:|:-------------------:|
| PHP 8.x AST parsing | ✓ | ✓ | Partial |
| Facade resolution | ✓ | ✗ | ✗ |
| Eloquent relationship graph | ✓ | ✗ | ✗ |
| Route → middleware → controller chain | ✓ | ✗ | ✗ |
| Service container bindings | ✓ | ✗ | ✗ |
| Event → listener → job dispatch graph | ✓ | ✗ | ✗ |
| Blade template inheritance | ✓ | ✗ | ✗ |
| Dead code (Laravel-aware) | ✓ | Partial | Partial |
| Git change coupling | ✓ | ✗ | ✗ |
| Hybrid semantic search | ✓ | ✗ | Partial |
| MCP server for AI agents | ✓ | ✗ | ✗ |
| AI-generated symbol summaries | ✓ | ✗ | ✗ |
| 18 LLM providers + local models | ✓ | ✗ | ✗ |
| Zero cloud dependencies (core) | ✓ | ✓ | ✓ |
| No PHP installation required | ✓ | ✗ | ✗ |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, coding conventions, and the pull request process.

---

## License

[MIT](LICENSE)
