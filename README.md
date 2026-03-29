# LaravelGraph

**A graph-powered code intelligence engine for Laravel/PHP codebases, built for AI agents.**

[![PyPI](https://img.shields.io/pypi/v/laravelgraph)](https://pypi.org/project/laravelgraph/)
[![Python](https://img.shields.io/pypi/pyversions/laravelgraph)](https://pypi.org/project/laravelgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Table of Contents

1. [What It Is and Why It Exists](#1-what-it-is-and-why-it-exists)
2. [How It Works — Architecture Overview](#2-how-it-works--architecture-overview)
3. [Installation](#3-installation)
4. [Quickstart](#4-quickstart)
5. [The Analysis Pipeline — 33 Phases](#5-the-analysis-pipeline--33-phases)
6. [The Knowledge Graph Schema](#6-the-knowledge-graph-schema)
7. [MCP Server and Tools](#7-mcp-server-and-tools)
8. [The Plugin System](#8-the-plugin-system)
9. [Database Intelligence](#9-database-intelligence)
10. [LLM Providers and Semantic Summaries](#10-llm-providers-and-semantic-summaries)
11. [CLI Reference](#11-cli-reference)
12. [HTTP Serving for Teams](#12-http-serving-for-teams)
13. [Agent Integration Setup](#13-agent-integration-setup)
14. [Configuration Reference](#14-configuration-reference)
15. [Storage Layout](#15-storage-layout)
16. [Health Check](#16-health-check)

---

## 1. What It Is and Why It Exists

### The Problem with AI Agents and Laravel

Laravel is built on magic. Routes are defined declaratively. Models have dozens of implicit relationship methods. Events fire listeners that queue jobs that trigger more events. The service container resolves interfaces to concrete classes based on bindings registered inside service providers that may themselves be conditionally loaded.

An AI agent reading files one by one sees this:

```php
event(new UserRegistered($user));
```

And has no idea that this fires two listeners: one that sends a welcome email, one that dispatches a `SetupDefaultPreferences` job that writes to four different tables. It has to guess, or ask you.

LaravelGraph gives agents the full picture. Every class, method, route, model, event, job, binding, migration, and Blade template is indexed into a **knowledge graph** — a connected network of nodes and edges where relationships between things are first-class citizens. An agent querying the graph gets structural facts, not probabilistic guesses.

### What LaravelGraph Does

LaravelGraph does four things:

1. **Analyzes** your Laravel project with a 33-phase pipeline and stores the result in a local graph database (KuzuDB). This happens once, in ~10–60 seconds depending on project size. No PHP required — parsing is done with tree-sitter.

2. **Serves** the graph to AI agents through an MCP (Model Context Protocol) server exposing 44 tools and 9 resources. Agents call tools instead of reading files.

3. **Generates plugins** — product-specific domain lenses over the graph that answer questions no generic tool can answer. "What is the order lifecycle?" requires knowing your specific app's routes, models, and events. Plugins are auto-generated, validated, and deployed by the system.

4. **Learns** — plugins accumulate domain knowledge across conversations. Agents store their findings; the system surfaces them in future sessions. Plugin tools auto-improve when their results degrade.

### What This Means in Practice

When your agent asks "how does checkout work?", instead of reading 15 files and hallucinating the answer, it calls `laravelgraph_feature_context("checkout")` and gets back in one response:

- The exact routes that handle checkout (URI, method, middleware stack, controller action)
- The PHP source of the controller method
- A 2-sentence AI-generated summary of what the method does
- Every Eloquent model it touches
- Every event it fires and which listeners those events have
- Every job dispatched and what it does
- Which config keys and env variables it depends on

All from a single tool call. Real structural knowledge. No hallucination.

---

## 2. How It Works — Architecture Overview

### Data Flow

```
Laravel project on disk
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  33-Phase Analysis Pipeline                          │
│  (tree-sitter PHP parser + regex + PyMySQL)          │
└──────────────────┬──────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
.laravelgraph/           .laravelgraph/
graph.kuzu               summaries.json
(KuzuDB graph DB)        (LLM summary cache)
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  MCP Server (FastMCP)                                │
│  44 tools · 9 resources · plugin tools               │
└──────────────────┬──────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
  Claude Code            Cursor / Windsurf /
  (stdio)                any MCP client (HTTP/SSE)
```

### Key Technical Decisions

**Why KuzuDB?** Relationships are first-class in a graph database. Asking "which symbols break if I change this method?" is a graph traversal — not a SQL join across 10 tables. KuzuDB is embedded (no separate server), stores the graph locally inside `.laravelgraph/`, and supports Cypher queries.

**Why tree-sitter?** PHP AST parsing without requiring PHP to be installed. Tree-sitter is a native parser with language grammars compiled as Python extensions. It handles syntax errors gracefully and is significantly faster than invoking PHP CLI.

**Why a 33-phase pipeline?** Each phase builds on the previous. You can't resolve Eloquent relationships until models are parsed. You can't detect dead code until the call graph exists. Phases run in dependency order and share a `PipelineContext` object — a single state object carrying the DB connection, config, parsed file maps, and the FQN (fully qualified name) index.

**Why MCP?** The Model Context Protocol is the standard way AI agents call external tools. LaravelGraph exposes its intelligence as MCP tools, which means any agent that supports MCP — Claude Code, Cursor, Windsurf, Cline, Aider, and others — can use it without any custom integration.

---

## 3. Installation

```bash
pipx install laravelgraph
```

`pipx` installs the tool in an isolated environment and makes `laravelgraph` available globally. This is the recommended method because the MCP server is launched as a subprocess by Claude Code — it must be available on `$PATH`.

```bash
# If you don't have pipx:
pip install pipx && pipx ensurepath

# After any source change (for development):
pipx reinstall laravelgraph
```

**Requirements:** Python 3.11+. No PHP, no Composer, no Node.js. LaravelGraph parses PHP using tree-sitter (downloaded automatically on first run).

**Optional dependencies** (installed automatically but noted here):
- `fastembed` — local vector embeddings for semantic search (384-dim BGE-Small)
- `pymysql` — live database introspection (phases 24–26)
- `watchfiles` — live file watching in `serve --watch` mode

---

## 4. Quickstart

```bash
# 1. Index your project (one-time, ~10-60 seconds)
laravelgraph analyze /path/to/your/laravel-app

# 2. Verify everything is working
laravelgraph doctor /path/to/your/laravel-app

# 3. (Optional) Configure an LLM provider for semantic summaries
laravelgraph configure /path/to/your/laravel-app

# 4. Install agent instructions into your AI tool's config file
laravelgraph agent install /path/to/your/laravel-app

# 5. Connect your AI agent (prints the config JSON to paste)
laravelgraph setup /path/to/your/laravel-app --claude   # Claude Code
laravelgraph setup /path/to/your/laravel-app --cursor   # Cursor
laravelgraph setup /path/to/your/laravel-app --windsurf # Windsurf
```

For Claude Code, the MCP server auto-starts when you open a session. Add this to `~/.claude.json`:

```json
{
  "mcpServers": {
    "laravelgraph": {
      "command": "bash",
      "args": ["-c", "laravelgraph serve \"/path/to/your/laravel-app\""]
    }
  }
}
```

After that, your AI agent has access to all 44 tools whenever it opens a session on your project.

---

## 5. The Analysis Pipeline — 33 Phases

Running `laravelgraph analyze` executes 33 phases in sequence. Each phase reads from the graph, adds nodes and edges, and passes control to the next phase through the shared `PipelineContext`. Nothing is sent to any external service during analysis.

### Phase 1 — File Discovery

**What it does:** Walks the project directory, classifies every PHP and Blade file by its Laravel role. Creates `File` nodes in the graph for each file found, attaching the role (`controller`, `model`, `event`, `job`, `middleware`, `service_provider`, `blade`, etc.).

**Why it matters:** Role classification is the foundation everything else builds on. It determines which parsers to apply and which relationships to look for. A file in `app/Models/` is treated differently from one in `app/Http/Controllers/` — the phase uses both path heuristics and file content inspection to determine role.

### Phase 2 — Project Structure

**What it does:** Creates `Folder` nodes for every directory and links them to their `File` children with `CONTAINS` edges. Also identifies the project root, namespaces, and PSR-4 autoload mappings from `composer.json`.

**Why it matters:** The filesystem hierarchy is a structural signal. Knowing that `app/Services/Billing/` contains 12 files creates context for community detection and feature clustering later. PSR-4 mappings enable accurate FQN (fully qualified class name) resolution.

### Phase 3 — PHP/Blade AST Parsing

**What it does:** The largest single phase. Parses every PHP file with tree-sitter (a C-based incremental parser) to extract: classes, traits, interfaces, enums, methods, properties, constants, functions, and Blade directives. Creates the corresponding graph nodes with attributes like `fqn`, `file_path`, `line_number`, `visibility`, `is_abstract`, `is_static`, `docblock`.

**Why it matters:** This is the raw extraction layer. All higher phases depend on the symbol index built here. The parser handles partial/broken PHP gracefully — a syntax error in one file doesn't abort the pipeline. For files where tree-sitter fails entirely, a regex-based fallback extracts class and method names.

### Phase 4 — Import Resolution

**What it does:** Resolves `use` statements at the top of every PHP file. Maps short class names to their fully qualified forms using the PSR-4 index built in Phase 2. Creates `IMPORTS` edges.

**Why it matters:** PHP uses short names everywhere but the actual identity of a class is its FQN. Without import resolution, `new Order()` in `CheckoutController.php` is ambiguous — it could be `App\Models\Order` or `Modules\Billing\Models\Order`. Phase 4 collapses that ambiguity so Phase 5 can build a correct call graph.

### Phase 5 — Call Graph Tracing

**What it does:** Analyzes method bodies to find calls to other methods. Creates `CALLS` edges with confidence scores (0–1). Detects:
- Direct method calls (`$this->doSomething()`)
- Static calls (`OrderService::create()`)
- Facade calls (`Auth::user()`, resolved to the actual facade target)
- `event(new X())` dispatches
- `dispatch(new X())` job dispatches
- `Bus::chain()` and `Bus::batch()` chains

**Why it matters:** The call graph is the backbone of impact analysis. "Which methods call this one?" is a reverse traversal of `CALLS` edges. Dead code detection, community detection, and execution flow analysis all depend on an accurate call graph. Confidence scoring handles dynamic dispatch cases where the callee can't be statically determined.

### Phase 6 — Heritage Analysis (Inheritance, Interfaces, Traits)

**What it does:** Resolves `extends`, `implements`, and `use` (trait) declarations. Creates `EXTENDS_CLASS`, `IMPLEMENTS_INTERFACE`, and `USES_TRAIT` edges between nodes.

**Why it matters:** Inheritance is load-bearing in Laravel. A controller extends `Controller` which extends `BaseController` which provides `$this->middleware()`. A model inherits Eloquent magic methods. Without heritage edges, these relationships are invisible. Phase 6 also resolves interface-to-concrete-class mappings, which feeds Phase 16's service container analysis.

### Phase 7 — Type Analysis

**What it does:** Extracts type hints from method signatures (parameters and return types). Creates `USES_TYPE` edges from methods to the classes they type-hint against.

**Why it matters:** Type hints are static evidence of coupling. If `OrderController::store` type-hints `OrderRequest`, `OrderService`, and `Order`, those three classes are load-bearing for that method. Type analysis surfaces implicit dependencies that don't appear in the call graph because the constructor injection handles them (see Phase 21).

### Phase 8 — Community Detection

**What it does:** Runs the Leiden clustering algorithm on the call graph to group tightly-coupled symbols into communities. Creates `Community` nodes and `MEMBER_OF` edges.

**Why it matters:** Communities are natural domain boundaries detected algorithmically. Symbols that call each other frequently cluster together. A billing community might contain `OrderController`, `PaymentService`, `Invoice`, `InvoiceLineItem`, `ChargeProcessed`, and `PaymentFailed`. Communities seed Phase 27's feature clustering and help agents understand module boundaries without reading the entire codebase.

### Phase 9 — Execution Flow Analysis

**What it does:** Identifies entry points (routes, commands, event listeners, jobs, scheduled tasks) and traces BFS (breadth-first search) execution flows from each entry point. Creates `Process` nodes representing distinct execution paths and `STEP_IN_PROCESS` edges connecting the steps.

**Why it matters:** Understanding that "this job can be triggered by either a webhook or a CLI command and in both cases it eventually calls `BillingService::charge`" is impossible without execution flow analysis. It gives agents a map of how code is actually traversed at runtime, not just how it's structured.

### Phase 10 — Dead Code Detection

**What it does:** Multi-pass analysis to identify unreachable symbols. Starts with known entry points (routes, commands, listeners, scheduled tasks) and marks everything reachable. What remains is potentially dead. Applies Laravel-aware exemptions for:
- Classes registered as service providers
- Eloquent models (accessed via magic methods)
- Controllers bound to routes
- Observer methods (`created`, `updated`, `deleted`)
- Methods matching Laravel lifecycle hooks

**Why it matters:** Dead code detection on Laravel codebases is notoriously hard because so much is resolved dynamically. LaravelGraph's exemption system handles the cases where static analysis would generate false positives. The result is a dead code report that's actually useful — not a list of "dead" observer methods that get called by Eloquent automatically.

### Phase 11 — Git Change Coupling

**What it does:** Reads `git log` history to find pairs of files that frequently change together in the same commit. Creates `COUPLED_WITH` edges with a coupling score.

**Why it matters:** Change coupling is an empirical signal that two files are logically related even if there's no explicit code dependency between them. A migration file and a model file consistently committed together suggests they describe the same domain object. Agents use this to identify the full blast radius of a change — "these two files always change together, so changing one probably requires changing the other."

### Phase 12 — Vector Embeddings

**What it does:** Generates 384-dimensional semantic embeddings for every symbol using `fastembed` with the BGE-Small-EN model (runs locally). Embeddings are stored in the graph and used for semantic similarity search.

**Why it matters:** Keyword search finds "UserController" when you search "UserController". Semantic search finds "UserController" when you search "handles user account creation" because the embeddings encode meaning, not just tokens. Phase 12 enables the semantic component of the hybrid search (BM25 + vector + fuzzy) that powers `laravelgraph_query` and `laravelgraph_explain`.

### Phase 13 — Eloquent Relationship Analysis

**What it does:** Parses model methods looking for Eloquent relationship declarations. Detects all 11 relationship types: `hasOne`, `hasMany`, `belongsTo`, `belongsToMany`, `hasOneThrough`, `hasManyThrough`, `morphTo`, `morphOne`, `morphMany`, `morphToMany`, `morphedByMany`. Extracts foreign keys, local keys, and pivot table names. Creates `HAS_RELATIONSHIP` edges.

**Why it matters:** Eloquent relationships define the data model of the application. `$order->items` is not visible in the call graph — it's a dynamic method defined by the relationship declaration. Phase 13 makes these relationships explicit in the graph so agents can answer "which models does Order connect to and through what keys?" without reading model files.

### Phase 14 — Route Analysis

**What it does:** Parses `routes/` files (web.php, api.php, channels.php, console.php, plus any custom route files). Resolves route closures and controller bindings to FQNs. Extracts URI patterns, HTTP methods, names, middleware assignments, and route groups. Creates `Route` nodes and `ROUTES_TO` edges to controller methods.

**Why it matters:** Routes are the entry surface of a Laravel application — the mapping from HTTP verbs and URIs to code. Every HTTP-facing feature starts here. Phase 14 gives agents the authoritative list of what the application exposes and where each endpoint leads.

### Phase 15 — Middleware Resolution

**What it does:** Extracts middleware from three levels: global (registered in `Kernel.php`), route group (applied to groups in route files), and per-route (the `->middleware()` chain). Resolves aliases to their actual class FQNs. Creates `APPLIES_MIDDLEWARE` edges from routes to middleware classes.

**Why it matters:** Middleware is invisible from route definitions alone. `->middleware('auth:sanctum,throttle:api,verified')` is three separate middleware classes with distinct behaviors. Understanding what middleware a route has determines its security surface, rate limits, and preconditions. `laravelgraph_request_flow` and `laravelgraph_security_surface` both depend on complete middleware resolution.

### Phase 16 — Service Container Bindings

**What it does:** Parses `ServiceProvider` classes looking for `$this->app->bind()`, `$this->app->singleton()`, `$this->app->scoped()`, `$this->app->instance()`, and `$this->app->alias()` calls. Creates `ServiceBinding` nodes and `BINDS_TO` edges.

**Why it matters:** The service container is the backbone of Laravel's dependency injection. When a controller type-hints `PaymentGatewayInterface`, the container injects the concrete class registered in a provider. Without Phase 16, that injection path is invisible. Agents use binding data to understand which concrete implementation is active for an interface and where that decision is made.

### Phase 17 — Event and Job Graph

**What it does:** Parses `EventServiceProvider` to extract `$listen` array mappings (event class → listener classes). Also follows `implements ShouldQueue` on listeners to detect which listeners run asynchronously. Creates `LISTENS_TO` edges (Listener → Event), `HANDLES` edges (Listener → Event), and `DISPATCHES` edges (code → Event/Job).

**Why it matters:** Events and jobs are the asynchronous nervous system of a Laravel app. `event(new OrderShipped($order))` may trigger 4 listeners, 2 of which queue jobs, one of which dispatches a chained job. Without Phase 17, none of this is visible to an agent. The full event→listener→job dispatch map enables `laravelgraph_events` and `laravelgraph_job_chain`.

### Phase 18 — Blade Template Analysis

**What it does:** Parses Blade templates to extract: layout inheritance (`@extends`), component inclusions (`@include`, `@component`), Livewire component usage, and template-to-controller relationships (which route renders which view). Creates `BladeTemplate` nodes and `EXTENDS_TEMPLATE`, `INCLUDES_TEMPLATE`, `RENDERS_TEMPLATE`, `HAS_COMPONENT` edges.

**Why it matters:** Blade templates are part of the application's surface area. A change to a base layout file affects every child template. A component used in 40 views has 40 dependents. Phase 18 makes the template dependency tree explicit and surfaces Livewire components as first-class graph nodes.

### Phase 19 — Database Schema from Migrations

**What it does:** Parses all migration files in `database/migrations/` to reconstruct the logical database schema. Extracts table names, column names, types, nullable flags, default values, indexes, and foreign key declarations. Creates `DatabaseTable`, `DatabaseColumn`, and `InferredRelationship` nodes.

**Why it matters:** Most Laravel applications don't have a live database connection during development. Phase 19 provides database schema without requiring a DB connection — just by reading migrations. The inferred schema is used by Phases 25 and 26 to link models to tables and analyze DB access patterns. It's also the fallback when no live DB connection is configured.

### Phase 20 — Config and Env Dependencies

**What it does:** Scans all PHP files for `config('key.path')` and `env('VAR_NAME')` calls. Creates `ConfigKey` and `EnvVariable` nodes and `USES_CONFIG`, `USES_ENV` edges from the calling methods.

**Why it matters:** Configuration dependencies are implicit coupling. If `PaymentService` reads `config('services.stripe.secret')`, it depends on that config key being set correctly. Phase 20 makes these dependencies explicit so agents can answer "what code depends on this env variable?" and understand the full impact of a config change.

### Phase 21 — Dependency Injection Graph

**What it does:** Analyzes constructor signatures and method signatures for type-hinted class dependencies. Traces the injection chain: what a controller receives via constructor, what it passes to service methods, what those services receive. Creates `INJECTS` edges.

**Why it matters:** The DI graph shows the wiring of the application. When you look at a controller test and wonder "what do I need to mock?", the answer is the constructor injections. Phase 21 makes this visible as graph traversal rather than file reading.

### Phase 22 — API Contract Analysis

**What it does:** Parses `FormRequest` classes to extract validation rules (`rules()` method). Parses `JsonResource` / `ResourceCollection` classes to extract the data shape they transform. Creates `Contract` nodes with `VALIDATES_WITH` and `TRANSFORMS_WITH` edges to routes and controllers.

**Why it matters:** FormRequests and Resources define the API contract — what input is valid and what output looks like. They're separate from the controller but tightly coupled to it. Phase 22 surfaces them explicitly so `laravelgraph_api_surface` can return a full contract: route + validation rules + response shape in one call.

### Phase 23 — Scheduled Task Analysis

**What it does:** Parses `Console/Kernel.php` (and Laravel 11's `routes/console.php`) to extract scheduled commands with their schedules (cron expressions, frequency methods like `->daily()`, `->hourly()`). Creates `ScheduledTask` nodes and `SCHEDULES` edges.

**Why it matters:** Scheduled tasks are invisible entry points — they run without an HTTP request. Phase 23 makes them visible in the graph so dead code analysis doesn't flag the methods they call as unreachable, and so agents can understand the full set of automated processes that run in the background.

### Phase 24 — Live Database Introspection

**What it does:** If a database connection is configured, connects to the live MySQL/PostgreSQL database via PyMySQL and pulls the ground-truth schema: all tables, columns (names, types, nullable, defaults, character sets), indexes, foreign keys, stored procedures (with their source SQL), and views. Creates or enriches `DatabaseTable`, `DatabaseColumn`, `StoredProcedure`, and `DatabaseView` nodes.

**Why it matters:** Migrations lie. Tables accumulate columns added manually, dropped in production but not in migrations, or modified by external tools. The live database schema is authoritative. Phase 24 pulls that ground truth and stores it in the graph so agents see what's actually in the database, not what was true at the time migrations were written. This phase is skipped when no DB connection is configured — Phase 19's migration-based schema is used instead.

### Phase 25 — Model-to-Table Linking

**What it does:** Links every `EloquentModel` node to its corresponding `DatabaseTable` node. Uses three resolution strategies in order: (1) explicit `$table` property on the model, (2) Laravel's default convention (snake_case plural of class name), (3) fuzzy matching against known table names.

**Why it matters:** Models and tables are the same concept from different angles — the model is the PHP interface, the table is the storage. Phase 25 creates the bridge. Without it, `laravelgraph_models` can't show you which database table a model maps to, and `laravelgraph_db_context` can't show which model owns a given table.

### Phase 26 — Database Access Analysis

**What it does:** Static analysis of all PHP files for raw database access patterns: `DB::table()`, `DB::select()`, `DB::statement()`, raw query strings, `Schema::` calls, and Eloquent method chains. Creates `UsageContext` nodes and `QUERIES_TABLE`, `USES_TABLE`, `CONTEXT_READS`, `CONTEXT_WRITES` edges.

**Why it matters:** Models aren't the only way code touches the database. Many applications have raw DB queries in services, repositories, or jobs that bypass the ORM. Phase 26 captures all database access regardless of how it's done. This powers `laravelgraph_db_context`'s "code access summary" and `laravelgraph_cross_cutting_concerns`.

### Phase 27 — Feature Clustering

**What it does:** Groups routes, models, events, and jobs into domain `Feature` clusters by URI segment analysis and semantic similarity. A feature named "checkout" might contain routes under `/checkout/*`, `Order`, `Cart`, and `Payment` models, `OrderPlaced` and `PaymentProcessed` events. Creates `Feature` nodes and `BELONGS_TO_FEATURE` edges.

**Why it matters:** Features are how humans think about applications — not in terms of individual files, but in terms of business domains. Phase 27 provides the product-level view: "what belongs to checkout?" yields a complete set of related symbols. Features are also the basis for plugin generation gap detection and proactive plugin suggestions.

### Phase 28 — Behavioral Contract Extraction

**What it does:** Extracts behavioral contracts from code patterns: authorization checks (`$this->authorize()`, `Gate::check()`), policy authorizations, validation rules, lifecycle hooks, and authentication guards. Creates `Contract` nodes and `GOVERNS` edges.

**Why it matters:** Behavioral contracts are implicit specifications encoded in the code. "This route requires authentication, validates with these rules, and authorizes using this policy" is scattered across three different places in the source. Phase 28 collects them into one place. `laravelgraph_contracts` and `laravelgraph_security_surface` both read from this data.

### Phase 29 — Change Intelligence

**What it does:** Reads the git log to identify recently changed symbols and symbols changed in specific commits. Marks `Method` and `Class_` nodes with `changed_recently: true` and `changed_in_commit: "sha"` properties.

**Why it matters:** Change intelligence combines with impact analysis to create a highly targeted answer to "what might be broken by this change?". `laravelgraph_detect_changes` takes a git diff and uses change intelligence + call graph traversal to map exactly which symbols are affected.

### Phase 30 — Test Coverage Mapping

**What it does:** Scans the `tests/` directory and parses test method names and class names. Uses naming conventions and direct method calls to link test methods to the production code they test. Creates `TestCase` nodes and `TESTS` edges.

**Why it matters:** Knowing which tests cover a given method changes the cost calculation of a change. If `OrderService::charge` is covered by 3 unit tests and 2 feature tests, an agent can point you directly to the tests to run after modifying it. `laravelgraph_suggest_tests` and `laravelgraph_detect_changes` both use test coverage data.

### Phase 31 — Query Pattern and Performance Risk Detection

**What it does:** Analyzes loops (foreach, for, while) containing Eloquent method calls to detect N+1 query patterns. Also detects missing eager loading, large result set retrievals without pagination, and repeated `count()` calls. Creates `PerformanceRisk` nodes and `HAS_PERFORMANCE_RISK` edges.

**Why it matters:** N+1 queries are the most common Laravel performance bug. They're invisible in static analysis because the problem is the combination of a loop and a lazy-loaded relationship. Phase 31 detects them automatically and surfaces them in `laravelgraph_performance_risks`.

### Phase 32 — External HTTP Client Detection

**What it does:** Scans all PHP files for outbound HTTP calls using Laravel's HTTP facade (`Http::get()`, `Http::post()`), Guzzle (`$client->request()`), and raw curl (`curl_exec()`). Extracts the HTTP verb, URL pattern, and client type. Creates `HttpClientCall` nodes and `CALLS_EXTERNAL` edges from the calling method.

**Why it matters:** External HTTP calls are a system boundary — they couple your application to third-party APIs. Knowing which methods make outbound calls, what URLs they target, and whether they're using the right client type is essential for security review, performance analysis, and mocking in tests. `laravelgraph_outbound_apis` reads from this data.

### Phase 33 — Notification Channel Enrichment

**What it does:** Parses `Notification` subclasses to extract their `via()` method and populate the `channels` field (mail, database, broadcast, slack, sms, etc.). Also detects `Mailable` classes and creates them as Notification nodes with `channels: ["mail"]`.

**Why it matters:** Laravel notifications are polymorphic — the same notification can go to multiple channels based on the user's preferences. Phase 33 makes the channel configuration explicit so agents know which delivery mechanism a notification uses without reading every Notification class manually.

---

## 6. The Knowledge Graph Schema

### What a Knowledge Graph Is

The graph is a network of **nodes** (things) and **edges** (relationships between things). In LaravelGraph, a `Route` node connects via `ROUTES_TO` to a `Method` node (the controller action), which connects via `CALLS` to a `Method` on a service, which connects via `DISPATCHES` to an `Event`, which connects via `LISTENS_TO` to a `Listener`. This chain is traversable in any direction with a single graph query.

### Node Types (50 total)

**Filesystem:**
| Node | Key Properties |
|------|----------------|
| `Folder` | path, name |
| `File` | path, name, role, loc |

**PHP Symbols:**
| Node | Key Properties |
|------|----------------|
| `Namespace` | name, fqn |
| `Class_` | fqn, name, is_abstract, is_final, file_path, line_number, docblock |
| `Trait_` | fqn, name, file_path |
| `Interface_` | fqn, name, file_path |
| `Enum_` | fqn, name, file_path |
| `Method` | fqn, name, visibility, is_static, is_abstract, file_path, line_number, return_type, source |
| `Function_` | fqn, name, file_path |
| `Property` | name, visibility, type |
| `Constant` | name, value |

**Laravel Constructs:**
| Node | Key Properties |
|------|----------------|
| `EloquentModel` | fqn, table, fillable, guarded, casts, relationships |
| `Controller` | fqn, type (resource/invokable/plain) |
| `Middleware` | fqn, alias |
| `ServiceProvider` | fqn |
| `Job` | fqn, queue, connection, should_queue |
| `Event` | fqn |
| `Listener` | fqn, queued |
| `Policy` | fqn, model_fqn |
| `FormRequest` | fqn, rules_source |
| `Resource` | fqn |
| `Notification` | fqn, channels |
| `Observer` | fqn |
| `Command` | fqn, signature, description |
| `Factory` | fqn, model_fqn |
| `Gate` | name, callback_class, file_path, line_number |

**Routing:**
| Node | Key Properties |
|------|----------------|
| `Route` | uri, http_method, name, action_method, middleware_stack |

**Blade:**
| Node | Key Properties |
|------|----------------|
| `BladeTemplate` | path, name, extends |
| `BladeComponent` | name, class_fqn |
| `LivewireComponent` | name, class_fqn |

**Database:**
| Node | Key Properties |
|------|----------------|
| `DatabaseConnection` | name, driver, host, database |
| `DatabaseTable` | name, connection, row_count, engine |
| `DatabaseColumn` | name, type, nullable, default, character_set |
| `StoredProcedure` | name, body, param_count |
| `DatabaseView` | name, definition |
| `Migration` | file_path, batch |
| `InferredRelationship` | from_table, to_table, via_column, confidence |
| `UsageContext` | label, description |

**Analysis:**
| Node | Key Properties |
|------|----------------|
| `Community` | id, size, density |
| `Feature` | name, symbol_count, has_changes |
| `Contract` | type, description, source |
| `TestCase` | fqn, test_method, file_path |
| `PerformanceRisk` | type, description, severity |
| `HttpClientCall` | caller_fqn, http_verb, url_pattern, client_type |
| `ServiceBinding` | abstract, concrete, binding_type |
| `ConfigKey` | key |
| `EnvVariable` | name |
| `ScheduledTask` | command, schedule, description |
| `Process` | name, entry_type |

### Relationship Types (55 total)

**Filesystem:** `CONTAINS`, `DEFINES`

**PHP Structure:** `EXTENDS_CLASS`, `IMPLEMENTS_INTERFACE`, `USES_TRAIT`, `IMPORTS`, `CALLS`, `USES_TYPE`

**Routing:** `ROUTES_TO`, `APPLIES_MIDDLEWARE`

**Eloquent:** `HAS_RELATIONSHIP`

**Container:** `BINDS_TO`

**Events / Jobs:** `DISPATCHES`, `LISTENS_TO`, `HANDLES`, `NOTIFIES`

**Blade:** `BLADE_CALLS`, `RENDERS_TEMPLATE`, `INCLUDES_TEMPLATE`, `EXTENDS_TEMPLATE`, `HAS_COMPONENT`

**Database:** `MIGRATES_TABLE`, `HAS_COLUMN`, `REFERENCES_TABLE`, `HAS_TABLE`, `HAS_PROCEDURE`, `HAS_VIEW`, `USES_TABLE`, `QUERIES_TABLE`, `PROCEDURE_READS`, `PROCEDURE_WRITES`, `INFERRED_REFERENCES`, `HAS_USAGE_CONTEXT`, `CONTEXT_READS`, `CONTEXT_WRITES`

**Config:** `USES_CONFIG`, `USES_ENV`

**DI:** `INJECTS`

**Authorization / Validation:** `AUTHORIZES_WITH`, `VALIDATES_WITH`, `TRANSFORMS_WITH`, `SCHEDULES`

**Analysis:** `MEMBER_OF`, `STEP_IN_PROCESS`, `COUPLED_WITH`

**Lifecycle:** `OBSERVES`, `AUTHORIZES_MODEL`, `DEFINES_FACTORY`

**Features:** `BELONGS_TO_FEATURE`

**Contracts:** `GOVERNS`

**Testing:** `TESTS`

**Performance:** `HAS_PERFORMANCE_RISK`

**External:** `CALLS_EXTERNAL`

**Gates:** `DEFINES_GATE`, `CHECKS_GATE`

### Querying the Graph Directly

If you want to explore the raw graph with Cypher:

```bash
# CLI
laravelgraph cypher "MATCH (r:Route) RETURN r.uri, r.http_method, r.action_method LIMIT 20" .

# Or via MCP tool
laravelgraph_cypher(query="MATCH (m:EloquentModel)-[:HAS_RELATIONSHIP]->(r) RETURN m.name, r.type, r.related_model LIMIT 30")

# Query the plugin graph (agent-written discoveries)
laravelgraph_cypher(query="MATCH (d:PluginNode {label: 'Discovery'}) RETURN d.data LIMIT 10", graph="plugin")
```

---

## 7. MCP Server and Tools

### How the MCP Server Works

When `laravelgraph serve` runs, it starts a FastMCP server that registers 44 tools and 9 resources. Your AI agent connects to it (via stdio for local use, or HTTP/SSE for remote/team use) and receives the tool list at connection time.

At the top of the tool list, the server injects a `LOADED PLUGINS` section listing every plugin installed for this project — names, descriptions, tool names, and how to call them. This means your agent sees all available domain-specific tools in the very first message of every session, without needing to discover them.

The server also injects the agent instruction block if `laravelgraph agent install` has been run — a condensed protocol guide telling the agent which tools to call first, how to escalate, and how to use the plugin system.

### Tool Categories

#### Core Investigation Tools

These are the primary tools for understanding any part of the codebase.

**`laravelgraph_feature_context(feature: str)`**

The recommended starting point for any feature investigation. Makes one call, returns everything relevant:
- All routes for that feature (URI, method, middleware, controller)
- The PHP source of each controller action
- All Eloquent models touched
- All events fired and their listeners
- All jobs dispatched
- Config keys and env variables used
- Semantic summaries of key symbols (if LLM configured)

**How it works:** Takes the feature name, runs a token-overlap match against `Feature` nodes (built by Phase 27), expands to all symbols with `BELONGS_TO_FEATURE` edges, fetches source and summaries for each.

**`laravelgraph_context(symbol: str, include_source: bool = True)`**

360° view of any specific symbol — a class, method, route, or event. Returns:
- Full PHP source code of the symbol
- AI-generated semantic summary
- All callers (what calls this)
- All callees (what this calls)
- Events and jobs dispatched
- Eloquent relationships (for models)
- Rendered Blade views
- Test coverage
- Change history

**`laravelgraph_explain(feature: str)`**

Uses semantic search (embeddings) to find the best anchor class for a feature and generates an end-to-end explanation of how that feature works. Unlike `feature_context` which returns structured data, `explain` returns a narrative prose explanation generated by traversing the graph and composing the story.

**`laravelgraph_request_flow(route: str)`**

Traces the full lifecycle of an HTTP request. Given a route URI or name:
```
middleware stack → controller → FormRequest validation → service layer
  → Eloquent models → events dispatched → listeners → jobs queued
```
Goes 3 hops deep and includes all events and jobs at every level.

**`laravelgraph_query(query: str, limit: int = 20, role: str = "")`**

Hybrid search combining:
- **BM25** — keyword relevance (exact and token overlap)
- **Vector** — semantic similarity (384-dim embeddings from fastembed)
- **Fuzzy** — typo-tolerant name matching (rapidfuzz)

Results ranked by RRF (Reciprocal Rank Fusion) — a proven fusion algorithm that outperforms any single ranking method. Can be filtered by Laravel role (`controller`, `model`, `event`, `job`, etc.).

---

#### Impact and Change Analysis Tools

**`laravelgraph_impact(symbol: str, depth: int = 3)`**

Blast radius analysis. Given a class or method name, returns all symbols that would be affected if it changed — grouped by depth:
- **Depth 1** — direct callers
- **Depth 2** — callers of callers
- **Depth 3** — transitive impact

Includes affected routes (so you know which endpoints are at risk) and suggested tests to run.

**`laravelgraph_detect_changes(diff: str)`**

Takes a git diff and maps it to affected graph symbols. Returns:
- Which methods were changed
- Which callers are affected
- Which routes expose the affected methods
- Which tests cover the changed code
- Severity assessment

**`laravelgraph_suggest_tests(symbol: str)`**

Given a symbol (method FQN, class FQN, or route), returns the specific test files and methods most likely to catch a regression. Uses the `TESTS` edges from Phase 30 plus heuristic name matching.

---

#### Route and Model Tools

**`laravelgraph_routes(method: str = "", uri: str = "", name: str = "")`**

Full route table with filterable columns. Each route entry includes URI, HTTP method, route name, middleware stack (expanded to class FQNs), controller class, action method, FormRequest (if used), and which feature it belongs to.

**`laravelgraph_models(model: str = "")`**

Eloquent relationship graph. For each model: table name, fillable/guarded fields, casts, all 11 relationship types with their target models, foreign keys, pivot tables, and the linked `DatabaseTable` node (if introspected).

**`laravelgraph_api_surface(route: str)`**

Full API contract for a specific route. Returns: HTTP method + URI, middleware stack, controller action source, FormRequest validation rules, JSON Resource response shape, and a semantic description of what the endpoint does.

---

#### Database Intelligence Tools

These tools are covered in depth in [Section 9](#9-database-intelligence).

- `laravelgraph_schema` — full database schema
- `laravelgraph_db_context` — semantic picture of a table
- `laravelgraph_resolve_column` — deep-dive on one column
- `laravelgraph_procedure_context` — stored procedure analysis
- `laravelgraph_connection_map` — all DB connections and their stats
- `laravelgraph_db_query` — run read-only SQL
- `laravelgraph_db_impact` — trace DB writes → events/jobs
- `laravelgraph_cross_cutting_concerns` — methods called across layers
- `laravelgraph_boundary_map` — all layers accessing a table
- `laravelgraph_data_quality_report` — live DB data quality scan
- `laravelgraph_race_conditions` — check-then-act pattern detection

---

#### Event, Job, and Binding Tools

**`laravelgraph_events(event: str = "")`**

Full event-to-listener-to-job map. Given an event class name, returns:
- All registered listeners for that event
- Whether each listener is queued (async) or synchronous
- All jobs dispatched from those listeners
- The full dispatch chain

**`laravelgraph_job_chain(job: str)`**

Traces the execution chain from a Job or Console Command. Follows `CALLS` edges through the job's `handle()` method up to 4 hops deep. Shows every model, service, event, and job involved.

**`laravelgraph_bindings(abstract: str = "")`**

Service container binding map. Shows what's bound, the binding type (bind/singleton/scoped/instance), which ServiceProvider registered it, and the concrete implementation. Filters by interface/abstract class name.

---

#### Code Quality and Security Tools

**`laravelgraph_dead_code()`**

Symbols unreachable from any entry point (routes, commands, listeners, scheduled tasks), after applying Laravel-aware exemptions for lifecycle methods, observer methods, and magic-method-accessible symbols.

**`laravelgraph_security_surface()`**

Security gaps prioritized by severity. Detects:
- Routes missing authentication middleware
- Routes missing authorization checks
- Mass assignment vulnerabilities (missing `$fillable`/`$guarded`)
- Unprotected file upload paths
- SQL injection risks from raw query strings

**`laravelgraph_performance_risks()`**

N+1 query patterns and other performance risks detected by Phase 31. Each risk includes the file, line number, description of the pattern, and severity.

**`laravelgraph_contracts(symbol: str = "")`**

Behavioral contracts for routes or classes: what validation rules apply, what authorization checks are performed, which policy governs access, which middleware provides preconditions.

---

#### Config and Feature Tools

**`laravelgraph_config_usage(key: str)`**

All code that reads a specific config key or env variable. Returns method FQNs, file paths, and line numbers. Essential for impact analysis before changing a config value or env variable.

**`laravelgraph_features(feature: str = "")`**

Auto-detected feature clusters from Phase 27. Each feature shows its routes, models, events, and jobs — the product-boundary view of the codebase.

**`laravelgraph_outbound_apis(caller: str = "", url_contains: str = "")`**

All outbound HTTP calls made by the application (from Phase 32). Shows caller FQN, HTTP verb, URL pattern, client type (laravel_http/guzzle/curl). Filterable by caller or URL.

---

#### Plugin Management Tools

These tools are covered in depth in [Section 8](#8-the-plugin-system).

- `laravelgraph_request_plugin` — generate a new domain plugin
- `laravelgraph_run_plugin_tool` — run a plugin tool immediately (hot dispatch)
- `laravelgraph_update_plugin` — regenerate a plugin with critique
- `laravelgraph_remove_plugin` — remove an underperforming plugin
- `laravelgraph_suggest_plugins` — list valuable plugin opportunities
- `laravelgraph_plugin_knowledge` — recall agent discoveries from past sessions

---

#### Utility Tools

**`laravelgraph_intent(query: str)`**

Structured intent analysis. Takes a natural language query and returns a parsed intent with confidence scores, the most likely symbol or feature being asked about, and a recommended tool to call. Useful when an agent isn't sure where to start.

**`laravelgraph_cypher(query: str, graph: str = "core")`**

Execute raw Cypher queries against the graph. Read-only — destructive operations are blocked. Pass `graph="plugin"` to query the plugin knowledge graph.

**`laravelgraph_list_repos()`**

All indexed repositories with stats: project path, last analyzed time, node count, edge count, phase count.

**`laravelgraph_provider_status()`**

Which LLM providers are configured, which is active, model name, and whether the connection is working.

---

### MCP Resources

Resources are read-only data endpoints available to agents without tool invocation.

| Resource URI | Contents |
|---|---|
| `laravelgraph://overview` | Node and edge counts by type |
| `laravelgraph://schema` | Full graph schema reference (all node/rel types) |
| `laravelgraph://providers` | LLM provider configuration and status |
| `laravelgraph://summaries` | Summary cache stats (count, hit rate, size) |
| `laravelgraph://routes` | Full route table |
| `laravelgraph://models` | Model relationship map |
| `laravelgraph://events` | Event/listener map |
| `laravelgraph://dead-code` | Full dead code report |
| `laravelgraph://bindings` | Service container binding map |

---

## 8. The Plugin System

### The Problem Plugins Solve

LaravelGraph's built-in tools give generic Laravel intelligence. But "what is the order lifecycle in this specific app?" requires knowing *your* routes, *your* models, *your* events — not Laravel's in general. No built-in tool can answer that without already knowing your domain.

Plugins are product-specific domain lenses over the graph. Each plugin adds a small set of MCP tools that answer domain-specific questions for your application. A plugin named `order-flow` might expose `ord_summary` (the order domain overview), `ord_state_transitions` (all possible order status changes), and `ord_payment_sequence` (the payment → fulfillment chain for this specific app).

Plugins live in `.laravelgraph/plugins/` inside your project — they belong to the product, not to LaravelGraph.

### How Plugin Generation Works

When you call `laravelgraph_request_plugin("order lifecycle and payment flow")`, the system runs a 4-stage pipeline:

**Stage 1 — Domain Anchor Resolution (no LLM)**

Pure Python + Cypher queries resolve the actual nodes in your graph that correspond to the requested domain:
- Matches Feature nodes by token overlap (removes stop-words, finds "order" in Feature names)
- Falls back to scanning Route URIs, EloquentModel names, Event names
- Expands events to their listener classes via `LISTENS_TO` edges
- Returns a structured dict of real node names (routes, models, events, jobs) — not invented names

**Stage 2 — LLM Spec Generation (grounded)**

The LLM receives the domain anchors found in Stage 1 — real class names and route URIs from your codebase. It generates a compact JSON spec describing what tools the plugin should have:

```json
{
  "slug": "order-flow",
  "tool_prefix": "ord_",
  "tools": [
    {
      "name": "ord_state_transitions",
      "description": "All order status transitions and their triggers",
      "cypher_query": "MATCH (m:Method) WHERE m.fqn CONTAINS 'OrderService' ...",
      "result_format": "{status}: {trigger}"
    }
  ]
}
```

The LLM only fills in query patterns and substitutes real names it was given. It cannot invent node names that don't exist in the graph.

**Stage 3 — Deterministic Code Assembly (no LLM)**

Python code is assembled from the spec — the LLM never writes Python directly. Every plugin automatically gets three tools:
1. `{prefix}summary` — hard-coded domain overview from the anchors (always works, no LLM)
2. `{prefix}X` — one or more LLM-specified query tools
3. `{prefix}store_discoveries` — writes agent findings to the plugin knowledge graph

**Stage 4 — 4-Layer Validation Pipeline**

1. **Layer 1 — Static AST**: Syntax check, manifest field presence, tool-name prefix compliance
2. **Layer 2 — Schema**: All Cypher node labels and relationship types validated against the known schema (no bad `MATCH (x:Ordr)` typos)
3. **Layer 3 — Execution sandbox**: `register_tools()` is called; tools must register without error
4. **Layer 4 — LLM Judge**: Quality score ≥ 7/10 required; the LLM reviews the plugin for correctness and usefulness

If a layer fails, the critique is fed back into the loop. The system retries up to 3 times with the previous failure reason in the prompt. If all 3 iterations fail, the request returns a detailed failure message with the last layer number, the reason, and actionable options.

### Skeleton Plugins (Explicit Opt-In)

By default, if generation fails, no file is written. The failure message tells you:
- Which layer failed (AST / schema / execution / quality-judge)
- Why it failed
- What to try next (better description, explore the graph first, stronger LLM)

You can force a skeleton placeholder with `allow_skeleton=True`:

```python
laravelgraph_request_plugin("order lifecycle", allow_skeleton=True)
```

A skeleton plugin is generated with:
- `"status": "skeleton"` in `PLUGIN_MANIFEST` — visible in LOADED PLUGINS as `⚠ SKELETON`
- Query tools that return "edit me" messages instead of executing Cypher
- `store_discoveries` tool that works normally

To fix a skeleton: `laravelgraph_update_plugin("order-flow", "describe what the tool should actually query")`.

### Hot Dispatch — Using Plugins Without Restart

MCP tool lists are sent to the client at connection start. Native plugin tools only appear after the MCP server restarts. To use a just-generated plugin in the same conversation:

```python
# Generate the plugin
laravelgraph_request_plugin("booking and availability domain")
# → success message lists tool names: bkn_summary, bkn_availability, bkn_store_discoveries

# Use it immediately — no restart needed
laravelgraph_run_plugin_tool("booking", "bkn_summary")
laravelgraph_run_plugin_tool("booking", "bkn_availability")

# Store what you found
laravelgraph_run_plugin_tool("booking", "bkn_store_discoveries",
    tool_args={"findings": "Bookings use a soft-lock pattern via Redis for slot reservation..."})
```

Next conversation: `bkn_summary()` is a native tool registered at startup.

### The `store_discoveries` Protocol

Every plugin has a `{prefix}store_discoveries(findings: str)` tool. After investigating with a plugin, call it with a plain-text summary of what you found:

```python
bkn_store_discoveries(findings="Booking cancellations within 2 hours are handled by \
the BookingCancellationJob which dispatches 3 events. The refund path goes through \
Stripe for card payments and credits wallet for in-app payments.")
```

The finding is stored as a `Discovery` node in `plugin_graph.kuzu` with a timestamp. In every future conversation, these discoveries are recalled via `laravelgraph_plugin_knowledge()`. The `{prefix}summary` output always ends with a nudge to call `store_discoveries` so agents remember to use it.

This is how LaravelGraph accumulates institutional knowledge over time — insights discovered by agents in one session become available to agents in all future sessions.

### Plugin Lifecycle Engine

Three mechanisms keep plugin knowledge current:

**1. Discovery** — `plugin suggest` and `laravelgraph_suggest_plugins` scan Feature nodes with more than 10 symbols that have no plugin yet. Results are boosted by log mining — features frequently queried via `laravelgraph_feature_context` or `laravelgraph_explain` get higher priority scores.

**2. Self-Improvement** — At server startup, plugins with degraded performance are flagged:
- `call_count > 20` and `empty_result_count / call_count > 0.25` → too many empty results
- `call_count > 20` and `error_count / call_count > 0.15` → too many errors
- `call_count > 30` and `agent_followup_count / call_count > 0.40` → agent always needs another tool after this one

Flagged plugins are regenerated automatically with a critique based on the failure mode. A 48-hour cooldown prevents thrashing.

**3. Domain Drift Detection** — When a plugin is generated, a snapshot of its domain's graph counts (routes, models, events) is stored in `PluginMeta.domain_coverage_snapshot`. On each startup, current counts are compared. If routes changed >20%, new models appeared, or the Feature's `has_changes` flag is set, the plugin is scheduled for regeneration. A 14-day cooldown applies.

### Plugin CLI Commands

```bash
laravelgraph plugin list .                   # List all plugins — health, call count, contribution score
laravelgraph plugin suggest .                # Detect feature gaps and suggest new plugins
laravelgraph plugin scaffold <name> .        # Scaffold a plugin from graph context (no LLM)
laravelgraph plugin validate <file>          # Validate a plugin file before loading
laravelgraph plugin enable <name> .          # Re-enable a disabled plugin
laravelgraph plugin disable <name> .         # Disable a plugin (keeps file, stops loading)
laravelgraph plugin delete <name> .          # Permanently delete plugin + discoveries + meta
laravelgraph plugin prompt <name> "..." .    # Attach a system prompt (injected at startup)
laravelgraph plugin migrate .                # Apply store_discoveries and Cypher property migrations
laravelgraph plugin evolve . --dry-run       # Show what would be generated without generating
laravelgraph plugin evolve . -n 2            # Generate up to 2 new plugins from feature gaps
```

### Auto-Migration

When plugins are loaded, two migrations are applied automatically:

1. **store_discoveries migration** — Old plugins (pre-v0.3) had `store_discoveries()` with no parameters. The loader detects the old signature and replaces the function body with the new `store_discoveries(findings: str)` implementation that accepts free-text agent input.

2. **Cypher property migration** — Old plugins may reference `r.method` (should be `r.http_method`), `r.action` (should be `r.action_method`), `e.model` (should be `e.name`), or `c.class` (should be `c.fqn`). The loader applies regex replacements automatically, using negative lookaheads to avoid touching Python method calls.

Run `laravelgraph plugin migrate .` to apply these migrations manually (useful after upgrading).

### Plugin Graph

Plugins write to a separate writable KuzuDB at `.laravelgraph/plugin_graph.kuzu`. The core graph (`.laravelgraph/graph.kuzu`) is read-only for plugins.

Every tool in a plugin receives a `DualDB` object:
- `db()` — proxies to the core graph (read-only, backwards compatible)
- `db().core()` — explicit core graph access
- `db().plugin()` — the writable plugin graph

Discoveries stored via `store_discoveries` accumulate in the plugin graph and persist across server restarts and re-analyses. They are never overwritten by `laravelgraph analyze`.

If the server is killed without a clean shutdown, the plugin graph file may have a stale lock in its header. LaravelGraph detects this automatically on startup, removes the stale file, and recreates a fresh plugin graph with a warning in the logs.

---

## 9. Database Intelligence

Database intelligence is a distinct layer that requires a live database connection. Configure one with:

```bash
laravelgraph db-connections add
```

This opens an interactive wizard to configure host, port, database name, credentials, and options (SSL, analyze stored procedures, cache TTL).

### What Live Introspection Adds

Without a DB connection, LaravelGraph uses migration-derived schema (Phase 19). With a connection, Phase 24 pulls the ground truth:

- **All tables** — including tables created outside migrations (audit tables, shadow tables, legacy tables)
- **All columns** — exact types, character sets, defaults, `NULL`/`NOT NULL`
- **All indexes** — primary, unique, composite, fulltext
- **All foreign keys** — confirmed relationships (not just inferred)
- **All stored procedures** — names, parameters, bodies
- **All views** — names, definitions
- **Live row counts** — production data distribution (what tables have data, what's empty)

### Database Tools in Detail

**`laravelgraph_schema(table_name: str = "", connection: str = "")`**

Full database schema. Without a table filter, returns all tables with column summaries. With a table filter, returns full column details, indexes, FK relationships, and code access summary (which methods read/write this table).

**`laravelgraph_db_context(table: str, connection: str = "")`**

The full semantic picture of a database table:
- All columns with types and constraints
- FK relationships (both explicit and inferred)
- Which Eloquent models own this table
- Which methods read vs write it
- Live row count
- Lazy LLM-generated annotation (what does this table store, what domain does it belong to?)

The LLM annotation is generated once and cached in `.laravelgraph/db_context.json` keyed by table + column structure hash. If the table structure changes, the cache is invalidated.

**`laravelgraph_resolve_column(table: str, column: str, connection: str = "")`**

Deep investigation of a single column:
- All PHP code paths that write to this column
- Guard conditions (when does the write happen?)
- Polymorphic hints (if the column is a `*_type` discriminator)
- Whether the column is set by migrations, seeders, or code
- Lazy LLM resolution of what the column's values mean in business terms

**`laravelgraph_procedure_context(procedure: str, connection: str = "")`**

For stored procedures:
- Full procedure body (SQL)
- All tables it reads and writes
- Parameter names and types
- Lazy LLM semantic annotation
- Which PHP code calls this procedure

**`laravelgraph_connection_map()`**

All configured database connections with: driver, host, database name, live connection status, table count, stored procedure count, and whether the connection is the Laravel default.

**`laravelgraph_db_query(sql: str, connection: str = "", limit: int = 100)`**

Run a read-only SQL query against a configured live database. Blocked: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`. Results cached for the configured `query_cache_ttl` seconds (default 300). Returns results as a table with column headers.

**`laravelgraph_db_impact(table: str, column: str = "", connection: str = "")`**

Traces writes to a specific table (or column) through to the events and jobs they trigger. Shows the full chain: `code that writes → event dispatched → listener → job → downstream effect`.

**`laravelgraph_cross_cutting_concerns(table: str)`**

Methods that access a given table from multiple application layers (controllers, services, jobs, commands). Identifies architectural violations where a controller accesses a table directly that should go through a service.

**`laravelgraph_boundary_map(table: str)`**

Shows every layer of the application that touches a table: PHP code paths, stored procedures, and views. Gives a complete ownership picture.

**`laravelgraph_data_quality_report(connection: str = "")`**

Runs live data quality checks:
- Tables with unexpectedly high null rates in non-nullable columns
- Foreign key references to missing rows
- Enum columns with values not in the declared enum set
- Timestamp inconsistencies

**`laravelgraph_race_conditions()`**

Detects check-then-act patterns — code that reads a value and then acts on it without proper locking. Common example: `if ($slot->available) { $slot->reserve(); }` without a transaction or database lock. Returns code locations with severity ratings.

---

## 10. LLM Providers and Semantic Summaries

### What Summaries Are

A semantic summary is a 2–4 sentence description of what a PHP symbol does and why it exists. Generated by an LLM the first time a symbol is queried, then cached forever (until the source file changes).

They're optional — every tool works without them. When configured, they make agent responses dramatically more useful: instead of returning 50 lines of PHP source for an agent to read, a tool returns "2-sentence summary + source" and the agent understands the symbol much faster.

### How the Cache Works

```
First query for a symbol:
  1. Source code read from disk
  2. Prompt built: symbol name + docblock + source (capped at 50 lines)
  3. LLM called → 2-4 sentence summary returned
  4. Summary + file mtime stored in .laravelgraph/summaries.json
  5. Summary included in tool response

All subsequent queries:
  1. Cache checked → hit
  2. Summary returned instantly — zero LLM cost

When source file changes:
  1. Current file mtime compared to stored mtime
  2. Mtime differs → cache entry deleted
  3. Summary regenerated on next query
```

Summaries survive server restarts, re-analyses, and upgrades. On a team sharing the same index, all developers benefit from summaries generated by whoever queried first.

### Supported Providers (18 total)

**Cloud Providers:**

| Provider | Env Variable | Recommended Model | Notes |
|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` | Native SDK |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` | |
| OpenRouter | `OPENROUTER_API_KEY` | `anthropic/claude-3-5-haiku` | 200+ models |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Ultra-fast, free tier |
| Mistral AI | `MISTRAL_API_KEY` | `mistral-small-latest` | |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` | |
| Google Gemini | `GEMINI_API_KEY` | `gemini-2.0-flash` | |
| xAI (Grok) | `XAI_API_KEY` | `grok-3-mini` | |
| Together AI | `TOGETHER_API_KEY` | `Llama-3.3-70B-Instruct-Turbo` | |
| Fireworks AI | `FIREWORKS_API_KEY` | `llama-v3p3-70b-instruct` | |
| Perplexity | `PERPLEXITY_API_KEY` | `sonar` | |
| Cerebras | `CEREBRAS_API_KEY` | `llama3.1-8b` | Fast inference |
| Cohere | `COHERE_API_KEY` | `command-r` | |
| Novita AI | `NOVITA_API_KEY` | `meta-llama/llama-3.1-8b-instruct` | |
| Hugging Face | `HF_TOKEN` | `Qwen/Qwen2.5-Coder-32B-Instruct` | |

**Local / Self-Hosted Providers** (require explicit configuration, no auto-detect):

| Provider | Default URL | Setup |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | `ollama pull qwen2.5-coder:7b` |
| LM Studio | `http://localhost:1234/v1` | Load a model in LM Studio UI |
| vLLM | `http://localhost:8000/v1` | Self-hosted inference server |

### Provider Selection Logic

**Auto mode** (default): Scans env vars in order. First cloud provider with an API key set is selected. Local providers are never auto-selected — they require explicit configuration.

**Explicit mode**: Set `"provider": "groq"` in config. Only that provider is used regardless of what env vars are set.

### Configuring a Provider

**Interactive wizard (recommended):**
```bash
laravelgraph configure /path/to/your/laravel-app
```

**Environment variable (instant):**
```bash
export GROQ_API_KEY=gsk_...
# LaravelGraph detects it automatically on next call
```

**Config file** (`~/.laravelgraph/config.json` for global, `.laravelgraph/config.json` for project):
```json
{
  "summary": {
    "provider": "groq",
    "api_keys": { "groq": "gsk_..." },
    "models": { "groq": "llama-3.3-70b-versatile" }
  }
}
```

**For local providers:**
```json
{
  "summary": {
    "provider": "ollama",
    "models": { "ollama": "qwen2.5-coder:7b" },
    "base_urls": { "ollama": "http://127.0.0.1:11434" }
  }
}
```

### Plugin Generation Requires an LLM

Semantic summaries are optional. Plugin generation is not — it requires an LLM to generate the plugin spec. Any configured provider works. For cost efficiency, a fast/cheap model (Groq's free tier, `ollama` locally) works well for generation. The quality judge in Layer 4 requires a model that can reason about code quality — a 7B parameter model may score plugins too generously or too harshly.

Recommended for plugin generation: Groq (free tier), Claude Haiku, GPT-4o-mini, or any Ollama model with 13B+ parameters.

---

## 11. CLI Reference

### Analysis and Index Management

```bash
laravelgraph analyze [PATH]
    --full                    # Force full rebuild (ignores incremental state)
    --no-embeddings           # Skip vector embedding generation (faster, disables semantic search)
    --phases 1,2,3            # Run only specific phases (for debugging/development)

laravelgraph status [PATH]    # Show index status, node/edge counts, last analyzed time
laravelgraph list             # List all indexed repositories globally
laravelgraph clean [PATH]     # Delete the .laravelgraph/graph.kuzu index
    --force / -f              # Skip confirmation prompt
laravelgraph doctor [PATH]    # Full health check: all dependencies, graph DB, MCP tools, LLM
laravelgraph download         # Download tree-sitter PHP grammar and fastembed model
    --check                   # Check download status without downloading
```

### Server and Watch Mode

```bash
laravelgraph serve [PATH]
    --watch / -w              # Enable live file watching — re-indexes changed files
    --http                    # Use HTTP/SSE transport (default: stdio)
    --port N                  # HTTP port (default 3000)
    --host HOST               # Bind host (default 127.0.0.1, use 0.0.0.0 for EC2)
    --api-key KEY             # Require Authorization: Bearer <key> for HTTP mode

laravelgraph watch [PATH]     # Standalone file watcher (without MCP server)
```

### Search and Exploration (CLI)

```bash
laravelgraph query QUERY [PATH]
    --limit / -n N            # Max results (default 20)
    --role ROLE               # Filter by Laravel role

laravelgraph context SYMBOL [PATH]     # 360° symbol view
laravelgraph impact SYMBOL [PATH]      # Blast radius
    --depth / -d N            # BFS depth (default 3)

laravelgraph routes [PATH]
    --method GET|POST|...
    --uri /api/fragment

laravelgraph models [PATH]
    --model ModelName

laravelgraph events [PATH]
laravelgraph bindings [PATH]
laravelgraph dead-code [PATH]
laravelgraph schema [PATH]
    --table table_name
laravelgraph cypher "MATCH (n:Route) RETURN n.uri" [PATH]
laravelgraph diff BASE..HEAD [PATH]    # Structural branch comparison
```

### LLM Provider Management

```bash
laravelgraph providers [PATH]                    # List all 18 providers with status
laravelgraph configure [PATH]                    # Interactive wizard
    --global / -g                                # Save to ~/.laravelgraph/config.json
laravelgraph providers add [PATH]                # Add/reconfigure a specific provider
laravelgraph providers edit [PATH]               # Edit provider settings
laravelgraph providers remove PROVIDER [PATH]    # Remove a provider config
laravelgraph providers activate PROVIDER [PATH]  # Switch active provider
laravelgraph providers test [PATH]               # Live test the active provider
```

### Database Connection Management

```bash
laravelgraph db-connections list [PATH]          # Show all configured connections
laravelgraph db-connections add [PATH]           # Interactive wizard: host, port, DB, creds
laravelgraph db-connections remove NAME [PATH]   # Remove connection (with confirmation)
laravelgraph db-connections test [NAME] [PATH]   # Test connectivity
```

### Plugin Management

```bash
laravelgraph plugin list [PATH]                  # List all plugins: health, calls, contribution %
laravelgraph plugin suggest [PATH]               # Detect feature gaps, suggest plugins to generate
laravelgraph plugin scaffold NAME [PATH]         # Scaffold plugin from graph context (no LLM needed)
laravelgraph plugin validate FILE                # Validate a plugin file
laravelgraph plugin enable NAME [PATH]           # Re-enable a disabled plugin
laravelgraph plugin disable NAME [PATH]          # Disable (keeps file, stops loading)
laravelgraph plugin delete NAME [PATH]           # Permanently remove plugin + discoveries + meta
laravelgraph plugin prompt NAME "PROMPT" [PATH]  # Attach a system prompt to a plugin
laravelgraph plugin migrate [PATH]               # Apply store_discoveries and Cypher migrations
laravelgraph plugin evolve [PATH]
    --dry-run                                    # Show what would be generated without generating
    -n / --max-generate N                        # Limit plugins generated per run (default 3)
```

### Log Management

```bash
laravelgraph logs [PATH]
    --level error|warn|info|debug               # Filter by severity level
    --tool laravelgraph_routes                  # Filter by MCP tool name
    --since 2h                                  # Show last N hours (e.g. 1h, 30m, 24h)
    --limit N                                   # Max entries to show

laravelgraph logs tail [PATH]                    # Live tail — Ctrl+C to stop
laravelgraph logs stats [PATH]                   # Statistics by level and tool
laravelgraph logs clear [PATH]
    --all                                        # Clear all logs (with confirmation)
```

### Agent Integration

```bash
laravelgraph setup [PATH]                        # Print MCP config JSON for your AI tool
    --claude                                     # Claude Code format (~/.claude.json)
    --cursor                                     # Cursor format (~/.cursor/mcp.json)
    --windsurf                                   # Windsurf format

laravelgraph agent install [PATH]
    --tool claude-code|opencode|cursor|all       # Target AI tool (default: claude-code)
```

### Export

```bash
laravelgraph export [PATH]
    --format json|dot|graphml                    # Output format
    --output FILE                                # Output file (default: stdout)

laravelgraph version                             # Print version
```

---

## 12. HTTP Serving for Teams

By default, LaravelGraph uses stdio transport — Claude Code starts the server process automatically when you open a session. This works perfectly for a single developer on a local machine.

For teams, running one shared server on an EC2 instance is more efficient. All developers connect to it; the graph is analyzed once and shared.

### Starting an HTTP Server

```bash
# On your EC2 instance (use systemd or pm2 to keep alive)
laravelgraph serve /path/to/project \
    --http \
    --host 0.0.0.0 \
    --port 3000 \
    --api-key your-secret-key
```

```bash
# Health check (always public, no auth)
curl http://your-server:3000/health
# → {"status": true}
```

### Connecting Each Developer's Agent

Add to each developer's `~/.claude.json`:

```json
{
  "mcpServers": {
    "laravelgraph": {
      "type": "sse",
      "url": "http://your-server:3000/sse",
      "headers": {
        "Authorization": "Bearer your-secret-key"
      }
    }
  }
}
```

### Important Constraint: Re-indexing

KuzuDB requires exclusive write access. You cannot run `laravelgraph analyze` while the MCP server is running — they'd conflict on the database lock. To re-index:

```bash
# 1. Stop the MCP server (e.g., systemd stop laravelgraph)
# 2. Run the analysis
laravelgraph analyze /path/to/project
# 3. Restart the server
```

This is also why `laravelgraph serve --watch` (live re-indexing on file changes) only works in single-developer/local mode. For team servers, re-index on deploy or on a schedule.

### Config via Environment Variables

```bash
LARAVELGRAPH_API_KEY=your-secret-key   # HTTP authentication key
LARAVELGRAPH_PORT=3000                  # HTTP port
```

### Note on Ollama with Remote Servers

Ollama runs on your laptop at `localhost:11434`. A remote EC2 server cannot reach that address. For team servers, configure a cloud LLM provider:

```bash
# On the EC2 server
laravelgraph configure /path/to/project
# → pick groq (free tier), add API key
```

---

## 13. Agent Integration Setup

### Agent Instructions

After indexing, run:

```bash
laravelgraph agent install /path/to/your/laravel-app
```

This writes an optimized instruction block to `CLAUDE.md` (or `.opencode/instructions.md` for OpenCode, `.cursorrules` for Cursor). The block teaches your agent:

- **Tool hierarchy** — which tool to call first for which type of question
- **Escalation protocol** — if Tool A returns sparse results, call Tool B next
- **Plugin workflow** — how to request, run, and update plugins
- `store_discoveries` protocol — when and how to store findings
- **Common pitfalls** — what not to do (don't read files when you can query the graph)

The install is idempotent — safe to re-run after upgrades to refresh the instructions.

```bash
# Supported targets
laravelgraph agent install . --tool claude-code   # → CLAUDE.md
laravelgraph agent install . --tool opencode      # → .opencode/instructions.md
laravelgraph agent install . --tool cursor        # → .cursorrules
laravelgraph agent install . --tool all           # All three
```

### Claude Code (Local stdio)

```json
{
  "mcpServers": {
    "laravelgraph": {
      "type": "local",
      "command": ["bash", "-c", "laravelgraph serve \"/path/to/project\""],
      "enabled": true
    }
  }
}
```

### Claude Code (Remote HTTP/SSE)

```json
{
  "mcpServers": {
    "laravelgraph": {
      "type": "sse",
      "url": "http://your-server:3000/sse",
      "headers": { "Authorization": "Bearer your-secret-key" }
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "laravelgraph": {
      "command": "laravelgraph",
      "args": ["serve", "/path/to/project"]
    }
  }
}
```

### Windsurf

Add to `~/.windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "laravelgraph": {
      "command": "laravelgraph",
      "args": ["serve", "/path/to/project"]
    }
  }
}
```

---

## 14. Configuration Reference

Config priority: **env vars → project config → global config → defaults**

### Config File Locations

- `~/.laravelgraph/config.json` — global defaults, applies to all projects
- `<project>/.laravelgraph/config.json` — project-level overrides

### Full Config Structure

```json
{
  "summary": {
    "enabled": true,
    "provider": "auto",
    "api_keys": {
      "anthropic": "sk-ant-...",
      "groq": "gsk_..."
    },
    "models": {
      "groq": "llama-3.3-70b-versatile",
      "ollama": "qwen2.5-coder:7b"
    },
    "base_urls": {
      "ollama": "http://127.0.0.1:11434",
      "vllm": "http://my-vllm-server:8000/v1"
    },
    "max_source_lines": 50
  },
  "databases": [
    {
      "name": "mysql",
      "driver": "mysql",
      "host": "127.0.0.1",
      "port": 3306,
      "database": "my_app",
      "username": "root",
      "password": "${DB_PASSWORD}",
      "analyze_procedures": true,
      "analyze_views": true,
      "analyze_triggers": false,
      "ssl": false,
      "query_cache_ttl": 300
    }
  ],
  "mcp": {
    "transport": "http",
    "host": "0.0.0.0",
    "port": 3000,
    "api_key": "your-secret-key"
  }
}
```

### Config Field Reference

**`summary.provider`**: `"auto"` (first env var found), or explicit: `"anthropic"`, `"groq"`, `"ollama"`, etc.

**`summary.max_source_lines`**: Max PHP source lines sent to LLM for summary generation. Default `50`. Reduce to cut token costs.

**`databases[].password`**: Supports `${ENV_VAR}` syntax for env var substitution. Never store passwords in plaintext in project config files committed to git.

**`databases[].query_cache_ttl`**: Seconds to cache SQL query results from `laravelgraph_db_query`. Set to `0` to disable caching. Default `300`.

**`databases[].ssl`**: Enable SSL/TLS for database connection. Required for AWS RDS, Google Cloud SQL, Azure.

---

## 15. Storage Layout

```
~/.laravelgraph/
  repos.json            Global registry of all indexed projects
  config.json           Global config defaults
  logs/                 Structured JSON log files (rotated daily)

<project>/.laravelgraph/
  graph.kuzu/           KuzuDB graph database (directory, read-only after analyze)
  plugin_graph.kuzu     KuzuDB plugin knowledge graph (writable at runtime)
  summaries.json        LLM-generated semantic summaries (mtime-invalidated)
  db_context.json       LLM-generated DB table/column annotations (hash-invalidated)
  config.json           Project-level config overrides
  plugins/              Plugin Python files (.py) — one per domain plugin
  plugins/*.py          Each file is a self-contained MCP plugin
```

### graph.kuzu vs plugin_graph.kuzu

`graph.kuzu` is the core analysis graph — written by `laravelgraph analyze`, read by all MCP tools. It is rebuilt from scratch on every full analysis run.

`plugin_graph.kuzu` is the runtime knowledge graph — written by plugin `store_discoveries` calls during agent sessions. It accumulates across sessions and is never cleared by `laravelgraph analyze`. It holds `PluginNode` records (domain discoveries) and `PluginEdge_Node` records (relationships between discoveries).

### summaries.json vs db_context.json

Both are lazy caches populated on first use:

`summaries.json` — keyed by symbol FQN, stores the LLM summary and the file mtime at generation time. Automatically invalidated when the source file changes.

`db_context.json` — keyed by table name + column structure hash, stores LLM annotations for tables and columns. Automatically invalidated when the table schema changes (new columns, changed types).

---

## 16. Health Check

Before using LaravelGraph with an AI agent, run the full health check:

```bash
laravelgraph doctor /path/to/your/laravel-app
```

The doctor runs ~40 checks organized into sections:

```
Config              ✓  Config loaded
Dependencies        ✓  kuzu, fastmcp, typer, rich, anthropic, openai
Graph DB            ✓  1,234 nodes · 5,678 edges
Index Health        ✓  All phases present
                    ✓  Routes indexed (47 routes)
                    ✓  Models indexed (18 models)
                    ✓  Events indexed (23 events)
Context Quality     ✓  Summaries cache: 142 entries
Transport           ✓  laravelgraph binary found
                    ✓  HTTP server reachable
LLM Provider        ✓  Provider: groq / Model: llama-3.3-70b-versatile
                    ✓  Live test passed (0.8s)
Optional Features   ✓  fastembed — vector search available
                    ✓  watchfiles — watch mode available
Database            ✓  PyMySQL installed
                    !  No DB connections configured
MCP Tool Signatures ✓  All 23 tool signature scenarios pass
Plugins             ✓  Plugin 'order-flow' v1.0.0 — valid
                    !  Plugin 'payment-gateway' — SKELETON (Cypher not configured)
Plugin System       ✓  plugin_graph init + schema + query — working
                    ✓  _build_template_fallback — produces status=skeleton
                    ✓  scan_plugin_manifests — extracts status field
                    ✓  MCP plugin tools registered — request/update/remove/run/knowledge
Plugin Generator    ✓  Plugin Generator — generated in 12.3s (score: 8/10)
Downloads           ✓  fastembed-bge-small — ready
                    ✓  tree-sitter-php — ready
```

Any `✗` (fail) should be resolved before using the tool. Any `!` (warning) is advisory — the tool still works but with reduced capability.
