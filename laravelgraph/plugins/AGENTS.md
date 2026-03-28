# AGENTS.md — laravelgraph/plugins/

## OVERVIEW

The plugin system lets agents and developers extend LaravelGraph with domain-specific MCP tools that are deployed at runtime without modifying core code. Plugins are Python files stored in `.laravelgraph/plugins/` and are auto-loaded when the MCP server starts.

## STRUCTURE

```
plugins/
├── __init__.py        # Package docstring only
├── validator.py       # Layer 1: Static AST validation before any plugin code runs
├── loader.py          # Plugin loading at server startup (pipeline + MCP)
├── suggest.py         # Domain recipe detection — recommends applicable plugin types
├── scaffolder.py      # Pre-populated plugin file generation from graph context
├── plugin_graph.py    # DualDB + PluginGraphDB — writable runtime graph for plugins
├── meta.py            # PluginMetaStore — usage tracking, contribution scoring, status
├── generator.py       # 4-layer validation pipeline + LLM generation + reflection loop
└── self_improve.py    # Proactive self-improvement on server startup
```

## FILE-BY-FILE GUIDE

### `validator.py` — Layer 1: Static AST Validation

`PluginValidator.validate(path)` runs before any plugin code is imported. It:

- Parses the file with `ast.parse()` — raises `PluginValidationError` on syntax errors
- Requires a `PLUGIN_MANIFEST` dict at module level with fields: `name`, `version`, `tool_prefix`
- Enforces `tool_prefix` does not start with `laravelgraph_`
- Enforces plugin `name` is alphanumeric with hyphens only (no spaces)
- Enforces `version` is semver-ish (`MAJOR.MINOR.PATCH`)
- Blocks forbidden imports: `requests`, `httpx`, `urllib.request`
- Blocks dangerous calls: `os.system()`, `subprocess.Popen()` without `timeout=`
- Blocks destructive Cypher string patterns: `DELETE node/edge`, `DROP TABLE`, `TRUNCATE TABLE`
- Emits warnings (non-blocking) for `open()` with write mode, `subprocess.run()` without timeout, `time.sleep()`
- Verifies `@mcp.tool()` decorated functions use the declared `tool_prefix`

Returns `(manifest: dict, warnings: list[str])` or raises `PluginValidationError`.

### `loader.py` — Plugin Loading

Two entry points:

**`load_pipeline_plugins(plugins_dir, ctx)`** — called after the main 26-phase pipeline:
- Validates each `.py` plugin via `validator.py`
- Imports with `importlib.util` (no permanent `sys.modules` pollution)
- Wraps `ctx.db` in `PluginSafeDB` before calling `module.run(ctx)`
- Restores the real `ctx.db` after each plugin runs (in `finally`)

**`load_mcp_plugins(plugins_dir, mcp, db_factory)`** — called at MCP server startup:
- Validates each plugin via `validator.py`
- Wraps `mcp` in `PluginSafeMCP` to enforce `tool_prefix` at registration time
- Calls `module.register_tools(safe_mcp, db=db_factory)` if `db` is in the signature

**`PluginSafeDB`** — proxy around `GraphDB`:
- Blocks `DELETE`, `DROP`, `TRUNCATE` in `execute()`
- Auto-tags `plugin_source` on all `upsert_node()` calls
- Blocks `_insert_node()` entirely (must use `upsert_node()`)
- Passes all read-only ops through `__getattr__`

**`PluginSafeMCP`** — proxy around FastMCP:
- On `@mcp.tool()` registration, verifies function name starts with `tool_prefix`
- Blocks any tool name starting with `laravelgraph_`

### `plugin_graph.py` — DualDB and PluginGraphDB

**`PluginGraphDB`** — a separate KuzuDB instance at `<index_dir>/plugin_graph.kuzu`:
- Creates its own schema on init (`PluginNode` table with `node_id`, `plugin_source`, `label`, `props_json`, `created_at`, `updated_at`)
- `upsert_plugin_node(plugin_name, node_id, label, props)` — insert-or-update with `plugin_source` tagging
- `delete_plugin_data(plugin_name)` — removes all nodes for a given plugin
- `get_plugin_node_count(plugin_name)` — count of nodes owned by a plugin
- `execute(cypher)` — raw Cypher on the plugin graph
- `close()` — closes the database connection

**`DualDB`** — wrapper that gives plugins access to both graphs:
- `dual.execute(q)` — proxies to core graph (backwards compatibility)
- `dual()` — calling the object as a function returns `self` (so `db()` in old plugin code still works)
- `dual.core()` — returns the core `GraphDB` (read-only access)
- `dual.plugin()` — returns the `PluginGraphDB` (writable)

**`init_plugin_graph(index_dir)`** — convenience factory that creates the `PluginGraphDB` at `index_dir/plugin_graph.kuzu`.

### `meta.py` — PluginMetaStore

File-backed JSON sidecar at `<index_dir>/plugin_meta.json`.

**`PluginMeta`** dataclass fields:
- `name: str` — plugin identifier
- `status: str` — `"active"` | `"disabled"` (default `"active"`)
- `call_count: int` — total MCP tool calls served
- `empty_result_count: int` — calls that returned empty results
- `error_count: int` — calls that raised exceptions
- `plugin_node_count: int` — nodes written to the plugin graph
- `system_prompt: str` — optional text appended to MCP server instructions
- `improvement_cooldown_until: str | None` — ISO datetime; improvement blocked until this time

**`PluginMetaStore`** methods:
- `all()` — all `PluginMeta` objects
- `get(name)` — single meta or `None`
- `set(meta)` — persist
- `delete(name)` — remove
- `enable(name)` / `disable(name)` — toggle status
- `is_active(name)` — True only for `status == "active"`
- `log_call(name, empty_result, error)` — increment counters; noop for unknown plugins
- `set_system_prompt(name, prompt)` — attach a system prompt
- `get_all_system_prompts()` — returns combined prompt text for all active plugins only
- `check_improvement_needed(name)` — returns `True` when:
  - `call_count > 20` AND empty rate > 25%
  - OR `call_count > 20` AND error rate > 15%
  - AND `improvement_cooldown_until` is not set or has expired
  - Returns `False` for unknown plugins, during cooldown, or when count is too low
- `compute_contribution(name, total_system_calls, total_plugin_nodes)` — returns 0–100 score based on call share, node share, and reliability (errors penalised); returns 0.0 on division by zero

### `generator.py` — 4-Layer Validation + LLM Generation

Generates new plugins via LLM with schema-aware context injection.

**Validation layers (run in order):**

1. **`_validate_ast(code)`** — AST parse; wraps `validator.PluginValidator.validate()`
2. **`_validate_schema(code)`** — extracts Cypher string literals from the code, checks all node labels against `core/schema.py`'s `NODE_TYPES`; fails if any unknown label is used; returns `ValidationResult`
3. **`_validate_execution(code, db)`** — imports the code in a sandbox; verifies `register_tools` exists and can be called with a mock MCP; catches `SyntaxError`, `ImportError`, and runtime exceptions; returns `ValidationResult`
4. **`_validate_llm_judge(code, context, config)`** — sends code + rubric to configured LLM; parses a 1–10 score; passes at ≥ 7; returns `ValidationResult`

**`ValidationResult`** — simple dataclass with `.passed: bool` and `.critique: str`.

**Reflection loop** — if any layer fails, the critique is fed back into the LLM for regeneration (up to 3 iterations). After 3 failures the generator raises `PluginGenerationError`.

### `self_improve.py` — Proactive Self-Improvement

Called at MCP server startup (after plugins are loaded). For each active plugin:
- Calls `meta.check_improvement_needed(name)`
- If True: triggers `generator.regenerate_plugin(name, meta, db, config)` with the performance critique
- Sets `improvement_cooldown_until` to now + 48 hours after regeneration attempt

### `suggest.py` — Domain Recipe Detection

`RECIPES: list[PluginRecipe]` — 7 built-in domain recipes:
- `payment-lifecycle`, `tenant-isolation`, `booking-state-machine`
- `subscription-lifecycle`, `rbac-coverage`, `audit-trail`, `feature-flags`

Each recipe has `signals` (Cypher queries) and `min_signals` threshold. `detect_applicable_recipes(db)` runs signals against the live graph and returns matched recipes sorted by signal count. `format_suggestions(results)` formats output as Markdown for CLI display.

### `scaffolder.py` — Pre-Populated Plugin Generation

`scaffold_plugin(plugin_name, recipe_slug, project_root, db)` generates a `.py` file at `.laravelgraph/plugins/<name>.py`:
- Raises `FileExistsError` if the file already exists
- Uses `_gather_context(recipe, db)` to query the graph for domain-relevant models, tables, and routes
- Populates the generated file with actual symbol names found in the project
- File includes working `PLUGIN_MANIFEST`, `run(ctx)` pipeline hook, and `register_tools(mcp)` with one example tool

## CONVENTIONS

- All plugin files must have a `PLUGIN_MANIFEST` dict literal at module level.
- `tool_prefix` in `PLUGIN_MANIFEST` must end with `_` (e.g. `"payment_"`).
- `register_tools(mcp, db=None)` — `db` is optional; if present, the loader injects `db_factory`.
- `run(ctx)` — optional; only called during pipeline (not at MCP serve time).
- Plugins must not import from `laravelgraph.core.graph` directly — use the injected `db`.

## ANTI-PATTERNS

- **Do not import plugin modules at module level** in `loader.py` — always use `importlib.util`.
- **Do not write to the core graph from MCP plugins** — MCP plugins are read-only; writes happen in pipeline plugins via `run(ctx)`.
- **Do not use `plugin_graph.py` from pipeline plugins** — the plugin graph is for MCP-time runtime data only; pipeline data goes in the core graph via `ctx.db`.
- **Do not skip the validator** — always run `validate_plugin()` before `_import_plugin_module()`.
